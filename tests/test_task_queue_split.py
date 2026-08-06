"""Split insertion must preserve the positional opus_plan-to-row mapping.

``get_dispatchable_tasks`` zips ``opus_plan["tasks"]`` against
``get_tasks_for_plan`` (ordered by rowid) BY INDEX.  Children therefore append
to both, in the same order, and the superseded parent is never removed from
either.
"""

from __future__ import annotations

import json

import pytest

from orchestrator.core.task_queue import TaskQueue
from orchestrator.models.schemas import LeafTask, TaskStatus
from tests.conftest import seed_user


async def _seed(db) -> tuple[TaskQueue, str, list[str]]:
    """Seed a three-leaf chain plan (a -> b -> c) and return its row ids."""
    tq = TaskQueue(db)
    # PRAGMA foreign_keys is ON (database.py connect), so the user row must
    # exist before the project that references it.
    user_id = await seed_user(db)
    await db.execute(
        "INSERT INTO projects (id, user_id, name, repo_url, default_branch) "
        "VALUES (?, ?, 'p', 'https://github.com/o/r', 'main')",
        ("proj1", user_id),
    )
    # create_plan(project_id, summary=None, source="user", ...) -> plan_id
    plan_id = await tq.create_plan("proj1", "test")
    opus_plan = {
        "tasks": [
            {
                "id": "a",
                "slug": "a",
                "title": "A",
                "description": "A",
                "depends_on": [],
            },
            {
                "id": "b",
                "slug": "b",
                "title": "B",
                "description": "B",
                "depends_on": ["a"],
            },
            {
                "id": "c",
                "slug": "c",
                "title": "C",
                "description": "C",
                "depends_on": ["b"],
            },
        ]
    }
    await tq.activate_plan(plan_id, opus_plan, "plan/x")
    rows = await tq.get_tasks_for_plan(plan_id)
    return tq, plan_id, [row["id"] for row in rows]


def _children() -> list[LeafTask]:
    return [
        LeafTask(id="x1", title="B one", plan_text="Goal: one"),
        LeafTask(id="x2", title="B two", plan_text="Goal: two"),
    ]


@pytest.mark.unit
async def test_supersede_sets_the_status_and_records_the_decision(db):
    tq, _plan_id, ids = await _seed(db)
    await tq.supersede_task(ids[1], "split", "too big")
    task = await tq.get_task(ids[1])
    assert task["status"] == TaskStatus.SUPERSEDED
    assert task["triage_decision"] == "split"
    assert "too big" in task["review_feedback"]


@pytest.mark.unit
async def test_supersede_drops_the_worker_session_handle(db):
    tq, _plan_id, ids = await _seed(db)
    await tq.record_worker_session(ids[1], "sess-1", "opencode")
    await tq.supersede_task(ids[1], "split", "too big")
    task = await tq.get_task(ids[1])
    assert task["worker_session_id"] is None
    assert task["worker_session_harness"] is None


@pytest.mark.unit
async def test_record_triage_decision_without_superseding(db):
    tq, _plan_id, ids = await _seed(db)
    await tq.record_triage_decision(ids[1], "retry")
    task = await tq.get_task(ids[1])
    assert task["triage_decision"] == "retry"
    assert task["status"] != TaskStatus.SUPERSEDED


@pytest.mark.unit
async def test_insert_split_children_appends_rows_in_plan_order(db):
    tq, plan_id, ids = await _seed(db)
    await tq.insert_split_children(plan_id, ids[1], "b", _children())

    plan = await tq.get_plan(plan_id)
    graph = json.loads(plan["opus_plan"])
    rows = await tq.get_tasks_for_plan(plan_id)
    assert [t["slug"] for t in graph["tasks"]] == ["a", "b", "c", "b-s1", "b-s2"]
    assert [r["branch_name"] for r in rows] == [
        "agent/a",
        "agent/b",
        "agent/c",
        "agent/b-s1",
        "agent/b-s2",
    ]


