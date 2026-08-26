"""The adaptive-split CORRECTION is graded, not merely asked for.

``docs/decomposition-standard.md`` policy 1 makes the first decomposition a
hypothesis and observed failure the signal to split further.  Until
``leaf_validator.validate_split_children`` existed, the hypothesis was graded
by F3 and the correction was graded by nothing: ``validate_leaves`` had exactly
one call site, in ``core/execute_plan_decompose``, so every child the triage
brain invented reached a worker unexamined.  The triage prompt ASKS for the
leaf standard (it renders the same ``core/leaf_templates`` block the decompose
prompt renders); asking is not enforcing.

Every bound here fails SILENTLY when it breaks.  A child dispatched without an
Acceptance section looks like an ordinary task, a refusal that stopped
happening looks like a working split, and an unscored child looks exactly like
a child that scored well.
"""

from __future__ import annotations

import inspect
import json
import logging
from typing import Any
from unittest.mock import AsyncMock

import pytest

from orchestrator.core import leaf_validator
from orchestrator.core.leaf_validator import validate_split_children
from orchestrator.models.schemas import (
    CapabilityProfile,
    LeafTask,
    LeafType,
    TaskStatus,
    TriageDecision,
)
from tests.test_orchestrator_triage import valid_child


def _profile(**overrides: Any) -> CapabilityProfile:
    """The profile the fixture's triage path resolves, unless a test bends it."""
    base: dict[str, Any] = {
        "model_name": "qwen3.6-27b",
        "parameter_count_b": 27.0,
        "context_window": 32768,
    }
    return CapabilityProfile(**{**base, **overrides})


def _bend(child: LeafTask, **fields: Any) -> LeafTask:
    """Return ``child`` with exactly the named fields replaced.

    ``model_copy`` rather than a fresh ``LeafTask``: the after-validator that
    fills ``description``/``plan_text``/``checklist`` from ``title`` has already
    run, so a copy cannot silently re-derive a field a test meant to pin.
    """
    return child.model_copy(update=fields)


# ---------------------------------------------------------------------------
# Rule isolation: each rule gets an input where IT and only it fires.
#
# Two rules that fire together mask each other: delete one and the test stays
# green on the other's finding.  Asserting the EXACT rule set is what makes a
# deleted rule visible.
# ---------------------------------------------------------------------------


def _one_bad_child(**fields: Any) -> list[LeafTask]:
    """A valid pair whose FIRST child carries exactly one defect."""
    return [
        _bend(valid_child("c1", "One", "src/one.py"), **fields),
        valid_child("c2", "Two", "src/two.py"),
    ]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("rule", "children"),
    [
        (
            "leaf_template",
            _one_bad_child(
                plan_text="## Goal\nOne.\n## Files\nsrc/one.py\n## Steps\n1. Do it."
            ),
        ),
        ("verification", _one_bad_child(verification="Review the output manually")),
        (
            "max_files",
            _one_bad_child(files=[f"src/f{n}.py" for n in range(6)]),
        ),
        ("max_loc", _one_bad_child(estimated_loc=9001)),
        (
            "dep_cycle",
            [
                _bend(valid_child("c1", "One", "src/one.py"), depends_on=["c2"]),
                _bend(valid_child("c2", "Two", "src/two.py"), depends_on=["c1"]),
            ],
        ),
    ],
    ids=["leaf_template", "verification", "max_files", "max_loc", "dep_cycle"],
)
def test_each_hard_rule_fires_alone(rule: str, children: list[LeafTask]) -> None:
    result = validate_split_children(children, _profile())
    assert {v.rule for v in result.hard} == {rule}
    assert not result.dispatchable


@pytest.mark.unit
def test_escalate_mismatch_fires_alone_when_the_profile_asks_for_it() -> None:
    """Inert on a stock profile, live the moment an operator names a type.

    Separate from the parametrized cases because it is the one HARD rule whose
    firing depends on the PROFILE rather than the child, so a child that trips
    it is byte-identical to one that does not on a default profile.
    """
    children = _one_bad_child(task_type="migration")
    assert validate_split_children(children, _profile()).dispatchable

    strict = _profile(escalate_task_types=["migration"])
    result = validate_split_children(children, strict)
    assert {v.rule for v in result.hard} == {"escalate_mismatch"}


