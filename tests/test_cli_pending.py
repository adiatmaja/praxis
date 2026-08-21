"""`praxis pending` must print something the operator can act on."""

from __future__ import annotations

import httpx
from typer.testing import CliRunner

from cli.main import app


runner = CliRunner()

TASK_ID = "8b1bafa2-e401-4b17-81c2-56b56c91c906"
PR_URL = "https://github.com/adiatmaja/playground/pull/37"


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


def test_pending_prints_the_full_task_id_and_pr_url(monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "count": 1,
                "oldest_hours": 0.0,
                "tasks": [
                    {
                        "task_id": TASK_ID,
                        "title": "Implement initials() helper function",
                        "branch": "agent/implement-initials-function",
                        "pr_url": PR_URL,
                        "age_hours": 0.0,
                    }
                ],
            },
        )

    _patch_client(monkeypatch, handler)
    result = runner.invoke(app, ["pending"])

    assert result.exit_code == 0
    # Rich may wrap, so compare with whitespace collapsed.
    flat = "".join(result.stdout.split())
    assert TASK_ID in flat
    assert PR_URL in flat


def test_pending_is_quiet_when_nothing_is_parked(monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"count": 0, "oldest_hours": 0.0, "tasks": []})

    _patch_client(monkeypatch, handler)
    result = runner.invoke(app, ["pending"])

    assert result.exit_code == 0
    assert "Nothing awaiting approval" in result.stdout


PROPOSAL_ID = "2dca2c06-9f17-45ec-97db-21c31c9aa8f2"
PROJECT_ID = "2b8c255d-ca43-4e39-bf97-033464ddc2cc"


def test_pending_surfaces_an_autonomous_proposal(monkeypatch) -> None:
    """A proposal is parked work too, even though `count` excludes it.

    The improvement loop's entire output arrives as a PENDING autonomous plan.
    Reading `count` alone printed "Nothing awaiting approval" over the top of
    it, so the only way to find a proposal was `praxis plans <project-id>`,
    which requires already knowing it exists and which project it belongs to.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "count": 0,
                "oldest_hours": 0.0,
                "tasks": [],
                "plans": [],
                "proposal_count": 1,
                "proposals": [
                    {
                        "plan_id": PROPOSAL_ID,
                        "project_id": PROJECT_ID,
                        "age_hours": 0.0,
                    }
                ],
            },
        )

    _patch_client(monkeypatch, handler)
    monkeypatch.setenv("COLUMNS", "160")
    result = runner.invoke(app, ["pending"])

    assert result.exit_code == 0
    assert "Nothing awaiting approval" not in result.stdout
    # Both verbs, on a line the operator can copy whole.
    assert any(
        PROPOSAL_ID in line and "praxis approve" in line
        for line in result.stdout.splitlines()
    )
    assert "praxis reject" in result.stdout


def test_pending_stays_quiet_when_there_are_no_proposals_either(monkeypatch) -> None:
    """The quiet path must survive the new key being absent or empty."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "count": 0,
                "oldest_hours": 0.0,
                "tasks": [],
                "plans": [],
                "proposal_count": 0,
                "proposals": [],
            },
        )

    _patch_client(monkeypatch, handler)
    result = runner.invoke(app, ["pending"])

    assert result.exit_code == 0
    assert "Nothing awaiting approval" in result.stdout
