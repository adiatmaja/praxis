"""A remedy that names a document is claiming the document exists.

`tests/test_doctor_hints_name_real_verbs.py` already gates the other half of
this: a hint may not name a command that cannot do the job. This is the
document half, and it found two live instances at once. `GENERIC_HINT` sent
the reader to `docs/reference.md` and the planner row to
`docs/getting-started.md`, and this repository has never contained either. The
planner row is the one an operator hits most, so the reddest light in the table
came with the deadest link.

Scoped to strings the product PRINTS. A repo-relative path in a comment is a
maintainer's note that goes stale harmlessly; a path in a hint is an
instruction.
"""
# ruff: noqa: S101

from __future__ import annotations

import re
from pathlib import Path

import pytest

from orchestrator.core.doctor import CHECKS, GENERIC_HINT


REPO = Path(__file__).resolve().parents[1]

#: Any repo-relative markdown path, with an optional `#anchor`.
_DOC_PATH = re.compile(r"docs/[A-Za-z0-9_./-]*\.md(?:#[A-Za-z0-9_-]+)?")


def _doc_paths(text: str) -> list[str]:
    """Return every repo-relative doc path a string names, anchors stripped."""
    return [match.split("#", 1)[0] for match in _DOC_PATH.findall(text)]


def _hint_sources() -> list[tuple[str, str]]:
    """Return every hint the doctor can print, as ``(where, text)``."""
    sources = [("GENERIC_HINT", GENERIC_HINT)]
    sources.extend((f"CHECKS[{check.check_id}]", check.hint) for check in CHECKS)
    return sources


@pytest.mark.unit
@pytest.mark.parametrize(("where", "text"), _hint_sources(), ids=lambda v: str(v)[:40])
def test_every_document_a_hint_names_exists(where: str, text: str) -> None:
    """Follow the remedy: the file it points at has to be there."""
    for path in _doc_paths(text):
        assert (REPO / path).is_file(), (
            f"{where} sends the operator to {path}, which does not exist. A "
            "hint is the last thing a red row says, so a dead link there ends "
            "the recovery path rather than continuing it."
        )


@pytest.mark.unit
def test_the_probe_details_name_real_documents_too() -> None:
    """The detail text is as user-visible as the hint.

    `probe_planner_cli` and its siblings compose bespoke strings that never
    pass through the registry, so iterating `CHECKS` alone would miss them.
    Read as source rather than executed, because reaching every branch of
    every probe would need more fixtures than the property is worth.
    """
    probes = (REPO / "src" / "orchestrator" / "core" / "doctor_probes.py").read_text(
        encoding="utf-8"
    )
    missing = [path for path in _doc_paths(probes) if not (REPO / path).is_file()]

    assert missing == [], (
        f"doctor_probes.py names documents that do not exist: {missing}"
    )


@pytest.mark.unit
def test_the_guard_would_notice_a_dead_link() -> None:
    """The extractor has to actually match the shape hints are written in.

    A guard that finds no paths passes over every dead link there is, which is
    the failure mode this whole file exists to catch one layer down.
    """
    found = _doc_paths("install it and see docs/deployment.md#agy-harness for setup")

    assert found == ["docs/deployment.md"]
