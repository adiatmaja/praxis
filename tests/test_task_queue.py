"""Task queue lifecycle tests."""
# ruff: noqa: S101

from __future__ import annotations

import json
import logging
from typing import Any

import pytest

from orchestrator.core.task_queue import TaskQueue
from orchestrator.database import Database
from orchestrator.models.schemas import PlanStatus, TaskStatus


QUEUE_LOGGER = "orchestrator.core.task_queue"


def _graph(*tasks: dict[str, Any]) -> dict[str, Any]:
    """Wrap task dicts in the ``opus_plan`` envelope ``activate_plan`` expects."""
    return {"plan_summary": "T", "plan_slug": "t", "tasks": list(tasks)}


def _leaf(slug: str, title: str, depends_on: list[str] | None = None) -> dict[str, Any]:
    """One graph entry, named so a test can tell the ROWS apart by title."""
    return {
        "title": title,
        "slug": slug,
        "description": f"do {title}",
        "depends_on": depends_on or [],
    }


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


@pytest.mark.integration
async def test_dispatchable_raises_on_dangling_dependency(db: Database) -> None:
    """A task with depends_on referencing an unknown slug raises ValueError."""
    opus_plan = {
        "plan_summary": "Test",
        "plan_slug": "test",
        "tasks": [
            {"title": "A", "description": "a", "slug": "a", "depends_on": []},
            {
                "title": "B",
                "description": "b",
                "slug": "b",
                "depends_on": ["nonexistent"],
            },
        ],
    }
    queue, plan_id = await _activate_test_plan(db, opus_plan)

    with pytest.raises(ValueError, match="dangling dependency"):
        await queue.get_dispatchable_tasks(plan_id)


# --- a repeated slug must not collapse the positional graph-to-row map --------


_DUPLICATE_GRAPH = _graph(
    _leaf("add-tests", "first"),
    _leaf("add-tests", "second"),
    _leaf("wire-it", "third", depends_on=["add-tests"]),
)


@pytest.mark.integration
async def test_duplicate_slugs_do_not_collapse_the_positional_map(
    db: Database,
) -> None:
    """Two entries sharing a slug are still two rows, each returned once.

    The graph and the rows are paired by POSITION. Re-keying that pairing into
    a slug -> row dict made it non-injective the moment a slug repeated: the
    EARLIER row became unreachable and stayed PENDING forever (so
    ``all_tasks_done`` never turned true and ``plan_stalled``, which needs a
    FAILED task, never fired either), while the LATER row came back once per
    duplicate, which puts two workers on one branch and widens both ends of
    per-task ``review_base_sha`` scoping.
    """
    queue, plan_id = await _activate_test_plan(db, _DUPLICATE_GRAPH)
    rows = await queue.get_tasks_for_plan(plan_id)

    dispatchable = await queue.get_dispatchable_tasks(plan_id)
    returned = [task["id"] for task in dispatchable]

    assert returned == [rows[0]["id"], rows[1]["id"]], (
        "both rows carrying the repeated slug must be dispatchable, in graph order"
    )
    assert len(set(returned)) == len(returned), "a row was returned more than once"


@pytest.mark.integration
async def test_a_dependency_on_a_repeated_slug_waits_for_every_row(
    db: Database,
) -> None:
    """Nothing records which row an edge onto a repeated slug meant, so all count.

    Satisfying the edge from whichever row the map happened to keep dispatched
    a leaf onto work that had never been built.
    """
    queue, plan_id = await _activate_test_plan(db, _DUPLICATE_GRAPH)
    rows = await queue.get_tasks_for_plan(plan_id)

    await queue.update_task_status(rows[1]["id"], TaskStatus.MERGED)
    half_done = await queue.get_dispatchable_tasks(plan_id)
    assert [task["id"] for task in half_done] == [rows[0]["id"]], (
        "'third' must stay blocked while one row carrying 'add-tests' is pending"
    )

    await queue.update_task_status(rows[0]["id"], TaskStatus.MERGED)
    all_done = await queue.get_dispatchable_tasks(plan_id)
    assert [task["id"] for task in all_done] == [rows[2]["id"]]

    await queue.update_task_status(rows[2]["id"], TaskStatus.MERGED)
    assert await queue.all_tasks_done(plan_id) is True, (
        "an orphaned row leaves the plan ACTIVE and looking healthy forever"
    )


