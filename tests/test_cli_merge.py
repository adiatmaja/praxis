"""Tests for the merge-gate CLI verbs."""

from __future__ import annotations

import httpx
import pytest
from typer.testing import CliRunner

from cli import main as cli_main
from cli.main import app


runner = CliRunner()


def _patch_client(monkeypatch, handler, seen: list[float] | None = None) -> None:
    """Route the CLI at a mock transport, recording the timeout it asked for.

    The stand-in mirrors the real ``_client`` signature and default, so a verb
    that stops passing its own timeout is recorded as having asked for the
    default rather than silently satisfying the assertion.
    """
    monkeypatch.setenv("ORCHESTRATOR_TOKEN", "t")

    def _fake_client(timeout: float = cli_main._DEFAULT_TIMEOUT) -> httpx.Client:
        if seen is not None:
            seen.append(timeout)
        return httpx.Client(
            base_url="http://x",
            headers={"Authorization": "Bearer t"},
            transport=httpx.MockTransport(handler),
        )

    monkeypatch.setattr("cli.main._client", _fake_client)


def test_merge_posts_approve_merge_for_the_task(monkeypatch) -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            seen["path"] = request.url.path
            return httpx.Response(200, json={"task_id": "abc-123", "status": "merged"})
        return httpx.Response(404, json={"detail": "not found"})

    _patch_client(monkeypatch, handler)
    result = runner.invoke(app, ["merge", "abc-123"])

    assert result.exit_code == 0
    assert seen["path"] == "/api/tasks/abc-123/approve-merge"
    assert "merged" in result.stdout
    assert "abc-123" in result.stdout


def test_merge_surfaces_a_gate_conflict(monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"detail": "task is not parked"})

    _patch_client(monkeypatch, handler)
    result = runner.invoke(app, ["merge", "abc-123"])

    assert result.exit_code == 1
    assert "409" in result.stdout


def test_merge_plan_posts_batch_and_reports_counts(monkeypatch) -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            seen["path"] = request.url.path
            # The REAL shape from api/plans.py: approved is an int, errors is a
            # list of {"task_id", "error"} dicts.
            return httpx.Response(
                200,
                json={
                    "plan_id": "plan-9",
                    "approved": 2,
                    "errors": [{"task_id": "t3", "error": "boom"}],
                },
            )
        return httpx.Response(404, json={"detail": "not found"})

    _patch_client(monkeypatch, handler)
    result = runner.invoke(app, ["merge-plan", "plan-9"])

    assert result.exit_code == 1
    assert seen["path"] == "/api/plans/plan-9/approve-merges"
    assert "2" in result.stdout
    assert "t3" in result.stdout
    assert "boom" in result.stdout


def test_merge_plan_reports_zero_without_crashing(monkeypatch) -> None:
    """The all-quiet case must not depend on falsy fallbacks to survive.

    It must also not read as success. `Merged: 0 task(s)` on its own was the
    exact output a finished plan produced while its integration PR sat open,
    so the genuinely-empty case now has to SAY it did nothing.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"plan_id": "plan-9", "approved": 0, "errors": []}
        )

    _patch_client(monkeypatch, handler)
    result = runner.invoke(app, ["merge-plan", "plan-9"])

    assert result.exit_code == 0
    assert "Nothing to merge" in result.stdout
    assert "Merged:" not in result.stdout


def test_merge_plan_reports_the_integration_merge(monkeypatch) -> None:
    """A finished plan's last step must be named, with its PR URL."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "plan_id": "plan-9",
                "approved": 0,
                "integration": {
                    "status": "merged",
                    "pr_url": "https://github.com/o/r/pull/48",
                },
                "errors": [],
            },
        )

    _patch_client(monkeypatch, handler)
    result = runner.invoke(app, ["merge-plan", "plan-9"])

    assert result.exit_code == 0
    assert "Integrated" in result.stdout
    assert "https://github.com/o/r/pull/48" in result.stdout
    assert "Nothing to merge" not in result.stdout


def test_merge_plan_says_when_the_plan_is_not_ready(monkeypatch) -> None:
    """An unfinished plan must not report an integration that did not happen."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "plan_id": "plan-9",
                "approved": 1,
                "integration": {
                    "status": "not_ready",
                    "reason": "2 task(s) not merged yet",
                },
                "errors": [],
            },
        )

    _patch_client(monkeypatch, handler)
    result = runner.invoke(app, ["merge-plan", "plan-9"])

    assert result.exit_code == 0
    assert "Merged:" in result.stdout
    assert "Not integrated" in result.stdout
    assert "2 task(s) not merged yet" in result.stdout


def test_pending_lists_a_plan_awaiting_integration(monkeypatch) -> None:
    """The integration PR must reach the operator, copyable, from `pending`.

    This is the run #5 blocker in one assertion: with an integration PR open,
    `pending` printed "Nothing awaiting approval." and the URL existed only in
    the orchestrator's log.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "count": 1,
                "task_count": 0,
                "plan_count": 1,
                "oldest_hours": 3.0,
                "tasks": [],
                "plans": [
                    {
                        "plan_id": FULL_PLAN_ID,
                        "project_id": "proj-1",
                        "branch": "plan/2026-08-21-add-slugify-helper",
                        "pr_url": "https://github.com/o/r/pull/48",
                        "age_hours": 3.0,
                    }
                ],
            },
        )

    _patch_client(monkeypatch, handler)
    result = runner.invoke(app, ["pending"])

    assert result.exit_code == 0
    assert "Nothing awaiting approval" not in result.stdout
    assert f"praxis merge-plan {FULL_PLAN_ID}" in result.stdout
    assert "https://github.com/o/r/pull/48" in result.stdout


