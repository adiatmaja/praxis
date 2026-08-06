"""Triage is deterministic around exactly one brain call.

One re-ask on malformed output, then fall back to `human`. Never guess.
"""

import json

import pytest

from orchestrator.core.leaf_triage import (
    TriageEvidence,
    build_triage_prompt,
    parse_triage_response,
    triage_leaf,
)
from orchestrator.models.schemas import CapabilityProfile, TriageDecision


def _evidence(**overrides) -> TriageEvidence:
    base = {
        "task_slug": "add-widget",
        "leaf_type": "function_add",
        "plan_text": "## Goal\nAdd it.\n## Files\nsrc/a.py\n## Steps\n1. go\n## Acceptance\n`pytest`",
        "profile": CapabilityProfile(
            model_name="m", parameter_count_b=30, context_window=8192
        ),
        "attempts": [
            {
                "attempt": 1,
                "files_touched": 4,
                "loc_delta": 210,
                "diff": "diff --git a/src/a.py",
                "verify_exit_code": 1,
                "verify_tail": "3 failed",
                "review_reason": "Missing the AbortSignal parameter",
            },
            {
                "attempt": 2,
                "files_touched": 5,
                "loc_delta": 260,
                "diff": "diff --git a/src/a.py",
                "verify_exit_code": 1,
                "verify_tail": "2 failed",
                "review_reason": "Still missing the AbortSignal parameter",
            },
        ],
        "difficulty_score": 0.41,
        "remaining_leaf_budget": 20,
        "escalation_available": True,
    }
    base.update(overrides)
    return TriageEvidence(**base)


@pytest.mark.unit
def test_prompt_names_the_four_decisions():
    prompt = build_triage_prompt(_evidence())
    for decision in ("retry", "split", "escalate", "human"):
        assert f'"{decision}"' in prompt


@pytest.mark.unit
def test_prompt_states_the_hard_rules():
    prompt = build_triage_prompt(_evidence())
    assert "2 and 4" in prompt or "2 to 4" in prompt
    assert "may not split again" in prompt
    # Pinned as the whole rendered sentence, not a bare "20": a substring test
    # against a prompt full of other numbers can pass for the wrong reason the
    # moment an unrelated rendered value happens to contain the same digits.
    assert "At most 20 more leaves" in prompt


@pytest.mark.unit
def test_prompt_carries_the_verbatim_plan_text():
    ev = _evidence()
    assert ev.plan_text in build_triage_prompt(ev)


@pytest.mark.unit
def test_prompt_carries_every_attempt_reason():
    prompt = build_triage_prompt(_evidence())
    assert "Missing the AbortSignal parameter" in prompt
    assert "Still missing the AbortSignal parameter" in prompt


@pytest.mark.unit
def test_prompt_forbids_escalation_when_the_ladder_is_exhausted():
    prompt = build_triage_prompt(_evidence(escalation_available=False))
    assert "escalation ladder is exhausted" in prompt


@pytest.mark.unit
def test_prompt_caps_a_huge_diff():
    huge = "x" * 500_000
    ev = _evidence(
        attempts=[
            {
                "attempt": 1,
                "files_touched": 1,
                "loc_delta": 1,
                "diff": huge,
                "verify_exit_code": 1,
                "verify_tail": "1 failed",
                "review_reason": "nope",
            }
        ]
    )
    assert len(build_triage_prompt(ev)) < 100_000


@pytest.mark.unit
def test_prompt_survives_braces_in_the_substituted_evidence():
    """Braces in a diff or a plan_text are ordinary, and must stay literal.

    ``str.format`` is a single pass: substituted values are never re-scanned
    for replacement fields. If the prompt were ever built by formatting an
    already-substituted string, a diff containing ``{}`` would raise here.
    """
    ev = _evidence(
        plan_text="## Goal\nreturn {}\n## Files\nsrc/a.py",
        attempts=[
            {
                "attempt": 1,
                "files_touched": 1,
                "loc_delta": 1,
                "diff": "-  x = {}\n+  x = {'k': {v}}",
                "verify_exit_code": 1,
                "verify_tail": "KeyError: {oops}",
                "review_reason": "brace {soup}",
            }
        ],
    )
    prompt = build_triage_prompt(ev)
    assert "x = {'k': {v}}" in prompt
    assert "KeyError: {oops}" in prompt
    assert "brace {soup}" in prompt
    assert "return {}" in prompt


@pytest.mark.unit
def test_parse_accepts_a_fenced_json_object():
    raw = '```json\n{"decision": "human", "reason": "unclear"}\n```'
    assert parse_triage_response(raw).decision == "human"


@pytest.mark.unit
def test_parse_accepts_prose_before_the_object():
    raw = 'Here is my call.\n{"decision": "escalate", "reason": "ceiling"}'
    assert parse_triage_response(raw).decision == "escalate"


@pytest.mark.unit
def test_parse_raises_on_a_malformed_object():
    from orchestrator.core.leaf_triage import TriageParseError

    with pytest.raises(TriageParseError):
        parse_triage_response("not json at all")


