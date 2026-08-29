"""How long an agent run has been going, derived and never guessed.

A worker ran for about two hours against the owner's own hardware on
2026-08-28 and nothing anywhere said so; he noticed because his machine was
busy. A wedged worker, a slow worker and one burning somebody's GPU read
identically on every surface Praxis has, because no surface reported elapsed
time at all. This module is the single answer to "how long", so the CLI, the
dashboard, MCP and the wall-clock bound cannot each derive a different one.

Three rules hold everywhere here, and each of them has already cost this
repository a defect somewhere else:

1. **A naive stamp is UTC.** ``agent_runs.started_at`` defaults to SQLite's
   ``CURRENT_TIMESTAMP``, which is naive UTC (``"2026-08-27 21:13:33"``). Read
   as local time it is wrong by exactly the reader's offset, which is how the
   dashboard came to render a 20-minute-old plan as "7h ago". A stamp that
   DOES carry a zone is left alone or it shifts twice.
2. **Unmeasurable is ``None``, never ``0.0``.** Every surface below exists to
   make a long run visible, so a stamp nobody could parse must not render as a
   run that just started. ``core.approvals._age_hours`` answers ``0.0`` for the
   same input and is right to, because it feeds a sort key; here the number is
   the message. Same rule as the sidebar stats that shipped a numeric ``0``
   for four measurements nobody took.
3. **Open is ``finished_at IS NULL``.** Never ``status``: that column carries
   whatever string the harness reported, and a harness answering with the word
   "running" produced a CLOSED row that still read as open. The claim
   (``claim_agent_run_completion``) and ``get_running_runs`` key on the same
   predicate, and this module has to agree with them or the bound would expire
   a run the callback already settled.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any


def parse_stamp(value: object) -> datetime | None:
    """Return an aware ``datetime`` for a stored timestamp, or None.

    Args:
        value: A timestamp as stored on an ``agent_runs`` row. Naive strings
            are read as UTC; strings carrying an offset keep theirs.

    Returns:
        An aware ``datetime``, or ``None`` when the value is missing or does
        not parse. ``None`` means "not known" and never "zero".
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        stamp = datetime.fromisoformat(text)
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    return stamp


def elapsed_seconds(
    started_at: object,
    finished_at: object = None,
    *,
    now: datetime | None = None,
) -> float | None:
    """Seconds a run has been going, or took.

    Args:
        started_at: When the container started.
        finished_at: When the run was disposed of, or ``None`` while it is
            still open, in which case ``now`` is the far end.
        now: Injected clock, for tests. Defaults to the current UTC instant.

    Returns:
        Seconds as a float, or ``None`` when either end could not be read. A
        result is clamped at zero: both ends come from the same host's clock
        (SQLite's ``CURRENT_TIMESTAMP`` and this process), so a negative span
        is not a fact about the run, it is arithmetic noise.
    """
    start = parse_stamp(started_at)
    if start is None:
        return None
    if finished_at is None:
        end = now if now is not None else datetime.now(UTC)
    else:
        end = parse_stamp(finished_at)  # type: ignore[assignment]
        if end is None:
            return None
    return max((end - start).total_seconds(), 0.0)


def open_run(runs: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Return the run still in flight, earliest first, or None.

    Args:
        runs: ``agent_runs`` rows for one task.

    Returns:
        The oldest row whose ``finished_at`` is NULL. Oldest rather than
        newest deliberately: two open rows means reconciliation has not caught
        up, and the older one is both the longer-running and the more alarming
        of the two, which is the direction this surface should err in.
    """
    open_rows = [run for run in runs if run.get("finished_at") is None]
    if not open_rows:
        return None
    return min(
        open_rows,
        key=lambda run: (
            parse_stamp(run.get("started_at")) or datetime.max.replace(tzinfo=UTC)
        ),
    )


def running_for_seconds(
    runs: list[dict[str, Any]], *, now: datetime | None = None
) -> float | None:
    """Seconds the task's live run has been going, or None if none is.

    Args:
        runs: ``agent_runs`` rows for one task.
        now: Injected clock, for tests.

    Returns:
        Seconds, or ``None`` when no run is open OR when the open run's start
        could not be read. The two collapse on purpose: both mean "this
        surface cannot tell you", and a renderer that distinguished them would
        be reporting on Praxis rather than on the worker.
    """
    run = open_run(runs)
    if run is None:
        return None
    return elapsed_seconds(run.get("started_at"), None, now=now)


def annotate_runs(
    runs: list[dict[str, Any]], *, now: datetime | None = None
) -> list[dict[str, Any]]:
    """Copy each run row with an ``elapsed_seconds`` key added.

    Args:
        runs: ``agent_runs`` rows for one task.
        now: Injected clock, for tests.

    Returns:
        New dicts; the inputs are not mutated. A closed run carries how long
        it took, an open one how long it has been going, and either may be
        ``None``.
    """
    return [
        {
            **run,
            "elapsed_seconds": elapsed_seconds(
                run.get("started_at"), run.get("finished_at"), now=now
            ),
        }
        for run in runs
    ]


def format_duration(seconds: float | None) -> str:
    """Render a duration for a human, or say it is not known.

    Args:
        seconds: A span from this module, possibly ``None``.

    Returns:
        ``"unknown"`` for ``None`` -- a word, never a number, because every
        caller prints this next to real measurements. Otherwise ``"2h 14m"``,
        ``"14m 03s"`` or ``"43s"``: the largest two units, so an hours-long
        run cannot be mistaken for a minutes-long one at a glance.
    """
    if seconds is None:
        return "unknown"
    total = int(max(seconds, 0.0))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"
