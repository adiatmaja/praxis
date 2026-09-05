"""`praxis wait <id>`: block on the server, print each transition, exit honestly.

A shell user hand-rolling a curl loop makes the same mistake an assistant's
poll loop made on 2026-09-05: read the wrong field, wait ten minutes on a
worker that finished in forty seconds. This verb is built on the wait
endpoint, so there is no loop to write. Exit codes are the contract: 0 when
the engine has come to rest (terminal, or parked on a person, and the last
line says which and what to do), 2 when the deadline passed with the engine
still moving, 1 on any error.
"""
# ruff: noqa: S101

from __future__ import annotations

from typing import Any

import httpx
import pytest
from typer.testing import CliRunner

from cli.main import app
from tests.cli_text import flat, on_one_line


runner = CliRunner()

TASK_ID = "8b1bafa2-e401-4b17-81c2-56b56c91c906"
PLAN_ID = "11111111-2222-3333-4444-555555555555"
PR = "https://github.com/u/r/pull/7"


def _task_wait(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "task_id": TASK_ID,
        "plan_id": PLAN_ID,
        "status": "in_progress",
        "previous": "pending",
        "changed": True,
        "timed_out": False,
        "terminal": False,
        "waiting_on": "worker",
        "attempt": 1,
        "pr_url": None,
        "fingerprint": "fp",
        "timeout_seconds": 90.0,
        "waited_seconds": 3.0,
        "running_for_seconds": 3.0,
        "task": {"id": TASK_ID, "title": "Add [core] slugify", "status": "in_progress"},
    }
    body.update(overrides)
    return body


class _Script:
    """A transport answering the detail GET once and the wait GET in sequence."""

    def __init__(
        self,
        waits: list[dict[str, Any]],
        *,
        kind: str = "task",
        detail_status: int = 200,
    ) -> None:
        self.waits = list(waits)
        self.kind = kind
        self.detail_status = detail_status
        self.wait_requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/wait"):
            self.wait_requests.append(request)
            if not self.waits:
                msg = "wait called after the script ran out"
                raise AssertionError(msg)
            return httpx.Response(200, json=self.waits.pop(0))
        if path == f"/api/tasks/{TASK_ID}":
            if self.kind == "task":
                return httpx.Response(200, json={"task": {"id": TASK_ID}})
            return httpx.Response(404, json={"detail": "Task not found"})
        if path == f"/api/plans/{PLAN_ID}":
            if self.kind == "plan":
                return httpx.Response(200, json={"id": PLAN_ID, "status": "active"})
            return httpx.Response(404, json={"detail": "Plan not found"})
        if path.startswith(("/api/tasks/", "/api/plans/")):
            return httpx.Response(404, json={"detail": "not found"})
        msg = f"unexpected {request.method} {path}"
        raise AssertionError(msg)


def _wire(
    monkeypatch: pytest.MonkeyPatch, script: _Script, columns: str = "80"
) -> None:
    monkeypatch.setenv("ORCHESTRATOR_TOKEN", "t")
    monkeypatch.setenv("COLUMNS", columns)
    monkeypatch.setattr(
        "cli.main._client",
        lambda _timeout=None: httpx.Client(
            transport=httpx.MockTransport(script), base_url="http://test"
        ),
    )


def test_wait_prints_each_transition_and_exits_zero_at_the_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = _Script(
        [
            _task_wait(),
            _task_wait(status="reviewing", previous="in_progress", pr_url=PR),
            _task_wait(
                status="passed",
                previous="reviewing",
                waiting_on="human",
                pr_url=PR,
                running_for_seconds=None,
            ),
        ]
    )
    _wire(monkeypatch, script)
    result = runner.invoke(app, ["wait", TASK_ID])
    assert result.exit_code == 0, result.stdout
    assert on_one_line(result, "pending -> in_progress")
    assert on_one_line(result, "in_progress -> reviewing")
    assert on_one_line(result, "reviewing -> passed")
    assert on_one_line(result, f"praxis merge {TASK_ID}")
    assert "[core]" in flat(result)


