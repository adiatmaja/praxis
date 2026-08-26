"""Headless multi-turn brainstorming session over claude -p."""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from orchestrator.core.git_ops import clone_with_token, commit_and_push
from orchestrator.core.github_credentials import (
    GitHubCredentialProvider,
    PatCredentialProvider,
)
from orchestrator.core.markdown_utils import (
    checklist_progress,
    extract_frontmatter_field,
    extract_title,
)


logger = logging.getLogger(__name__)

PLAN_BOOTSTRAP = (
    "Use the superpowers:writing-plans skill. Read the spec at {spec_path}. "
    "Produce a fully self-contained implementation plan — every task executes in a fresh "
    "container with zero prior context, so embed all needed file paths, background, and "
    "acceptance criteria per task. Honor these extra notes: {notes}. "
    "At the very top of the plan file, add YAML front-matter linking back to the spec, "
    "exactly: ---\\nspec_path: {spec_path}\\ntype: plan\\n--- . "
    "CRITICAL markdown rule: when a task embeds file content that itself contains a "
    "fenced code block (e.g. ```bash, ```python, ```yaml), wrap that file content in a "
    "FOUR-backtick (````) outer fence so the inner three-backtick fence is preserved. "
    "Never wrap content that contains ``` in a three-backtick fence — it produces "
    "malformed markdown that renders incorrectly even on GitHub. "
    "Write the plan to docs/superpowers/plans/, commit it, and push it."
)

# Ceiling on a repository clone made to read something out of it. Mirrors
# ``orchestrator._CLONE_TIMEOUT_SECONDS``, and deliberately carries the same
# number: it bounds the same operation (``clone_with_token`` against the same
# repository) and a second, different answer to "how long may a clone take"
# would be a knob nobody knew they had.
#
# Honest limitation, same as there: cancelling the wait does not kill the git
# process. It keeps running in its worker thread and may still be writing into
# the workspace the caller then deletes. Unblocking the caller is worth a leaked
# process; the alternative is a request (and an orchestration pass) that never
# returns at all.
_CLONE_TIMEOUT_SECONDS = 300.0

