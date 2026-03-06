#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

VENDOR_DIR = Path(__file__).resolve().parent / '.vendor'
if VENDOR_DIR.exists():
    sys.path.insert(0, str(VENDOR_DIR))

from orchestrator import (
    DB_PATH,
    WORKERS,
    conn,
    configure_stdio,
    export_markdown,
    get_state,
    init_db,
    last_non_sender_message,
    push,
)

try:
    from playwright.sync_api import BrowserContext, Page, sync_playwright
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
except ModuleNotFoundError as exc:  # pragma: no cover
    raise SystemExit(
        "playwright is not installed. Run `python -m pip install -r requirements.txt` first."
    ) from exc

DEFAULT_BROWSER_PATHS = {
    "CHATGPT": [
        Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
        Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
        Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
    ],
    "GEMINI": [
        Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
        Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
        Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
    ],
}
DEFAULT_URLS = {
    "CHATGPT": "https://chatgpt.com/",
    "GEMINI": "https://gemini.google.com/app",
}
DEFAULT_PROFILE_DIRS = {
    "CHATGPT": Path("profiles/shared"),
    "GEMINI": Path("profiles/shared"),
}
DEFAULT_SELECTORS = {
    "CHATGPT": {
        "composer_selectors": [
            "#prompt-textarea",
            "textarea[data-testid='prompt-textarea']",
            "textarea[placeholder*='Message']",
            "div[contenteditable='true'][data-testid='composer-input']",
        ],
        "send_button_selectors": [
            "button[data-testid='send-button']",
            "button[aria-label*='Send prompt']",
            "button[aria-label*='Send message']",
        ],
        "assistant_message_selectors": [
            "[data-message-author-role='assistant']",
            "article [data-message-author-role='assistant']",
        ],
        "new_chat_selectors": [
            "a[href='/']",
            "button[aria-label*='New chat']",
        ],
    },
    "GEMINI": {
        "composer_selectors": [
            "rich-textarea .ql-editor",
            "div.ql-editor",
            "div[contenteditable='true'][role='textbox']",
            "textarea",
        ],
        "send_button_selectors": [
            "button[aria-label*='Send message']",
            "button[aria-label*='Send']",
            "button.send-button",
        ],
        "assistant_message_selectors": [
            "message-content .markdown",
            "model-response .markdown",
            "model-response",
            "message-content",
        ],
        "new_chat_selectors": [
            "button[aria-label*='New chat']",
            "a[href='/app']",
        ],
    },
}


@dataclass
class ProviderConfig:
    name: str
    url: str
    browser_executable: Path
    profile_dir: Path
    headless: bool = False
    new_chat_on_start: bool = True
    allow_manual_login: bool = True
    submit_shortcut: str = "Control+Enter"
    composer_selectors: list[str] = field(default_factory=list)
    send_button_selectors: list[str] = field(default_factory=list)
    assistant_message_selectors: list[str] = field(default_factory=list)
    new_chat_selectors: list[str] = field(default_factory=list)


@dataclass
class BridgeConfig:
    db_path: Path
    export_path: Path
    poll_interval_seconds: float = 3.0
    response_timeout_seconds: float = 240.0
    stability_window_seconds: float = 6.0
    providers: dict[str, ProviderConfig] = field(default_factory=dict)


