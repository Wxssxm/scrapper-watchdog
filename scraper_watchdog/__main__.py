"""CLI entrypoint: python -m scraper_watchdog"""
from __future__ import annotations

import argparse
import sys

from dotenv import load_dotenv

load_dotenv()


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="scraper_watchdog",
        description="Watch and auto-repair web scrapers using Claude AI.",
    )
    parser.add_argument(
        "--config",
        required=True,
        metavar="PATH",
        help="Path to the YAML config file (e.g. configs/example.yaml)",
    )

    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--source",
        metavar="NAME",
        help="Run a single named source",
    )
    group.add_argument(
        "--all",
        action="store_true",
        help="Run all sources defined in the config",
    )

    args = parser.parse_args()

    # Import here so dotenv runs first and the API key check in Repairer works
    from .watcher import ScraperWatcher

    try:
        watcher = ScraperWatcher(config_path=args.config)
    except EnvironmentError as exc:
        print(f"[scraper-watchdog] startup error: {exc}", file=sys.stderr)
        return 1
    except FileNotFoundError as exc:
        print(f"[scraper-watchdog] config not found: {exc}", file=sys.stderr)
        return 1

    if args.all:
        results = watcher.run_all()
        failed = [name for name, ok in results.items() if not ok]
        if failed:
            print(f"[scraper-watchdog] sources with unresolved failures: {', '.join(failed)}", file=sys.stderr)
            return 1
        return 0
    else:
        try:
            ok = watcher.run(source_name=args.source)
        except ValueError as exc:
            print(f"[scraper-watchdog] {exc}", file=sys.stderr)
            return 1
        return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
