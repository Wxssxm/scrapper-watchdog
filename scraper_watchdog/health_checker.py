"""Validates scraper output CSV against an expected schema."""
from __future__ import annotations

import csv
import os
from dataclasses import dataclass, field
from typing import Literal


ErrorType = Literal["missing_columns", "low_volume", "empty_output", "parse_error"]


@dataclass
class HealthResult:
    success: bool
    error_type: ErrorType | None = None
    details: dict = field(default_factory=dict)


class HealthChecker:
    """Check that a scraper's CSV output meets the declared schema."""

    def check(self, output_path: str, expected_schema: dict) -> HealthResult:
        """
        Validate the CSV at *output_path* against *expected_schema*.

        expected_schema keys:
            columns  : list[str]  – required column names
            min_rows : int        – minimum acceptable row count
        """
        expected_columns: list[str] = expected_schema.get("columns", [])
        min_rows: int = expected_schema.get("min_rows", 1)

        # ── existence / emptiness ─────────────────────────────────────────────
        if not os.path.exists(output_path):
            return HealthResult(
                success=False,
                error_type="empty_output",
                details={"reason": "file does not exist", "path": output_path},
            )

        if os.path.getsize(output_path) == 0:
            return HealthResult(
                success=False,
                error_type="empty_output",
                details={"reason": "file is empty", "path": output_path},
            )

        # ── parse ─────────────────────────────────────────────────────────────
        try:
            with open(output_path, newline="", encoding="utf-8") as fh:
                reader = csv.DictReader(fh)
                found_columns = list(reader.fieldnames or [])
                rows = list(reader)
        except Exception as exc:
            return HealthResult(
                success=False,
                error_type="parse_error",
                details={"reason": str(exc), "path": output_path},
            )

        # ── column presence ───────────────────────────────────────────────────
        missing = [c for c in expected_columns if c not in found_columns]
        if missing:
            return HealthResult(
                success=False,
                error_type="missing_columns",
                details={
                    "found_columns": found_columns,
                    "expected": expected_columns,
                    "missing": missing,
                },
            )

        # ── row count ─────────────────────────────────────────────────────────
        row_count = len(rows)
        if row_count < min_rows:
            return HealthResult(
                success=False,
                error_type="low_volume",
                details={
                    "row_count": row_count,
                    "min_rows": min_rows,
                },
            )

        return HealthResult(success=True, details={"row_count": row_count})
