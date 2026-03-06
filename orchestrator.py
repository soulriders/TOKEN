#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

DB_PATH = Path("orchestrator.db")
WORKERS = {"GEMINI", "CHATGPT"}
ALLOWED_SENDERS = {"SYSTEM", *WORKERS}


@dataclass
class State:
    current_turn: str
    turn_count: int
    max_turns: int
    status: str


@dataclass
class Message:
    id: int
    sender: str
    content: str
    created_at: str
    in_reply_to: Optional[int]


def configure_stdio() -> None:
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream is not None and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def conn(db: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(db)
    connection.row_factory = sqlite3.Row
    return connection


def _column_exists(connection: sqlite3.Connection, table: str, column: str) -> bool:
    rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
    return any(row[1] == column for row in rows)


def ensure_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS state (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            current_turn TEXT NOT NULL CHECK(current_turn IN ('GEMINI','CHATGPT')),
            turn_count INTEGER NOT NULL DEFAULT 0,
            max_turns INTEGER NOT NULL DEFAULT 10,
            status TEXT NOT NULL DEFAULT 'running' CHECK(status IN ('running','finished')),
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sender TEXT NOT NULL CHECK(sender IN ('SYSTEM','GEMINI','CHATGPT')),
            content TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        """
    )
    if not _column_exists(connection, "messages", "in_reply_to"):
        connection.execute("ALTER TABLE messages ADD COLUMN in_reply_to INTEGER")
    connection.commit()


def init_db(connection: sqlite3.Connection, first_turn: str, seed: str, max_turns: int) -> None:
    ensure_schema(connection)
    connection.execute("DELETE FROM state")
    connection.execute("DELETE FROM messages")
    connection.execute(
        """
        INSERT INTO state(id, current_turn, turn_count, max_turns, status, updated_at)
        VALUES(1, ?, 0, ?, 'running', ?)
        """,
        (first_turn, max_turns, now_iso()),
    )
    connection.execute(
        "INSERT INTO messages(sender, content, created_at, in_reply_to) VALUES('SYSTEM', ?, ?, NULL)",
        (seed, now_iso()),
    )
    connection.commit()


def get_state(connection: sqlite3.Connection) -> Optional[State]:
    row = connection.execute(
        "SELECT current_turn, turn_count, max_turns, status FROM state WHERE id = 1"
    ).fetchone()
    if not row:
        return None
    return State(**dict(row))


def row_to_message(row: Optional[sqlite3.Row]) -> Optional[Message]:
    if not row:
        return None
    return Message(**dict(row))


def get_last_message(connection: sqlite3.Connection) -> Optional[Message]:
    row = connection.execute(
        "SELECT id, sender, content, created_at, in_reply_to FROM messages ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return row_to_message(row)


def last_non_sender_message(connection: sqlite3.Connection, sender: str) -> Optional[Message]:
    row = connection.execute(
        """
        SELECT id, sender, content, created_at, in_reply_to
        FROM messages
        WHERE sender != ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (sender,),
    ).fetchone()
    return row_to_message(row)


def pull(connection: sqlite3.Connection, worker: str) -> str:
    state = get_state(connection)
    if not state:
        return "ERROR:DB_NOT_INITIALIZED"
    if state.status != "running":
        return "STOP:FINISHED"
    if state.current_turn != worker:
        return "WAIT"

    msg = last_non_sender_message(connection, worker)
    if not msg:
        return "PROMPT:"
    return f"PROMPT:{msg.content}"


def push(
    connection: sqlite3.Connection,
    worker: str,
    message: str,
    reply_to: Optional[int] = None,
) -> str:
    state = get_state(connection)
    if not state:
        return "ERROR:DB_NOT_INITIALIZED"
    if state.status != "running":
        return "STOP:FINISHED"
    if state.current_turn != worker:
        return "ERROR:NOT_YOUR_TURN"

    connection.execute(
        "INSERT INTO messages(sender, content, created_at, in_reply_to) VALUES(?, ?, ?, ?)",
        (worker, message, now_iso(), reply_to),
    )
    next_turn = "CHATGPT" if worker == "GEMINI" else "GEMINI"
    new_turn_count = state.turn_count + 1
    finished = new_turn_count >= state.max_turns
    new_status = "finished" if finished else "running"
    connection.execute(
        """
        UPDATE state
        SET current_turn=?, turn_count=?, status=?, updated_at=?
        WHERE id=1
        """,
        (next_turn, new_turn_count, new_status, now_iso()),
    )
    connection.commit()
    return "OK:FINISHED" if finished else f"OK:NEXT={next_turn}"


def export_markdown(connection: sqlite3.Connection, output: Path) -> None:
    rows = connection.execute(
        "SELECT id, sender, content, created_at, in_reply_to FROM messages ORDER BY id"
    ).fetchall()
    lines = ["# Dialogue Export", ""]
    for row in rows:
        reply_suffix = ""
        if row["in_reply_to"] is not None:
            reply_suffix = f" | reply_to={row['in_reply_to']}"
        lines.append(f"## #{row['id']} {row['sender']} ({row['created_at']}{reply_suffix})")
        lines.append("")
        lines.append(row["content"])
        lines.append("")
    output.write_text("\n".join(lines), encoding="utf-8")


def status_line(connection: sqlite3.Connection) -> str:
    state = get_state(connection)
    if not state:
        return "ERROR:DB_NOT_INITIALIZED"
    return (
        f"STATUS: current_turn={state.current_turn} turn_count={state.turn_count} "
        f"max_turns={state.max_turns} state={state.status}"
    )


def read_message_arg(args: argparse.Namespace) -> str:
    if args.message and args.message_file:
        raise SystemExit("Use either --message or --message-file, not both.")
    if args.message_file:
        return args.message_file.read_text(encoding="utf-8")
    if args.message is None:
        raise SystemExit("Either --message or --message-file is required.")
    return args.message


def main() -> None:
    configure_stdio()

    parser = argparse.ArgumentParser(description="Local turn-token orchestrator")
    parser.add_argument("--db", type=Path, default=DB_PATH)

    sub = parser.add_subparsers(dest="cmd", required=True)

    p_init = sub.add_parser("init")
    p_init.add_argument("--first-turn", choices=sorted(WORKERS), default="GEMINI")
    p_init.add_argument("--seed", required=True)
    p_init.add_argument("--max-turns", type=int, default=10)

    sub.add_parser("status")

    p_pull = sub.add_parser("pull")
    p_pull.add_argument("--worker", choices=sorted(WORKERS), required=True)

    p_push = sub.add_parser("push")
    p_push.add_argument("--worker", choices=sorted(WORKERS), required=True)
    p_push.add_argument("--message")
    p_push.add_argument("--message-file", type=Path)
    p_push.add_argument("--reply-to", type=int)

    p_export = sub.add_parser("export")
    p_export.add_argument("--format", choices=["markdown"], default="markdown")
    p_export.add_argument("--output", type=Path, required=True)

    args = parser.parse_args()

    with conn(args.db) as connection:
        ensure_schema(connection)
        if args.cmd == "init":
            init_db(connection, args.first_turn, args.seed, args.max_turns)
            print("OK:INITIALIZED")
        elif args.cmd == "status":
            print(status_line(connection))
        elif args.cmd == "pull":
            print(pull(connection, args.worker))
        elif args.cmd == "push":
            message = read_message_arg(args)
            print(push(connection, args.worker, message, reply_to=args.reply_to))
        elif args.cmd == "export":
            export_markdown(connection, args.output)
            print(f"OK:EXPORTED:{args.output}")


if __name__ == "__main__":
    main()
