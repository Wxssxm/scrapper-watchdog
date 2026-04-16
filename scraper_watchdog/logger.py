"""Structured JSON logging for scraper-watchdog."""
import json
import logging
import sys
from datetime import datetime, timezone


class JSONFormatter(logging.Formatter):
    """Format log records as single-line JSON objects."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "source_name": getattr(record, "source_name", None),
            "event": getattr(record, "event", record.getMessage()),
            "details": getattr(record, "details", {}),
        }
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def get_logger(name: str = "scraper_watchdog") -> logging.Logger:
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(JSONFormatter())
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
    return logger


class SourceLoggerAdapter(logging.LoggerAdapter):
    """Attach source_name to every log record emitted through this adapter."""

    def __init__(self, logger: logging.Logger, source_name: str) -> None:
        super().__init__(logger, {"source_name": source_name})
        self.source_name = source_name

    def log_event(self, event: str, details: dict | None = None, level: int = logging.INFO) -> None:
        self.log(
            level,
            event,
            extra={
                "source_name": self.source_name,
                "event": event,
                "details": details or {},
            },
        )

    def process(self, msg: str, kwargs: dict):
        kwargs.setdefault("extra", {})
        kwargs["extra"].setdefault("source_name", self.source_name)
        kwargs["extra"].setdefault("event", msg)
        kwargs["extra"].setdefault("details", {})
        return msg, kwargs
