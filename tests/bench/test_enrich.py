"""Enrichment must fail loudly, because a blank issue text is invisible.

The committed sample carries the upstream issue text as ``problem_statement``;
it is what the worker is actually asked to fix. The instance pool does not have
it (it is built from patch metadata plus a tracked-file count), so a re-draw
that is not re-enriched produces a sample that parses, validates, and is
unrunnable. ``bench.runner`` does refuse such an entry, but only once a run is
already under way and containers are already being spawned.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from bench.enrich import (
    BlankProblemStatementError,
    MissingInstanceError,
    enrich_sample,
    fetch_problem_statements,
)


def _pager(rows: list[dict[str, Any]], page: int = 2) -> Any:
    """Return a paging callable over ``rows``, recording the offsets asked for."""
    calls: list[int] = []

    def fetch_page(dataset: str, split: str, offset: int, length: int) -> list[dict]:
        calls.append(offset)
        return rows[offset : offset + min(length, page)]

    fetch_page.calls = calls  # type: ignore[attr-defined]
    return fetch_page


def _rows(*pairs: tuple[str, str]) -> list[dict[str, Any]]:
    return [{"instance_id": i, "problem_statement": s} for i, s in pairs]


@pytest.mark.unit
def test_it_pages_until_every_requested_instance_is_found():
    rows = _rows(("a", "A"), ("b", "B"), ("c", "C"), ("d", "D"))
    fetch = _pager(rows)
    assert fetch_problem_statements(["a", "d"], fetch_page=fetch) == {
        "a": "A",
        "d": "D",
    }


@pytest.mark.unit
def test_it_stops_paging_as_soon_as_the_last_one_is_found():
    """A full pass over the corpus for a 36-instance sample is wasted requests."""
    rows = _rows(("a", "A"), ("b", "B"), ("c", "C"), ("d", "D"))
    fetch = _pager(rows)
    fetch_problem_statements(["a"], fetch_page=fetch)
    assert fetch.calls == [0]


@pytest.mark.unit
def test_an_instance_the_corpus_does_not_have_raises():
    fetch = _pager(_rows(("a", "A")))
    with pytest.raises(MissingInstanceError, match="zz"):
        fetch_problem_statements(["a", "zz"], fetch_page=fetch)


@pytest.mark.unit
@pytest.mark.parametrize("blank", ["", "   ", "\n\t "])
def test_a_blank_statement_raises_rather_than_being_written(blank: str):
    fetch = _pager(_rows(("a", blank)))
    with pytest.raises(BlankProblemStatementError, match="a"):
        fetch_problem_statements(["a"], fetch_page=fetch)


@pytest.mark.unit
def test_enrich_sample_fills_every_entry_and_leaves_the_rest_alone(tmp_path: Path):
    path = tmp_path / "s.json"
    path.write_text(
        json.dumps(
            {
                "corpus": "swe-bench-lite",
                "seed": 1,
                "instances": [
                    {"instance_id": "a", "base_commit": "0" * 40, "patch_loc": 3},
                    {"instance_id": "b", "base_commit": "1" * 40, "patch_loc": 9},
                ],
            }
        ),
        encoding="utf-8",
        newline="\n",
    )

    assert enrich_sample(path, {"a": "A text", "b": "B text"}) == 2

    data = json.loads(path.read_text(encoding="utf-8"))
    assert [e["problem_statement"] for e in data["instances"]] == ["A text", "B text"]
    assert data["seed"] == 1
    assert data["instances"][1]["patch_loc"] == 9


@pytest.mark.unit
def test_enrich_sample_writes_lf_only(tmp_path: Path):
    """The sample is a COMMITTED artifact in an LF repo, and this ships twice."""
    path = tmp_path / "s.json"
    path.write_text(
        json.dumps({"instances": [{"instance_id": "a"}]}),
        encoding="utf-8",
        newline="\n",
    )
    enrich_sample(path, {"a": "A text"})
    raw = path.read_bytes()
    assert b"\r\n" not in raw
    assert raw.endswith(b"\n")


@pytest.mark.unit
def test_enrich_sample_raises_when_a_statement_is_missing_for_an_entry(tmp_path: Path):
    path = tmp_path / "s.json"
    path.write_text(
        json.dumps({"instances": [{"instance_id": "a"}, {"instance_id": "b"}]}),
        encoding="utf-8",
        newline="\n",
    )
    with pytest.raises(MissingInstanceError, match="b"):
        enrich_sample(path, {"a": "A text"})


@pytest.mark.unit
def test_enrich_sample_does_not_write_a_partial_file_when_it_raises(tmp_path: Path):
    """A half-written sample is worse than an unenriched one."""
    path = tmp_path / "s.json"
    original = json.dumps({"instances": [{"instance_id": "a"}, {"instance_id": "b"}]})
    path.write_text(original, encoding="utf-8", newline="\n")
    with pytest.raises(MissingInstanceError):
        enrich_sample(path, {"a": "A text"})
    assert path.read_text(encoding="utf-8") == original