class BrowserChatClient:
    def __init__(self, config: ProviderConfig, runtime: BridgeConfig) -> None:
        self.config = config
        self.runtime = runtime
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None

    def attach(self, context: BrowserContext, page: Page) -> None:
        self.context = context
        self.page = page
        self.page.set_default_timeout(int(self.runtime.response_timeout_seconds * 1000))
        self.page.goto(self.config.url, wait_until="domcontentloaded")
        self.ensure_ready(initial=True)
        if self.config.new_chat_on_start:
            self.try_start_new_chat()
            self.ensure_ready(initial=True)

    def ensure_ready(self, initial: bool = False) -> None:
        try:
            self._get_composer(timeout_ms=15000 if initial else 5000)
            return
        except RuntimeError:
            if not self.config.allow_manual_login:
                raise
        assert self.page is not None
        print(
            f"[{self.config.name}] composer not found. Log in or solve any challenge in the opened browser, then press Enter.",
            flush=True,
        )
        self.page.bring_to_front()
        input()
        self.page.goto(self.config.url, wait_until="domcontentloaded")
        self._get_composer(timeout_ms=60000)

    def try_start_new_chat(self) -> None:
        locator = self._first_visible_locator(self.config.new_chat_selectors, timeout_ms=4000, required=False)
        if locator is None:
            return
        try:
            locator.click()
            self.page.wait_for_timeout(1500)
        except PlaywrightTimeoutError:
            return

    def send_and_receive(self, prompt: str) -> str:
        assert self.page is not None
        self.page.goto(self.config.url, wait_until="domcontentloaded")
        self.ensure_ready()
        baseline_count = self._assistant_count()
        baseline_text = self._last_assistant_text()
        composer = self._get_composer(timeout_ms=20000)
        self._fill_composer(composer, prompt)
        self._submit_prompt(composer)
        response = self._wait_for_response(baseline_count, baseline_text)
        if not response:
            raise RuntimeError(f"[{self.config.name}] empty response captured.")
        return response

    def capture_debug_artifact(self, artifact_dir: Path) -> Optional[Path]:
        if self.page is None:
            return None
        artifact_dir.mkdir(parents=True, exist_ok=True)
        target = artifact_dir / f"{self.config.name.lower()}-{int(time.time())}.png"
        self.page.screenshot(path=str(target), full_page=True)
        return target

    def _first_visible_locator(self, selectors: list[str], timeout_ms: int, required: bool) -> Any:
        assert self.page is not None
        deadline = time.time() + (timeout_ms / 1000)
        while time.time() < deadline:
            for selector in selectors:
                locator = self.page.locator(selector)
                try:
                    if locator.count() == 0:
                        continue
                    first = locator.first
                    if first.is_visible(timeout=250):
                        return first
                except PlaywrightTimeoutError:
                    continue
            self.page.wait_for_timeout(250)
        if required:
            raise RuntimeError(f"[{self.config.name}] no visible selector matched: {selectors}")
        return None

    def _get_composer(self, timeout_ms: int) -> Any:
        return self._first_visible_locator(self.config.composer_selectors, timeout_ms, required=True)

    def _clear_editable(self, composer: Any) -> None:
        composer.click()
        try:
            composer.press("Control+A")
            composer.press("Backspace")
        except PlaywrightTimeoutError:
            pass
        tag_name = (composer.evaluate("node => node.tagName") or "").lower()
        if tag_name == "textarea":
            composer.fill("")
        else:
            composer.evaluate(
                """
                node => {
                    node.innerHTML = '';
                    node.dispatchEvent(new InputEvent('input', { bubbles: true }));
                }
                """
            )

    def _fill_composer(self, composer: Any, prompt: str) -> None:
        self._clear_editable(composer)
        tag_name = (composer.evaluate("node => node.tagName") or "").lower()
        if tag_name == "textarea":
            composer.fill(prompt)
            return
        composer.evaluate(
            """
            (node, value) => {
                node.focus();
                node.innerHTML = '';
                const lines = value.split(/\r?\n/);
                for (let index = 0; index < lines.length; index += 1) {
                    if (index > 0) {
                        node.appendChild(document.createElement('br'));
                    }
                    node.appendChild(document.createTextNode(lines[index]));
                }
                node.dispatchEvent(new InputEvent('input', { bubbles: true, data: value }));
            }
            """,
            prompt,
        )

    def _submit_prompt(self, composer: Any) -> None:
        button = self._first_visible_locator(self.config.send_button_selectors, timeout_ms=4000, required=False)
        if button is not None:
            try:
                button.click()
                return
            except PlaywrightTimeoutError:
                pass
        composer.press(self.config.submit_shortcut)

    def _assistant_count(self) -> int:
        assert self.page is not None
        counts = [self.page.locator(selector).count() for selector in self.config.assistant_message_selectors]
        return max(counts) if counts else 0

    def _last_assistant_text(self) -> str:
        assert self.page is not None
        for selector in self.config.assistant_message_selectors:
            locator = self.page.locator(selector)
            count = locator.count()
            if count == 0:
                continue
            text = locator.nth(count - 1).inner_text(timeout=2000).strip()
            if text:
                return text
        return ""

    def _wait_for_response(self, baseline_count: int, baseline_text: str) -> str:
        assert self.page is not None
        deadline = time.time() + self.runtime.response_timeout_seconds
        stable_since: Optional[float] = None
        latest_text = baseline_text
        while time.time() < deadline:
            try:
                current_count = self._assistant_count()
                current_text = self._last_assistant_text()
            except PlaywrightTimeoutError:
                self.page.wait_for_timeout(500)
                continue
            response_started = current_count > baseline_count or (current_text and current_text != baseline_text)
            if not response_started:
                self.page.wait_for_timeout(750)
                continue
            if current_text != latest_text:
                latest_text = current_text
                stable_since = time.time()
            elif stable_since is None:
                stable_since = time.time()
            elif time.time() - stable_since >= self.runtime.stability_window_seconds:
                return latest_text.strip()
            self.page.wait_for_timeout(1000)
        raise RuntimeError(f"[{self.config.name}] response timeout after {self.runtime.response_timeout_seconds} seconds.")


