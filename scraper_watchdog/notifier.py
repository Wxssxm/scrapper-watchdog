"""Send human-readable alerts when automated repair exhausts its retry budget."""
from __future__ import annotations

import os
import sys

import httpx

from .health_checker import HealthResult
from .logger import get_logger, SourceLoggerAdapter

_base_logger = get_logger()


class Notifier:
    """Dispatch alerts via Slack webhook and/or stderr."""

    def alert(
        self,
        source_config: dict,
        error_info: HealthResult,
        attempt: int,
        logger: SourceLoggerAdapter | None = None,
    ) -> None:
        log = logger or SourceLoggerAdapter(_base_logger, source_config.get("name", "unknown"))

        name: str = source_config.get("name", "unknown")
        script_path: str = source_config.get("script_path", "unknown")
        max_attempts: int = source_config.get("repair", {}).get("max_attempts", 3)
        notify_cfg: dict = source_config.get("notify", {})

        message = (
            f"\u26a0\ufe0f *Scraper watchdog alert*\n"
            f"Source: `{name}`\n"
            f"Error: `{error_info.error_type}` \u2014 {error_info.details}\n"
            f"Attempts: {attempt}/{max_attempts}\n"
            f"Action required: manual fix needed on `{script_path}`"
        )

        # ── Slack ─────────────────────────────────────────────────────────────
        raw_webhook: str = notify_cfg.get("slack_webhook", "")
        webhook_url = os.path.expandvars(raw_webhook) if raw_webhook else ""

        if webhook_url and not webhook_url.startswith("$"):
            try:
                with httpx.Client(timeout=10) as client:
                    resp = client.post(webhook_url, json={"text": message})
                    resp.raise_for_status()
                log.log_event(
                    "alert_sent",
                    {"channel": "slack", "source": name, "attempt": attempt},
                )
            except Exception as exc:
                log.log_event(
                    "alert_sent",
                    {"channel": "slack_failed", "reason": str(exc), "attempt": attempt},
                )

        # ── stderr fallback ───────────────────────────────────────────────────
        print(message, file=sys.stderr)
        log.log_event(
            "alert_sent",
            {"channel": "stderr", "source": name, "attempt": attempt},
        )
