"""The progress handover has to read a repository that actually exists.

This is a seam test, deliberately. Both ends were individually correct for as
long as the feature has existed: ``render_handover`` renders a checklist
faithfully, and ``branch_commit_log`` reads a branch faithfully. What was wrong
was the ARGUMENT between them. ``_build_worker_bible`` called
``branch_commit_log(".", ...)``, and inside the orchestrator container ``.`` is
``/app``: no ``.git``, and no clone of the target repo anywhere on the
filesystem. So the call raised on every dispatch and was swallowed into an
empty list.

Nothing caught it because every existing test mocks the reader to ``[]``, which
is exactly what the broken production path also produced. The observable cost:
a re-dispatched worker was told nothing had been done and redid completed work,
while the Bible told it that per-item commits were how progress survived a
restart. Under bare uvicorn it was worse than empty, because ``.`` is the
Praxis repo and the refspec resolved against Praxis's own branches.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from orchestrator.core.event_bus import EventBus
from orchestrator.core.orchestrator import Orchestrator
from orchestrator.core.progress_handover import ChecklistItem, Commit, render_handover
from orchestrator.core.task_queue import TaskQueue
from orchestrator.database import Database


REPO_URL = "https://github.com/u/a"


async def _setup(db: Database, attempt: int = 1) -> tuple[TaskQueue, str, dict]:
    await db.execute(
        "INSERT INTO users (id, name, token_hash) VALUES (?, ?, ?)",
        ("u1", "User", "hash"),
    )
    await db.execute(
        """INSERT INTO projects
             (id, user_id, name, repo_url, model_name, max_retries)
           VALUES (?, ?, ?, ?, ?, ?)""",
        ("p1", "u1", "App", REPO_URL, "deepseek", 3),
    )
    task_queue = TaskQueue(db)
    plan_id = await task_queue.create_plan("p1", "Build auth")
    await task_queue.activate_plan(
        plan_id,
        {
            "plan_summary": "Auth",
            "plan_slug": "auth",
            "tasks": [
                {
                    "title": "Login",
                    "slug": "login",
                    "description": "Build login",
                    "depends_on": [],
                }
            ],
        },
        "plan/2026-06-01-auth",
    )
    task_id = (await task_queue.get_tasks_for_plan(plan_id))[0]["id"]
    if attempt != 1:
        await db.execute(
            "UPDATE tasks SET attempt = ? WHERE id = ?", (attempt, task_id)
        )
    project = await db.fetch_one("SELECT * FROM projects WHERE id = 'p1'")
    assert project is not None
    return task_queue, plan_id, project


async def _dispatch(
    db: Database, task_queue: TaskQueue, plan_id: str, project: dict, git: Any
) -> str:
    agents = MagicMock()
    agents.spawn_agent = AsyncMock(return_value="container-123")
    orch = Orchestrator(
        task_queue=task_queue,
        agent_manager=agents,
        opus_bridge=AsyncMock(),
        git_ops=git,
        event_bus=EventBus(),
    )
    orch._start_monitor = lambda *_: None  # type: ignore[assignment, method-assign]
    orch._effective_settings = None
    await orch.dispatch_pending_tasks(plan_id, project)
    agents.spawn_agent.assert_called_once()
    return str(agents.spawn_agent.call_args.kwargs["bible_text"])


@pytest.mark.integration
async def test_the_handover_reads_the_target_repo_not_the_orchestrators_cwd(
    db: Database,
) -> None:
    """The argument is the defect, so the argument is what this asserts.

    Revert ``_build_worker_bible`` to ``branch_commit_log(".", ...)`` and only
    this goes red: every other test mocks the reader to ``[]``, which is
    indistinguishable from the broken path's swallowed failure.
    """
    task_queue, plan_id, project = await _setup(db)
    git = AsyncMock()
    git.remote_branch_commit_log = AsyncMock(
        return_value=[Commit(sha="abc1234def", subject="agent: Build login")]
    )

    bible = await _dispatch(db, task_queue, plan_id, project, git)

    git.remote_branch_commit_log.assert_awaited_once()
    args = git.remote_branch_commit_log.await_args.args
    assert args[0] == REPO_URL, "the history must be read from the TARGET repo"
    assert "." not in args[:1]
    # And the commit it found actually reached the worker, ticked.
    assert "[x] Build login (abc1234)" in bible


@pytest.mark.integration
async def test_a_first_attempt_with_no_branch_says_no_commits_yet(
    db: Database,
) -> None:
    """On attempt 1 the branch does not exist on the remote yet.

    An unreadable history there IS "nothing has been done", and saying
    "history unavailable, verify before redoing work" would send every first
    worker looking for work nobody has done.
    """
    task_queue, plan_id, project = await _setup(db, attempt=1)
    git = AsyncMock()
    git.remote_branch_commit_log = AsyncMock(side_effect=RuntimeError("404"))

    bible = await _dispatch(db, task_queue, plan_id, project, git)

    assert "no commits on this branch yet" in bible
    assert "commit history unavailable" not in bible


@pytest.mark.integration
async def test_a_retry_with_an_unreadable_history_says_so(db: Database) -> None:
    """On a re-dispatch the branch exists, so a failed read is genuinely unknown.

    Reporting that as "nothing done" is what makes a resumed worker redo
    completed work, which is the exact failure the handover exists to prevent.
    """
    task_queue, plan_id, project = await _setup(db, attempt=2)
    git = AsyncMock()
    git.remote_branch_commit_log = AsyncMock(side_effect=RuntimeError("gh: 500"))

    bible = await _dispatch(db, task_queue, plan_id, project, git)

    assert "commit history unavailable" in bible
    assert "no commits on this branch yet" not in bible


@pytest.mark.unit
def test_render_handover_distinguishes_all_three_states() -> None:
    """Unit-level companion, so the three headings cannot silently converge."""
    items = [ChecklistItem("Add model"), ChecklistItem("Add test")]
    assert "no commits on this branch yet" in render_handover(items, [], None)
    assert "commit history unavailable" in render_handover(items, None, None)
    done = render_handover(items, [Commit("aaaaaaa1", "Add model")], None)
    assert "resume here" in done
    assert "[x] Add model" in done
