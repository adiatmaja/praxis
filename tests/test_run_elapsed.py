"""How long a run has been going: the derivation, and the three rules it keeps.

Each test here pins a rule that has already cost this repository a defect at
some other seat, so a mutation of the module must turn one of them red:

- a naive stamp is UTC (the dashboard's "7h ago" for a 20-minute-old plan),
- unmeasurable is ``None``, never ``0.0`` (the sidebar's four measured zeros),
- open is ``finished_at IS NULL``, never ``status`` (a harness answering with
  the word "running" produced a closed row that read as open).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from orchestrator.core import run_elapsed


NOW = datetime(2026, 8, 29, 12, 0, 0, tzinfo=UTC)


def test_a_naive_stamp_is_read_as_utc_not_as_local_time():
    """The SQLite shape, exactly as ``agent_runs.started_at`` stores it.

    Read as local time this is wrong by the reader's whole UTC offset. The
    fixture is deliberately the stored shape (a space, no ``T``, no zone)
    rather than an ISO instant, because the ISO one parses correctly under
    either reading and so could not fail.
    """
    assert run_elapsed.elapsed_seconds("2026-08-29 11:00:00", None, now=NOW) == 3600.0


def test_a_zone_bearing_stamp_is_not_shifted_a_second_time():
    """``finished_at`` is written as a full ISO instant with ``+00:00``."""
    assert (
        run_elapsed.elapsed_seconds(
            "2026-08-29T11:00:00+00:00", "2026-08-29T11:30:00+00:00"
        )
        == 1800.0
    )


def test_an_offset_stamp_keeps_its_own_offset():
    """Not every zone-bearing stamp is UTC; the offset must be honoured."""
    # 11:00 at UTC+7 is 04:00 UTC, so eight hours before NOW.
    assert (
        run_elapsed.elapsed_seconds("2026-08-29T11:00:00+07:00", None, now=NOW)
        == 8 * 3600.0
    )


@pytest.mark.parametrize("bad", [None, "", "   ", "not a timestamp", "2026-13-45"])
def test_an_unreadable_start_is_none_and_never_zero(bad):
    """Zero would render as "a run that just started" on a screen whose whole
    purpose is spotting one that did not."""
    assert run_elapsed.elapsed_seconds(bad, None, now=NOW) is None


def test_an_unreadable_finish_is_none_rather_than_measured_to_now():
    """A closed run with a corrupt end stamp must not be reported as still
    running: falling back to ``now`` would invent an open-ended duration for a
    run that is over."""
    assert (
        run_elapsed.elapsed_seconds("2026-08-29 11:00:00", "gibberish", now=NOW) is None
    )


def test_a_negative_span_is_clamped_to_zero():
    assert run_elapsed.elapsed_seconds("2026-08-29 13:00:00", None, now=NOW) == 0.0


def test_open_is_finished_at_null_not_the_status_string():
    """A harness may report the literal word "running" on a row Praxis has
    already closed. ``finished_at`` is the predicate the claim and
    ``get_running_runs`` use, and this module must agree with them or the
    wall-clock bound would expire a run the callback already settled."""
    runs = [
        {
            "id": "closed-but-says-running",
            "status": "running",
            "started_at": "2026-08-29 09:00:00",
            "finished_at": "2026-08-29T10:00:00+00:00",
        }
    ]
    assert run_elapsed.open_run(runs) is None
    assert run_elapsed.running_for_seconds(runs, now=NOW) is None


def test_open_is_finished_at_null_even_when_the_status_says_failed():
    runs = [
        {
            "id": "open-but-says-failed",
            "status": "failed",
            "started_at": "2026-08-29 11:00:00",
            "finished_at": None,
        }
    ]
    assert run_elapsed.open_run(runs)["id"] == "open-but-says-failed"
    assert run_elapsed.running_for_seconds(runs, now=NOW) == 3600.0


def test_two_open_runs_report_the_older_one():
    """Two open rows means reconciliation has not caught up. The older is the
    longer-running and the more alarming, which is the direction to err in."""
    runs = [
        {"id": "newer", "started_at": "2026-08-29 11:30:00", "finished_at": None},
        {"id": "older", "started_at": "2026-08-29 09:00:00", "finished_at": None},
    ]
    assert run_elapsed.open_run(runs)["id"] == "older"
    assert run_elapsed.running_for_seconds(runs, now=NOW) == 3 * 3600.0


def test_a_closed_run_reports_how_long_it_took_not_how_long_ago_it_ran():
    runs = [
        {
            "id": "done",
            "started_at": "2026-08-29 09:00:00",
            "finished_at": "2026-08-29T09:45:00+00:00",
        }
    ]
    annotated = run_elapsed.annotate_runs(runs, now=NOW)
    assert annotated[0]["elapsed_seconds"] == 45 * 60.0


def test_annotate_runs_does_not_mutate_its_input():
    runs = [{"id": "a", "started_at": "2026-08-29 11:00:00", "finished_at": None}]
    run_elapsed.annotate_runs(runs, now=NOW)
    assert "elapsed_seconds" not in runs[0]


def test_annotate_runs_keeps_every_original_key():
    runs = [
        {
            "id": "a",
            "status": "running",
            "logs": "hello",
            "container_id": "c1",
            "started_at": "2026-08-29 11:00:00",
            "finished_at": None,
        }
    ]
    annotated = run_elapsed.annotate_runs(runs, now=NOW)[0]
    assert annotated["logs"] == "hello"
    assert annotated["container_id"] == "c1"
    assert annotated["elapsed_seconds"] == 3600.0


def test_no_runs_at_all_is_none():
    assert run_elapsed.running_for_seconds([], now=NOW) is None
    assert run_elapsed.annotate_runs([], now=NOW) == []


def test_the_default_clock_is_now_in_utc():
    """``now`` is injected everywhere above, so nothing else here would catch a
    default that read the local wall clock as UTC."""
    started = (datetime.now(UTC) - timedelta(seconds=30)).replace(tzinfo=None)
    measured = run_elapsed.elapsed_seconds(started.isoformat(sep=" "))
    assert measured is not None
    assert 25.0 <= measured <= 60.0


def test_the_default_clock_is_not_the_local_wall_clock(monkeypatch):
    """A start stamp written by SQLite on a box seven hours east of UTC.

    If the default clock were naive local time the span would come back
    seven hours out. Asserted through a real offset rather than by reading
    the source.
    """
    east = timezone(timedelta(hours=7))
    started = datetime.now(east).astimezone(UTC).replace(tzinfo=None)
    measured = run_elapsed.elapsed_seconds(started.isoformat(sep=" "))
    assert measured is not None
    assert measured < 60.0


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (0.0, "0s"),
        (43.0, "43s"),
        (59.9, "59s"),
        (60.0, "1m 00s"),
        (843.0, "14m 03s"),
        (3600.0, "1h 00m"),
        (8040.0, "2h 14m"),
        (7200.0 + 59.0, "2h 00m"),
    ],
)
def test_format_duration_shows_the_largest_two_units(seconds, expected):
    assert run_elapsed.format_duration(seconds) == expected


def test_format_duration_says_unknown_rather_than_printing_a_number():
    """A word, not a number. Every caller prints this beside real
    measurements, and "0s" there is a measurement nobody took."""
    rendered = run_elapsed.format_duration(None)
    assert rendered == "unknown"
    assert not any(char.isdigit() for char in rendered)
