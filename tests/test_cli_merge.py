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


def test_merge_surfaces_a_gate_conflict(monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"detail": "task is not parked"})

    _patch_client(monkeypatch, handler)
    result = runner.invoke(app, ["merge", "abc-123"])

    assert result.exit_code == 1
    assert "409" in result.stdout
