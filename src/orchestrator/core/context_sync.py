"""Auto-draft CLAUDE.md / MEMORY.md updates after plan completion."""

from __future__ import annotations

import asyncio
import logging
import shutil
import subprocess  # noqa: S404
import uuid
from pathlib import Path

from orchestrator.core.git_ops import clone_with_token, commit_and_push
from orchestrator.core.github_credentials import (
    GitHubCredentialProvider,
    PatCredentialProvider,
)


logger = logging.getLogger(__name__)

REVISE_PROMPT = (
    "Use the claude-md-management:revise-claude-md skill. Update CLAUDE.md and {memory_path} "
    "to reflect the work just completed. Do NOT commit — only edit the files in place. "
    "Summary of completed work:\n\n{summary}"
)


class ContextSync:
    """Drafts and (on approval) commits CLAUDE.md / MEMORY.md updates."""

    def __init__(
        self,
        workspace_base: str,
        credentials: GitHubCredentialProvider | str,
        memory_md_path: str,
    ) -> None:
        self._base = workspace_base
        if isinstance(credentials, str):
            self._provider: GitHubCredentialProvider = PatCredentialProvider(
                credentials
            )
        else:
            self._provider = credentials
        self._memory_path = memory_md_path
        self._drafts: dict[str, dict] = {}

    async def _clone_repo(self, repo_url: str, dest: str) -> None:
        token = await self._provider.token_for_repo(repo_url)
        clone_with_token(repo_url, dest, token, depth=20)

    async def _run_revise(self, workspace: str, summary: str) -> None:
        prompt = REVISE_PROMPT.format(memory_path=self._memory_path, summary=summary)
        proc = await asyncio.create_subprocess_exec(
            "claude",
            "-p",
            prompt,
            "--dangerously-skip-permissions",
            cwd=workspace,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc.communicate()

    def _git_diff(self, workspace: str) -> str:
        result = subprocess.run(  # noqa: S603, S607
            ["git", "-C", workspace, "diff"],
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout

    async def draft(self, repo_url: str, summary: str) -> dict:
        draft_id = uuid.uuid4().hex
        workspace = str(Path(self._base) / f"ctx-{draft_id}")
        Path(workspace).mkdir(parents=True, exist_ok=True)
        await self._clone_repo(repo_url, workspace)
        await self._run_revise(workspace, summary)
        diff = self._git_diff(workspace)
        self._drafts[draft_id] = {
            "workspace": workspace,
            "repo_url": repo_url,
            "diff": diff,
        }
        return {"draft_id": draft_id, "diff": diff}

    async def approve(self, draft_id: str) -> dict:
        draft = self._drafts.pop(draft_id)
        ws = draft["workspace"]
        repo_url = draft["repo_url"]
        token = await self._provider.token_for_repo(repo_url)
        commit_and_push(ws, token, "docs: sync CLAUDE.md and MEMORY.md")
        shutil.rmtree(ws, ignore_errors=True)
        return {"status": "committed", "draft_id": draft_id}

    async def current(self, repo_url: str) -> dict:
        """Return current CLAUDE.md and MEMORY.md content from the repo.

        Clones the repo into a temporary directory, reads the files, then
        removes the directory unconditionally (even on failure).

        Args:
            repo_url: HTTPS URL of the project repository.

        Returns:
            Dict with ``claude_md`` and ``memory_md`` string values.

        Raises:
            subprocess.CalledProcessError: If the git clone fails.
        """
        draft_id = uuid.uuid4().hex
        ws = str(Path(self._base) / f"read-{draft_id}")
        Path(ws).mkdir(parents=True, exist_ok=True)
        try:
            await self._clone_repo(repo_url, ws)

            def _read(rel: str) -> str:
                p = Path(ws) / rel
                return p.read_text(encoding="utf-8") if p.is_file() else ""

            return {
                "claude_md": _read("CLAUDE.md"),
                "memory_md": _read(self._memory_path),
            }
        finally:
            shutil.rmtree(ws, ignore_errors=True)
