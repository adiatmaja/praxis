"""The dashboard read naive UTC timestamps as local time.

``GET /api/plans`` serves SQLite's own ``created_at``: ``"2026-08-27 21:13:33"``
-- a space instead of a "T", and no offset and no "Z". A string in that shape
is not an ISO instant, so ``new Date(s)`` takes the implementation-defined
branch of ECMA-262, and V8 reads it as LOCAL time. Every relative age on the
completed-plan rows was therefore wrong by exactly the viewer's UTC offset.

Measured in the live dashboard on 2026-08-28 from UTC+7, with the plan that had
just finished::

    new Date("2026-08-27 21:13:33").toISOString()  ->  2026-08-27T14:13:33.000Z
    true age  0.33 h        rendered age  7.33 h   ->  "7h ago"

East of UTC this over-reports, which is merely wrong. WEST of UTC it
UNDER-reports, and ``timeAgo``'s own ``Math.max(0, ...)`` then clamps a
timestamp that parses into the future all the way down to "0s ago", so work
that is hours old reads as "just now". That is the direction that actually
misleads, and it is the reason this is worth a guard rather than a shrug.

Two properties are pinned, because the fix has two halves that fail
differently:

* ``timeAgo`` must route through the normaliser. A fix that sat in the file but
  was not called from the one function that needs it is the exact shape
  ``body_of`` exists to catch.
* the normaliser's own test for "does this string already carry a zone" must
  classify real timestamps from this API correctly. The pattern is read OUT of
  the shipped source and exercised here rather than restated, so a tightened or
  loosened regex is measured rather than assumed. ``agent_runs.finished_at`` is
  a full ISO instant with ``+00:00`` and MUST be left alone; shifting it a
  second time would be the same bug with the sign flipped.
"""

# ruff: noqa: S101

from __future__ import annotations

import re

from tests.test_dashboard_pending_surfaces import body_of


def _zone_pattern() -> re.Pattern[str]:
    """Return the shipped 'already has a zone' test, as a Python regex.

    Read out of ``web/app.js`` rather than restated, so this guard measures the
    pattern the browser actually runs. The JS and Python flavours agree on
    every construct used here (a non-capturing group, a character class, an
    alternation and ``$``), so the translation is the delimiters only.
    """
    match = re.search(r"const hasZone = /(.+?)/\.test\(raw\)", body_of("toInstant"))
    assert match is not None, (
        "toInstant no longer tests for an existing timezone with an inline "
        "regex; if that check moved, this guard must move with it"
    )
    return re.compile(match.group(1))


def test_time_ago_normalises_before_parsing() -> None:
    """The naive-UTC fix has to be on the path ``timeAgo`` actually takes."""
    body = body_of("timeAgo")
    assert "toInstant(isoString)" in body, (
        "timeAgo must parse through toInstant; parsing the raw string again "
        "reinstates the local-time misreading"
    )
    assert "new Date(isoString)" not in body, (
        "timeAgo constructs a Date from the raw API string again, which is the "
        "defect itself"
    )


def test_a_naive_sqlite_timestamp_is_treated_as_utc() -> None:
    """The exact shape the plans API serves must be recognised as zone-less."""
    assert not _zone_pattern().search("2026-08-27 21:13:33"), (
        "the naive UTC timestamp the API actually serves was classified as "
        "already carrying a zone, so no Z is appended and it is read as local "
        "time -- the original defect"
    )


def test_a_zone_bearing_timestamp_is_left_alone() -> None:
    """A full ISO instant must not be shifted a second time."""
    pattern = _zone_pattern()
    for stamp in (
        "2026-08-27T21:23:17.361781+00:00",  # agent_runs.finished_at
        "2026-08-27T21:23:17Z",
        "2026-08-27T21:23:17+07:00",
        "2026-08-27T14:23:17-0500",
    ):
        assert pattern.search(stamp), (
            f"{stamp!r} carries a timezone but was classified as naive; "
            "appending Z to it would shift an already-correct instant"
        )


def test_the_normaliser_appends_a_zulu_marker_for_naive_input() -> None:
    """Zone-less input is completed to UTC rather than left ambiguous."""
    body = body_of("toInstant")
    assert '+ "Z"' in body, (
        "toInstant no longer completes a naive timestamp to UTC, so the "
        "browser falls back to reading it as local time"
    )
    assert 'replace(" ", "T")' in body, (
        "a space-separated timestamp must become a real ISO instant, or "
        "appending Z alone still leaves it on the non-ISO parse path"
    )


def test_a_stalled_plan_is_marked_on_the_lane_not_only_in_the_detail() -> None:
    """A stalled plan's own status badge says ACTIVE, like a healthy one.

    ``plan_reachability`` leaves a stalled plan ACTIVE with a null ``error`` on
    purpose: writing it FAILED would hand its branch to the stale-branch
    sweeper. Every other surface compensates. ``praxis plans`` prints
    "(stalled; N tasks blocked by a failure)" beside the status and a copyable
    ``praxis retry <blocking-task-id>``; MCP ``poll_plan`` sets
    ``stalled.action_required`` to ``retry_failed_task`` with a hint naming all
    three recovery routes; ``PlanResponse`` carries ``stalled_task_ids``.

    The dashboard lane carried none of it. The stall was rendered only in the
    plan DETAIL panel, so the swim lane -- the view the dashboard opens on --
    showed a plan nothing will ever move as an ordinary ACTIVE lane. Found on
    the live dashboard on 2026-08-28 against playground plan ``01029a25``,
    whose CLI and MCP views both flagged it at list level.
    """
    body = body_of("renderSwimLane")
    assert "lane-stalled" in body, (
        "the swim lane no longer builds a stalled marker, so the dashboard is "
        "back to showing a stalled plan as an ordinary active lane"
    )
    assert "plan.stalled_task_ids" in body, (
        "the lane marker must be driven by the served stalled_task_ids, not by "
        "a status string a stalled plan does not have"
    )
    # Declaring the chip is not rendering it. Deleting the ONE line that
    # concatenates it into the header leaves both strings above intact, so
    # asserting on them alone is a guard that cannot fail its own defect --
    # measured, it stayed green through exactly that deletion. Pin the
    # EMISSION: the chip has to sit in the header, next to the status badge
    # whose ambiguity it exists to resolve.
    header = re.search(
        r"badge\(plan\.status\)(.{0,120}?)'<div class=\"lane-spec\">",
        body,
        re.DOTALL,
    )
    assert header is not None, (
        "the lane header no longer renders badge(plan.status) before the spec "
        "preview; this guard must be re-anchored if that markup moved"
    )
    assert "stalledChip" in header.group(1), (
        "the stalled marker is built but never concatenated into the lane "
        "header, so nothing reaches the screen"
    )
