"""Task queue lifecycle tests."""
# ruff: noqa: S101

from __future__ import annotations

from typing import Any

import pytest

from orchestrator.core.task_queue import TaskQueue
from orchestrator.database import Database
from orchestrator.models.schemas import PlanStatus, TaskStatus


async def _seed_user_and_project(db: Database) -> tuple[str, str]:
    await db.execute(
        "INSERT INTO users (id, name, token_hash) VALUES (?, ?, ?)",
        ("u1", "TestUser", "hash"),
    )
    await db.execute(
        """INSERT INTO projects (id, user_id, name, repo_url, model_name)
           VALUES (?, ?, ?, ?, ?)""",
        ("p1", "u1", "TestProject", "https://github.com/test/repo", "deepseek"),
    )
    return ("u1", "p1")


async def _activate_test_plan(
    db: Database, opus_plan: dict[str, Any] | None = None
) -> tuple[TaskQueue, str]:
    _, project_id = await _seed_user_and_project(db)
    queue = TaskQueue(db)
    plan_id = await queue.create_plan(project_id, "Test")
    await queue.activate_plan(
        plan_id,
        opus_plan
        or {
            "plan_summary": "Test",
            "plan_slug": "test",
            "tasks": [
                {
                    "title": "Task1",
                    "slug": "task1",
                    "description": "Do it",
                    "depends_on": [],
                }
            ],
        },
        "plan/2026-06-01-test",
    )
    return queue, plan_id


@pytest.mark.integration
async def test_create_plan(db: Database) -> None:
    _, project_id = await _seed_user_and_project(db)
    queue = TaskQueue(db)
    plan_id = await queue.create_plan(project_id)

    plan = await queue.get_plan(plan_id)

    assert plan is not None
    assert plan["status"] == PlanStatus.PENDING
    assert plan["source"] == "user"


@pytest.mark.integration
async def test_activate_plan_with_tasks(db: Database) -> None:
    _, project_id = await _seed_user_and_project(db)
    queue = TaskQueue(db)
    plan_id = await queue.create_plan(project_id, "Build auth")
    opus_plan = {
        "plan_summary": "Auth system",
        "plan_slug": "auth",
        "tasks": [
            {
                "title": "Login",
                "slug": "login",
                "description": "Build login",
                "depends_on": [],
            },
            {
                "title": "Signup",
                "slug": "signup",
                "description": "Build signup",
                "depends_on": ["login"],
            },
        ],
    }

    await queue.activate_plan(plan_id, opus_plan, "plan/2026-06-01-auth")
    plan = await queue.get_plan(plan_id)
    tasks = await queue.get_tasks_for_plan(plan_id)

    assert plan is not None
    assert plan["status"] == PlanStatus.ACTIVE
    assert plan["plan_branch_name"] == "plan/2026-06-01-auth"
    assert len(tasks) == 2
    assert tasks[0]["title"] == "Login"
    assert tasks[1]["branch_name"] == "agent/signup"


@pytest.mark.integration
async def test_update_plan_status(db: Database) -> None:
    _, project_id = await _seed_user_and_project(db)
    queue = TaskQueue(db)
    plan_id = await queue.create_plan(project_id, "Simple task")

    await queue.update_plan_status(plan_id, PlanStatus.COMPLETED)
    plan = await queue.get_plan(plan_id)

    assert plan is not None
    assert plan["status"] == PlanStatus.COMPLETED


@pytest.mark.integration
async def test_task_status_transitions(db: Database) -> None:
    queue, plan_id = await _activate_test_plan(db)
    task_id = (await queue.get_tasks_for_plan(plan_id))[0]["id"]

    await queue.update_task_status(task_id, TaskStatus.IN_PROGRESS)
    assert (await queue.get_task(task_id))["status"] == TaskStatus.IN_PROGRESS  # type: ignore[index]
    await queue.update_task_status(task_id, TaskStatus.REVIEWING)
    assert (await queue.get_task(task_id))["status"] == TaskStatus.REVIEWING  # type: ignore[index]
    await queue.update_task_status(task_id, TaskStatus.PASSED)
    assert (await queue.get_task(task_id))["status"] == TaskStatus.PASSED  # type: ignore[index]


