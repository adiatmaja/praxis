"""A flagged leaf dispatches with a tightened pack and a visible flag.

Spec 2.3: between reject_below and flag_below the context pack is tightened to
priority order, an acceptance check becomes mandatory, and the flag is visible
on SSE.

The score has to survive three hops to be worth anything: the decomposer stamps
it on the plan graph, ``activate_plan`` writes it to the task row, and dispatch
reads that row back. The middle hop is the one that fails silently, so the test
that matters here drives a scored graph through ``activate_plan`` rather than
setting the column by hand.
"""

from typing import Any

import pytest

from orchestrator.core.orchestrator_dispatch import MANDATORY_ACCEPTANCE
from orchestrator.models.schemas import TaskStatus


def _configure(orch: Any, flag_below: float) -> None:
    """Pin the difficulty thresholds and keep the dispatch path hermetic.

    ``orchestrator_fixture`` hands out a bare ``AsyncMock`` for effective
    settings, so an unconfigured ``difficulty_config()`` yields a ``MagicMock``
    whose ``float()`` is 1.0: every scored leaf would read as flagged and an
    "unflagged" assertion would pass or fail for the wrong reason.
    """
    orch._effective_settings.difficulty_config.return_value = {
        "weights": {},
        "bias": 0.0,
        "reject_below": 0.35,
        "flag_below": flag_below,
    }
    orch._effective_settings.auto_delegate_enabled.return_value = False
    # Falsy URL short-circuits detect_context_limit, so no HTTP is attempted.
    orch._effective_settings.lm_studio_url.return_value = ""


def _graph(slug: str, **extra: Any) -> dict[str, Any]:
    task: dict[str, Any] = {
        "id": slug,
        "slug": slug,
        "title": slug.upper(),
        "description": "Add the widget",
        "depends_on": [],
    }
    task.update(extra)
    return {"tasks": [task]}


async def _activate(orch: Any, slug: str, **extra: Any) -> tuple[str, dict[str, Any]]:
    """Create and activate a one-leaf plan, returning (plan_id, task row)."""
    plan_id = await orch._tq.create_plan("proj1", "scored")
    await orch._tq.activate_plan(plan_id, _graph(slug, **extra), f"plan/{slug}")
    rows = await orch._tq.get_tasks_for_plan(plan_id)
    return plan_id, dict(rows[0])


def _last_dispatch(events: Any) -> dict[str, Any]:
    dispatched = [e for e in events if e.get("type") == "agent_dispatched"]
    assert dispatched, "dispatch never reached the spawn path"
    return dispatched[-1]


@pytest.mark.unit
async def test_a_flagged_leaf_publishes_the_flag_on_dispatch(
    orchestrator_fixture, captured_events
):
    orch, task_id, project = orchestrator_fixture
    _configure(orch, flag_below=0.55)
    await orch._tq._db.execute(
        "UPDATE tasks SET difficulty_score = 0.44 WHERE id = ?", (task_id,)
    )
    await orch._tq.update_task_status(task_id, TaskStatus.PENDING)
    task = await orch._tq.get_task(task_id)
    orch._agents.spawn_agent.return_value = "container-1"
    await orch.dispatch_pending_tasks(task["plan_id"], project)
    event = _last_dispatch(captured_events)
    assert event["difficulty_flagged"] is True
    assert event["difficulty_score"] == pytest.approx(0.44)


@pytest.mark.unit
async def test_an_unflagged_leaf_reports_flagged_false(
    orchestrator_fixture, captured_events
):
    orch, task_id, project = orchestrator_fixture
    _configure(orch, flag_below=0.55)
    await orch._tq._db.execute(
        "UPDATE tasks SET difficulty_score = 0.82 WHERE id = ?", (task_id,)
    )
    await orch._tq.update_task_status(task_id, TaskStatus.PENDING)
    task = await orch._tq.get_task(task_id)
    orch._agents.spawn_agent.return_value = "container-1"
    await orch.dispatch_pending_tasks(task["plan_id"], project)
    event = _last_dispatch(captured_events)
    assert event["difficulty_flagged"] is False
    assert event["difficulty_score"] == pytest.approx(0.82)


