"""Repair a broken scraper script using the Claude API."""
from __future__ import annotations

import os
import re

import httpx

from .health_checker import HealthResult
from .logger import get_logger, SourceLoggerAdapter

_base_logger = get_logger()

_SYSTEM_PROMPT_TEMPLATE = """\
You are an expert Python web scraping engineer.
You will receive a broken scraper script, the error it produced, and the current HTML of the target page.
Your task is to return a fixed Python script that scrapes the same data correctly.

Rules:
- Return ONLY the Python code, no explanation, no markdown fences
- The script must write its output as a CSV to the path stored in the OUTPUT_PATH environment variable
- The output CSV must contain exactly these columns: {expected_columns}
- Use httpx or requests for HTTP. Use BeautifulSoup or lxml for parsing. Do NOT use Selenium or Playwright.
- Handle common anti-bot patterns: rotate User-Agent, add realistic headers, add random sleep between 1-3s
- If the page requires JS rendering and static scraping fails, raise a clear exception: NotImplementedError("JS rendering required")
"""

_USER_PROMPT_TEMPLATE = """\
## Broken script (attempt {attempt})
```python
{current_script_code}
```

## Error detected
error_type: {error_type}
details: {error_details}

## Current live HTML of the target page (truncated to 4000 chars)
{html_sample}

## Expected output schema
columns: {expected_columns}
min_rows: {min_rows}

Return the fixed Python script now.
"""

_HTML_TRUNCATION = 4000
_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


class Repairer:
    """Use Claude to repair a broken scraper script."""

    def __init__(self) -> None:
        self._ensure_api_key()

    # ── public API ─────────────────────────────────────────────────────────────

    def repair(
        self,
        source_config: dict,
        error_info: HealthResult,
        attempt: int,
        logger: SourceLoggerAdapter | None = None,
    ) -> str | None:
        """
        Attempt to repair the broken scraper described by *source_config*.

        Returns the repaired Python source code as a string, or *None* if
        extraction from the model response fails.
        """
        log = logger or SourceLoggerAdapter(_base_logger, source_config.get("name", "unknown"))
        expected_schema = source_config.get("expected_schema", {})
        expected_columns = expected_schema.get("columns", [])
        min_rows = expected_schema.get("min_rows", 1)
        model = source_config.get("repair", {}).get("model", "claude-opus-4-7")

        # 1. Fetch live HTML
        html_sample = self._fetch_html(source_config["url"])

        # 2. Read broken script
        script_path = source_config["script_path"]
        try:
            with open(script_path, encoding="utf-8") as fh:
                current_code = fh.read()
        except OSError as exc:
            log.log_event("repair_fail", {"reason": f"cannot read script: {exc}", "attempt": attempt})
            return None

        # 3. Build prompts
        system_prompt = _SYSTEM_PROMPT_TEMPLATE.format(
            expected_columns=", ".join(expected_columns)
        )
        user_prompt = _USER_PROMPT_TEMPLATE.format(
            attempt=attempt,
            current_script_code=current_code,
            error_type=error_info.error_type,
            error_details=error_info.details,
            html_sample=html_sample,
            expected_columns=", ".join(expected_columns),
            min_rows=min_rows,
        )

        # 4. Call Claude API
        log.log_event("repair_attempt", {"attempt": attempt, "model": model})
        try:
            import anthropic  # lazy import so the module loads without the key

            client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
            message = client.messages.create(
                model=model,
                max_tokens=4096,
                system=system_prompt,
                messages=[{"role": "user", "content": user_prompt}],
            )
            raw_response = message.content[0].text
        except Exception as exc:
            log.log_event("repair_fail", {"reason": str(exc), "attempt": attempt})
            return None

        # 5. Extract code block
        repaired_code = self._extract_code(raw_response)
        if repaired_code is None:
            log.log_event(
                "repair_fail",
                {"reason": "could not extract Python code from model response", "attempt": attempt},
            )
            return None

        log.log_event("repair_ok", {"attempt": attempt, "code_length": len(repaired_code)})
        return repaired_code

    # ── helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _ensure_api_key() -> None:
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise EnvironmentError(
                "ANTHROPIC_API_KEY environment variable is not set.\n"
                "Export it before running scraper-watchdog:\n"
                "  export ANTHROPIC_API_KEY=sk-ant-..."
            )

    @staticmethod
    def _fetch_html(url: str) -> str:
        headers = {"User-Agent": _USER_AGENT, "Accept-Language": "en-US,en;q=0.9"}
        try:
            with httpx.Client(follow_redirects=True, timeout=30) as client:
                response = client.get(url, headers=headers)
                response.raise_for_status()
                html = response.text
        except Exception as exc:
            html = f"<!-- could not fetch HTML: {exc} -->"
        return html[:_HTML_TRUNCATION]

    @staticmethod
    def _extract_code(text: str) -> str | None:
        """Strip markdown fences and return raw Python code, or None."""
        # Try ```python ... ``` first, then bare ``` ... ```
        for pattern in (
            r"```python\s*\n(.*?)```",
            r"```\s*\n(.*?)```",
        ):
            match = re.search(pattern, text, re.DOTALL)
            if match:
                return match.group(1).strip()

        # If the model obeyed "no fences", the whole response is the code.
        # Heuristic: it must contain at least one import or def statement.
        stripped = text.strip()
        if re.search(r"^\s*(import |from |def |class )", stripped, re.MULTILINE):
            return stripped

        return None
