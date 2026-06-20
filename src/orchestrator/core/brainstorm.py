"""Headless multi-turn brainstorming session over claude -p."""

from __future__ import annotations

import json
import logging


logger = logging.getLogger(__name__)

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

    def __init__(self, session_id: str, workspace: str) -> None:
        self.session_id = session_id
        self.workspace = workspace

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