@pytest.mark.unit
async def test_an_unscored_leaf_is_not_flagged(orchestrator_fixture, captured_events):
    """A pre-Phase-C task row has a NULL score and must dispatch normally."""
    orch, task_id, project = orchestrator_fixture
    _configure(orch, flag_below=0.55)
    await orch._tq.update_task_status(task_id, TaskStatus.PENDING)
    task = await orch._tq.get_task(task_id)
    orch._agents.spawn_agent.return_value = "container-1"
    await orch.dispatch_pending_tasks(task["plan_id"], project)
    event = _last_dispatch(captured_events)
    assert event["difficulty_flagged"] is False
    assert event["difficulty_score"] is None


@pytest.mark.unit
async def test_the_flag_threshold_follows_the_configured_flag_below(
    orchestrator_fixture, captured_events
):
    """A score of 0.60 is clean at the 0.55 default and flagged at 0.70.

    Hardcoding the threshold would give an operator who raised ``flag_below`` a
    dashboard flag that disagrees with the gate that produced it.
    """
    orch, task_id, project = orchestrator_fixture
    _configure(orch, flag_below=0.70)
    await orch._tq._db.execute(
        "UPDATE tasks SET difficulty_score = 0.60 WHERE id = ?", (task_id,)
    )
    await orch._tq.update_task_status(task_id, TaskStatus.PENDING)
    task = await orch._tq.get_task(task_id)
    orch._agents.spawn_agent.return_value = "container-1"
    await orch.dispatch_pending_tasks(task["plan_id"], project)
    assert _last_dispatch(captured_events)["difficulty_flagged"] is True


@pytest.mark.unit
async def test_a_flagged_leaf_gets_a_mandatory_acceptance_line_in_its_bible(
    orchestrator_fixture,
):
    """The fixture project declares no verify_cmd, so nothing else supplies one."""
    orch, task_id, project = orchestrator_fixture
    _configure(orch, flag_below=0.55)
    assert not project.get("verify_cmd")
    await orch._tq._db.execute(
        "UPDATE tasks SET difficulty_score = 0.44 WHERE id = ?", (task_id,)
    )
    await orch._tq.update_task_status(task_id, TaskStatus.PENDING)
    task = await orch._tq.get_task(task_id)
    orch._agents.spawn_agent.return_value = "container-1"
    await orch.dispatch_pending_tasks(task["plan_id"], project)
    bible = orch._agents.spawn_agent.await_args.kwargs["bible_text"]
    assert "ACCEPTANCE" in bible


@pytest.mark.unit
async def test_an_unflagged_leaf_gets_no_invented_acceptance_section(
    orchestrator_fixture,
):
    """The mandatory line is flag-driven; a clean leaf's pack is unchanged."""
    orch, task_id, project = orchestrator_fixture
    _configure(orch, flag_below=0.55)
    await orch._tq._db.execute(
        "UPDATE tasks SET difficulty_score = 0.82 WHERE id = ?", (task_id,)
    )
    await orch._tq.update_task_status(task_id, TaskStatus.PENDING)
    task = await orch._tq.get_task(task_id)
    orch._agents.spawn_agent.return_value = "container-1"
    await orch.dispatch_pending_tasks(task["plan_id"], project)
    bible = orch._agents.spawn_agent.await_args.kwargs["bible_text"]
    assert "ACCEPTANCE" not in bible


@pytest.mark.unit
async def test_a_flagged_leaf_keeps_the_declared_acceptance_check(
    orchestrator_fixture,
):
    """A declared check wins; the fallback only fills a genuine hole."""
    orch, task_id, project = orchestrator_fixture
    _configure(orch, flag_below=0.55)
    plan_id, row = await _activate(
        orch,
        "declared",
        difficulty_score=0.44,
        verification="uv run pytest tests/test_widget.py",
    )
    orch._agents.spawn_agent.return_value = "container-1"
    await orch.dispatch_pending_tasks(plan_id, project)
    bible = orch._agents.spawn_agent.await_args.kwargs["bible_text"]
    assert "uv run pytest tests/test_widget.py" in bible
    assert "high risk" not in bible


