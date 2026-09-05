"""The dashboard's health strip must show the measured agent count, not "?".

Seen twice on 2026-09-05 (probes 1 and 7): the header strip read "Agents: ?"
with the tooltip "not measured yet" while the sidebar stat on the same page
read the measured number (0, then 3). ``renderHealthBar`` runs on the
dashboard render, before the first ``pollStatus`` answers, and nothing
refreshed the strip afterwards; the Opus pill next to it IS refreshed in
place by ``pollStatus``, so the fix is the same shape.

Pinned on the EMISSION (the class the strip renders and the selector the
poll writes to), never on a declaration, so a refactor that stops rendering
or stops writing turns this red.
"""
# ruff: noqa: S101

from __future__ import annotations

import re
from pathlib import Path


APP_JS = Path("web/app.js").read_text(encoding="utf-8")


def _body(function_name: str) -> str:
    start = APP_JS.index(f"function {function_name}(")
    depth = 0
    for i in range(start, len(APP_JS)):
        if APP_JS[i] == "{":
            depth += 1
        elif APP_JS[i] == "}":
            depth -= 1
            if depth == 0:
                return APP_JS[start:i]
    raise AssertionError(function_name)


def test_health_bar_renders_addressable_agent_and_queue_items() -> None:
    body = _body("renderHealthBar")
    assert re.search(r'class="health-agents"[^>]*>Agents: ', body)
    assert re.search(r'class="health-queue"[^>]*>Queue: ', body)


def test_poll_status_refreshes_the_health_bar_agents_and_queue_in_place() -> None:
    body = _body("pollStatus")
    assert '.health-bar .health-agents' in body
    assert '.health-bar .health-queue' in body
    # The measured values, never the sidebar's rendered text (that read back
    # "?" as NaN once already), and "?" when nothing was measured.
    agents_write = body[body.index(".health-bar .health-agents") :]
    assert "measuredAgentCount == null" in agents_write[:600]