FULL_PLAN_ID = "11111111-2222-3333-4444-555555555555"


def test_plans_prints_the_full_plan_id(monkeypatch) -> None:
    """`praxis plans` must print an id `praxis merge-plan` can actually use."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[
                {
                    "id": FULL_PLAN_ID,
                    "spec_path": "docs/spec.md",
                    "source": "human",
                    "status": "active",
                }
            ],
        )

    _patch_client(monkeypatch, handler)
    result = runner.invoke(app, ["plans", "project-1"])

    assert result.exit_code == 0
    flat = "".join(result.stdout.split())
    assert FULL_PLAN_ID in flat


# ---------------------------------------------------------------------------
# Timeouts
#
# The merge verbs are the only ones that wait on real work: the orchestrator
# merges a plan's PASSED tasks SEQUENTIALLY (bounded by max_leaves_per_plan =
# 24), and one merge_pr under repeated GitHub 504s is three `gh pr merge`
# attempts plus backoff plus up to four `gh pr view` calls. At the CLI's
# blanket 60 s a 12-task plan times out deterministically, and it did so in
# exactly the 504 case Task 6 taught the server to survive: the orchestrator
# finished the merges correctly while the operator saw a bare ReadTimeout
# traceback and no way to tell whether anything had landed.
# ---------------------------------------------------------------------------


def _timing_out_handler(request: httpx.Request) -> httpx.Response:
    message = "timed out waiting for the orchestrator"
    raise httpx.ReadTimeout(message)


@pytest.mark.parametrize(
    ("argv", "timeout_index"),
    [(["merge", "abc-123"], 0), (["merge-plan", "plan-9"], 0)],
)
def test_the_merge_verbs_ask_for_more_than_the_read_only_timeout(
    monkeypatch, argv, timeout_index
) -> None:
    """A verb that waits on 24 sequential merges cannot use the read-only budget."""
    seen: list[float] = []
    _patch_client(monkeypatch, _timing_out_handler, seen)

    runner.invoke(app, argv)

    assert seen[timeout_index] == cli_main._MERGE_TIMEOUT
    assert cli_main._MERGE_TIMEOUT > cli_main._DEFAULT_TIMEOUT
    # 24 leaves is the plan ceiling and a single 504-retrying merge is tens of
    # seconds, so the budget has to be minutes, not a nudge upward.
    assert cli_main._MERGE_TIMEOUT >= 600.0


def test_a_read_only_verb_keeps_the_short_timeout(monkeypatch) -> None:
    """Widening the merge budget must not slow down every other command."""
    seen: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[])

    _patch_client(monkeypatch, handler, seen)
    result = runner.invoke(app, ["plans", "project-1"])

    assert result.exit_code == 0
    assert seen == [cli_main._DEFAULT_TIMEOUT]


@pytest.mark.parametrize("argv", [["merge", "abc-123"], ["merge-plan", "plan-9"]])
def test_a_merge_that_times_out_says_it_may_still_have_landed(
    monkeypatch, argv
) -> None:
    """No traceback, and never the claim that the merge failed.

    The orchestrator keeps merging after the client gives up, so "no answer"
    does not mean "not merged". A re-run then answers 409 "not awaiting
    merge", which reads as an error rather than "already done", so the
    operator has to be pointed at `praxis pending` instead.
    """
    _patch_client(monkeypatch, _timing_out_handler)

    result = runner.invoke(app, argv)

    assert result.exit_code == 1
    # A bare httpx exception escaping is the defect itself.
    assert not isinstance(result.exception, httpx.RequestError)
    flat = " ".join(result.stdout.split())
    assert "timed out" in flat.lower()
    assert "may still be running" in flat.lower()
    assert "praxis pending" in flat


@pytest.mark.parametrize("argv", [["merge", "abc-123"], ["merge-plan", "plan-9"]])
def test_a_merge_that_cannot_connect_is_reported_too(monkeypatch, argv) -> None:
    """Every RequestError, not only the timeout subclass, owes an explanation."""

    def handler(request: httpx.Request) -> httpx.Response:
        message = "connection refused"
        raise httpx.ConnectError(message)

    _patch_client(monkeypatch, handler)

    result = runner.invoke(app, argv)

    assert result.exit_code == 1
    assert not isinstance(result.exception, httpx.RequestError)
    assert "praxis pending" in " ".join(result.stdout.split())
