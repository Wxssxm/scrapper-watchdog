# scraper-watchdog

![CI](https://github.com/Wxssxm/scrapper-watchdog/actions/workflows/ci.yml/badge.svg)
![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)
![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)

**scraper-watchdog** is a Python library that monitors your web scrapers and automatically repairs them when they break. It runs each scraper, validates the output against a declared schema, and — if the scraper fails — sends the broken script, the live HTML of the target page, and the error to the Claude AI API to generate a repaired version. The repaired script is tested in an isolated sandbox before being deployed, keeping your data pipelines running without manual intervention.

---

## Quick start

### 1. Install

```bash
pip install scraper-watchdog
# or, from source:
git clone https://github.com/Wxssxm/scrapper-watchdog.git
cd scrapper-watchdog
pip install -e ".[dev]"
```

### 2. Set up environment variables

```bash
cp .env.example .env   # then fill in your keys
export ANTHROPIC_API_KEY=sk-ant-...
export SLACK_WEBHOOK_URL=https://hooks.slack.com/services/...   # optional
```

### 3. Create a config file

Copy `configs/example.yaml` and edit it for your scraper:

```yaml
sources:
  - name: "my_source"
    script_path: "scrapers/my_source.py"
    url: "https://example.com/data"
    expected_schema:
      columns: ["title", "price", "date"]
      min_rows: 10
    output_path: "data/my_source.csv"
    repair:
      max_attempts: 3
      model: "claude-opus-4-7"
    notify:
      slack_webhook: "${SLACK_WEBHOOK_URL}"
```

### 4. Run

```bash
# Run a single source
python -m scraper_watchdog --config configs/example.yaml --source my_source

# Run all sources
python -m scraper_watchdog --config configs/example.yaml --all

# Or via the installed CLI entry point
scraper-watchdog --config configs/example.yaml --all
```

A working example scraper is provided in `scrapers/example_scraper.py`.

---

## YAML config reference

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `sources[].name` | string | yes | — | Unique identifier for this scraper source |
| `sources[].script_path` | string | yes | — | Path to the scraper script (relative to config file) |
| `sources[].url` | string | yes | — | Target URL; also fetched to give Claude live HTML context |
| `sources[].expected_schema.columns` | list[str] | yes | — | Column names the output CSV must contain |
| `sources[].expected_schema.min_rows` | int | yes | — | Minimum number of data rows required |
| `sources[].output_path` | string | yes | — | Where the scraper writes its CSV output |
| `sources[].repair.max_attempts` | int | no | `3` | Max Claude repair attempts per source per day |
| `sources[].repair.model` | string | no | `claude-opus-4-7` | Claude model used for repairs |
| `sources[].notify.slack_webhook` | string | no | — | Slack incoming webhook URL (supports `${ENV_VAR}`) |

---

## How the repair loop works

```
┌──────────────────────────────────────────────────────────┐
│                     ScraperWatcher.run()                  │
│                                                           │
│  1. RUN SCRAPER                                           │
│     subprocess → scrapers/my_source.py                   │
│     (timeout: 60 s, OUTPUT_PATH injected via env)        │
│            │                                              │
│            ▼                                              │
│  2. HEALTH CHECK                                          │
│     HealthChecker validates output CSV                    │
│     ┌─ pass ──► log "health_ok", done ✓                  │
│     └─ fail ──► enter repair loop ─────────────┐         │
│                                                 │         │
│  3. REPAIR LOOP  (max_attempts times per day)   │         │
│     │                                           │         │
│     ├─ Repairer.repair()                        │         │
│     │    • fetch live HTML (httpx)              │         │
│     │    • read broken script                   │         │
│     │    • call Claude API → new script code    │         │
│     │                                           │         │
│     ├─ Sandbox.test()                           │         │
│     │    • run in isolated subprocess           │         │
│     │    • health-check the sandbox output      │         │
│     │    ┌─ pass ──► Deployer.deploy()          │         │
│     │    │             backup .bak, overwrite   │         │
│     │    │             log "deploy_success" ✓   │         │
│     │    └─ fail ──► next attempt ──────────────┘         │
│     │                                                      │
│     └─ (exhausted) ──► Notifier.alert() ── Slack/stderr   │
└──────────────────────────────────────────────────────────┘
```

Attempt counts are persisted in `.watchdog_state.json` (in the config file's directory) so that the counter survives multiple scheduler runs within the same calendar day. Old entries are pruned automatically.

---

## Environment variables

| Variable | Required | Description |
|---|---|---|
| `ANTHROPIC_API_KEY` | **yes** | Your Anthropic API key. Get one at [console.anthropic.com](https://console.anthropic.com). |
| `SLACK_WEBHOOK_URL` | no | Slack incoming webhook URL for human-readable alerts. |
| `GITHUB_TOKEN` | no | GitHub personal access token — only required for PR deploy mode. |

Place these in a `.env` file at the project root; `python-dotenv` loads it automatically.

```
# .env  (copy from .env.example)
ANTHROPIC_API_KEY=sk-ant-...
SLACK_WEBHOOK_URL=https://hooks.slack.com/services/T.../B.../...
GITHUB_TOKEN=ghp_...
```

---

## Scraper script contract

scraper-watchdog treats each scraper as a black box. Your scraper must:

1. **Read `OUTPUT_PATH`** from the environment and write its results there as a CSV.
2. **Exit with code 0** on success; any non-zero exit triggers the repair flow.
3. Contain exactly the declared columns in its header row.

```python
# scrapers/my_source.py  — minimal example
import csv, os, httpx
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}

resp = httpx.get("https://example.com/data", headers=HEADERS, timeout=30)
resp.raise_for_status()
soup = BeautifulSoup(resp.text, "lxml")

rows = [
    {"title": el.text, "price": el["data-price"], "date": el["data-date"]}
    for el in soup.select(".item")
]

with open(os.environ["OUTPUT_PATH"], "w", newline="") as f:
    w = csv.DictWriter(f, fieldnames=["title", "price", "date"])
    w.writeheader()
    w.writerows(rows)
```

See `scrapers/example_scraper.py` for a fully working example scraping Hacker News.

---

## JavaScript-heavy sites & anti-bot measures

scraper-watchdog is designed for **static HTML scraping** (httpx + BeautifulSoup/lxml). If you are targeting sites that rely heavily on JavaScript rendering, keep the following in mind:

- **JS detection**: when the repaired script cannot extract data from static HTML, it raises `NotImplementedError("JS rendering required")`. This surfaces in the logs and triggers a human alert instead of an infinite repair loop.
- **Proxy rotation**: if your target site rate-limits or bans your IP before the watchdog can even attempt a repair, no amount of AI-generated code will help. Set up proxy rotation at the scraper level (e.g. with `httpx` transport layers or a proxy provider) **before** plugging the scraper into watchdog.
- **Headers & fingerprinting**: always send realistic headers (`User-Agent`, `Accept-Language`, `Accept`, `Referer`). The example scraper and the Claude repair prompt both include this pattern by default.
- **Recommended stack for JS-heavy sites**: pair scraper-watchdog with a headless browser layer (Playwright/Selenium) and expose a static-HTML interface to your scraper script. The watchdog then monitors the output schema regardless of the rendering strategy used underneath.

---

## Running tests

```bash
pip install -e ".[dev]"
pytest
# With coverage:
pytest --cov=scraper_watchdog --cov-report=term-missing
```

---

## Contributing — adding a new notifier adapter

All notification logic lives in `scraper_watchdog/notifier.py`. The `Notifier.alert()` method is the single extension point.

**Steps to add a new channel (e.g. PagerDuty, Discord, email via SMTP):**

1. Add the new channel's configuration key(s) to `configs/example.yaml` under `notify:`.
2. In `notifier.py`, read the new key from `notify_cfg` inside `Notifier.alert()`.
3. Implement a private helper method `_send_<channel>(message, config)` on the `Notifier` class.
4. Call it after the Slack block, guarded by `if notify_cfg.get("your_key"):`.
5. Emit a `"alert_sent"` log event with `"channel": "<your_channel>"`.
6. Add a unit test in `tests/test_watcher.py` that mocks the HTTP call and asserts the message is sent.

```python
# Example skeleton
def _send_discord(self, message: str, webhook_url: str) -> None:
    with httpx.Client(timeout=10) as client:
        client.post(webhook_url, json={"content": message}).raise_for_status()
```

No interface or base class is required — the notifier is intentionally simple and imperative.

---

## License

[MIT](LICENSE)
