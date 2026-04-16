"""Unit tests for scraper-watchdog components."""
from __future__ import annotations

import csv
import os
import subprocess
import tempfile
import textwrap
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ── fixtures ──────────────────────────────────────────────────────────────────

def _write_csv(path: str, rows: list[dict]) -> None:
    if not rows:
        open(path, "w").close()
        return
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


SCHEMA = {"columns": ["title", "price", "date"], "min_rows": 3}


# ═══════════════════════════════════════════════════════════════════════════════
# HealthChecker
# ═══════════════════════════════════════════════════════════════════════════════

class TestHealthChecker:
    def setup_method(self):
        from scraper_watchdog.health_checker import HealthChecker
        self.checker = HealthChecker()

    # ── empty_output ──────────────────────────────────────────────────────────

    def test_missing_file(self, tmp_path):
        result = self.checker.check(str(tmp_path / "nonexistent.csv"), SCHEMA)
        assert result.success is False
        assert result.error_type == "empty_output"

    def test_empty_file(self, tmp_path):
        p = tmp_path / "out.csv"
        p.write_text("")
        result = self.checker.check(str(p), SCHEMA)
        assert result.success is False
        assert result.error_type == "empty_output"

    # ── parse_error ───────────────────────────────────────────────────────────

    def test_parse_error(self, tmp_path):
        p = tmp_path / "out.csv"
        p.write_bytes(b"\xff\xfe bad bytes that are not valid utf-8 \x00\x00")
        result = self.checker.check(str(p), SCHEMA)
        # Will be empty_output (size > 0 but unreadable) or parse_error
        assert result.success is False
        assert result.error_type in ("parse_error", "missing_columns", "empty_output", "low_volume")

    # ── missing_columns ───────────────────────────────────────────────────────

    def test_missing_columns(self, tmp_path):
        p = tmp_path / "out.csv"
        rows = [{"title": "t1", "price": "1.0"} for _ in range(5)]  # missing "date"
        _write_csv(str(p), rows)
        result = self.checker.check(str(p), SCHEMA)
        assert result.success is False
        assert result.error_type == "missing_columns"
        assert "date" in result.details["missing"]

    # ── low_volume ────────────────────────────────────────────────────────────

    def test_low_volume(self, tmp_path):
        p = tmp_path / "out.csv"
        rows = [{"title": "t", "price": "1", "date": "2024-01-01"} for _ in range(2)]
        _write_csv(str(p), rows)
        result = self.checker.check(str(p), SCHEMA)
        assert result.success is False
        assert result.error_type == "low_volume"
        assert result.details["row_count"] == 2

    # ── success ───────────────────────────────────────────────────────────────

    def test_success(self, tmp_path):
        p = tmp_path / "out.csv"
        rows = [{"title": f"t{i}", "price": str(i), "date": "2024-01-01"} for i in range(5)]
        _write_csv(str(p), rows)
        result = self.checker.check(str(p), SCHEMA)
        assert result.success is True
        assert result.details["row_count"] == 5


# ═══════════════════════════════════════════════════════════════════════════════
# Repairer
# ═══════════════════════════════════════════════════════════════════════════════

class TestRepairer:
    """Mock the Anthropic API call and assert the prompt contains required fields."""

    SCRIPT_CODE = textwrap.dedent("""\
        import csv, os
        with open(os.environ['OUTPUT_PATH'], 'w', newline='') as f:
            w = csv.writer(f)
            w.writerow(['title', 'price', 'date'])
    """)

    SOURCE_CONFIG = {
        "name": "test_source",
        "url": "https://example.com/data",
        "script_path": "",          # will be filled in fixture
        "expected_schema": SCHEMA,
        "repair": {"max_attempts": 3, "model": "claude-opus-4-5"},
    }

    @pytest.fixture(autouse=True)
    def write_script(self, tmp_path):
        script = tmp_path / "scraper.py"
        script.write_text(self.SCRIPT_CODE)
        self.SOURCE_CONFIG = dict(self.SOURCE_CONFIG, script_path=str(script))

    def _make_health_result(self):
        from scraper_watchdog.health_checker import HealthResult
        return HealthResult(
            success=False,
            error_type="low_volume",
            details={"row_count": 1, "min_rows": 3},
        )

    def test_prompt_contains_expected_fields(self):
        """The user message sent to Claude must reference key context fields."""
        from scraper_watchdog.repairer import Repairer

        fake_response = MagicMock()
        fake_response.content = [MagicMock(text=self.SCRIPT_CODE)]

        captured_kwargs: dict = {}

        def fake_create(**kwargs):
            captured_kwargs.update(kwargs)
            return fake_response

        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-fake"}):
            with patch("anthropic.Anthropic") as MockClient:
                MockClient.return_value.messages.create.side_effect = fake_create
                with patch.object(Repairer, "_fetch_html", return_value="<html>test</html>"):
                    repairer = Repairer()
                    result = repairer.repair(self.SOURCE_CONFIG, self._make_health_result(), attempt=1)

        user_msg = captured_kwargs["messages"][0]["content"]
        assert "low_volume" in user_msg, "error_type should appear in prompt"
        assert "title" in user_msg, "expected columns should appear in prompt"
        assert "attempt 1" in user_msg.lower() or "attempt=1" in user_msg.lower() or "(attempt 1)" in user_msg

    def test_returns_code_when_api_succeeds(self):
        from scraper_watchdog.repairer import Repairer

        fake_response = MagicMock()
        fake_response.content = [MagicMock(text=self.SCRIPT_CODE)]

        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-fake"}):
            with patch("anthropic.Anthropic") as MockClient:
                MockClient.return_value.messages.create.return_value = fake_response
                with patch.object(Repairer, "_fetch_html", return_value="<html/>"):
                    repairer = Repairer()
                    code = repairer.repair(self.SOURCE_CONFIG, self._make_health_result(), attempt=1)

        assert code is not None
        assert "import" in code or "csv" in code

    def test_returns_none_when_api_raises(self):
        from scraper_watchdog.repairer import Repairer

        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-fake"}):
            with patch("anthropic.Anthropic") as MockClient:
                MockClient.return_value.messages.create.side_effect = RuntimeError("api down")
                with patch.object(Repairer, "_fetch_html", return_value="<html/>"):
                    repairer = Repairer()
                    result = repairer.repair(self.SOURCE_CONFIG, self._make_health_result(), attempt=1)

        assert result is None

    def test_missing_api_key_raises(self):
        from scraper_watchdog.repairer import Repairer

        env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
        with patch.dict(os.environ, env, clear=True):
            with pytest.raises(EnvironmentError, match="ANTHROPIC_API_KEY"):
                Repairer()


