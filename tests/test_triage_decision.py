"""TriageDecision is a brain-output contract, so it gets golden fixtures.

Same discipline as LeafTask: a decision must round-trip through the model, so
extend these fixtures when you add a field.
"""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from orchestrator.models.schemas import TriageDecision


FIXTURES = Path(__file__).parent / "fixtures" / "triage"


@pytest.mark.unit
@pytest.mark.parametrize(
    "name",
    ["retry_decision", "split_decision", "escalate_decision", "human_decision"],
)
def test_golden_fixture_round_trips(name: str):
    raw = json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
    decision = TriageDecision.model_validate(raw)
    assert json.loads(decision.model_dump_json()) == raw


@pytest.mark.unit
def test_split_requires_children():
    with pytest.raises(ValidationError, match="children"):
        TriageDecision(decision="split", reason="too big")


@pytest.mark.unit
def test_split_rejects_fewer_than_two_children():
    child = {"id": "a", "title": "A", "plan_text": "Goal: a"}
    with pytest.raises(ValidationError):
        TriageDecision(decision="split", reason="too big", children=[child])


@pytest.mark.unit
def test_split_rejects_more_than_four_children():
    children = [
        {"id": f"c{i}", "title": f"C{i}", "plan_text": "Goal: c"} for i in range(5)
    ]
    with pytest.raises(ValidationError):
        TriageDecision(decision="split", reason="too big", children=children)


@pytest.mark.unit
def test_non_split_decisions_reject_children():
    child = {"id": "a", "title": "A", "plan_text": "Goal: a"}
    with pytest.raises(ValidationError):
        TriageDecision(decision="retry", reason="one more go", children=[child, child])


@pytest.mark.unit
def test_unknown_decision_is_rejected():
    with pytest.raises(ValidationError):
        TriageDecision(decision="give_up", reason="nope")


@pytest.mark.unit
def test_reason_is_required():
    with pytest.raises(ValidationError):
        TriageDecision(decision="human")