@pytest.mark.integration
async def test_fail_and_retry_task(db: Database) -> None:
    queue, plan_id = await _activate_test_plan(db)
    task_id = (await queue.get_tasks_for_plan(plan_id))[0]["id"]

    await queue.fail_task(task_id, "Missing validation")
    failed = await queue.get_task(task_id)
    await queue.retry_task(task_id)
    retried = await queue.get_task(task_id)

    assert failed is not None
    assert failed["status"] == TaskStatus.FAILED
    assert failed["review_feedback"] == "Missing validation"
    assert failed["attempt"] == 1
    assert retried is not None
    assert retried["status"] == TaskStatus.PENDING
    assert retried["attempt"] == 2


@pytest.mark.integration
async def test_set_pr_url(db: Database) -> None:
    queue, plan_id = await _activate_test_plan(db)
    task_id = (await queue.get_tasks_for_plan(plan_id))[0]["id"]

    await queue.set_task_pr_url(task_id, "https://github.com/user/repo/pull/1")
    task = await queue.get_task(task_id)

    assert task is not None
    assert task["pr_url"] == "https://github.com/user/repo/pull/1"


@pytest.mark.integration
async def test_get_dispatchable_tasks_respects_dependencies(db: Database) -> None:
    opus_plan = {
        "plan_summary": "Test",
        "plan_slug": "test",
        "tasks": [
            {
                "title": "Task1",
                "slug": "task1",
                "description": "First",
                "depends_on": [],
            },
            {
                "title": "Task2",
                "slug": "task2",
                "description": "Second",
                "depends_on": ["task1"],
            },
        ],
    }
    queue, plan_id = await _activate_test_plan(db, opus_plan)

    dispatchable = await queue.get_dispatchable_tasks(plan_id)
    tasks = await queue.get_tasks_for_plan(plan_id)
    await queue.update_task_status(tasks[0]["id"], TaskStatus.MERGED)
    after_merge = await queue.get_dispatchable_tasks(plan_id)

    assert [task["title"] for task in dispatchable] == ["Task1"]
    assert [task["title"] for task in after_merge] == ["Task2"]


@pytest.mark.integration
async def test_all_tasks_done(db: Database) -> None:
    queue, plan_id = await _activate_test_plan(db)
    task_id = (await queue.get_tasks_for_plan(plan_id))[0]["id"]

    assert await queue.all_tasks_done(plan_id) is False
    await queue.update_task_status(task_id, TaskStatus.MERGED)

    assert await queue.all_tasks_done(plan_id) is True


@pytest.mark.integration
async def test_agent_run_lifecycle(db: Database) -> None:
    queue, plan_id = await _activate_test_plan(db)
    task_id = (await queue.get_tasks_for_plan(plan_id))[0]["id"]

    run_id = await queue.create_agent_run(task_id, "container_abc123")
    await queue.complete_agent_run(run_id, "completed", "All done\nLog line 2")
    run = await queue.get_agent_run(run_id)
    runs = await queue.get_runs_for_task(task_id)

    assert run is not None
    assert run["container_id"] == "container_abc123"
    assert run["status"] == "completed"
    assert run["logs"] == "All done\nLog line 2"
    assert run["finished_at"] is not None
    assert len(runs) == 1


@pytest.mark.integration
async def test_get_running_runs(db: Database) -> None:
    queue, plan_id = await _activate_test_plan(db)
    task_id = (await queue.get_tasks_for_plan(plan_id))[0]["id"]

    running_id = await queue.create_agent_run(task_id, "container_running")
    done_id = await queue.create_agent_run(task_id, "container_done")
    await queue.complete_agent_run(done_id, "completed", "ok")

    running = await queue.get_running_runs()

    assert [run["id"] for run in running] == [running_id]


