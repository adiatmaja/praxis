"""Guards for the dashboard connection-dot rendering fed by ``/api/status``.

Same technique as ``tests/test_dashboard_pending_surfaces.py``: there is no JS
test runner here, so these read ``web/app.js`` as text and assert against ONE
function body, brace matched. A plain grep across the whole file would still
pass if the fix moved somewhere the caller never reaches; scoping to the body
is what makes the guard mean something.

Covers two defects in ``GET /api/status``:

- The planner dot painted a confident green or red for a provider this
  endpoint never actually probed (a ``local`` planner, or any provider other
  than the one it hardcoded a check against). ``setConnection`` must read the
  new ``connected_measured`` flag and render a neutral state instead of
  asserting a verdict nobody measured.
- The agent counter read "0" identically for "idle" and for "the agent
  manager could not be reached" (no Docker, or a raised listing).
  ``pollStatus`` must read the new ``agents_reachable`` flag and say so.
"""
# ruff: noqa: S101

from __future__ import annotations

import re

from tests.test_dashboard_pending_surfaces import body_of


def test_set_connection_does_not_paint_a_verdict_for_an_unmeasured_planner() -> None:
    """`connected_measured === false` must steer the dot away from both colors.

    The reverted form falls straight to `connected ? "connected" :
    "disconnected"`, which is exactly the fabrication this fixes: a `local`
    planner (or any provider `/api/status` did not actually probe) would
    still get a confident green or red dot.
    """
    body = body_of("setConnection")

    assert "connected_measured" in body, (
        "setConnection ignores connected_measured and can only ever render "
        "connected or disconnected, even for a planner never probed"
    )

    # The measured-vs-not branch must gate BEFORE the connected/disconnected
    # decision, not merely mention the flag somewhere unreachable.
    guard = re.search(r"if\s*\(\s*!\s*measured\s*\)", body)
    assert guard is not None, (
        "no `if (!measured)` branch: connected_measured is read but never "
        "used to short-circuit the colour decision"
    )

    # The class it assigns for the unmeasured case must be neither of the
    # two verdict colours.
    branch_start = guard.start()
    next_brace = body.index("{", branch_start)
    depth = 0
    end = next_brace
    for index in range(next_brace, len(body)):
        char = body[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                end = index
                break
    unmeasured_branch = body[next_brace : end + 1]
    assert '"connected"' not in unmeasured_branch
    assert '"disconnected"' not in unmeasured_branch


def test_set_connection_missing_measured_key_still_reads_as_measured() -> None:
    """`subagent_model` never carries `connected_measured`; it must not go grey.

    The comparison has to be `!== false`, not a bare truthiness check on the
    key's presence, or the subagent pill (which has no such field) would be
    treated as unmeasured too and lose its real connected/disconnected dot.
    """
    body = body_of("setConnection")
    assert re.search(r"connected_measured\s*!==\s*false", body), (
        "measured must be computed as `!== false` so a missing key (every "
        "info shape except agent_model) still reads as measured"
    )


def test_poll_status_distinguishes_idle_from_cannot_ask() -> None:
    """The agent counter must not print the same "0" for both states.

    Before this fix, `stat-agents` was set to `status.active_agents`
    unconditionally, so an agent manager that could not be reached (no
    Docker, or a raised container listing) rendered identically to a real,
    reachable, idle system.
    """
    body = body_of("pollStatus")

    assert "agents_reachable" in body, (
        "pollStatus never reads agents_reachable, so a reader cannot tell "
        "idle from unreachable"
    )

    guard = re.search(r"if\s*\(\s*status\.agents_reachable\s*\)\s*\{", body)
    assert guard is not None, (
        "no `if (status.agents_reachable)` branch: the flag is read but never "
        "used to gate the active_agents assignment"
    )

    # The assignment from the reverted code has to live INSIDE that branch,
    # not merely appear somewhere in the function (which would still be the
    # unconditional defect with the guard bolted on but unused).
    depth = 0
    end = guard.end() - 1
    for index in range(guard.end() - 1, len(body)):
        char = body[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                end = index
                break
    reachable_branch = body[guard.end() - 1 : end + 1]
    assert re.search(
        r"agentsStat\.textContent\s*=\s*status\.active_agents;", reachable_branch
    ), (
        "the active_agents assignment does not live inside the "
        "agents_reachable branch, so it still runs (or never runs) "
        "unconditionally"
    )
