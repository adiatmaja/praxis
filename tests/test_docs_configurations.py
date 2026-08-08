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


#: English number words, indexed by count.  Only used to check the Ceilings
#: prose against ``len(REGISTRY)``; the doc says "is two" in words, not digits.
_NUMBER_WORDS = {1: "one", 2: "two", 3: "three", 4: "four", 5: "five"}


def _region(text: str, marker: str) -> str:
    """Return the body of a BEGIN/END comment region.

    Args:
        text: The full document text.
        marker: The marker name, e.g. ``harness-list``.

    Returns:
        The text between the markers.

    Raises:
        AssertionError: If the region is missing, unterminated, or duplicated.
            A duplicated marker is the nastiest case: ``re.search`` takes the
            first match, so a stale leading region would silently shadow a
            corrected one below it.
    """
    opens = text.count(f"<!-- BEGIN {marker} -->")
    closes = text.count(f"<!-- END {marker} -->")
    assert opens == 1, f"expected exactly one BEGIN {marker}, found {opens}"
    assert closes == 1, f"expected exactly one END {marker}, found {closes}"
    pattern = rf"<!-- BEGIN {marker} -->(.*?)<!-- END {marker} -->"
    match = re.search(pattern, text, re.DOTALL)
    assert match is not None, f"docs/configurations.md is missing the {marker} region"
    return match.group(1)


def _delimited_ids(text: str, marker: str) -> set[str]:
    """Return the backticked identifiers inside a BEGIN/END comment region.

    Only backticked tokens that appear in a TABLE ROW count.  Scoping to the
    region alone is not enough: a region holding nothing but prose that happens
    to mention every id would otherwise pass, which defeats the point.

    Args:
        text: The full document text.
        marker: The marker name, e.g. ``harness-list``.

    Returns:
        Every backtick-quoted token inside a table row in the region.
    """
    rows = [line for line in _region(text, marker).splitlines() if line.startswith("|")]
    assert rows, f"the {marker} region contains no table rows"
    return set(re.findall(r"`([^`]+)`", "\n".join(rows)))


@pytest.fixture
def doc_text() -> str:
    assert DOC.exists(), "docs/configurations.md does not exist"
    return DOC.read_text(encoding="utf-8")


def test_harness_list_matches_registry(doc_text: str) -> None:
    assert _delimited_ids(doc_text, "harness-list") == set(REGISTRY)


def test_harness_rows_carry_the_registry_display_names(doc_text: str) -> None:
    """The id alone is not the row: a row may name the wrong harness."""
    region = _region(doc_text, "harness-list")
    for harness_id, spec in REGISTRY.items():
        assert spec.display_name in region, (
            f"the {harness_id} row does not carry its registry display name "
            f"{spec.display_name!r}"
        )


def test_ceiling_states_the_real_harness_count(doc_text: str) -> None:
    """The honesty claim must track the registry, which is the whole point.

    Without this, adding a third harness plus its table row leaves the suite
    green while the Ceilings section still says "is two".  That is exactly the
    drift this module's docstring promises to catch, and the delimited-region
    checks alone do not catch it.
    """
    word = _NUMBER_WORDS[len(REGISTRY)]
    assert f'"Many harnesses" is {word}.' in doc_text, (
        f"the Ceilings section must say the harness count is {word!r} to match REGISTRY"
    )


def test_worker_preset_list_matches_settings_yaml(doc_text: str) -> None:
    presets = load_yaml_settings(config_file_path()).get("worker_presets") or []
    names = {str(entry["name"]) for entry in presets}
    assert names, "settings YAML declares no worker_presets to document"
    assert _delimited_ids(doc_text, "worker-presets") == names


def test_preset_rows_declare_whether_they_need_a_credential(doc_text: str) -> None:
    """``requires`` decides the ``praxis init`` default, so the doc must track it.

    A preset flipped from ``requires: [api_key]`` to ``[]`` changes which preset
    ``init`` offers first.  Pinning only the name would leave that invisible.
    """
    region = _region(doc_text, "worker-presets")
    rows = {
        line.split("|")[1].strip().strip("`"): line
        for line in region.splitlines()
        if line.startswith("|") and "`" in line
    }
    presets = load_yaml_settings(config_file_path()).get("worker_presets") or []
    for entry in presets:
        row = rows[str(entry["name"])]
        needs_nothing = "nothing" in row.casefold()
        assert needs_nothing == (not entry["requires"]), (
            f"the {entry['name']} row disagrees with its requires list "
            f"{entry['requires']!r}"
        )
