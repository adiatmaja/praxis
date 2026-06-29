"""Unit tests for the MCP orchestration-guide resource."""

from __future__ import annotations

import pytest

from mcp_server import server


@pytest.mark.unit
def test_load_orchestration_guide_returns_nonempty_markdown() -> None:
    text = server.load_orchestration_guide()
    assert isinstance(text, str)
    assert text.strip()
    assert text.lstrip().startswith("#")
