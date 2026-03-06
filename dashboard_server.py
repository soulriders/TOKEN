#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from orchestrator import conn, export_markdown, get_state
from web_bridge import load_config

ROOT = Path(__file__).resolve().parent
DASHBOARD_DIR = ROOT / "dashboard"
DEFAULT_CONFIG = ROOT / "bridge_config.json"


@dataclass
class ManagedProcess:
    name: str
    command: list[str]
    cwd: Path
    process: subprocess.Popen[str]
    started_at: float
    status: str = "running"
    lines: deque[str] = None

    def __post_init__(self) -> None:
        if self.lines is None:
            self.lines = deque(maxlen=300)

    @property
    def pid(self) -> Optional[int]:
        return self.process.pid if self.process else None


class DashboardState:
    def __init__(self, config_path: Path) -> None:
        self.config_path = config_path.resolve()
        self.lock = threading.Lock()
        self.run_process: Optional[ManagedProcess] = None
        self.setup_processes: dict[str, ManagedProcess] = {}

    def read_config_text(self) -> str:
        return self.config_path.read_text(encoding="utf-8-sig")

    def read_config_json(self) -> dict:
        return json.loads(self.read_config_text())

    def save_config(self, raw_text: str) -> dict:
        parsed = json.loads(raw_text)
        self.config_path.write_text(json.dumps(parsed, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return parsed

    def start_setup(self, provider: str) -> None:
        with self.lock:
            existing = self.setup_processes.get(provider)
            if existing and existing.process.poll() is None:
                raise RuntimeError(f"{provider} setup is already running.")
            proc = self._spawn(
                name=f"setup:{provider}",
                args=[
                    sys.executable,
                    str(ROOT / "web_bridge.py"),
                    "--config",
                    str(self.config_path),
                    "setup",
                    "--provider",
                    provider,
                ],
            )
            self.setup_processes[provider] = proc

    def finish_setup(self, provider: str) -> None:
        with self.lock:
            proc = self.setup_processes.get(provider)
            if not proc or proc.process.poll() is not None:
                raise RuntimeError(f"{provider} setup is not running.")
            assert proc.process.stdin is not None
            proc.process.stdin.write("\n")
            proc.process.stdin.flush()

    def cancel_setup(self, provider: str) -> None:
        with self.lock:
            proc = self.setup_processes.get(provider)
            if not proc or proc.process.poll() is not None:
                return
            self._terminate(proc)

    def start_run(self, seed: str, first_turn: str, max_turns: int, resume: bool) -> None:
        with self.lock:
            if self.run_process and self.run_process.process.poll() is None:
                raise RuntimeError("Conversation run is already active.")
            args = [
                sys.executable,
                str(ROOT / "web_bridge.py"),
                "--config",
                str(self.config_path),
                "run",
            ]
            if resume:
                args.append("--resume")
            else:
                args.extend(["--seed", seed, "--first-turn", first_turn, "--max-turns", str(max_turns)])
            self.run_process = self._spawn(name="run", args=args)

    def stop_run(self) -> None:
        with self.lock:
            if self.run_process and self.run_process.process.poll() is None:
                self._terminate(self.run_process)

    def snapshot(self) -> dict:
        with self.lock:
            config = load_config(self.config_path)
            state_payload = None
            if config.db_path.exists():
                with conn(config.db_path) as db:
                    state = get_state(db)
                    if state:
                        state_payload = asdict(state)
                        export_markdown(db, config.export_path)
            run_info = self._proc_payload(self.run_process)
            setup_info = {provider: self._proc_payload(proc) for provider, proc in self.setup_processes.items()}
            export_preview = ""
            if config.export_path.exists():
                export_preview = config.export_path.read_text(encoding="utf-8", errors="ignore")
            return {
                "config_path": str(self.config_path),
                "orchestrator_state": state_payload,
                "run": run_info,
                "setup": setup_info,
                "export_preview": export_preview,
            }

    def _proc_payload(self, proc: Optional[ManagedProcess]) -> Optional[dict]:
        if proc is None:
            return None
        poll = proc.process.poll()
        status = proc.status
        if poll is None:
            status = "running"
        elif status == "running":
            status = "finished" if poll == 0 else "failed"
            proc.status = status
        return {
            "name": proc.name,
            "pid": proc.pid,
            "status": status,
            "exit_code": poll,
            "started_at": proc.started_at,
            "command": proc.command,
            "logs": list(proc.lines),
        }

    def _spawn(self, name: str, args: list[str]) -> ManagedProcess:
        env = os.environ.copy()
        vendor_dir = ROOT / ".vendor"
        if vendor_dir.exists():
            existing = env.get("PYTHONPATH", "")
            env["PYTHONPATH"] = str(vendor_dir) if not existing else str(vendor_dir) + os.pathsep + existing
        process = subprocess.Popen(
            args,
            cwd=str(ROOT),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=env,
        )
        managed = ManagedProcess(name=name, command=args, cwd=ROOT, process=process, started_at=time.time())
        threading.Thread(target=self._pump_logs, args=(managed,), daemon=True).start()
        return managed

    def _pump_logs(self, managed: ManagedProcess) -> None:
        assert managed.process.stdout is not None
        for line in managed.process.stdout:
            managed.lines.append(line.rstrip())
        exit_code = managed.process.wait()
        managed.status = "finished" if exit_code == 0 else "failed"

    def _terminate(self, managed: ManagedProcess) -> None:
        if managed.process.poll() is not None:
            return
        managed.process.terminate()
        try:
            managed.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            managed.process.kill()
            managed.process.wait(timeout=5)
        managed.status = "stopped"


class DashboardHandler(SimpleHTTPRequestHandler):
    state: DashboardState

    def __init__(self, *args, directory: str | None = None, **kwargs) -> None:
        super().__init__(*args, directory=str(DASHBOARD_DIR), **kwargs)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/status":
            self._write_json(self.state.snapshot())
            return
        if parsed.path == "/api/config":
            self._write_json(self.state.read_config_json())
            return
        if parsed.path == "/":
            self.path = "/index.html"
        return super().do_GET()

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        payload = self._read_json()
        try:
            if parsed.path == "/api/config":
                saved = self.state.save_config(payload["raw_text"])
                self._write_json({"ok": True, "config": saved})
                return
            if parsed.path == "/api/setup/start":
                self.state.start_setup(payload["provider"])
                self._write_json({"ok": True})
                return
            if parsed.path == "/api/setup/finish":
                self.state.finish_setup(payload["provider"])
                self._write_json({"ok": True})
                return
            if parsed.path == "/api/setup/cancel":
                self.state.cancel_setup(payload["provider"])
                self._write_json({"ok": True})
                return
            if parsed.path == "/api/run/start":
                self.state.start_run(
                    seed=payload.get("seed", ""),
                    first_turn=payload.get("first_turn", "GEMINI"),
                    max_turns=int(payload.get("max_turns", 10)),
                    resume=bool(payload.get("resume", False)),
                )
                self._write_json({"ok": True})
                return
            if parsed.path == "/api/run/stop":
                self.state.stop_run()
                self._write_json({"ok": True})
                return
            self.send_error(HTTPStatus.NOT_FOUND, "Unknown API endpoint")
        except Exception as exc:
            self._write_json({"ok": False, "error": str(exc)}, status=HTTPStatus.BAD_REQUEST)

    def log_message(self, format: str, *args) -> None:
        return

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        if not raw:
            return {}
        return json.loads(raw.decode("utf-8"))

    def _write_json(self, payload: dict, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> None:
    parser = argparse.ArgumentParser(description="Local dashboard for the ChatGPT/Gemini web bridge.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    args = parser.parse_args()

    state = DashboardState(args.config)
    handler_class = DashboardHandler
    handler_class.state = state
    server = ThreadingHTTPServer((args.host, args.port), handler_class)
    print(f"Dashboard running at http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        state.stop_run()
        for provider in list(state.setup_processes):
            state.cancel_setup(provider)
        server.server_close()


if __name__ == "__main__":
    main()
