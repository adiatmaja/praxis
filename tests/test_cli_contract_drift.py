"""`praxis task` and `praxis pending` must show what the diff did to the plan.

Both verbs, on purpose: `pending` is the queue and `task` is the detail view,
and a fact that appears only in the queue is invisible to anyone who went
straight to the task. The feature shipped with the renderer in `pending` alone
and a commit message claiming both, which is the kind of gap only a test for
the second surface closes.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from typer.testing import CliRunner

from cli.main import app

from .cli_text import flat


runner = CliRunner()

TASK_ID = "6f0d4a2e-2b71-4e63-9a44-1f4a1f6f5a01"

DRIFT_STRONG = {
    "gradable": True,
    "why_not": "",
    "named_not_authorised": ["src/playground/test_guard.py"],
    "unmentioned": [],
    "summary": (
        "Plan paths: this diff edits src/playground/test_guard.py, which the "
        "plan NAMES but never authorises for any task - check it is not the "
        "plan's acceptance contract before approving."
    ),
}

DRIFT_CLEAN = {
    "gradable": True,
    "why_not": "",
    "named_not_authorised": [],
    "unmentioned": [],
    "summary": "Plan paths: this diff stayed inside the paths the plan authorised.",
}


def _patch(monkeypatch, handler, columns: str = "100") -> None:
    monkeypatch.setenv("ORCHESTRATOR_TOKEN", "t")
    monkeypatch.setenv("COLUMNS", columns)
    monkeypatch.setattr(
        "cli.main._client",
        lambda _timeout=None: httpx.Client(
            base_url="http://x",
            headers={"Authorization": "Bearer t"},
            transport=httpx.MockTransport(handler),
        ),
    )


def _json(payload: Any, status: int = 200):
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=payload)

    return handler


def _task_detail(drift: Any) -> dict[str, Any]:
    return {
        "task": {
            "id": TASK_ID,
            "title": "Add require_mapping and require_choice",
            "branch_name": "agent/add-require-mapping",
            "status": "passed",
            "attempt": 2,
            "pr_url": "https://github.com/u/repo/pull/107",
            "review_feedback": "The implementation follows the convention.",
            "contract_drift": drift,
        },
        "runs": [],
    }


def _pending_payload(drift: Any) -> dict[str, Any]:
    return {
        "count": 1,
        "task_count": 1,
        "plan_count": 0,
        "proposal_count": 0,
        "oldest_hours": 1.0,
        "tasks": [
            {
                "task_id": TASK_ID,
                "title": "Add require_mapping and require_choice",
                "branch": "agent/add-require-mapping",
                "pr_url": "https://github.com/u/repo/pull/107",
                "age_hours": 1.0,
                "review_scope": None,
                "contract_drift": drift,
            }
        ],
        "plans": [],
        "proposals": [],
    }


@pytest.mark.unit
def test_praxis_task_prints_the_unauthorised_path(monkeypatch) -> None:
    """The detail view names the file, not just that something happened.

    Taken from the live walk of 2026-08-27 (playground PR #107): the plan asked
    for tests in ``test_guard.py`` and authorised only ``guard.py``, so the
    worker's diff touched a path the plan names and never authorises. The
    reviewer's own feedback cannot say this - it grades against the leaf.
    """
    _patch(monkeypatch, _json(_task_detail(DRIFT_STRONG)))

    result = runner.invoke(app, ["task", TASK_ID])

    assert result.exit_code == 0
    out = flat(result)
    assert "src/playground/test_guard.py" in out
    assert "never authorises" in out


@pytest.mark.unit
def test_praxis_task_stays_quiet_on_a_clean_result(monkeypatch) -> None:
    """A clean result is the normal case and must not add a line.

    A "nothing to see" line on every task is the fastest way to train a reader
    to skip the block that also carries the warnings.
    """
    _patch(monkeypatch, _json(_task_detail(DRIFT_CLEAN)))

    result = runner.invoke(app, ["task", TASK_ID])

    assert result.exit_code == 0
    assert "Plan paths" not in flat(result)


@pytest.mark.unit
def test_praxis_task_survives_a_row_that_was_never_checked(monkeypatch) -> None:
    """``None`` is every pre-feature row; it must print nothing and not crash."""
    _patch(monkeypatch, _json(_task_detail(None)))

    result = runner.invoke(app, ["task", TASK_ID])

    assert result.exit_code == 0
    assert "Plan paths" not in flat(result)


@pytest.mark.unit
def test_praxis_pending_names_the_path_below_the_table(monkeypatch) -> None:
    """The queue carries the same fact, in full, under the copyable line."""
    _patch(monkeypatch, _json(_pending_payload(DRIFT_STRONG)))

    result = runner.invoke(app, ["pending"])

    assert result.exit_code == 0
    out = flat(result)
    assert "src/playground/test_guard.py" in out
    # And the table gains its column only when a row has something to say.
    assert "Plan paths" in out


@pytest.mark.unit
def test_praxis_pending_omits_the_column_when_every_row_is_clean(monkeypatch) -> None:
    """An always-on column of blanks costs width on an 80-column table."""
    _patch(monkeypatch, _json(_pending_payload(DRIFT_CLEAN)), columns="80")

    result = runner.invoke(app, ["pending"])

    assert result.exit_code == 0
    assert "Plan paths" not in flat(result)
