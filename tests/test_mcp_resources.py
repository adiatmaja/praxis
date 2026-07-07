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


@pytest.mark.unit
async def test_guide_names_every_registered_tool() -> None:
    """Every live MCP tool must be documented, so the guide cannot drift."""
    text = server.load_orchestration_guide()
    tools = await server.mcp.list_tools()
    for tool in tools:
        assert tool.name in text, f"guide omits tool {tool.name}"


@pytest.mark.unit
def test_guide_mentions_execute_plan_even_before_implemented() -> None:
    """execute_plan is documented as part of the decision tree (Spec 2)."""
    text = server.load_orchestration_guide()
    assert "execute_plan" in text


@pytest.mark.unit
def test_guide_documents_awaiting_merge() -> None:
    """The guide must document the human-approval parking state, not imply auto-merge."""
    guide = server.load_orchestration_guide()
    assert "awaiting_merge" in guide
    assert "approve" in guide.lower()


@pytest.mark.unit
def test_guide_documents_resolve_model_flow() -> None:
    """The guide must teach reading the configured model before dispatching."""
    guide = server.load_orchestration_guide()
    assert "Resolve the worker model" in guide
    assert "get_project" in guide
    assert "list_projects" in guide
    # The fallback path when no project is configured yet.
    assert "list_providers" in guide