# --- a malformed or half-written graph must not abort the pass for every plan --


async def _activate_then_corrupt(
    db: Database, graph: dict[str, Any], corrupted: dict[str, Any]
) -> tuple[TaskQueue, str]:
    """Activate a well-formed graph, then overwrite it with a broken one.

    Two steps because ``activate_plan`` reads ``title``/``description``/``slug``
    off every entry to write the rows: the states below are ones a crash or a
    hand-edit leaves behind on a plan whose rows already exist.
    """
    queue, plan_id = await _activate_test_plan(db, graph)
    await db.execute(
        "UPDATE plans SET opus_plan = ? WHERE id = ?",
        (json.dumps(corrupted), plan_id),
    )
    return queue, plan_id


@pytest.mark.integration
async def test_a_graph_with_no_tasks_list_does_not_abort_the_dispatch_pass(
    db: Database, caplog: pytest.LogCaptureFixture
) -> None:
    """``run_once`` has no per-plan try/except, so a KeyError here stops them all."""
    queue, plan_id = await _activate_then_corrupt(
        db, _graph(_leaf("a", "A")), {"plan_summary": "T"}
    )

    with caplog.at_level(logging.WARNING, logger=QUEUE_LOGGER):
        dispatchable = await queue.get_dispatchable_tasks(plan_id)

    assert dispatchable == []
    assert "no 'tasks' list" in caplog.text, "an unrunnable plan must say so"


@pytest.mark.integration
async def test_a_graph_entry_with_no_slug_is_skipped_without_shifting_the_rest(
    db: Database, caplog: pytest.LogCaptureFixture
) -> None:
    """Skipping must cost no alignment: entry 1 still belongs to ROW 1.

    Filtering the malformed entry out of the list instead would slide entry 1
    onto row 0, which is the same silent mis-association the positional map
    exists to prevent.
    """
    queue, plan_id = await _activate_then_corrupt(
        db,
        _graph(_leaf("a", "A"), _leaf("b", "B")),
        {"tasks": [{"title": "A", "description": "a"}, _leaf("b", "B")]},
    )
    rows = await queue.get_tasks_for_plan(plan_id)

    with caplog.at_level(logging.WARNING, logger=QUEUE_LOGGER):
        dispatchable = await queue.get_dispatchable_tasks(plan_id)

    assert [task["id"] for task in dispatchable] == [rows[1]["id"]]
    assert "no usable slug" in caplog.text


@pytest.mark.integration
async def test_a_dependency_whose_row_is_unwritten_holds_instead_of_raising(
    db: Database, caplog: pytest.LogCaptureFixture
) -> None:
    """A graph longer than its rows means "not written yet", not "dangling".

    ``activate_plan`` writes the graph BEFORE the rows, so a crash between the
    two leaves exactly this state, and raising on it wedged dispatch for every
    runnable plan on every tick with no recovery but a hand-edit.
    """
    queue, plan_id = await _activate_then_corrupt(
        db,
        _graph(_leaf("a", "A")),
        _graph(
            _leaf("a", "A"),
            _leaf("a-s1", "A one"),
            _leaf("a-s2", "A two", depends_on=["a-s1"]),
        ),
    )
    rows = await queue.get_tasks_for_plan(plan_id)

    with caplog.at_level(logging.WARNING, logger=QUEUE_LOGGER):
        dispatchable = await queue.get_dispatchable_tasks(plan_id)

    assert [task["id"] for task in dispatchable] == [rows[0]["id"]]
    assert "no task row yet" in caplog.text


