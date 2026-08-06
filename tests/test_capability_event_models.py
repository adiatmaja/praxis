"""Tests for capability_events Pydantic models (S1).

Verifies the versioned decision-record model family used to log
capability-engine events.  The emitter is delivered in Task 7; these
tests cover the model layer only.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from orchestrator.core.capability_events import (
    CAPABILITY_EVENT_SCHEMA_VERSION,
    CAPABILITY_EVENT_TYPES,
    CapabilityEventModel,
    DecomposeInputEvent,
    LeafDifficultyScoredEvent,
    LeafRejectedEvent,
    LeafRejectedPredispatchEvent,
    LeafValidatedEvent,
    OutcomeRecordedEvent,
    PlanRejectedEvent,
    TaskEscalatedEvent,
    TaskSplitEvent,
)


# ---------------------------------------------------------------------------
# Schema version
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_schema_version_is_one():
    assert CAPABILITY_EVENT_SCHEMA_VERSION == 1


# ---------------------------------------------------------------------------
# Event type registry
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_capability_event_types_contains_all_nine():
    assert len(CAPABILITY_EVENT_TYPES) == 9
    assert "decompose_input" in CAPABILITY_EVENT_TYPES
    assert "leaf_validated" in CAPABILITY_EVENT_TYPES
    assert "leaf_rejected" in CAPABILITY_EVENT_TYPES
    assert "leaf_difficulty_scored" in CAPABILITY_EVENT_TYPES
    assert "leaf_rejected_predispatch" in CAPABILITY_EVENT_TYPES
    assert "plan_rejected" in CAPABILITY_EVENT_TYPES
    assert "task_split" in CAPABILITY_EVENT_TYPES
    assert "task_escalated" in CAPABILITY_EVENT_TYPES
    assert "outcome_recorded" in CAPABILITY_EVENT_TYPES


# ---------------------------------------------------------------------------
# Base behaviour
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_base_schema_version_default():
    evt = LeafValidatedEvent(plan_id="plan-1", leaf_slug="task-a")
    assert evt.schema_version == CAPABILITY_EVENT_SCHEMA_VERSION


@pytest.mark.unit
def test_extra_fields_forbidden():
    with pytest.raises(ValidationError):
        LeafValidatedEvent(
            plan_id="plan-1",
            leaf_slug="task-a",
            unexpected_field="oops",
        )


# ---------------------------------------------------------------------------
# DecomposeInputEvent
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_decompose_input_event_minimal():
    evt = DecomposeInputEvent(
        plan_id="plan-1",
        model_name="qwen3.6-27b",
        per_leaf_budget=2000,
        profile_summary="medium capability",
        history_summary_hash="abc123",
        plan_hash="def456",
    )
    assert evt.event_type == "decompose_input"
    assert evt.plan_id == "plan-1"
    assert evt.model_name == "qwen3.6-27b"
    assert evt.per_leaf_budget == 2000
    assert evt.profile_summary == "medium capability"
    assert evt.history_summary_hash == "abc123"
    assert evt.plan_hash == "def456"


@pytest.mark.unit
def test_decompose_input_event_requires_all_fields():
    with pytest.raises(ValidationError):
        DecomposeInputEvent(plan_id="plan-1")


# ---------------------------------------------------------------------------
# LeafValidatedEvent
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_leaf_validated_event():
    evt = LeafValidatedEvent(plan_id="plan-1", leaf_slug="task-a")
    assert evt.event_type == "leaf_validated"
    assert evt.plan_id == "plan-1"
    assert evt.leaf_slug == "task-a"


# ---------------------------------------------------------------------------
# LeafRejectedEvent
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_leaf_rejected_event_minimal():
    evt = LeafRejectedEvent(
        plan_id="plan-1",
        leaf_slug="task-b",
        rule_id="max_context_window",
    )
    assert evt.event_type == "leaf_rejected"
    assert evt.rule_id == "max_context_window"
    assert evt.measured is None
    assert evt.limit is None


@pytest.mark.unit
def test_leaf_rejected_event_with_numeric_limits():
    evt = LeafRejectedEvent(
        plan_id="plan-1",
        leaf_slug="task-b",
        rule_id="max_loc_delta",
        measured=5000,
        limit=4000,
    )
    assert evt.measured == 5000
    assert evt.limit == 4000


@pytest.mark.unit
def test_leaf_rejected_event_with_string_limits():
    evt = LeafRejectedEvent(
        plan_id="plan-1",
        leaf_slug="task-b",
        rule_id="max_complexity",
        measured="high",
        limit="medium",
    )
    assert evt.measured == "high"
    assert evt.limit == "medium"


# ---------------------------------------------------------------------------
# PlanRejectedEvent
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_plan_rejected_event():
    evt = PlanRejectedEvent(
        plan_id="plan-2",
        violations=["too many leaves", "exceeds model capability"],
        rounds=3,
    )
    assert evt.event_type == "plan_rejected"
    assert len(evt.violations) == 2
    assert evt.rounds == 3


@pytest.mark.unit
def test_plan_rejected_event_empty_violations():
    evt = PlanRejectedEvent(
        plan_id="plan-2",
        violations=[],
        rounds=1,
    )
    assert evt.violations == []


# ---------------------------------------------------------------------------
# TaskSplitEvent
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_task_split_event_minimal():
    evt = TaskSplitEvent(
        plan_id="plan-3",
        parent_slug="task-big",
        child_slugs=["task-small-1", "task-small-2"],
    )
    assert evt.event_type == "task_split"
    assert evt.parent_slug == "task-big"
    assert evt.child_slugs == ["task-small-1", "task-small-2"]
    assert evt.tightened_limits == {}
    assert evt.failure_evidence_ref is None


@pytest.mark.unit
def test_task_split_event_with_details():
    evt = TaskSplitEvent(
        plan_id="plan-3",
        parent_slug="task-big",
        child_slugs=["task-small-1", "task-small-2"],
        tightened_limits={"max_loc": 500},
        failure_evidence_ref="run-abc123",
    )
    assert evt.tightened_limits == {"max_loc": 500}
    assert evt.failure_evidence_ref == "run-abc123"


# ---------------------------------------------------------------------------
# TaskEscalatedEvent
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_task_escalated_event():
    evt = TaskEscalatedEvent(
        plan_id="plan-4",
        leaf_slug="task-hard",
        policy="needs_stronger_model",
    )
    assert evt.event_type == "task_escalated"
    assert evt.policy == "needs_stronger_model"


# ---------------------------------------------------------------------------
# OutcomeRecordedEvent
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_outcome_recorded_event_minimal():
    evt = OutcomeRecordedEvent(
        plan_id="plan-5",
        task_id="task-1",
        outcome_id="outcome-1",
    )
    assert evt.event_type == "outcome_recorded"
    assert evt.failure_class is None
    assert evt.counts_against_worker is False


@pytest.mark.unit
def test_outcome_recorded_event_with_failure():
    evt = OutcomeRecordedEvent(
        plan_id="plan-5",
        task_id="task-1",
        outcome_id="outcome-1",
        failure_class="review_fail",
        counts_against_worker=True,
    )
    assert evt.failure_class == "review_fail"
    assert evt.counts_against_worker is True


# ---------------------------------------------------------------------------
# Union type (CapabilityEventModel)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_capability_event_model_accepts_each_variant():
    events = [
        DecomposeInputEvent(
            plan_id="p1",
            model_name="m",
            per_leaf_budget=100,
            profile_summary="s",
            history_summary_hash="h",
            plan_hash="ph",
        ),
        LeafValidatedEvent(plan_id="p1", leaf_slug="t1"),
        LeafRejectedEvent(plan_id="p1", leaf_slug="t1", rule_id="r1"),
        LeafDifficultyScoredEvent(
            plan_id="p1",
            leaf_slug="t1",
            p_success=0.5,
            features={"f1": 1.0},
        ),
        LeafRejectedPredispatchEvent(
            plan_id="p1",
            leaf_slug="t1",
            p_success=0.2,
            failing_features=["f1"],
        ),
        PlanRejectedEvent(plan_id="p1", violations=["v"], rounds=1),
        TaskSplitEvent(plan_id="p1", parent_slug="t1", child_slugs=["t2"]),
        TaskEscalatedEvent(plan_id="p1", leaf_slug="t1", policy="p"),
        OutcomeRecordedEvent(plan_id="p1", task_id="t1", outcome_id="o1"),
    ]
    typed: list[CapabilityEventModel] = events
    assert len(typed) == 9


@pytest.mark.unit
def test_capability_event_model_preserves_event_type():
    evt = LeafValidatedEvent(plan_id="p1", leaf_slug="t1")
    typed: CapabilityEventModel = evt
    assert typed.event_type == "leaf_validated"


# ---------------------------------------------------------------------------
# LeafDifficultyScoredEvent
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_leaf_difficulty_scored_event_round_trips():
    from orchestrator.core.capability_events import LeafDifficultyScoredEvent

    event = LeafDifficultyScoredEvent(
        plan_id="p1",
        leaf_slug="add-widget",
        p_success=0.42,
        features={"files_touched": 2.0, "loc_ratio": 0.5},
    )
    payload = event.model_dump()
    assert payload["event_type"] == "leaf_difficulty_scored"
    assert payload["schema_version"] == 1
    assert LeafDifficultyScoredEvent.model_validate(payload) == event


# ---------------------------------------------------------------------------
# LeafRejectedPredispatchEvent
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_leaf_rejected_predispatch_event_round_trips():
    from orchestrator.core.capability_events import LeafRejectedPredispatchEvent

    event = LeafRejectedPredispatchEvent(
        plan_id="p1",
        leaf_slug="add-widget",
        p_success=0.19,
        failing_features=["loc_ratio", "files_touched"],
    )
    payload = event.model_dump()
    assert payload["event_type"] == "leaf_rejected_predispatch"
    assert LeafRejectedPredispatchEvent.model_validate(payload) == event


# ---------------------------------------------------------------------------
# Registry consistency
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_both_new_types_are_in_the_registry():
    from orchestrator.core.capability_events import CAPABILITY_EVENT_TYPES

    assert "leaf_difficulty_scored" in CAPABILITY_EVENT_TYPES
    assert "leaf_rejected_predispatch" in CAPABILITY_EVENT_TYPES


@pytest.mark.unit
def test_the_registry_matches_the_union_members():
    """The frozenset and the union must never drift apart."""
    import typing

    from orchestrator.core.capability_events import (
        CAPABILITY_EVENT_TYPES,
        CapabilityEventModel,
    )

    members = typing.get_args(CapabilityEventModel)
    declared = {
        typing.get_args(m.model_fields["event_type"].annotation)[0] for m in members
    }
    assert declared == set(CAPABILITY_EVENT_TYPES)
