"""Guard: docs/configurations.md must match the code it documents.

The harness table and the worker preset table describe registries that live
elsewhere (``core/harnesses.REGISTRY`` and the ``worker_presets`` block in the
settings YAML).  Either can grow without anyone reopening the doc, and a doc
that understates the harness count is worse than no doc, because the spec's
honest-ceiling language depends on that count being true.

Both regions are delimited by HTML comments so this test reads exactly the
table it means to check.  Grepping the whole file would match prose mentions
of ``opencode`` and pass on a doc that never listed it.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from orchestrator.core.harnesses import REGISTRY
from orchestrator.core.settings_file import config_file_path, load_yaml_settings


DOC = Path(__file__).resolve().parents[1] / "docs" / "configurations.md"


def _delimited_ids(text: str, marker: str) -> set[str]:
    """Return the backticked identifiers inside a BEGIN/END comment region.

    Args:
        text: The full document text.
        marker: The marker name, e.g. ``harness-list``.

    Returns:
        Every backtick-quoted token inside the region.

    Raises:
        AssertionError: If the region is missing or unterminated, which is
            itself a doc defect worth failing on.
    """
    pattern = rf"<!-- BEGIN {marker} -->(.*?)<!-- END {marker} -->"
    match = re.search(pattern, text, re.DOTALL)
    assert match is not None, f"docs/configurations.md is missing the {marker} region"
    return set(re.findall(r"`([^`]+)`", match.group(1)))


@pytest.fixture
def doc_text() -> str:
    assert DOC.exists(), "docs/configurations.md does not exist"
    return DOC.read_text(encoding="utf-8")


def test_harness_list_matches_registry(doc_text: str) -> None:
    assert _delimited_ids(doc_text, "harness-list") == set(REGISTRY)


def test_worker_preset_list_matches_settings_yaml(doc_text: str) -> None:
    presets = load_yaml_settings(config_file_path()).get("worker_presets") or []
    names = {str(entry["name"]) for entry in presets}
    assert names, "settings YAML declares no worker_presets to document"
    assert _delimited_ids(doc_text, "worker-presets") == names