@pytest.mark.unit
@pytest.mark.parametrize(
    ("rule", "children"),
    [
        (
            "file_overlap",
            [
                valid_child("c1", "One", "src/shared.py"),
                valid_child("c2", "Two", "src/shared.py"),
            ],
        ),
        ("vague_phrase", _one_bad_child(title="Clean up the widget")),
        (
            "checklist_size",
            _one_bad_child(
                checklist=[{"text": f"step {n}"} for n in range(13)],
            ),
        ),
    ],
    ids=["file_overlap", "vague_phrase", "checklist_size"],
)
def test_each_soft_rule_fires_alone_and_never_blocks(
    rule: str, children: list[LeafTask]
) -> None:
    result = validate_split_children(children, _profile())
    assert {v.rule for v in result.soft} == {rule}
    # SOFT is the whole point: it is recorded and the split proceeds.
    assert result.dispatchable


@pytest.mark.unit
def test_a_correct_pair_of_children_trips_nothing() -> None:
    """The control.  Without it every rule test above passes on a broken rule
    set that rejects everything."""
    result = validate_split_children(
        [
            valid_child("c1", "One", "src/one.py"),
            valid_child("c2", "Two", "src/two.py"),
        ],
        _profile(),
    )
    assert result.hard == []
    assert result.soft == []


# ---------------------------------------------------------------------------
# The three rules that are deliberately NOT run.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_a_dangling_child_dep_is_left_for_the_rewiring_to_drop() -> None:
    """``rewire_plan_for_split`` drops an unresolvable dep on purpose.

    Rejecting the split for it would trade a graph the next function repairs
    losslessly for a plain retry of the leaf that already failed twice.  If
    ``dangling_dep`` is ever added to the split rule set this goes red, and the
    drop in ``core/leaf_split`` has to be reconsidered in the same change.
    """
    children = _one_bad_child(depends_on=["a-leaf-that-does-not-exist"])
    assert validate_split_children(children, _profile()).dispatchable


@pytest.mark.unit
def test_dep_depth_and_verbatim_are_not_among_the_split_rules() -> None:
    """Read off the function body, because their absence has no observable
    output to assert on: a rule that is not called produces nothing, which is
    indistinguishable from a rule that is called and passes."""
    body = inspect.getsource(leaf_validator.validate_split_children)
    called = {
        line.strip()
        for line in body.splitlines()
        if "_check_" in line and not line.strip().startswith("def ")
    }
    assert not [c for c in called if "_check_dep_depth" in c]
    assert not [c for c in called if "_check_plan_text_verbatim" in c]
    assert not [c for c in called if "_check_dangling_dep" in c]


@pytest.mark.unit
def test_the_split_rules_are_the_same_implementations_f3_runs() -> None:
    """No second copy.  A forked rule lets the hypothesis and its correction be
    graded differently, which is the drift ``core/leaf_templates`` exists to
    prevent."""

    def _rule_calls(func: Any) -> set[str]:
        return {
            line.strip().split("(")[0].strip()
            for line in inspect.getsource(func).splitlines()
            if "_check_" in line and not line.strip().startswith("def ")
        }

    split_rules = _rule_calls(leaf_validator.validate_split_children)
    f3_rules = _rule_calls(leaf_validator.validate_leaves)
    assert split_rules, "validate_split_children runs no rules at all"
    assert split_rules <= f3_rules, (
        f"split-only rule implementations have appeared: {split_rules - f3_rules}"
    )


# ---------------------------------------------------------------------------
# The REAL path: a real TriageDecision, real LeafTask children, the real
# orchestrator_review split branch, a real database.
# ---------------------------------------------------------------------------


async def _second_attempt(orch: Any, task_id: str) -> None:
    await orch._tq.retry_task(task_id)
    await orch._tq.update_task_status(task_id, TaskStatus.REVIEWING)


async def _split_with(orch: Any, task_id: str, children: list[LeafTask]) -> None:
    """Drive one real review that decides ``split`` with these children."""
    await _second_attempt(orch, task_id)
    orch._triage_leaf = AsyncMock(
        return_value=TriageDecision(
            decision="split", reason="two concerns", children=children
        )
    )


async def _child_rows(orch: Any, parent_id: str) -> list[dict[str, Any]]:
    parent = await orch._tq.get_task(parent_id)
    rows = await orch._tq.get_tasks_for_plan(parent["plan_id"])
    return [dict(r) for r in rows if r["parent_task_id"] == parent_id]


