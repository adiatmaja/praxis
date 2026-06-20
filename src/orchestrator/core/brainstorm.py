"""Headless multi-turn brainstorming session over claude -p."""

from __future__ import annotations

import logging


logger = logging.getLogger(__name__)

BRAINSTORM_BOOTSTRAP = (
    "Use the superpowers:brainstorming skill to design a spec interactively. "
    "Ask the user one question at a time. When the design is approved, write the spec to "
    "docs/superpowers/specs/<date>-<slug>-design.md, commit it, and STOP — do NOT proceed to "
    "writing-plans. The user's opening request follows:\n\n{request}"
)


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