# ═══════════════════════════════════════════════════════════════════════════════
# Sandbox
# ═══════════════════════════════════════════════════════════════════════════════

class TestSandbox:
    SOURCE_CONFIG = {
        "name": "sandbox_test",
        "expected_schema": SCHEMA,
    }

    def _good_script(self) -> str:
        return textwrap.dedent("""\
            import csv, os
            path = os.environ['OUTPUT_PATH']
            with open(path, 'w', newline='') as f:
                w = csv.DictWriter(f, fieldnames=['title', 'price', 'date'])
                w.writeheader()
                for i in range(5):
                    w.writerow({'title': f't{i}', 'price': str(i), 'date': '2024-01-01'})
        """)

    def _bad_script(self) -> str:
        return textwrap.dedent("""\
            import sys
            sys.exit(1)
        """)

    def test_passing_script(self):
        from scraper_watchdog.sandbox import Sandbox
        s = Sandbox()
        result = s.test(self._good_script(), self.SOURCE_CONFIG)
        assert result.passed is True
        assert result.health is not None and result.health.success

    def test_failing_script(self):
        from scraper_watchdog.sandbox import Sandbox
        s = Sandbox()
        result = s.test(self._bad_script(), self.SOURCE_CONFIG)
        assert result.passed is False
        assert result.error is not None

    def test_timeout_is_handled(self):
        from scraper_watchdog.sandbox import Sandbox

        slow_script = "import time; time.sleep(999)"
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="py", timeout=90)):
            s = Sandbox()
            result = s.test(slow_script, self.SOURCE_CONFIG)

        assert result.passed is False
        assert "timeout" in (result.error or "").lower()

    def test_temp_files_cleaned_up(self):
        """No leftover temp files after a test run."""
        from scraper_watchdog.sandbox import Sandbox
        import glob

        before = set(glob.glob(tempfile.gettempdir() + "/watchdog_*"))
        s = Sandbox()
        s.test(self._good_script(), self.SOURCE_CONFIG)
        after = set(glob.glob(tempfile.gettempdir() + "/watchdog_*"))
        assert after - before == set(), "temp files were not cleaned up"


# ═══════════════════════════════════════════════════════════════════════════════
# Deployer
# ═══════════════════════════════════════════════════════════════════════════════

