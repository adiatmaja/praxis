"""The dashboard says how long a worker has been running, on both seats.

A worker ran unattended for about two hours on 2026-08-28 and no surface said
so. The dashboard is the one somebody watching a plan actually leaves open, so
it needs the fact in two places that fail differently: the swim-lane CARD (the
view the dashboard opens on) and the task DETAIL panel.

Both are pinned by EMISSION, not by declaration. A guard that greps a function
body for a marker cannot fail if the marker's declaration stays behind while
the one line that concatenates it into the returned markup is deleted; that
happened to the first stalled-lane guard in this same file's neighbour, which
stayed green through exactly that deletion.
"""

# ruff: noqa: S101

from __future__ import annotations

import re

from tests.test_dashboard_pending_surfaces import body_of


def test_the_duration_is_rendered_not_derived_from_a_timestamp() -> None:
    """The SERVER measures ``running_for_seconds``; this only formats it.

    Client-side date math on these stamps is exactly what read naive UTC as
    local time and rendered a 20-minute-old plan as "7h ago". A ``Date`` inside
    the formatter would be that defect returning by a different door.
    """
    body = body_of("formatDuration")
    assert "new Date" not in body, (
        "formatDuration builds a Date, so it is deriving the span in the "
        "browser again instead of rendering the server's measurement"
    )
    assert "toInstant" not in body


def test_a_non_number_renders_as_nothing_never_as_zero() -> None:
    """``null`` means nothing is running, or its start could not be read.

    Rendering that as "0s" would be a measurement nobody took, on the one
    surface whose purpose is making a LONG run stand out. Same class as the
    four sidebar stats that shipped a numeric ``0``.
    """
    body = body_of("formatDuration")
    assert 'typeof seconds !== "number"' in body, (
        "formatDuration no longer refuses a non-number, so null falls through "
        "the arithmetic below and renders as a duration nobody measured"
    )
    assert 'return "";' in body, (
        "the refusal must yield the empty string, which every caller treats as "
        "'print nothing'; any digit here becomes a fabricated measurement"
    )


def test_the_swim_lane_card_emits_the_running_line() -> None:
    """The at-a-glance seat. A card that has said "in progress" for two hours
    is indistinguishable from one two minutes in without this."""
    body = body_of("renderTaskCard")
    assert "task.running_for_seconds" in body, (
        "the card no longer reads the served measurement, so it can only be "
        "re-deriving it or showing nothing"
    )
    # Pin the EMISSION: `runningLine` must be concatenated into the markup this
    # function RETURNS, between the branch name and the action row. Asserting
    # only that the variable is built leaves a guard that survives deleting the
    # single line that puts it on screen.
    emitted = re.search(
        r"esc\(task\.branch_name \|\| \"-\"\)(.*?)retryAction \+ prLink",
        body,
        re.DOTALL,
    )
    assert emitted is not None, (
        "the card markup no longer runs from the branch-name row to the action "
        "row; this guard must be re-anchored if that markup moved"
    )
    assert "runningLine" in emitted.group(1), (
        "the running line is built but never concatenated into the card, so "
        "nothing reaches the screen"
    )


def test_the_task_detail_panel_emits_a_running_for_field() -> None:
    body = body_of("renderTaskDetail")
    assert "task.running_for_seconds" in body
    # The label and the value must land in the SAME emitted field, or the panel
    # renders a header with nothing under it.
    field = re.search(
        r">Running for</span>(.{0,240}?)</div>",
        body,
        re.DOTALL,
    )
    assert field is not None, (
        "the detail panel no longer emits a 'Running for' field; a declared "
        "duration nothing renders is the defect this guard exists for"
    )
    assert "formatDuration(task.running_for_seconds)" in field.group(1), (
        "the 'Running for' field is emitted with something other than the "
        "formatted server measurement in it"
    )


def test_the_detail_field_is_conditional_so_finished_tasks_stay_clean() -> None:
    """A "Running for -" row on every finished task is the same noise the
    contract-drift block deliberately avoids: a line on every row is how a
    reader learns to skip the line that matters."""
    body = body_of("renderTaskDetail")
    assert "formatDuration(task.running_for_seconds) ?" in body, (
        "the 'Running for' field is emitted unconditionally, so every task "
        "that is not running now carries an empty measurement row"
    )