BRAINSTORM_BOOTSTRAP = (
    "Use the superpowers:brainstorming skill to design a spec interactively. "
    "Ask the user one question at a time. When the design is approved, write the spec to "
    "docs/superpowers/specs/<date>-<slug>-design.md, commit it, push it, and STOP — do NOT proceed to "
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
            # claude >= 2.x requires --verbose when combining --print (-p) with
            # --output-format=stream-json; without it the process exits 1.
            "--verbose",
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
        assert proc.stdout is not None  # nosec B101 — stdout is set via PIPE above
        async for raw in proc.stdout:
            yield raw.decode().strip()
        await proc.wait()
        if proc.returncode != 0:
            stderr_bytes = await proc.stderr.read() if proc.stderr else b""
            logger.warning(
                "claude process exited with code %d: %s",
                proc.returncode,
                stderr_bytes.decode(errors="replace").strip(),
            )

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
        self,
        workspace_base: str,
        event_bus: object,
        credentials: GitHubCredentialProvider | str,
    ) -> None:
        self._base = workspace_base
        self._bus = event_bus
        # Accept a bare str (legacy PAT) or a provider object.
        if isinstance(credentials, str):
            self._provider: GitHubCredentialProvider = PatCredentialProvider(
                credentials
            )
        else:
            self._provider = credentials
        self._sessions: dict[str, BrainstormSession] = {}
        self._started: dict[str, bool] = {}

    async def _clone_repo(self, repo_url: str, dest: str) -> None:
        """Check ``repo_url`` out into ``dest``, off the event loop and bounded.

        ``clone_with_token`` is a synchronous ``subprocess.run`` with no
        deadline of its own, and calling it bare from a coroutine blocks the
        single event loop this process has: not just the caller but FastAPI,
        SSE and every agent callback, for as long as the fetch takes. The same
        hazard was already recognised and fixed one seat over, in
        ``Orchestrator._clone_for_planning``; this is the same shape.

        The deadline is the half that is easy to leave out, and its absence has
        a second-order cost that is worse than the wait. A clone that HANGS
        never raises, so the fail-closed ``except Exception`` around
        ``survey_repo`` in the improvement loop can never fire: the loop does
        not fall back to a bad proposal, it stops answering entirely. A timeout
        converts a hang into an exception a caller can act on.

        Args:
            repo_url: The repository to clone.
            dest: The directory to clone into.

        Raises:
            TimeoutError: The clone outran ``_CLONE_TIMEOUT_SECONDS``.
            subprocess.CalledProcessError: git refused the clone.
        """
        token = await self._provider.token_for_repo(repo_url)
        await asyncio.wait_for(
            asyncio.to_thread(clone_with_token, repo_url, dest, token, depth=50),
            timeout=_CLONE_TIMEOUT_SECONDS,
        )

    async def create_session(self, repo_url: str) -> str:
        session_id = uuid.uuid4().hex
        workspace = str(Path(self._base) / session_id)
        Path(workspace).mkdir(parents=True, exist_ok=True)
        await self._clone_repo(repo_url, workspace)
        self._sessions[session_id] = BrainstormSession(session_id, workspace, self._bus)
        self._started[session_id] = False
        return session_id

    def has_session(self, session_id: str) -> bool:
        """True when this process still holds the session.

        Sessions are in-memory, so the honest answer after a restart is "no".
        Exposed so the route can say that instead of accepting a turn that
        cannot happen.
        """
        return session_id in self._sessions

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
        await self._clone_repo(repo_url, workspace)
        session = BrainstormSession(session_id, workspace, self._bus)
        prompt = PLAN_BOOTSTRAP.format(spec_path=spec_path, notes=notes or "none")
        try:
            await session.run_turn(prompt, resume=False)
        finally:
            shutil.rmtree(workspace, ignore_errors=True)
        return {"status": "generated"}

    async def read_doc(self, repo_url: str, path: str) -> str:
        """Clone the repo and return one doc's raw markdown."""
        session_id = uuid.uuid4().hex
        workspace = str(Path(self._base) / session_id)
        Path(workspace).mkdir(parents=True, exist_ok=True)
        await self._clone_repo(repo_url, workspace)
        try:
            ws_root = Path(workspace).resolve()
            target = (ws_root / path).resolve()
            if not target.is_relative_to(ws_root) or not target.is_file():
                msg = f"doc not found: {path}"
                raise FileNotFoundError(msg)
            return target.read_text(encoding="utf-8")
        finally:
            shutil.rmtree(workspace, ignore_errors=True)

    async def survey_repo(self, repo_url: str) -> str:
        """Clone the repo and return a bounded factual survey of its contents.

        Exists for the autonomous improvement loop, which used to ask "what
        should we build next?" while supplying only a project name, a repo URL
        and a plan path. See :mod:`orchestrator.core.repo_survey` for the
        measured failure that produced.

        Same clone-read-delete shape as the other readers on this class, so the
        workspace never outlives the call, and the clone goes through
        ``_clone_repo``, which runs it off the event loop under a deadline. The
        caller is the orchestration loop itself, so a blocking clone here does
        not merely slow this call down: it stops the whole process answering,
        and a HANGING one makes the caller's fail-closed guard unreachable.

        Returns:
            The survey text. Never "": an empty checkout yields
            ``repo_survey.EMPTY_REPO_SURVEY``, which the caller must recognise
            with ``describes_empty_repo`` rather than read as evidence.

        Raises:
            TimeoutError: The clone outran the deadline.
        """
        from orchestrator.core.repo_survey import build_repo_survey

        session_id = uuid.uuid4().hex
        workspace = str(Path(self._base) / session_id)
        Path(workspace).mkdir(parents=True, exist_ok=True)
        await self._clone_repo(repo_url, workspace)
        try:
            return build_repo_survey(Path(workspace))
        finally:
            shutil.rmtree(workspace, ignore_errors=True)

    async def list_lifecycle_docs(self, repo_url: str) -> list[dict]:
        """Clone the repo and list spec/plan markdown with metadata."""
        session_id = uuid.uuid4().hex
        workspace = str(Path(self._base) / session_id)
        Path(workspace).mkdir(parents=True, exist_ok=True)
        await self._clone_repo(repo_url, workspace)
        try:
            root = Path(workspace)
            out: list[dict] = []
            for category in ("spec", "plan"):
                folder = f"{category}s"
                for file in sorted(root.rglob(f"*/{folder}/*.md")):
                    rel = str(file.relative_to(root)).replace("\\", "/")
                    text = file.read_text(encoding="utf-8")
                    done, total = checklist_progress(text)
                    out.append(
                        {
                            "path": rel,
                            "category": category,
                            "title": extract_title(text) or rel,
                            "done_count": done,
                            "total_count": total,
                            "spec_path": extract_frontmatter_field(text, "spec_path"),
                        }
                    )
            return out
        finally:
            shutil.rmtree(workspace, ignore_errors=True)

    async def write_and_commit(self, repo_url: str, path: str, content: str) -> dict:
        session_id = uuid.uuid4().hex
        workspace = str(Path(self._base) / session_id)
        Path(workspace).mkdir(parents=True, exist_ok=True)
        await self._clone_repo(repo_url, workspace)
        ws_root = Path(workspace).resolve()
        target = (ws_root / path).resolve()
        if not target.is_relative_to(ws_root):
            msg = f"spec_path escapes workspace: {path}"
            raise ValueError(msg)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        try:
            token = await self._provider.token_for_repo(repo_url)
            committed = commit_and_push(
                workspace, token, "docs: update spec", paths=[path]
            )
        finally:
            shutil.rmtree(workspace, ignore_errors=True)
        # Saving a spec the operator did not actually edit is a no-op, not a
        # failure and not a commit. Reporting "committed" for it would put a
        # commit in the UI that is not in the repo.
        return {"status": "committed" if committed else "unchanged", "path": path}
