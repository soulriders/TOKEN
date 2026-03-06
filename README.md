# ChatGPT-Gemini Web Bridge

This project runs a local browser bridge that lets ChatGPT Web and Gemini Web talk to each other by taking turns.

Layers:
- `orchestrator.py`: SQLite state and transcript storage
- `web_bridge.py`: Playwright browser automation
- `dashboard_server.py` + `dashboard/index.html`: local control dashboard

## Requirements

- Windows PC
- Python 3.10+
- Google Chrome installed
- Logged-in ChatGPT Web and Gemini Web accounts
- Acceptance of the risks of browser UI automation

## Install

```powershell
python -m pip install -r requirements.txt
python -m playwright install
```

## Current Browser Mode

The bridge is configured to reuse your existing Chrome user data and `Profile 2`.
That means:
- you must fully close Chrome before launching automation
- Playwright will open your real Chrome profile
- ChatGPT and Gemini will run as tabs in the same Chrome window

Current profile target:
- user data dir: `C:\Users\soulr\AppData\Local\Google\Chrome\User Data`
- profile name: `Profile 2`

## Start The Dashboard

```powershell
python dashboard_server.py --config bridge_config.json --host 127.0.0.1 --port 8765
```

Then open:
- [http://127.0.0.1:8765](http://127.0.0.1:8765)

## Normal Flow

1. Close Chrome completely.
2. Start the dashboard server.
3. In the dashboard, click `Open Browser` for ChatGPT.
4. Confirm your existing ChatGPT session is available.
5. Click `Finish Setup`.
6. Repeat for Gemini.
7. Enter the seed prompt and click `Run`.

## CLI Alternative

```powershell
python web_bridge.py --config bridge_config.json setup --provider CHATGPT
python web_bridge.py --config bridge_config.json setup --provider GEMINI
python web_bridge.py --config bridge_config.json run --first-turn GEMINI --max-turns 10 --seed "Debate topic: ethical limits of AI automation"
```

## Important Files

- `bridge_config.json`: active runtime config
- `bridge_config.example.json`: example config
- `web_bridge.py`: Playwright bridge logic
- `dashboard_server.py`: local API and static dashboard server
- `dashboard/index.html`: dashboard UI
- `orchestrator.db`: runtime state database
- `dialogue.md`: exported transcript
- `artifacts/`: screenshots captured on failures

## Troubleshooting

- If a site DOM changes, update selectors in `bridge_config.json`.
- If login expires, run setup again.
- If responses are too slow, increase `response_timeout_seconds` or `stability_window_seconds`.
- If launch fails, make sure Chrome is fully closed.

## Notes

- This is browser automation, not an API integration.
- Sites may change their markup and break selectors over time.
- Reusing your real Chrome profile is convenient, but it is less isolated than a dedicated automation profile.