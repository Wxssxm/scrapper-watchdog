"""Atomically replace a broken scraper script with a repaired one.

Two deploy modes:
  - Direct (default): overwrite the file in-place, keep a .bak backup.
  - PR mode (repair.git_pr.enabled: true): create a branch, commit, push,
    and open a GitHub Pull Request. Requires GITHUB_TOKEN in the environment.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from datetime import date, datetime, timezone
from typing import Optional

import httpx

from .logger import get_logger, SourceLoggerAdapter

_base_logger = get_logger()

# GitHub API base URL — override in tests via GITHUB_API_URL env var
_GH_API = os.environ.get("GITHUB_API_URL", "https://api.github.com")


class Deployer:
    """Back up the existing script and deploy the repaired version."""

    def deploy(
        self,
        script_code: str,
        source_config: dict,
        logger: SourceLoggerAdapter | None = None,
    ) -> None:
        log = logger or SourceLoggerAdapter(_base_logger, source_config.get("name", "unknown"))
        pr_config: dict = source_config.get("repair", {}).get("git_pr", {})

        if pr_config.get("enabled"):
            self._deploy_via_pr(script_code, source_config, pr_config, log)
        else:
            self._deploy_direct(script_code, source_config, log)

    # ── direct deploy ─────────────────────────────────────────────────────────

    def _deploy_direct(
        self,
        script_code: str,
        source_config: dict,
        log: SourceLoggerAdapter,
    ) -> None:
        script_path: str = source_config["script_path"]
        backup_path = script_path + ".bak"

        if os.path.exists(script_path):
            shutil.copy2(script_path, backup_path)

        os.makedirs(os.path.dirname(os.path.abspath(script_path)), exist_ok=True)
        with open(script_path, "w", encoding="utf-8") as fh:
            fh.write(script_code)

        log.log_event(
            "deploy_success",
            {
                "source": source_config.get("name"),
                "mode": "direct",
                "script_path": script_path,
                "backup_path": backup_path,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        )

    # ── PR deploy ─────────────────────────────────────────────────────────────

    def _deploy_via_pr(
        self,
        script_code: str,
        source_config: dict,
        pr_config: dict,
        log: SourceLoggerAdapter,
    ) -> None:
        name: str = source_config.get("name", "unknown")
        script_path: str = source_config["script_path"]
        base_branch: str = pr_config.get("base_branch", "main")
        today = date.today().isoformat()
        branch = f"watchdog/auto-repair-{name}-{today}"

        # Locate the git repo root
        repo_root = self._git_repo_root(script_path)

        # Remember current branch so we can restore it afterwards
        original_branch = self._current_branch(repo_root)

        try:
            # 1. Create and switch to the repair branch
            self._git(["checkout", "-b", branch], cwd=repo_root)

            # 2. Write the repaired script (+ backup on disk)
            if os.path.exists(script_path):
                shutil.copy2(script_path, script_path + ".bak")
            os.makedirs(os.path.dirname(os.path.abspath(script_path)), exist_ok=True)
            with open(script_path, "w", encoding="utf-8") as fh:
                fh.write(script_code)

            # 3. Commit
            rel_path = os.path.relpath(script_path, repo_root)
            self._git(["add", rel_path], cwd=repo_root)
            self._git(
                ["commit", "-m", f"fix(watchdog): auto-repair scraper '{name}'"],
                cwd=repo_root,
            )

            # 4. Push
            self._git(["push", "-u", "origin", branch], cwd=repo_root)

            # 5. Open PR
            pr_url = self._open_pr(
                repo_root=repo_root,
                branch=branch,
                base_branch=base_branch,
                source_name=name,
                script_path=script_path,
            )

            log.log_event(
                "deploy_success",
                {
                    "source": name,
                    "mode": "pr",
                    "branch": branch,
                    "base_branch": base_branch,
                    "pr_url": pr_url,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                },
            )

        finally:
            # Always return to the original branch
            if original_branch:
                try:
                    self._git(["checkout", original_branch], cwd=repo_root)
                except subprocess.CalledProcessError:
                    pass  # best-effort; don't mask the real exception

    # ── GitHub PR creation ────────────────────────────────────────────────────

    def _open_pr(
        self,
        repo_root: str,
        branch: str,
        base_branch: str,
        source_name: str,
        script_path: str,
    ) -> str:
        token = os.environ.get("GITHUB_TOKEN", "")
        if not token:
            raise EnvironmentError(
                "GITHUB_TOKEN is not set. "
                "Export it to allow scraper-watchdog to open pull requests:\n"
                "  export GITHUB_TOKEN=ghp_..."
            )

        owner, repo = self._parse_remote(repo_root)
        rel_path = os.path.relpath(script_path, repo_root)

        title = f"fix(watchdog): auto-repair scraper `{source_name}`"
        body = (
            f"## Automated repair by scraper-watchdog\n\n"
            f"The scraper `{source_name}` failed its health check. "
            f"Claude API generated a repaired version of `{rel_path}`.\n\n"
            f"### What changed\n"
            f"- Repaired script: `{rel_path}`\n\n"
            f"### Review checklist\n"
            f"- [ ] Output columns match the declared schema\n"
            f"- [ ] No hardcoded credentials or URLs\n"
            f"- [ ] Scraper handles edge cases (empty page, rate-limit response)\n\n"
            f"---\n"
            f"*Opened automatically by [scraper-watchdog](https://github.com/your-org/scraper-watchdog)*"
        )

        with httpx.Client(timeout=15) as client:
            resp = client.post(
                f"{_GH_API}/repos/{owner}/{repo}/pulls",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
                json={
                    "title": title,
                    "body": body,
                    "head": branch,
                    "base": base_branch,
                },
            )
            resp.raise_for_status()
            return resp.json()["html_url"]

    # ── git / repo helpers ────────────────────────────────────────────────────

    @staticmethod
    def _git(args: list[str], cwd: str) -> str:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip()

    @staticmethod
    def _git_repo_root(path: str) -> str:
        start = os.path.dirname(os.path.abspath(path))
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=start,
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip()

    @staticmethod
    def _current_branch(repo_root: str) -> Optional[str]:
        try:
            result = subprocess.run(
                ["git", "branch", "--show-current"],
                cwd=repo_root,
                capture_output=True,
                text=True,
                check=True,
            )
            return result.stdout.strip() or None
        except subprocess.CalledProcessError:
            return None

    @staticmethod
    def _parse_remote(repo_root: str) -> tuple[str, str]:
        """Return (owner, repo) from the 'origin' remote URL."""
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=True,
        )
        url = result.stdout.strip()

        # https://github.com/owner/repo.git  or  git@github.com:owner/repo.git
        match = re.search(r"[:/]([^/]+)/([^/]+?)(?:\.git)?$", url)
        if not match:
            raise ValueError(f"Cannot parse GitHub owner/repo from remote URL: {url!r}")
        return match.group(1), match.group(2)
