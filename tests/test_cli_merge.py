"""Tests for the merge-gate CLI verbs."""

from __future__ import annotations

import httpx
from typer.testing import CliRunner

from cli.main import app


runner = CliRunner()


def _patch_client(monkeypatch, handler) -> None:
    monkeypatch.setenv("ORCHESTRATOR_TOKEN", "t")
    monkeypatch.setattr(
        "cli.main._client",
        lambda: httpx.Client(
            base_url="http://x",
            headers={"Authorization": "Bearer t"},
            transport=httpx.MockTransport(handler),
        ),
    )


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

    assert result.exit_code == 0
    assert seen["path"] == "/api/plans/plan-9/approve-merges"
    assert "2" in result.stdout
    assert "t3" in result.stdout
    assert "boom" in result.stdout


def test_merge_plan_reports_zero_without_crashing(monkeypatch) -> None:
    """The all-quiet case must not depend on falsy fallbacks to survive."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"plan_id": "plan-9", "approved": 0, "errors": []}
        )

    _patch_client(monkeypatch, handler)
    result = runner.invoke(app, ["merge-plan", "plan-9"])

    assert result.exit_code == 0
    assert "0" in result.stdout