@pytest.mark.integration
async def test_a_depends_on_that_is_a_bare_string_does_not_abort_the_pass(
    db: Database, caplog: pytest.LogCaptureFixture
) -> None:
    """The fourth exit of the same class, and it is reachable on the plan path.

    ``Orchestrator._validate_plan_shape`` checks that each task carries the
    required KEYS, never that ``depends_on`` is a list, so a planner answering
    ``"depends_on": "alpha"`` activates cleanly. Iterating a string then yields
    one CHARACTER per dependency, and the first one that names no slug raises,
    which aborts the pass for every runnable plan on every tick.
    """
    queue, plan_id = await _activate_then_corrupt(
        db,
        _graph(_leaf("alpha", "Alpha"), _leaf("beta", "Beta")),
        {
            "tasks": [
                _leaf("alpha", "Alpha"),
                {
                    "title": "Beta",
                    "slug": "beta",
                    "description": "b",
                    "depends_on": "alpha",
                },
            ]
        },
    )
    rows = await queue.get_tasks_for_plan(plan_id)

    with caplog.at_level(logging.WARNING, logger=QUEUE_LOGGER):
        dispatchable = await queue.get_dispatchable_tasks(plan_id)

    # The unreadable edge is discarded, not obeyed one character at a time.
    assert [task["id"] for task in dispatchable] == [rows[0]["id"], rows[1]["id"]]
    assert "not a list" in caplog.text


@pytest.mark.integration
async def test_a_well_formed_graph_still_dispatches_in_dependency_order(
    db: Database, caplog: pytest.LogCaptureFixture
) -> None:
    """Positive control for the four guards above.

    Every one of them is a refusal, and a refusal that fired on ordinary input
    would pass its own test while silently stopping every plan in the product.
    This asserts the untouched path: unique slugs, one row per entry, a real
    edge honoured, and NOT ONE of the warnings raised.
    """
    queue, plan_id = await _activate_test_plan(
        db, _graph(_leaf("a", "A"), _leaf("b", "B", depends_on=["a"]))
    )
    rows = await queue.get_tasks_for_plan(plan_id)

    with caplog.at_level(logging.WARNING, logger=QUEUE_LOGGER):
        first = await queue.get_dispatchable_tasks(plan_id)
        await queue.update_task_status(rows[0]["id"], TaskStatus.MERGED)
        second = await queue.get_dispatchable_tasks(plan_id)

    assert [task["id"] for task in first] == [rows[0]["id"]]
    assert [task["id"] for task in second] == [rows[1]["id"]]
    # Filtered by LEVEL, not just by logger: activate_plan logs at INFO on the
    # same logger, so a bare `caplog.text == ""` could never hold and the
    # control would be reporting on its own setup instead of on dispatch.
    complaints = [
        record.getMessage()
        for record in caplog.records
        if record.levelno >= logging.WARNING
    ]
    assert complaints == [], f"a healthy plan tripped a guard: {complaints}"


# --- needs_stronger_model must reach the row, not just the schema -------------


@pytest.mark.integration
async def test_activate_plan_persists_needs_stronger_model(db: Database) -> None:
    """The brain sets this flag and every read surface publishes it.

    While it was missing from the INSERT tuple, the CLI, the dashboard and MCP
    all reported False for every task on every install. The column's own test
    asserts only that it EXISTS, which is true whether or not anything writes.
    """
    queue, plan_id = await _activate_test_plan(
        db,
        _graph(
            {**_leaf("hard", "Hard"), "needs_stronger_model": True},
            {**_leaf("easy", "Easy"), "needs_stronger_model": False},
            _leaf("quiet", "Quiet"),
        ),
    )
    rows = await queue.get_tasks_for_plan(plan_id)

    assert bool(rows[0]["needs_stronger_model"]) is True
    # The two negatives are the control: without them the assertion above
    # passes just as well against a hardcoded 1.
    assert bool(rows[1]["needs_stronger_model"]) is False
    assert bool(rows[2]["needs_stronger_model"]) is False


@pytest.mark.integration
async def test_activate_plan_coerces_a_flag_the_brain_wrote_as_text(
    db: Database,
) -> None:
    """``needs_stronger_model`` is INTEGER, but SQLite affinity is advisory.

    The string "false" would be stored verbatim and read back TRUTHY by every
    consumer that does not re-parse it, which is the same trap
    ``difficulty_score`` documents one field earlier.
    """
    queue, plan_id = await _activate_test_plan(
        db,
        _graph(
            {**_leaf("t", "Text true"), "needs_stronger_model": "true"},
            {**_leaf("f", "Text false"), "needs_stronger_model": "false"},
        ),
    )
    rows = await queue.get_tasks_for_plan(plan_id)

    assert bool(rows[0]["needs_stronger_model"]) is True
    assert bool(rows[1]["needs_stronger_model"]) is False