class TestDeployer:
    NEW_CODE = "# repaired\nprint('hello')\n"

    # ── direct mode ───────────────────────────────────────────────────────────

    def test_backup_created_before_overwrite(self, tmp_path):
        from scraper_watchdog.deployer import Deployer

        script = tmp_path / "scraper.py"
        script.write_text("# original\n")
        source = {"name": "t", "script_path": str(script)}

        Deployer().deploy(self.NEW_CODE, source)

        backup = Path(str(script) + ".bak")
        assert backup.exists(), ".bak file should be created"
        assert backup.read_text() == "# original\n"

    def test_original_overwritten_with_new_code(self, tmp_path):
        from scraper_watchdog.deployer import Deployer

        script = tmp_path / "scraper.py"
        script.write_text("# original\n")
        source = {"name": "t", "script_path": str(script)}

        Deployer().deploy(self.NEW_CODE, source)

        assert script.read_text() == self.NEW_CODE

    def test_deploy_when_script_does_not_exist_yet(self, tmp_path):
        """Deployer should create the script even if it didn't exist before."""
        from scraper_watchdog.deployer import Deployer

        script = tmp_path / "new_scraper.py"
        source = {"name": "t", "script_path": str(script)}

        Deployer().deploy(self.NEW_CODE, source)

        assert script.read_text() == self.NEW_CODE
        assert not Path(str(script) + ".bak").exists()

    # ── PR mode ───────────────────────────────────────────────────────────────

    def _pr_source(self, script: Path) -> dict:
        return {
            "name": "pr_test",
            "script_path": str(script),
            "repair": {
                "git_pr": {"enabled": True, "base_branch": "main"},
            },
        }

    def _make_git_side_effect(self, pr_url: str = "https://github.com/owner/repo/pull/42"):
        """
        Return a side_effect function for subprocess.run that:
        - answers git helpers (rev-parse, branch --show-current, remote get-url)
        - silently accepts checkout / add / commit / push
        """

        def _run(cmd, *args, **kwargs):
            result = MagicMock()
            result.returncode = 0
            result.stdout = ""
            result.stderr = ""

            if "rev-parse" in cmd:
                result.stdout = "/fake/repo\n"
            elif "--show-current" in cmd:
                result.stdout = "main\n"
            elif "get-url" in cmd:
                result.stdout = "https://github.com/owner/repo.git\n"

            # Raise on check=True for non-zero (we never set non-zero here)
            return result

        return _run

    def test_pr_mode_creates_branch_and_opens_pr(self, tmp_path):
        from scraper_watchdog.deployer import Deployer

        script = tmp_path / "scraper.py"
        script.write_text("# original\n")
        source = self._pr_source(script)
        pr_url = "https://github.com/owner/repo/pull/42"

        fake_pr_resp = MagicMock()
        fake_pr_resp.status_code = 201
        fake_pr_resp.json.return_value = {"html_url": pr_url}
        fake_pr_resp.raise_for_status = MagicMock()

        with patch("subprocess.run", side_effect=self._make_git_side_effect(pr_url)):
            with patch.dict(os.environ, {"GITHUB_TOKEN": "ghp_fake"}):
                with patch("httpx.Client") as MockHTTP:
                    MockHTTP.return_value.__enter__.return_value.post.return_value = fake_pr_resp
                    Deployer().deploy(self.NEW_CODE, source)

        # The repaired script should have been written to disk
        assert script.read_text() == self.NEW_CODE

    def test_pr_mode_calls_correct_git_commands(self, tmp_path):
        """Verify that checkout -b, add, commit, push are all called."""
        from scraper_watchdog.deployer import Deployer

        script = tmp_path / "scraper.py"
        script.write_text("# original\n")
        source = self._pr_source(script)
        pr_url = "https://github.com/owner/repo/pull/99"

        called_args: list[list] = []

        def _recording_run(cmd, *args, **kwargs):
            called_args.append(list(cmd))
            r = MagicMock()
            r.returncode = 0
            r.stdout = ""
            if "rev-parse" in cmd:
                r.stdout = "/fake/repo\n"
            elif "--show-current" in cmd:
                r.stdout = "main\n"
            elif "get-url" in cmd:
                r.stdout = "https://github.com/owner/repo.git\n"
            return r

        fake_pr_resp = MagicMock()
        fake_pr_resp.json.return_value = {"html_url": pr_url}
        fake_pr_resp.raise_for_status = MagicMock()

        with patch("subprocess.run", side_effect=_recording_run):
            with patch.dict(os.environ, {"GITHUB_TOKEN": "ghp_fake"}):
                with patch("httpx.Client") as MockHTTP:
                    MockHTTP.return_value.__enter__.return_value.post.return_value = fake_pr_resp
                    Deployer().deploy(self.NEW_CODE, source)

        git_subcommands = [
            args[1] for args in called_args if args and args[0] == "git"
        ]
        assert "checkout" in git_subcommands, "should create branch with git checkout -b"
        assert "add" in git_subcommands, "should stage the file"
        assert "commit" in git_subcommands, "should commit"
        assert "push" in git_subcommands, "should push the branch"

    def test_pr_mode_missing_token_raises(self, tmp_path):
        from scraper_watchdog.deployer import Deployer

        script = tmp_path / "scraper.py"
        script.write_text("# original\n")
        source = self._pr_source(script)

        env = {k: v for k, v in os.environ.items() if k != "GITHUB_TOKEN"}

        with patch("subprocess.run", side_effect=self._make_git_side_effect()):
            with patch.dict(os.environ, env, clear=True):
                with pytest.raises(EnvironmentError, match="GITHUB_TOKEN"):
                    Deployer().deploy(self.NEW_CODE, source)

    def test_parse_remote_https(self, tmp_path):
        from scraper_watchdog.deployer import Deployer

        with patch("subprocess.run") as mock_run:
            mock_run.return_value.stdout = "https://github.com/myorg/my-repo.git\n"
            owner, repo = Deployer._parse_remote(str(tmp_path))

        assert owner == "myorg"
        assert repo == "my-repo"

    def test_parse_remote_ssh(self, tmp_path):
        from scraper_watchdog.deployer import Deployer

        with patch("subprocess.run") as mock_run:
            mock_run.return_value.stdout = "git@github.com:myorg/my-repo.git\n"
            owner, repo = Deployer._parse_remote(str(tmp_path))

        assert owner == "myorg"
        assert repo == "my-repo"