def test_wait_exits_zero_on_a_terminal_task_and_says_so(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = _Script(
        [
            _task_wait(
                status="merged",
                previous="passed",
                waiting_on="nothing",
                terminal=True,
                changed=False,
                pr_url=PR,
                running_for_seconds=None,
            )
        ]
    )
    _wire(monkeypatch, script)
    result = runner.invoke(app, ["wait", TASK_ID])
    assert result.exit_code == 0, result.stdout
    assert "merged" in flat(result)
    assert "nothing" in flat(result).lower()


def test_wait_exits_two_when_the_deadline_passes_with_the_engine_moving(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = _Script(
        [
            _task_wait(
                changed=False,
                timed_out=True,
                previous="in_progress",
                waited_seconds=1.0,
                running_for_seconds=61.0,
            )
        ]
    )
    _wire(monkeypatch, script)
    result = runner.invoke(app, ["wait", TASK_ID, "--timeout", "1"])
    assert result.exit_code == 2, result.stdout
    text = flat(result)
    assert "still in_progress" in text
    assert "1m 01s" in text
    assert on_one_line(result, f"praxis wait {TASK_ID}")


def test_wait_asks_the_server_for_no_more_than_the_remaining_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = _Script(
        [
            _task_wait(
                changed=False, timed_out=True, previous="pending", status="pending"
            )
        ]
    )
    _wire(monkeypatch, script)
    runner.invoke(app, ["wait", TASK_ID, "--timeout", "7"])
    (request,) = script.wait_requests
    assert request.url.params["timeout"] == "7"


def test_wait_passes_the_fingerprint_back_between_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = _Script(
        [
            _task_wait(fingerprint="first"),
            _task_wait(
                status="failed",
                previous="in_progress",
                waiting_on="nothing",
                terminal=True,
                fingerprint="second",
                running_for_seconds=None,
            ),
        ]
    )
    _wire(monkeypatch, script)
    result = runner.invoke(app, ["wait", TASK_ID])
    assert result.exit_code == 0, result.stdout
    first, second = script.wait_requests
    assert "fingerprint" not in first.url.params
    assert second.url.params["fingerprint"] == "first"
    assert on_one_line(result, f"praxis retry {TASK_ID}")


def test_wait_resolves_a_plan_id_and_prints_leaf_transitions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def leaf(status: str) -> dict[str, Any]:
        return {"task_id": TASK_ID, "title": "Add slugify", "status": status}

    script = _Script(
        [
            {
                "plan_id": PLAN_ID,
                "status": "active",
                "previous": "active",
                "changed": True,
                "timed_out": False,
                "terminal": False,
                "waiting_on": "worker",
                "fingerprint": "a",
                "timeout_seconds": 90.0,
                "waited_seconds": 2.0,
                "tasks": [leaf("in_progress")],
                "stalled": {"action_required": None},
                "merge_gate": {"action_required": None},
                "integration_pr_url": None,
                "integration_merged_at": None,
                "plan_attempts": 0,
                "error": None,
            },
            {
                "plan_id": PLAN_ID,
                "status": "completed",
                "previous": "active",
                "changed": True,
                "timed_out": False,
                "terminal": True,
                "waiting_on": "nothing",
                "fingerprint": "b",
                "timeout_seconds": 90.0,
                "waited_seconds": 40.0,
                "tasks": [leaf("merged")],
                "stalled": {"action_required": None},
                "merge_gate": {"action_required": None},
                "integration_pr_url": "https://github.com/u/r/pull/9",
                "integration_merged_at": None,
                "plan_attempts": 0,
                "error": None,
            },
        ],
        kind="plan",
    )
    _wire(monkeypatch, script)
    result = runner.invoke(app, ["wait", PLAN_ID])
    assert result.exit_code == 0, result.stdout
    assert on_one_line(result, "Add slugify: in_progress -> merged")
    assert on_one_line(result, "active -> completed")
    assert on_one_line(result, f"praxis merge-plan {PLAN_ID}")


def test_wait_exits_one_when_the_id_is_neither_a_task_nor_a_plan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = _Script([], kind="none")
    _wire(monkeypatch, script)
    result = runner.invoke(app, ["wait", TASK_ID])
    assert result.exit_code == 1
    assert "neither" in flat(result).lower()


def test_wait_help_names_the_exit_codes() -> None:
    result = runner.invoke(app, ["wait", "--help"])
    assert result.exit_code == 0
    text = flat(result)
    assert "exit 0" in text.lower() or "exits 0" in text.lower()
    assert "2" in text


def test_wait_on_a_pending_proposal_names_the_approve_and_reject_verbs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = _Script(
        [
            {
                "plan_id": PLAN_ID,
                "status": "pending",
                "source": "autonomous",
                "previous": "pending",
                "changed": False,
                "timed_out": False,
                "terminal": False,
                "waiting_on": "human",
                "fingerprint": "a",
                "timeout_seconds": 90.0,
                "waited_seconds": 0.0,
                "tasks": [],
                "integration_pr_url": None,
                "integration_merged_at": None,
                "plan_attempts": 0,
                "error": None,
            }
        ],
        kind="plan",
    )
    _wire(monkeypatch, script)
    result = runner.invoke(app, ["wait", PLAN_ID])
    assert result.exit_code == 0, result.stdout
    assert "proposal" in flat(result).lower()
    assert on_one_line(result, f"praxis approve {PLAN_ID}")
    assert on_one_line(result, f"praxis reject {PLAN_ID}")


def test_wait_on_a_completed_plan_with_nothing_to_integrate_says_so(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = _Script(
        [
            {
                "plan_id": PLAN_ID,
                "status": "completed",
                "previous": "completed",
                "changed": False,
                "timed_out": False,
                "terminal": True,
                "waiting_on": "nothing",
                "integration_state": "nothing_to_integrate",
                "fingerprint": "a",
                "timeout_seconds": 90.0,
                "waited_seconds": 0.0,
                "tasks": [
                    {"task_id": TASK_ID, "title": "Add slugify", "status": "no_changes"}
                ],
                "integration_pr_url": None,
                "integration_merged_at": None,
                "plan_attempts": 0,
                "error": None,
            }
        ],
        kind="plan",
    )
    _wire(monkeypatch, script)
    result = runner.invoke(app, ["wait", PLAN_ID])
    assert result.exit_code == 0, result.stdout
    assert "nothing to integrate" in flat(result).lower()
