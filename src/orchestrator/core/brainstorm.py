"""Headless multi-turn brainstorming session over claude -p."""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any


logger = logging.getLogger(__name__)

PLAN_BOOTSTRAP = (
    "Use the superpowers:writing-plans skill. Read the spec at {spec_path}. "
    "Produce a fully self-contained implementation plan — every task executes in a fresh "
    "container with zero prior context, so embed all needed file paths, background, and "
    "acceptance criteria per task. Honor these extra notes: {notes}. "
    "Write the plan to docs/superpowers/plans/ and commit it."
)

BRAINSTORM_BOOTSTRAP = (
    "Use the superpowers:brainstorming skill to design a spec interactively. "
    "Ask the user one question at a time. When the design is approved, write the spec to "
    "docs/superpowers/specs/<date>-<slug>-design.md, commit it, and STOP — do NOT proceed to "
    "writing-plans. The user's opening request follows:\n\n{request}"
)


def parse_stream_line(line: str) -> dict | None:
    """Map one claude -p stream-json line to a chat event, or None to ignore.

    NOTE: verify field names against the installed claude version's stream-json schema
    during implementation; adjust the extraction below if the shape differs.
    """
    try:
        obj = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return None
    kind = obj.get("type")
    if kind == "assistant":
        parts = obj.get("message", {}).get("content", [])
        text = "".join(p.get("text", "") for p in parts if p.get("type") == "text")
        return {"kind": "text", "text": text} if text else None
    if kind == "result":
        return {"kind": "result", "session_id": obj.get("session_id")}
    return None


class BrainstormSession:
    """One interactive brainstorming conversation."""

    def __init__(self, session_id: str, workspace: str, event_bus: Any = None) -> None:
        self.session_id = session_id
        self.workspace = workspace
        self._bus = event_bus

    def _build_args(self, message: str, *, resume: bool) -> list[str]:
        args = [
            "claude",
            "-p",
            message,
            "--output-format",
            "stream-json",
            "--dangerously-skip-permissions",
        ]
        if resume:
            args += ["--resume", self.session_id]
        return args

    async def _stream_lines(self, args: list[str]) -> AsyncIterator[str]:
        proc = await asyncio.create_subprocess_exec(
            *args,
            cwd=self.workspace,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        assert proc.stdout is not None
        async for raw in proc.stdout:
            yield raw.decode().strip()
        await proc.wait()

    async def run_turn(self, message: str, *, resume: bool) -> None:
        args = self._build_args(message, resume=resume)
        async for line in self._stream_lines(args):
            event = parse_stream_line(line)
            if event is None:
                continue
            if event["kind"] == "text" and self._bus:
                self._bus.publish(
                    {
                        "type": "brainstorm_message",
                        "session_id": self.session_id,
                        "text": event["text"],
                    }
                )
            elif event["kind"] == "result":
                if event.get("session_id"):
                    self.session_id = event["session_id"]
                if self._bus:
                    self._bus.publish(
                        {
                            "type": "brainstorm_turn_done",
                            "session_id": self.session_id,
                        }
                    )


class BrainstormManager:
    """Owns active brainstorming sessions and their cloned workspaces."""

    def __init__(
        self, workspace_base: str, event_bus: object, github_token: str
    ) -> None:
        self._base = workspace_base
        self._bus = event_bus
        self._token = github_token
        self._sessions: dict[str, BrainstormSession] = {}
        self._started: dict[str, bool] = {}

    def _clone_repo(self, repo_url: str, dest: str) -> None:
        authed = repo_url.replace("https://", f"https://x-access-token:{self._token}@")
        subprocess.run(  # noqa: S603 S607
            ["git", "clone", "--depth", "50", authed, dest], check=True
        )

    def create_session(self, repo_url: str) -> str:
        session_id = uuid.uuid4().hex
        workspace = str(Path(self._base) / session_id)
        Path(workspace).mkdir(parents=True, exist_ok=True)
        self._clone_repo(repo_url, workspace)
        self._sessions[session_id] = BrainstormSession(session_id, workspace, self._bus)
        self._started[session_id] = False
        return session_id

    async def send(self, session_id: str, message: str) -> None:
        session = self._sessions[session_id]
        first = not self._started[session_id]
        payload = BRAINSTORM_BOOTSTRAP.format(request=message) if first else message
        self._started[session_id] = True
        await session.run_turn(payload, resume=not first)

    async def generate_plan(self, repo_url: str, spec_path: str, notes: str) -> dict:
        session_id = uuid.uuid4().hex
        workspace = str(Path(self._base) / session_id)
        Path(workspace).mkdir(parents=True, exist_ok=True)
        self._clone_repo(repo_url, workspace)
        session = BrainstormSession(session_id, workspace, self._bus)
        prompt = PLAN_BOOTSTRAP.format(spec_path=spec_path, notes=notes or "none")
        await session.run_turn(prompt, resume=False)
        return {"status": "generated"}

    def write_and_commit(self, repo_url: str, path: str, content: str) -> dict:
        session_id = uuid.uuid4().hex
        workspace = str(Path(self._base) / session_id)
        Path(workspace).mkdir(parents=True, exist_ok=True)
        self._clone_repo(repo_url, workspace)
        ws_root = Path(workspace).resolve()
        target = (ws_root / path).resolve()
        if not target.is_relative_to(ws_root):
            msg = f"spec_path escapes workspace: {path}"
            raise ValueError(msg)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        subprocess.run(  # noqa: S603 S607
            ["git", "add", path], cwd=workspace, check=True
        )
        subprocess.run(  # noqa: S603 S607
            ["git", "commit", "-m", "docs: update spec"], cwd=workspace, check=True
        )
        subprocess.run(  # noqa: S603 S607
            ["git", "push"], cwd=workspace, check=True
        )
        return {"status": "committed", "path": path}