class BrowserSessionPool:
    def __init__(self, runtime: BridgeConfig, playwright: Any) -> None:
        self.runtime = runtime
        self.playwright = playwright
        self.contexts: dict[tuple[str, str], BrowserContext] = {}

    def attach_client(self, client: BrowserChatClient) -> None:
        key = (str(client.config.browser_executable), str(client.config.profile_dir))
        context = self.contexts.get(key)
        if context is None:
            client.config.profile_dir.mkdir(parents=True, exist_ok=True)
            context = self.playwright.chromium.launch_persistent_context(
                user_data_dir=str(client.config.profile_dir),
                executable_path=str(client.config.browser_executable),
                headless=client.config.headless,
                viewport={"width": 1440, "height": 1024},
                args=["--disable-blink-features=AutomationControlled"],
            )
            self.contexts[key] = context
        page = context.new_page()
        client.attach(context, page)

    def close_all(self) -> None:
        for context in self.contexts.values():
            context.close()
        self.contexts.clear()


class BridgeRunner:
    def __init__(self, config: BridgeConfig) -> None:
        self.config = config
        self.clients: dict[str, BrowserChatClient] = {}

    def setup_provider(self, provider_name: str) -> None:
        provider_config = self.config.providers[provider_name]
        with sync_playwright() as playwright:
            pool = BrowserSessionPool(self.config, playwright)
            client = BrowserChatClient(provider_config, self.config)
            pool.attach_client(client)
            print(
                f"[{provider_name}] ready in the shared Chrome profile. Log in on this tab, verify the composer is visible, then press Enter to close.",
                flush=True,
            )
            input()
            pool.close_all()

    def run(self, seed: Optional[str], first_turn: str, max_turns: int, resume: bool) -> None:
        artifact_dir = Path("artifacts")
        with conn(self.config.db_path) as connection:
            if resume:
                state = get_state(connection)
                if not state:
                    raise SystemExit("Cannot resume because the database is not initialized.")
            else:
                if not seed:
                    raise SystemExit("--seed is required unless --resume is used.")
                init_db(connection, first_turn, seed, max_turns)
                export_markdown(connection, self.config.export_path)

        with sync_playwright() as playwright:
            pool = BrowserSessionPool(self.config, playwright)
            for provider_name, provider_config in self.config.providers.items():
                client = BrowserChatClient(provider_config, self.config)
                pool.attach_client(client)
                self.clients[provider_name] = client
            try:
                self._run_loop(artifact_dir)
            finally:
                pool.close_all()

    def _run_loop(self, artifact_dir: Path) -> None:
        while True:
            with conn(self.config.db_path) as connection:
                state = get_state(connection)
                if not state:
                    raise RuntimeError("Database is not initialized.")
                if state.status != "running":
                    export_markdown(connection, self.config.export_path)
                    print("Conversation finished.")
                    return
                current_worker = state.current_turn
                prompt_message = last_non_sender_message(connection, current_worker)
                if prompt_message is None:
                    time.sleep(self.config.poll_interval_seconds)
                    continue

            client = self.clients[current_worker]
            print(f"[{current_worker}] turn {state.turn_count + 1}/{state.max_turns}", flush=True)
            try:
                response = client.send_and_receive(prompt_message.content)
            except Exception as exc:
                screenshot = client.capture_debug_artifact(artifact_dir)
                hint = f" Screenshot: {screenshot}" if screenshot else ""
                raise RuntimeError(f"{exc}.{hint}") from exc

            with conn(self.config.db_path) as connection:
                result = push(connection, current_worker, response, reply_to=prompt_message.id)
                export_markdown(connection, self.config.export_path)
                print(result, flush=True)
            time.sleep(self.config.poll_interval_seconds)


