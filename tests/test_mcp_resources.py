"""Unit tests for the MCP orchestration-guide resource."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from mcp_server import server


@pytest.mark.unit
def test_load_orchestration_guide_returns_nonempty_markdown() -> None:
    text = server.load_orchestration_guide()
    assert isinstance(text, str)
    assert text.strip()
    assert text.lstrip().startswith("#")


@pytest.mark.unit
async def test_orchestration_guide_resource_registered() -> None:
    resources_list = await server.mcp.list_resources()
    uris = {str(r.uri) for r in resources_list}
    assert "praxis://guide/orchestration" in uris


@pytest.mark.unit
async def test_orchestration_guide_resource_reads_content() -> None:
    contents = await server.mcp.read_resource("praxis://guide/orchestration")
    # FastMCP returns an iterable of content parts; join their text.
    text = "".join(part.content for part in contents)
    assert text.strip()
    assert text.lstrip().startswith("#")


@pytest.mark.unit
def test_guide_loads_regardless_of_cwd(tmp_path: Path) -> None:
    """The loader resolves the file via the package, not the working dir."""
    original = Path.cwd()
    os.chdir(tmp_path)
    try:
        text = server.load_orchestration_guide()
        assert text.strip()
    finally:
        os.chdir(original)
