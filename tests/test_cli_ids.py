"""Every id the CLI prints must be an id the CLI will accept back.

`praxis add-project` prints a full project id exactly once, at creation time.
After that the only way to see one is `praxis projects`, and every consumer of
it (`configure`, `submit`, `plans`) looks the project up by exact match. The
same holds for `praxis tasks` feeding `praxis task`, `praxis stop`, and
`praxis merge`. A truncated id therefore does not merely read badly: it means
that from a new terminal the next day, the documented end-to-end path is
unreachable without curl.

This is the defect `plans` already had and Task 2 already fixed
(`test_plans_prints_the_full_plan_id`), one line away in the same file.
"""
# ruff: noqa: S101

from __future__ import annotations

import httpx
from typer.testing import CliRunner

from cli.main import app


runner = CliRunner()

PROJECT_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
TASK_ID = "8b1bafa2-e401-4b17-81c2-56b56c91c906"


def _patch_client(monkeypatch, handler) -> None:
    monkeypatch.setenv("ORCHESTRATOR_TOKEN", "t")
    # rich reads COLUMNS when stdout is not a tty (CliRunner's case). Pin a
    # width an ordinary terminal has, so the assertion is about the id being
    # printed WHOLE and not about how a 80-column table folds it.
    monkeypatch.setenv("COLUMNS", "160")

    def _fake_client(timeout: float = 60.0) -> httpx.Client:
        return httpx.Client(
            base_url="http://x",
            headers={"Authorization": "Bearer t"},
            transport=httpx.MockTransport(handler),
        )

    monkeypatch.setattr("cli.main._client", _fake_client)


def test_projects_prints_the_full_project_id(monkeypatch) -> None:
    """`praxis projects` must print an id `praxis submit` can actually use."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[
                {
                    "id": PROJECT_ID,
                    "name": "playground",
                    "repo_url": "https://github.com/adiatmaja/playground",
                    "model_name": "qwen3.8-30b",
                    "approval_gate": True,
                }
            ],
        )

    _patch_client(monkeypatch, handler)
    result = runner.invoke(app, ["projects"])

    assert result.exit_code == 0
    # The id may be FOLDED across lines by the table; it must not be CUT.
    flat = "".join(result.stdout.split())
    assert PROJECT_ID in flat


def test_tasks_prints_the_full_task_id(monkeypatch) -> None:
    """`praxis tasks` must print an id `praxis task`/`stop`/`merge` can use."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[
                {
                    "id": TASK_ID,
                    "title": "Implement initials() helper function",
                    "branch_name": "agent/implement-initials-function",
                    "status": "passed",
                    "attempt": 1,
                }
            ],
        )

    _patch_client(monkeypatch, handler)
    result = runner.invoke(app, ["tasks", "plan-1"])

    assert result.exit_code == 0
    flat = "".join(result.stdout.split())
    assert TASK_ID in flat