@pytest.mark.unit
@pytest.mark.parametrize(
    "verification",
    ["ok", "manual review of the widget", 42],
    ids=["too-short", "prose", "non-string"],
)
async def test_a_flagged_leaf_does_not_accept_junk_as_its_mandatory_check(
    orchestrator_fixture, verification
):
    """Mandatory means a real check, not merely a non-empty slot.

    The guard used to be ``not acceptance``, which tests emptiness. ``ok`` is a
    value ``validate_leaves`` REJECTS as too short, and it satisfied that guard,
    so the ONE leaf the difficulty gate warned about was the one that shipped
    with ``ok`` as its entire acceptance floor. The fixture project declares no
    ``verify_cmd``, so nothing else fills the slot.
    """
    orch, _task_id, project = orchestrator_fixture
    _configure(orch, flag_below=0.55)
    assert not project.get("verify_cmd")
    plan_id, _row = await _activate(
        orch, "junk", difficulty_score=0.44, verification=verification
    )
    orch._agents.spawn_agent.return_value = "container-1"
    await orch.dispatch_pending_tasks(plan_id, project)
    bible = orch._agents.spawn_agent.await_args.kwargs["bible_text"]
    assert MANDATORY_ACCEPTANCE in bible
    assert f"# ACCEPTANCE (run this before you finish)\n{verification}" not in bible


@pytest.mark.unit
async def test_activate_plan_persists_the_score_and_the_leaf_type(
    orchestrator_fixture,
):
    """Nothing wrote these columns before, so every consumer read 'not scored'."""
    orch, _task_id, _project = orchestrator_fixture
    _plan_id, row = await _activate(
        orch, "scored", difficulty_score=0.44, leaf_type="function_add"
    )
    assert row["difficulty_score"] == pytest.approx(0.44)
    assert row["leaf_type"] == "function_add"


@pytest.mark.unit
async def test_activate_plan_still_inserts_a_graph_with_neither_field(
    orchestrator_fixture,
):
    """Every pre-Phase-C caller passes a graph without them; NULL, not a crash."""
    orch, _task_id, _project = orchestrator_fixture
    _plan_id, row = await _activate(orch, "plain")
    assert row["difficulty_score"] is None
    assert row["leaf_type"] is None
    assert row["status"] == TaskStatus.PENDING


@pytest.mark.unit
async def test_activate_plan_drops_a_non_numeric_difficulty_score(
    orchestrator_fixture,
):
    """A REAL column will happily hold text, and float() on it wedges dispatch.

    ``dispatch_pending_tasks`` runs on every loop tick, so a ValueError there is
    one log line per interval forever rather than a visible failure.
    """
    orch, _task_id, _project = orchestrator_fixture
    _plan_id, row = await _activate(orch, "garbage", difficulty_score="very hard")
    assert row["difficulty_score"] is None


@pytest.mark.unit
async def test_a_scored_graph_reaches_dispatch_through_the_database(
    orchestrator_fixture, captured_events
):
    """The whole wire, with no hand-written UPDATE anywhere.

    This is the test with teeth: stop persisting in ``activate_plan`` and it
    goes red, which the plan's own tests cannot see because they set the column
    themselves.
    """
    orch, _task_id, project = orchestrator_fixture
    _configure(orch, flag_below=0.55)
    plan_id, row = await _activate(
        orch, "wired", difficulty_score=0.44, leaf_type="function_add"
    )
    orch._agents.spawn_agent.return_value = "container-2"
    await orch.dispatch_pending_tasks(plan_id, project)
    event = _last_dispatch(captured_events)
    assert event["task_id"] == row["id"]
    assert event["difficulty_score"] == pytest.approx(0.44)
    assert event["difficulty_flagged"] is True