@pytest.mark.integration
async def test_update_agent_run_logs_keeps_running(db: Database) -> None:
    queue, plan_id = await _activate_test_plan(db)
    task_id = (await queue.get_tasks_for_plan(plan_id))[0]["id"]
    run_id = await queue.create_agent_run(task_id, "container_xyz")

    await queue.update_agent_run_logs(run_id, "partial log output")
    run = await queue.get_agent_run(run_id)

    assert run is not None
    assert run["logs"] == "partial log output"
    assert run["status"] == "running"
    assert run["finished_at"] is None


@pytest.mark.integration
async def test_mark_passed_sets_status_and_feedback(db: Database) -> None:
    queue, plan_id = await _activate_test_plan(db)
    task_id = (await queue.get_tasks_for_plan(plan_id))[0]["id"]

    await queue.mark_passed(task_id, "looks good")
    task = await queue.get_task(task_id)

    assert task is not None
    assert task["status"] == TaskStatus.PASSED
    assert task["review_feedback"] == "looks good"


@pytest.mark.integration
async def test_mark_merged_sets_status_and_approved_at(db: Database) -> None:
    queue, plan_id = await _activate_test_plan(db)
    task_id = (await queue.get_tasks_for_plan(plan_id))[0]["id"]

    await queue.mark_merged(task_id)
    task = await queue.get_task(task_id)

    assert task is not None
    assert task["status"] == TaskStatus.MERGED
    assert task["approved_at"] is not None


@pytest.mark.integration
async def test_passed_task_does_not_unblock_dependents(db: Database) -> None:
    """A dependent task stays blocked until its upstream is MERGED, not PASSED."""
    opus_plan = {
        "plan_summary": "Test",
        "plan_slug": "test",
        "tasks": [
            {"title": "A", "description": "a", "slug": "a", "depends_on": []},
            {"title": "B", "description": "b", "slug": "b", "depends_on": ["a"]},
        ],
    }
    queue, plan_id = await _activate_test_plan(db, opus_plan)

    tasks = await queue.get_tasks_for_plan(plan_id)
    task_a = next(t for t in tasks if t["title"] == "A")

    await queue.mark_passed(task_a["id"], "ok")
    dispatchable = await queue.get_dispatchable_tasks(plan_id)
    assert all(t["title"] != "B" for t in dispatchable), (
        "B must stay blocked while A is only PASSED, not MERGED"
    )

    await queue.mark_merged(task_a["id"])
    dispatchable = await queue.get_dispatchable_tasks(plan_id)
    assert any(t["title"] == "B" for t in dispatchable), (
        "B must become dispatchable once A is MERGED"
    )


@pytest.mark.integration
async def test_mark_needs_clarification_parks_without_burning_attempt(
    db: Database,
) -> None:
    queue, plan_id = await _activate_test_plan(db)
    tasks = await queue.get_tasks_for_plan(plan_id)
    task_id = tasks[0]["id"]
    before = tasks[0]["attempt"]
    await queue.mark_needs_clarification(
        task_id, "Which config file holds the API base?"
    )
    task = await queue.get_task(task_id)
    assert task is not None
    assert task["status"] == TaskStatus.NEEDS_CLARIFICATION
    assert task["clarification_question"] == "Which config file holds the API base?"
    assert task["clarification_state"] == "asked"
    assert task["attempt"] == before


@pytest.mark.integration
async def test_record_clarification_answer_requeues_with_progress_note(
    db: Database,
) -> None:
    queue, plan_id = await _activate_test_plan(db)
    tasks = await queue.get_tasks_for_plan(plan_id)
    task_id = tasks[0]["id"]
    before_attempt = tasks[0]["attempt"]
    await queue.mark_needs_clarification(task_id, "Which config file?")
    await queue.record_clarification_answer(
        task_id, "Use config/praxis.yaml", state="answered_by_brain"
    )
    task = await queue.get_task(task_id)
    assert task is not None
    assert task["status"] == TaskStatus.PENDING
    assert task["clarification_answer"] == "Use config/praxis.yaml"
    assert task["clarification_state"] == "answered_by_brain"
    assert "Which config file?" in task["progress_note"]
    assert "Use config/praxis.yaml" in task["progress_note"]
    assert task["attempt"] == before_attempt + 1
