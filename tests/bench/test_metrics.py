"""One JSONL row per task-attempt. The schema is the experiment's record."""

import json

import pytest

from bench.metrics import AttemptRecord, append_record, read_records


def _record(**overrides: object) -> AttemptRecord:
    base = {
        "run_id": "run-1",
        "instance_id": "astropy__astropy-12907",
        "condition": "B",
        "worker": "local-openweight",
        "seed": 1,
        "stratum_patch": "medium",
        "stratum_repo": "big",
        "resolved": True,
        "plausible": True,
        "leaf_count": 3,
        "leaf_retries": 1,
        "whole_task_retries": 0,
        "clarifications": 0,
        "human_gate_touches": 1,
        "brain_tokens": 12000,
        "worker_tokens": 48000,
        "wall_clock_s": 940.5,
        "error": None,
    }
    base.update(overrides)
    return AttemptRecord(**base)


@pytest.mark.unit
def test_a_record_round_trips_through_jsonl(tmp_path):
    path = tmp_path / "run.jsonl"
    append_record(path, _record())
    rows = read_records(path)
    assert len(rows) == 1
    assert rows[0].instance_id == "astropy__astropy-12907"
    assert rows[0].resolved is True


@pytest.mark.unit
def test_append_does_not_truncate_prior_rows(tmp_path):
    path = tmp_path / "run.jsonl"
    append_record(path, _record(condition="A"))
    append_record(path, _record(condition="B"))
    assert [r.condition for r in read_records(path)] == ["A", "B"]


@pytest.mark.unit
def test_every_row_is_a_single_line_of_json(tmp_path):
    path = tmp_path / "run.jsonl"
    append_record(path, _record())
    append_record(path, _record(condition="A"))
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    for line in lines:
        json.loads(line)


@pytest.mark.unit
def test_plausible_but_wrong_is_representable():
    """AutoCodeRover found 35 percent of plausible patches wrong; we measure it."""
    record = _record(resolved=False, plausible=True)
    assert record.plausible_but_wrong is True


@pytest.mark.unit
def test_a_resolved_patch_is_never_counted_as_plausible_but_wrong():
    assert _record(resolved=True, plausible=True).plausible_but_wrong is False


@pytest.mark.unit
def test_an_unapplied_patch_is_not_plausible_but_wrong():
    assert _record(resolved=False, plausible=False).plausible_but_wrong is False


@pytest.mark.unit
def test_leaf_scoped_and_whole_task_retries_are_separate_fields():
    """The distinction is the point: static decomposition raises whole-task retries."""
    record = _record(leaf_retries=3, whole_task_retries=1)
    assert record.leaf_retries == 3
    assert record.whole_task_retries == 1
    assert record.total_retries == 4


@pytest.mark.unit
def test_an_errored_attempt_is_recorded_not_dropped(tmp_path):
    """Silently dropping a crashed attempt inflates the resolve rate."""
    path = tmp_path / "run.jsonl"
    append_record(path, _record(resolved=False, error="worker endpoint unreachable"))
    assert read_records(path)[0].error == "worker endpoint unreachable"


@pytest.mark.unit
def test_reading_a_missing_file_returns_no_rows(tmp_path):
    assert read_records(tmp_path / "absent.jsonl") == []


@pytest.mark.unit
def test_the_jsonl_file_uses_lf_line_endings_only(tmp_path):
    """Windows text-mode writes translate \\n to os.linesep (CRLF); these rows
    become the published artifact docs/bench/raw/*.jsonl in an LF repo, so a
    CRLF row would be committed verbatim. splitlines() cannot see this class
    of bug because it normalizes CRLF away before the assertion runs.
    """
    path = tmp_path / "run.jsonl"
    append_record(path, _record())
    append_record(path, _record(error="line1\nline2"))
    raw = path.read_bytes()
    assert b"\r\n" not in raw
    assert raw.endswith(b"\n")
    assert raw.count(b"\n") == 2


@pytest.mark.unit
def test_an_embedded_newline_in_error_stays_one_physical_line_and_round_trips(
    tmp_path,
):
    """error is exactly the field most likely to carry a multi-line value in
    practice (a traceback, a worker's stderr). If json.dumps escaping ever
    broke, one crashed attempt would corrupt every downstream row boundary
    and the failure would look like a parse error a hundred rows later.
    """
    path = tmp_path / "run.jsonl"
    tricky_error = 'line1\nline2 with "quotes" and non-ascii: café ☃'
    append_record(path, _record(resolved=False, error=tricky_error))
    raw = path.read_bytes()
    assert raw.count(b"\n") == 1
    rows = read_records(path)
    assert len(rows) == 1
    assert rows[0].error == tricky_error
