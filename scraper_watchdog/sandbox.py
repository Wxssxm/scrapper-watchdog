"""Run a repaired scraper in isolation and validate its output."""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field

from .health_checker import HealthChecker, HealthResult
from .logger import get_logger, SourceLoggerAdapter

_base_logger = get_logger()


@dataclass
class SandboxResult:
    passed: bool
    health: HealthResult | None = None
    error: str | None = None


class Sandbox:
    """Write a candidate script to a temp file, run it, and health-check the output."""

    _TIMEOUT_SECONDS = 90

    def __init__(self) -> None:
        self._checker = HealthChecker()

    def test(
        self,
        script_code: str,
        source_config: dict,
        logger: SourceLoggerAdapter | None = None,
    ) -> SandboxResult:
        log = logger or SourceLoggerAdapter(_base_logger, source_config.get("name", "unknown"))
        expected_schema = source_config.get("expected_schema", {})

        script_fd = out_fd = None
        script_path = out_path = None
        try:
            # 1. Write script to temp file
            script_fd, script_path = tempfile.mkstemp(suffix=".py", prefix="watchdog_sandbox_")
            os.close(script_fd)
            script_fd = None
            with open(script_path, "w", encoding="utf-8") as fh:
                fh.write(script_code)

            # 2. Temp output CSV
            out_fd, out_path = tempfile.mkstemp(suffix=".csv", prefix="watchdog_out_")
            os.close(out_fd)
            out_fd = None

            # 3. Run
            env = {**os.environ, "OUTPUT_PATH": out_path}
            result = subprocess.run(
                [sys.executable, script_path],
                env=env,
                capture_output=True,
                text=True,
                timeout=self._TIMEOUT_SECONDS,
            )

            if result.returncode != 0:
                error_msg = (result.stderr or result.stdout or "non-zero exit").strip()
                log.log_event("sandbox_fail", {"reason": error_msg[:500]})
                return SandboxResult(passed=False, error=error_msg[:500])

            # 4. Health check
            health = self._checker.check(out_path, expected_schema)
            if health.success:
                log.log_event("sandbox_pass", {"details": health.details})
                return SandboxResult(passed=True, health=health)
            else:
                log.log_event("sandbox_fail", {"health": health.error_type, "details": health.details})
                return SandboxResult(passed=False, health=health, error=health.error_type)

        except subprocess.TimeoutExpired:
            error_msg = f"script exceeded {self._TIMEOUT_SECONDS}s timeout"
            log.log_event("sandbox_fail", {"reason": error_msg})
            return SandboxResult(passed=False, error=error_msg)

        except Exception as exc:
            log.log_event("sandbox_fail", {"reason": str(exc)})
            return SandboxResult(passed=False, error=str(exc))

        finally:
            for path in (script_path, out_path):
                if path and os.path.exists(path):
                    try:
                        os.unlink(path)
                    except OSError:
                        pass
