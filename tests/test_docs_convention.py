"""Static documentation convention tests."""

# ruff: noqa: S101

from __future__ import annotations

from pathlib import Path


def test_claude_md_has_auto_delegate_convention() -> None:
    text = Path("CLAUDE.md").read_text(encoding="utf-8")
    assert "auto-delegate" in text.lower()
    assert "/api/settings/auto-delegate" in text
