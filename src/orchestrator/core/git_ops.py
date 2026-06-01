"""Git and GitHub CLI subprocess operations."""

from __future__ import annotations

import asyncio
import logging
import os


logger = logging.getLogger(__name__)


class GitOps:
    """Git and GitHub CLI operations for branch and PR management."""

    def __init__(self, github_token: str) -> None:
        self._github_token = github_token

    async def _run_command(
        self,
        cmd: list[str],
        cwd: str | None = None,
    ) -> tuple[int, str, str]:
        env = os.environ.copy()
        env["GH_TOKEN"] = self._github_token
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        stdout, stderr = await proc.communicate()
        return (
            proc.returncode or 0,
            stdout.decode().strip(),
            stderr.decode().strip(),
        )

    async def _run_checked(self, cmd: list[str], cwd: str | None = None) -> str:
        code, stdout, stderr = await self._run_command(cmd, cwd)
        if code != 0:
            message = f"Git command failed (exit {code}): {' '.join(cmd)}\n{stderr}"
            raise RuntimeError(message)
        return stdout

    async def clone_repo(self, repo_url: str, workspace: str) -> None:
        await self._run_checked(["git", "clone", repo_url, workspace])
        logger.info("Cloned %s to %s", repo_url, workspace)

    async def create_branch(
        self,
        workspace: str,
        branch: str,
        base: str = "main",
    ) -> None:
        await self._run_checked(["git", "checkout", base], cwd=workspace)
        await self._run_checked(["git", "pull", "origin", base], cwd=workspace)
        await self._run_checked(["git", "checkout", "-b", branch], cwd=workspace)
        logger.info("Created branch %s from %s", branch, base)

    async def push_branch(self, workspace: str, branch: str) -> None:
        await self._run_checked(["git", "push", "-u", "origin", branch], cwd=workspace)
        logger.info("Pushed branch %s", branch)

    async def create_pr(
        self,
        workspace: str,
        title: str,
        body: str,
        base: str,
        head: str,
    ) -> str:
        stdout = await self._run_checked(
            [
                "gh",
                "pr",
                "create",
                "--title",
                title,
                "--body",
                body,
                "--base",
                base,
                "--head",
                head,
            ],
            cwd=workspace,
        )
        logger.info("Created PR: %s", stdout)
        return stdout.strip()

    async def merge_pr(self, workspace: str, pr_number: int) -> None:
        await self._run_checked(
            [
                "gh",
                "pr",
                "merge",
                str(pr_number),
                "--squash",
                "--delete-branch",
            ],
            cwd=workspace,
        )
        logger.info("Merged PR #%d", pr_number)

    async def comment_on_pr(self, workspace: str, pr_number: int, comment: str) -> None:
        await self._run_checked(
            ["gh", "pr", "comment", str(pr_number), "--body", comment],
            cwd=workspace,
        )
        logger.info("Commented on PR #%d", pr_number)

    async def get_pr_diff(self, workspace: str, pr_number: int) -> str:
        return await self._run_checked(
            ["gh", "pr", "diff", str(pr_number)],
            cwd=workspace,
        )

    async def get_changed_files(
        self,
        workspace: str,
        base: str,
        head: str,
    ) -> list[str]:
        stdout = await self._run_checked(
            ["git", "diff", "--name-only", f"{base}...{head}"],
            cwd=workspace,
        )
        return [line for line in stdout.splitlines() if line]

    async def extract_pr_number(self, pr_url: str) -> int:
        return int(pr_url.rstrip("/").split("/")[-1])