@pytest.mark.unit
async def test_insert_split_children_returns_the_new_row_ids_in_append_order(db):
    tq, plan_id, ids = await _seed(db)
    new_ids = await tq.insert_split_children(plan_id, ids[1], "b", _children())
    rows = await tq.get_tasks_for_plan(plan_id)
    assert new_ids == [rows[3]["id"], rows[4]["id"]]


@pytest.mark.unit
async def test_split_children_carry_the_parent_id(db):
    tq, plan_id, ids = await _seed(db)
    await tq.insert_split_children(plan_id, ids[1], "b", _children())
    rows = await tq.get_tasks_for_plan(plan_id)
    assert rows[3]["parent_task_id"] == ids[1]
    assert rows[4]["parent_task_id"] == ids[1]


@pytest.mark.unit
async def test_split_children_start_with_a_reduced_retry_budget(db):
    tq, plan_id, ids = await _seed(db)
    await tq.insert_split_children(plan_id, ids[1], "b", _children())
    rows = await tq.get_tasks_for_plan(plan_id)
    # attempt starts at 2, so with max_retries 3 a child gets 2 tries, not 3.
    assert rows[3]["attempt"] == 2
    assert rows[4]["attempt"] == 2


@pytest.mark.unit
async def test_split_children_carry_their_leaf_type(db):
    tq, plan_id, ids = await _seed(db)
    children = [
        LeafTask(id="x1", title="B one", plan_text="Goal: one", leaf_type="test_add"),
        LeafTask(id="x2", title="B two", plan_text="Goal: two"),
    ]
    await tq.insert_split_children(plan_id, ids[1], "b", children)
    rows = await tq.get_tasks_for_plan(plan_id)
    assert rows[3]["leaf_type"] == "test_add"
    assert rows[4]["leaf_type"] == "generic"


@pytest.mark.unit
async def test_insert_split_children_rejects_a_second_split_of_the_same_parent(db):
    """The fail-closed guard in rewire_plan_for_split must propagate."""
    tq, plan_id, ids = await _seed(db)
    await tq.insert_split_children(plan_id, ids[1], "b", _children())
    with pytest.raises(ValueError, match="already been split"):
        await tq.insert_split_children(plan_id, ids[1], "b", _children())
    # The rejected call left no partial rows behind.
    rows = await tq.get_tasks_for_plan(plan_id)
    assert len(rows) == 5


@pytest.mark.unit
async def test_insert_split_children_rejects_a_plan_with_no_graph(db):
    tq, _plan_id, _ids = await _seed(db)
    bare_plan_id = await tq.create_plan("proj1", "no graph")
    with pytest.raises(ValueError, match="no task graph"):
        await tq.insert_split_children(bare_plan_id, "nope", "b", _children())


@pytest.mark.unit
async def test_a_failed_child_insert_never_wedges_the_plan(db, monkeypatch):
    """The child rows must be written BEFORE the graph, never after.

    ``Database.execute`` commits every statement on its own and ``Database``
    exposes no transaction API, so this method cannot be atomic.  Writing the
    graph first would leave the parent's dependents naming child slugs that have
    no row, and ``get_dispatchable_tasks`` raises ``ValueError`` on exactly that,
    on every orchestration tick, forever.  Writing the rows first can only ever
    leave surplus rows the graph does not name, which nothing reads.
    """
    tq, plan_id, ids = await _seed(db)
    real_execute = db.execute

    async def failing_execute(query, params=()):
        if query.lstrip().startswith("INSERT INTO tasks"):
            message = "simulated crash mid-write"
            raise RuntimeError(message)
        return await real_execute(query, params)

    monkeypatch.setattr(db, "execute", failing_execute)
    with pytest.raises(RuntimeError, match="simulated crash"):
        await tq.insert_split_children(plan_id, ids[1], "b", _children())
    monkeypatch.undo()

    # The graph must never name a child slug that has no row.
    plan = await tq.get_plan(plan_id)
    graph = json.loads(plan["opus_plan"])
    assert [t["slug"] for t in graph["tasks"]] == ["a", "b", "c"]
    # And the loop keeps turning rather than raising on every tick.
    branches = {
        task["branch_name"] for task in await tq.get_dispatchable_tasks(plan_id)
    }
    assert branches == {"agent/a"}