@pytest.mark.unit
async def test_a_child_missing_a_required_section_is_refused(
    orchestrator_fixture: tuple[Any, str, dict[str, Any]],
) -> None:
    """The headline: a child with no Acceptance section never reaches a worker.

    It degrades to the PLAIN RETRY path rather than aborting the tick, the same
    honest degradation a graph the rewiring refuses already gets.
    """
    orch, task_id, project = orchestrator_fixture
    await _split_with(
        orch,
        task_id,
        _one_bad_child(
            plan_text="## Goal\nOne.\n## Files\nsrc/one.py\n## Steps\n1. Do it."
        ),
    )

    await orch.review_task(task_id, project)

    task = await orch._tq.get_task(task_id)
    assert task["status"] == TaskStatus.PENDING
    # Still stamped, so the refused split does not buy a second triage call.
    assert task["triage_decision"] == "split"
    assert await _child_rows(orch, task_id) == []


@pytest.mark.unit
async def test_a_child_with_an_unrunnable_verification_is_refused(
    orchestrator_fixture: tuple[Any, str, dict[str, Any]],
) -> None:
    """A second rule with its own end-to-end case.

    Without it, deleting ``_check_verification`` from the split rule set stays
    green: the template test above would still refuse its own child.
    """
    orch, task_id, project = orchestrator_fixture
    await _split_with(
        orch, task_id, _one_bad_child(verification="Review the output manually")
    )

    await orch.review_task(task_id, project)

    task = await orch._tq.get_task(task_id)
    assert task["status"] == TaskStatus.PENDING
    assert await _child_rows(orch, task_id) == []