def normalize_path(base_dir: Path, raw_path: str) -> Path:
    candidate = Path(raw_path)
    if not candidate.is_absolute():
        candidate = base_dir / candidate
    return candidate.resolve()


def pick_browser_path(provider_name: str, raw_value: Optional[str]) -> Path:
    if raw_value:
        candidate = Path(raw_value)
        if candidate.exists():
            return candidate
        raise SystemExit(f"Configured browser executable does not exist for {provider_name}: {candidate}")
    for candidate in DEFAULT_BROWSER_PATHS[provider_name]:
        if candidate.exists():
            return candidate
    raise SystemExit(f"No supported browser executable found for {provider_name}. Set browser_executable in your config.")


def provider_from_raw(base_dir: Path, provider_name: str, raw: dict[str, Any]) -> ProviderConfig:
    defaults = DEFAULT_SELECTORS[provider_name]
    return ProviderConfig(
        name=provider_name,
        url=raw.get("url", DEFAULT_URLS[provider_name]),
        browser_executable=pick_browser_path(provider_name, raw.get("browser_executable")),
        profile_dir=normalize_path(base_dir, raw.get("profile_dir", str(DEFAULT_PROFILE_DIRS[provider_name]))),
        headless=bool(raw.get("headless", False)),
        new_chat_on_start=bool(raw.get("new_chat_on_start", True)),
        allow_manual_login=bool(raw.get("allow_manual_login", True)),
        submit_shortcut=raw.get("submit_shortcut", "Control+Enter"),
        composer_selectors=list(raw.get("composer_selectors", defaults["composer_selectors"])),
        send_button_selectors=list(raw.get("send_button_selectors", defaults["send_button_selectors"])),
        assistant_message_selectors=list(raw.get("assistant_message_selectors", defaults["assistant_message_selectors"])),
        new_chat_selectors=list(raw.get("new_chat_selectors", defaults["new_chat_selectors"])),
    )


def load_config(config_path: Path) -> BridgeConfig:
    base_dir = config_path.parent.resolve()
    raw = json.loads(config_path.read_text(encoding="utf-8-sig"))
    provider_section = raw.get("providers", {})
    providers = {
        provider_name: provider_from_raw(base_dir, provider_name, provider_section.get(provider_name, {}))
        for provider_name in sorted(WORKERS)
    }
    return BridgeConfig(
        db_path=normalize_path(base_dir, raw.get("db_path", str(DB_PATH))),
        export_path=normalize_path(base_dir, raw.get("export_path", "dialogue.md")),
        poll_interval_seconds=float(raw.get("poll_interval_seconds", 3)),
        response_timeout_seconds=float(raw.get("response_timeout_seconds", 240)),
        stability_window_seconds=float(raw.get("stability_window_seconds", 6)),
        providers=providers,
    )


def main() -> None:
    configure_stdio()
    parser = argparse.ArgumentParser(description="Drive ChatGPT Web and Gemini Web turn-by-turn.")
    parser.add_argument("--config", type=Path, default=Path("bridge_config.json"), help="JSON config file. Start from bridge_config.example.json.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_setup = sub.add_parser("setup", help="Open a provider tab in the shared Chrome profile so you can log in manually.")
    p_setup.add_argument("--provider", choices=sorted(WORKERS), required=True)

    p_run = sub.add_parser("run", help="Initialize a conversation and keep both browser tabs running.")
    p_run.add_argument("--seed")
    p_run.add_argument("--first-turn", choices=sorted(WORKERS), default="GEMINI")
    p_run.add_argument("--max-turns", type=int, default=10)
    p_run.add_argument("--resume", action="store_true")

    sub.add_parser("validate", help="Validate config resolution without launching browsers.")

    args = parser.parse_args()
    config = load_config(args.config)
    runner = BridgeRunner(config)

    if args.cmd == "setup":
        runner.setup_provider(args.provider)
    elif args.cmd == "run":
        runner.run(args.seed, args.first_turn, args.max_turns, args.resume)
    elif args.cmd == "validate":
        print(json.dumps({
            "db_path": str(config.db_path),
            "export_path": str(config.export_path),
            "providers": {
                name: {
                    "url": provider.url,
                    "browser_executable": str(provider.browser_executable),
                    "profile_dir": str(provider.profile_dir),
                }
                for name, provider in config.providers.items()
            },
        }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
