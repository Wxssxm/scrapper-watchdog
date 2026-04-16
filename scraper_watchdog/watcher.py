"""Main ScraperWatcher orchestrator."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import date
from pathlib import Path
from typing import Any

import yaml

from .deployer import Deployer
from .health_checker import HealthChecker
from .logger import SourceLoggerAdapter, get_logger
from .notifier import Notifier
from .repairer import Repairer
from .sandbox import Sandbox

_base_logger = get_logger()

_STATE_FILE = ".watchdog_state.json"
_RUN_TIMEOUT = 60  # seconds


class ScraperWatcher:
    """
    Orchestrate scraper execution, health checking, and auto-repair.

    Usage::

        watcher = ScraperWatcher(config_path="configs/example.yaml")
        watcher.run(source_name="my_source")
        watcher.run_all()
    """

    def __init__(self, config_path: str) -> None:
        self._config_path = Path(config_path).resolve()
        self._base_dir = self._config_path.parent
        self._config = self._load_config(config_path)
        self._checker = HealthChecker()
        self._repairer = Repairer()
        self._sandbox = Sandbox()
        self._deployer = Deployer()
        self._notifier = Notifier()

    # ── public API ─────────────────────────────────────────────────────────────

    def run(self, source_name: str) -> bool:
        """Run a single source. Returns True if the source ended in a healthy state."""
        source = self._find_source(source_name)
        return self._run_source(source)

    def run_all(self) -> dict[str, bool]:
        """Run every source declared in the config. Returns name → success mapping."""
        results: dict[str, bool] = {}
        for source in self._config.get("sources", []):
            results[source["name"]] = self._run_source(source)
        return results

    # ── internals ─────────────────────────────────────────────────────────────

    def _run_source(self, source: dict) -> bool:
        source = self._resolve_paths(source)
        name: str = source["name"]
        log = SourceLoggerAdapter(_base_logger, name)
        log.log_event("run_start", {"script": source["script_path"]})

        # 1. Run scraper
        run_error = self._execute_script(source, log)

        # 2. Health check
        health = self._checker.check(
            source.get("output_path", ""),
            source.get("expected_schema", {}),
        )

        if health.success:
            log.log_event("health_ok", health.details)
            return True

        log.log_event("health_fail", {"error_type": health.error_type, **health.details})

        # 3. Repair loop
        max_attempts: int = source.get("repair", {}).get("max_attempts", 3)
        today = str(date.today())
        attempts_today = self._get_attempts(name, today)

        while attempts_today < max_attempts:
            attempts_today += 1
            self._set_attempts(name, today, attempts_today)

            repaired_code = self._repairer.repair(source, health, attempt=attempts_today, logger=log)
            if repaired_code is None:
                log.log_event("repair_fail", {"attempt": attempts_today})
                continue

            sandbox_result = self._sandbox.test(repaired_code, source, logger=log)
            if not sandbox_result.passed:
                log.log_event("sandbox_fail", {"attempt": attempts_today, "error": sandbox_result.error})
                continue

            # Sandbox passed — deploy
            self._deployer.deploy(repaired_code, source, logger=log)
            return True

        # Exhausted attempts
        self._notifier.alert(source, health, attempt=attempts_today, logger=log)
        return False

    def _execute_script(self, source: dict, log: SourceLoggerAdapter) -> str | None:
        """Run the scraper subprocess. Returns stderr on failure, None on success."""
        env = {**os.environ, "OUTPUT_PATH": source.get("output_path", "")}
        try:
            result = subprocess.run(
                [sys.executable, source["script_path"]],
                env=env,
                capture_output=True,
                text=True,
                timeout=_RUN_TIMEOUT,
            )
            if result.returncode != 0:
                err = (result.stderr or result.stdout or "non-zero exit").strip()
                log.log_event("health_fail", {"reason": "subprocess failed", "stderr": err[:300]})
                return err
            return None
        except subprocess.TimeoutExpired:
            err = f"scraper timed out after {_RUN_TIMEOUT}s"
            log.log_event("health_fail", {"reason": err})
            return err
        except Exception as exc:
            log.log_event("health_fail", {"reason": str(exc)})
            return str(exc)

    # ── config helpers ────────────────────────────────────────────────────────

    def _load_config(self, config_path: str) -> dict:
        with open(config_path, encoding="utf-8") as fh:
            return yaml.safe_load(fh)

    def _find_source(self, name: str) -> dict:
        for s in self._config.get("sources", []):
            if s["name"] == name:
                return s
        raise ValueError(f"Source '{name}' not found in config '{self._config_path}'")

    def _resolve_paths(self, source: dict) -> dict:
        """Make script_path and output_path absolute relative to the config directory."""
        source = dict(source)
        for key in ("script_path", "output_path"):
            if key in source:
                p = Path(source[key])
                if not p.is_absolute():
                    source[key] = str(self._base_dir / p)
        return source

    # ── state tracking ────────────────────────────────────────────────────────

    def _state_path(self) -> Path:
        return self._base_dir / _STATE_FILE

    def _load_state(self) -> dict[str, Any]:
        sp = self._state_path()
        if sp.exists():
            try:
                with open(sp, encoding="utf-8") as fh:
                    return json.load(fh)
            except Exception:
                pass
        return {}

    def _save_state(self, state: dict) -> None:
        with open(self._state_path(), "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2)

    def _get_attempts(self, source_name: str, day: str) -> int:
        state = self._load_state()
        return state.get(source_name, {}).get(day, 0)

    def _set_attempts(self, source_name: str, day: str, count: int) -> None:
        state = self._load_state()
        state.setdefault(source_name, {})[day] = count
        # Prune entries older than today to keep the file tidy
        state[source_name] = {d: v for d, v in state[source_name].items() if d >= day}
        self._save_state(state)