@pytest.mark.unit
async def test_the_refusal_names_the_rule_and_the_slug_the_child_would_have_had(
    orchestrator_fixture: tuple[Any, str, dict[str, Any]],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A refusal an operator cannot act on is barely better than none.

    ``caplog.records`` filtered by level, never ``caplog.text``: the message
    has to arrive as a WARNING, and ``caplog.text`` is green on a DEBUG line.
    """
    orch, task_id, project = orchestrator_fixture
    await _split_with(
        orch, task_id, _one_bad_child(verification="Review the output manually")
    )

    with caplog.at_level(logging.DEBUG):
        await orch.review_task(task_id, project)

    warnings = [
        r.getMessage()
        for r in caplog.records
        if r.levelname == "WARNING" and "Refusing the triage split" in r.getMessage()
    ]
    assert len(warnings) == 1
    # The brain's child id ("c1") appears nowhere else in the system, so the
    # message has to translate it to the slug the rewiring would have assigned.
    assert "a-s1" in warnings[0]
    assert "[verification]" in warnings[0]


@pytest.mark.unit
async def test_a_refused_split_reaches_the_capability_ledger(
    orchestrator_fixture: tuple[Any, str, dict[str, Any]],
) -> None:
    """The durable record, not just a log line the next restart forgets."""
    orch, task_id, project = orchestrator_fixture
    await _split_with(
        orch,
        task_id,
        _one_bad_child(
            plan_text="## Goal\nOne.\n## Files\nsrc/one.py\n## Steps\n1. Do it."
        ),
    )

    await orch.review_task(task_id, project)

    rows = await orch._tq._db.fetch_all(
        "SELECT * FROM capability_events WHERE event_type = 'leaf_rejected'"
    )
    assert len(rows) == 1
    payload = json.loads(rows[0]["payload"])
    assert payload["rule_id"] == "leaf_template"
    assert payload["leaf_slug"] == "a-s1"


@pytest.mark.unit
async def test_a_soft_finding_warns_and_still_splits(
    orchestrator_fixture: tuple[Any, str, dict[str, Any]],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """SOFT means recorded, not blocked.

    If soft findings ever start blocking, the split path inherits exactly the
    defect the decompose loop was carrying: a warning priced like a rejection.
    """
    orch, task_id, project = orchestrator_fixture
    await _split_with(
        orch,
        task_id,
        [
            valid_child("c1", "One", "src/shared.py"),
            valid_child("c2", "Two", "src/shared.py"),
        ],
    )

    with caplog.at_level(logging.DEBUG):
        await orch.review_task(task_id, project)

    soft_lines = [
        r.getMessage()
        for r in caplog.records
        if r.levelname == "WARNING" and "SOFT findings" in r.getMessage()
    ]
    assert len(soft_lines) == 1
    assert "[file_overlap]" in soft_lines[0]

    task = await orch._tq.get_task(task_id)
    assert task["status"] == TaskStatus.SUPERSEDED
    assert len(await _child_rows(orch, task_id)) == 2


@pytest.mark.unit
async def test_split_children_are_scored_and_carry_the_score_onto_their_rows(
    orchestrator_fixture: tuple[Any, str, dict[str, Any]],
) -> None:
    """A NULL score reads as "not flagged", which is right for a NULL and wrong
    for the one leaf on the plan already known to have failed.

    ``orchestrator_dispatch`` derives ``flagged`` from the row's
    ``difficulty_score`` alone, so an unwritten score silently exempts every
    split child from the mandatory-acceptance treatment a low score buys.
    """
    orch, task_id, project = orchestrator_fixture
    await _split_with(orch, task_id, _children_pair())

    await orch.review_task(task_id, project)

    rows = await _child_rows(orch, task_id)
    assert len(rows) == 2
    scores = [row["difficulty_score"] for row in rows]
    assert all(isinstance(s, float) for s in scores), scores
    assert all(0.0 <= s <= 1.0 for s in scores)

    # And the ledger saw the prediction, which is the calibration join's whole
    # point: a correction whose outcome cannot be joined to a prediction is
    # training data nobody can use.
    events = await orch._tq._db.fetch_all(
        "SELECT * FROM capability_events WHERE event_type = 'leaf_difficulty_scored'"
    )
    assert {json.loads(e["payload"])["leaf_slug"] for e in events} == {"a-s1", "a-s2"}


def _children_pair() -> list[LeafTask]:
    return [
        valid_child("c1", "One", "src/one.py"),
        valid_child("c2", "Two", "src/two.py"),
    ]


@pytest.mark.unit
async def test_a_scoring_failure_costs_the_flag_and_not_the_split(
    orchestrator_fixture: tuple[Any, str, dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Scoring FAILS OPEN, because the alternative is an aborted tick.

    ``Orchestrator.run_once`` has no per-plan try/except, so an exception out
    of the scorer would stop dispatch for EVERY plan on the install in order to
    lose a dashboard flag.  Patched on the MIXIN module that calls it, never on
    ``core.orchestrator``: the mixin's own global is the name that resolves.
    """

    async def _boom(*_args: Any, **_kwargs: Any) -> Any:
        message = "outcomes table is unreadable"
        raise RuntimeError(message)

    monkeypatch.setattr(
        "orchestrator.core.orchestrator_review.score_split_children", _boom
    )
    orch, task_id, project = orchestrator_fixture
    await _split_with(orch, task_id, _children_pair())

    with caplog.at_level(logging.DEBUG):
        await orch.review_task(task_id, project)

    parent = await orch._tq.get_task(task_id)
    assert parent["status"] == TaskStatus.SUPERSEDED
    rows = await _child_rows(orch, task_id)
    assert len(rows) == 2
    # Unscored, which dispatch reads as "not flagged" and never as "safe".
    assert all(row["difficulty_score"] is None for row in rows)
    assert [
        r
        for r in caplog.records
        if r.levelname == "ERROR" and "inserted UNSCORED" in r.getMessage()
    ]


@pytest.mark.unit
async def test_a_good_split_still_splits(
    orchestrator_fixture: tuple[Any, str, dict[str, Any]],
) -> None:
    """POSITIVE CONTROL, deliberately last.

    Every refusal above is satisfied by a gate that refuses everything.  This
    is the only test that says the gate lets correct work through, and its
    children are the same shape the triage prompt asks the brain for.
    """
    orch, task_id, project = orchestrator_fixture
    await _split_with(orch, task_id, _children_pair())

    await orch.review_task(task_id, project)

    parent = await orch._tq.get_task(task_id)
    assert parent["status"] == TaskStatus.SUPERSEDED
    rows = await _child_rows(orch, task_id)
    assert {row["branch_name"] for row in rows} == {"agent/a-s1", "agent/a-s2"}
    assert all(row["leaf_type"] == LeafType.FUNCTION_ADD for row in rows)