@pytest.mark.unit
def test_parse_raises_on_a_split_with_one_child():
    from orchestrator.core.leaf_triage import TriageParseError

    raw = json.dumps(
        {
            "decision": "split",
            "reason": "too big",
            "children": [{"id": "a", "title": "A", "plan_text": "Goal: a"}],
        }
    )
    with pytest.raises(TriageParseError):
        parse_triage_response(raw)


class _Router:
    def __init__(self, *responses: str) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, str]] = []

    async def run(self, call_site, prompt, project_id=None, cwd=None):
        self.calls.append((call_site, prompt))
        return self.responses.pop(0)


@pytest.mark.unit
async def test_triage_uses_the_leaf_failure_triage_call_site():
    router = _Router('{"decision": "human", "reason": "unclear"}')
    await triage_leaf(_evidence(), router, project_id="p1")
    assert router.calls[0][0] == "leaf_failure_triage"


@pytest.mark.unit
async def test_triage_re_asks_once_on_malformed_output():
    router = _Router("garbage", '{"decision": "retry", "reason": "one more"}')
    decision = await triage_leaf(_evidence(), router, project_id="p1")
    assert decision.decision == "retry"
    assert len(router.calls) == 2
    # The first ask must NOT already carry the phrase, or the re-ask assertion
    # below would hold for any prompt at all.
    assert "validation error" not in router.calls[0][1].lower()
    assert "validation error" in router.calls[1][1].lower()


@pytest.mark.unit
async def test_triage_falls_back_to_human_after_two_bad_answers():
    router = _Router("garbage", "still garbage")
    decision = await triage_leaf(_evidence(), router, project_id="p1")
    assert decision.decision == "human"
    assert len(router.calls) == 2


@pytest.mark.unit
async def test_triage_downgrades_escalate_when_the_ladder_is_exhausted():
    router = _Router('{"decision": "escalate", "reason": "ceiling"}')
    decision = await triage_leaf(
        _evidence(escalation_available=False), router, project_id="p1"
    )
    assert decision.decision == "human"


@pytest.mark.unit
async def test_triage_downgrades_split_that_would_breach_the_leaf_ceiling():
    router = _Router(
        json.dumps(
            {
                "decision": "split",
                "reason": "too big",
                "children": [
                    {"id": "a", "title": "A", "plan_text": "Goal: a"},
                    {"id": "b", "title": "B", "plan_text": "Goal: b"},
                    {"id": "c", "title": "C", "plan_text": "Goal: c"},
                ],
            }
        )
    )
    decision = await triage_leaf(
        _evidence(remaining_leaf_budget=2), router, project_id="p1"
    )
    assert decision.decision == "escalate"


@pytest.mark.unit
async def test_triage_downgrades_split_to_human_when_escalation_is_also_gone():
    router = _Router(
        json.dumps(
            {
                "decision": "split",
                "reason": "too big",
                "children": [
                    {"id": "a", "title": "A", "plan_text": "Goal: a"},
                    {"id": "b", "title": "B", "plan_text": "Goal: b"},
                    {"id": "c", "title": "C", "plan_text": "Goal: c"},
                ],
            }
        )
    )
    decision = await triage_leaf(
        _evidence(remaining_leaf_budget=0, escalation_available=False),
        router,
        project_id="p1",
    )
    assert decision.decision == "human"


@pytest.mark.unit
async def test_a_router_exception_falls_back_to_human():
    class _Boom:
        async def run(self, *args, **kwargs):
            message = "provider down"
            raise RuntimeError(message)

    decision = await triage_leaf(_evidence(), _Boom(), project_id="p1")
    assert decision.decision == "human"


@pytest.mark.unit
async def test_an_unbuildable_prompt_falls_back_to_human_without_calling_out():
    """A malformed evidence pack parks the leaf; it never propagates.

    Triage sits on top of a working retry path, so nothing here may raise into
    the review loop. Evidence assembly is the caller's job and it can get this
    wrong (a non-dict attempt, a non-numeric score); when it does, the leaf goes
    to a human instead of taking the whole loop down with it.
    """
    router = _Router('{"decision": "retry", "reason": "never asked"}')
    decision = await triage_leaf(
        _evidence(attempts=["not a dict at all"]), router, project_id="p1"
    )
    assert decision.decision == "human"
    assert router.calls == []


@pytest.mark.unit
def test_a_downgrade_of_every_decision_stays_a_valid_decision():
    from orchestrator.core.leaf_triage import _downgrade

    child = {"id": "a", "title": "A", "plan_text": "Goal: a"}
    cases = [
        TriageDecision(decision="retry", reason="r"),
        TriageDecision(decision="escalate", reason="r"),
        TriageDecision(decision="human", reason="r"),
        TriageDecision(decision="split", reason="r", children=[child, child]),
    ]
    for available in (True, False):
        for budget in (0, 2, 20):
            ev = _evidence(remaining_leaf_budget=budget, escalation_available=available)
            for case in cases:
                out = _downgrade(case, ev)
                assert out.decision in {"retry", "split", "escalate", "human"}
                if not available:
                    assert out.decision != "escalate"
