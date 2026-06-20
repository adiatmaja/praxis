"""Auto-draft CLAUDE.md / MEMORY.md updates after plan completion."""

from __future__ import annotations

import asyncio
import logging
import shutil
import subprocess  # noqa: S404
import uuid
from pathlib import Path

from orchestrator.core.git_ops import clone_with_token, commit_and_push


logger = logging.getLogger(__name__)

REVISE_PROMPT = (
    "Use the claude-md-management:revise-claude-md skill. Update CLAUDE.md and {memory_path} "
    "to reflect the work just completed. Do NOT commit — only edit the files in place. "
    "Summary of completed work:\n\n{summary}"
)


class ContextSync:
    """Drafts and (on approval) commits CLAUDE.md / MEMORY.md updates."""

    def __init__(
        self, workspace_base: str, github_token: str, memory_md_path: str
    ) -> None:
        self._base = workspace_base
        self._token = github_token
        self._memory_path = memory_md_path
        self._drafts: dict[str, dict] = {}

    def _clone_repo(self, repo_url: str, dest: str) -> None:
        clone_with_token(repo_url, dest, self._token, depth=20)

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
        self._clone_repo(repo_url, workspace)
        await self._run_revise(workspace, summary)
        diff = self._git_diff(workspace)
        self._drafts[draft_id] = {
            "workspace": workspace,
            "repo_url": repo_url,
            "diff": diff,
        }
        return {"draft_id": draft_id, "diff": diff}

    def approve(self, draft_id: str) -> dict:
        draft = self._drafts.pop(draft_id)
        ws = draft["workspace"]
        commit_and_push(ws, self._token, "docs: sync CLAUDE.md and MEMORY.md")
        shutil.rmtree(ws, ignore_errors=True)
        return {"status": "committed", "draft_id": draft_id}

    def current(self, repo_url: str) -> dict:
        draft_id = uuid.uuid4().hex
        ws = str(Path(self._base) / f"read-{draft_id}")
        Path(ws).mkdir(parents=True, exist_ok=True)
        self._clone_repo(repo_url, ws)

        def _read(rel: str) -> str:
            p = Path(ws) / rel
            return p.read_text(encoding="utf-8") if p.is_file() else ""

        result = {
            "claude_md": _read("CLAUDE.md"),
            "memory_md": _read(self._memory_path),
        }
        shutil.rmtree(ws, ignore_errors=True)
        return result
