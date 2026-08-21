"""The improvement loop must reason about the TARGET repo, or not at all.

Walkthrough #7, 2026-08-21. Registering `playground` (seven files of helper
functions) and letting one plan complete produced five proposed tasks: hash auth
tokens with bcrypt, add a transaction context manager to the Database class, add
a Content-Security-Policy header to the Caddyfile, rate-limit the auth
endpoints, add a foundational test suite. `playground` has no Caddyfile, no
Database class and no auth endpoints. Every proposal describes Praxis itself.

The cause was an absent input, not a bad prompt: `check_improvements` built its
entire summary from three strings and cloned nothing, so the only codebase in
the planner's context was the one it could see.

These tests are anchored on the SUMMARY STRING actually handed to
`analyze_improvements`, which is the seam that was empty. Asserting that a
survey function exists, or that some clone happened, would both pass while the
planner still received three lines: that is the `unit-green-seam-inert` shape
this codebase has been bitten by before.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from orchestrator.core.event_bus import EventBus
from orchestrator.core.orchestrator import Orchestrator
from orchestrator.core.task_queue import TaskQueue
from orchestrator.database import Database
from orchestrator.models.schemas import TaskStatus


PROJECT_ID = "proj-improve"
REPO_URL = "https://github.com/adiatmaja/playground"

_SURVEY = """Repository contents (3 files):
  README.md
  src/playground/greet.py
  src/playground/duration.py

--- README.md ---
# playground

Scratch repo for Praxis e2e runs.
"""


class _Reader:
    """Stands in for BrainstormManager, which owns clone-and-read."""

    def __init__(self, survey: str | None = _SURVEY, boom: bool = False) -> None:
        self._survey = survey
        self._boom = boom
        self.calls: list[str] = []

    async def survey_repo(self, repo_url: str) -> str:
        self.calls.append(repo_url)
        if self._boom:
            msg = "clone failed: repository not found"
            raise RuntimeError(msg)
        assert self._survey is not None
        return self._survey


async def _seed(db: Database) -> tuple[TaskQueue, str]:
    task_queue = TaskQueue(db)
    await db.execute(
        "INSERT INTO users (id, name, token_hash) VALUES (?, ?, ?)",
        ("u-improve", "User", "hash"),
    )
    await db.execute(
        "INSERT INTO projects (id, user_id, name, repo_url, model_name) "
        "VALUES (?, 'u-improve', 'playground', ?, 'qwen3.8-27b')",
        (PROJECT_ID, REPO_URL),
    )
    plan_id = await task_queue.create_plan(PROJECT_ID, source="user")
    await task_queue.activate_plan(
        plan_id,
        {
            "plan_summary": "s",
            "plan_slug": "s",
            "tasks": [{"title": "t", "slug": "t", "description": "d"}],
        },
        "plan/x",
    )
    rows = await db.fetch_all("SELECT id FROM tasks WHERE plan_id = ?", (plan_id,))
    for row in rows:
        await task_queue.update_task_status(row["id"], TaskStatus.MERGED)
    return task_queue, plan_id


def _project() -> dict[str, Any]:
    return {
        "id": PROJECT_ID,
        "name": "playground",
        "repo_url": REPO_URL,
        "confidence_threshold": 0.7,
        "approval_gate": True,
    }


def _orchestrator(task_queue: TaskQueue, opus: Any, reader: Any) -> Orchestrator:
    return Orchestrator(
        task_queue=task_queue,
        agent_manager=MagicMock(),
        opus_bridge=opus,
        git_ops=AsyncMock(),
        event_bus=EventBus(),
        spec_reader=reader,
    )


def _opus() -> AsyncMock:
    opus = AsyncMock()
    opus.is_available.return_value = True
    opus.analyze_improvements.return_value = {
        "confidence": 0.9,
        "reason": "r",
        "proposed_tasks": [{"title": "t", "slug": "t", "description": "d"}],
    }
    return opus


@pytest.mark.integration
async def test_the_planner_receives_the_repository_not_just_its_name(
    db: Database,
) -> None:
    """The defect itself: assert on what reached the planner.

    A test that only checked `reader.calls` would pass on an implementation
    that surveyed the repo and then threw the result away.
    """
    task_queue, plan_id = await _seed(db)
    opus = _opus()
    reader = _Reader()

    result = await _orchestrator(task_queue, opus, reader).check_improvements(
        plan_id, _project()
    )

    assert result is not None
    summary = opus.analyze_improvements.await_args.args[0]
    assert "src/playground/greet.py" in summary, (
        "the planner must be given the repository's real files; got:\n" + summary
    )
    assert "Scratch repo for Praxis e2e runs." in summary
    assert reader.calls == [REPO_URL], "the survey must be of the TARGET repo"


@pytest.mark.integration
async def test_no_repository_context_means_no_proposal(db: Database) -> None:
    """Fail CLOSED. Proceeding without the repo is what produced the defect.

    The alternative, falling back to the old three-line summary, reproduces
    walkthrough #7 exactly whenever a clone fails. There is no basis for
    proposing work on a codebase that could not be read, so the loop proposes
    nothing and says why.
    """
    task_queue, plan_id = await _seed(db)
    opus = _opus()
    reader = _Reader(boom=True)

    result = await _orchestrator(task_queue, opus, reader).check_improvements(
        plan_id, _project()
    )

    assert result is None
    opus.analyze_improvements.assert_not_awaited()


@pytest.mark.integration
async def test_an_unconfigured_reader_also_means_no_proposal(db: Database) -> None:
    """Same rule for "no reader wired" as for "the clone failed".

    Worth its own test because it is a DIFFERENT code path, and because it is
    the configuration every pre-fix unit test happened to use: constructing the
    Orchestrator with no spec_reader used to analyse happily.
    """
    task_queue, plan_id = await _seed(db)
    opus = _opus()

    result = await _orchestrator(task_queue, opus, None).check_improvements(
        plan_id, _project()
    )

    assert result is None
    opus.analyze_improvements.assert_not_awaited()


@pytest.mark.integration
async def test_an_empty_survey_string_is_treated_as_no_context(db: Database) -> None:
    """A blank survey is a failure wearing a success's clothes.

    `build_repo_survey` never returns "" (an empty repo yields a positive "no
    files" line), so a blank string here means something upstream went wrong
    silently, and silence must not buy a proposal.
    """
    task_queue, plan_id = await _seed(db)
    opus = _opus()

    result = await _orchestrator(
        task_queue, opus, _Reader(survey="   \n  ")
    ).check_improvements(plan_id, _project())

    assert result is None
    opus.analyze_improvements.assert_not_awaited()


@pytest.mark.integration
async def test_the_summary_still_carries_the_project_identity(db: Database) -> None:
    """The survey ADDS to the old summary, it does not replace it.

    Name, repo URL and the completed plan are still the framing the planner
    needs to know what was just done; they were simply never sufficient alone.
    """
    task_queue, plan_id = await _seed(db)
    opus = _opus()

    await _orchestrator(task_queue, opus, _Reader()).check_improvements(
        plan_id, _project()
    )

    summary = opus.analyze_improvements.await_args.args[0]
    assert "playground" in summary
    assert REPO_URL in summary