@pytest.mark.unit
async def test_a_superseded_parent_does_not_block_plan_completion(db):
    tq, plan_id, ids = await _seed(db)
    await tq.supersede_task(ids[1], "split", "too big")
    for task_id in (ids[0], ids[2]):
        await tq.update_task_status(task_id, TaskStatus.MERGED)
    assert await tq.all_tasks_done(plan_id) is True


@pytest.mark.unit
async def test_an_unfinished_task_still_blocks_plan_completion(db):
    """all_tasks_done must widen to SUPERSEDED without going vacuously true."""
    tq, plan_id, ids = await _seed(db)
    await tq.supersede_task(ids[1], "split", "too big")
    await tq.update_task_status(ids[0], TaskStatus.MERGED)
    assert await tq.all_tasks_done(plan_id) is False


@pytest.mark.unit
async def test_a_superseded_dependency_counts_as_satisfied(db):
    """A task whose dependency was superseded must still become dispatchable.

    A SUPERSEDED leaf can never reach MERGED, so a dependency predicate that
    only accepts MERGED deadlocks every dependent of it, silently and forever.
    """
    tq, plan_id, ids = await _seed(db)
    await tq.supersede_task(ids[0], "split", "too big")
    dispatchable = await tq.get_dispatchable_tasks(plan_id)
    branches = {task["branch_name"] for task in dispatchable}
    assert "agent/b" in branches
    # c still waits on b, which is neither merged nor superseded.
    assert "agent/c" not in branches


@pytest.mark.unit
async def test_a_pending_dependency_still_blocks_dispatch(db):
    """The widened predicate must not become "always true"."""
    tq, plan_id, _ids = await _seed(db)
    dispatchable = await tq.get_dispatchable_tasks(plan_id)
    branches = {task["branch_name"] for task in dispatchable}
    assert branches == {"agent/a"}


@pytest.mark.unit
async def test_a_child_of_a_superseded_parent_is_dispatchable_after_its_deps_merge(
    db,
):
    tq, plan_id, ids = await _seed(db)
    await tq.insert_split_children(plan_id, ids[1], "b", _children())
    await tq.supersede_task(ids[1], "split", "too big")
    await tq.update_task_status(ids[0], TaskStatus.MERGED)
    dispatchable = await tq.get_dispatchable_tasks(plan_id)
    slugs = {task["branch_name"] for task in dispatchable}
    assert "agent/b-s1" in slugs
    assert "agent/b-s2" in slugs
    # The superseded parent is never dispatchable again.
    assert "agent/b" not in slugs
    # The parent's dependent now waits on the children, not on the parent.
    assert "agent/c" not in slugs


@pytest.mark.unit
async def test_set_task_implementer_persists_the_escalated_pair(db):
    tq, _plan_id, ids = await _seed(db)
    await tq.set_task_implementer(ids[0], "agy", "gemini-3.6-flash-high", index=1)
    task = await tq.get_task(ids[0])
    assert task["implement_harness"] == "agy"
    assert task["implement_model"] == "gemini-3.6-flash-high"
    assert task["escalation_index"] == 1


@pytest.mark.unit
async def test_append_progress_note_accumulates_blocks(db):
    tq, _plan_id, ids = await _seed(db)
    await tq.append_progress_note(ids[0], "first")
    await tq.append_progress_note(ids[0], "second")
    task = await tq.get_task(ids[0])
    assert task["progress_note"] == "first\n\nsecond"


@pytest.mark.unit
async def test_append_progress_note_rejects_an_unknown_task(db):
    tq, _plan_id, _ids = await _seed(db)
    with pytest.raises(ValueError, match="not found"):
        await tq.append_progress_note("missing", "note")
