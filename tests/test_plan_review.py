"""Tests for capability-aware plan-review decomposition."""

import json

import pytest

from orchestrator.core.leaf_templates import missing_sections
from orchestrator.core.leaf_validator import validate_leaves
from orchestrator.core.plan_review import (
    PlanReviewError,
    build_review_prompt,
    parse_review_response,
)
from orchestrator.models.schemas import CapabilityProfile, LeafTask, LeafType


PROFILE = CapabilityProfile(
    model_name="qwen3",
    parameter_count_b=30,
    context_window=8192,
    strengths="single-file",
    weaknesses="refactors",
    max_task_complexity="medium",
)


@pytest.mark.unit
def test_prompt_includes_param_count_and_history_and_budget():
    prompt = build_review_prompt(
        plan_text="Build a thing",
        profile=PROFILE,
        history_summary="(no prior run history for this model)",
        per_leaf_token_budget=3200,
    )
    assert "30" in prompt
    assert "3200" in prompt
    assert "no prior run history" in prompt


@pytest.mark.unit
def test_parse_valid_graph_normalizes_to_opus_plan():
    raw = json.dumps(
        {
            "tasks": [
                {
                    "id": "t1",
                    "title": "Add model",
                    "description": "...",
                    "depends_on": [],
                    "checklist": [{"text": "write test"}],
                    "needs_stronger_model": False,
                },
            ]
        }
    )
    plan = parse_review_response(raw)
    assert plan["tasks"][0]["id"] == "t1"
    assert plan["tasks"][0]["checklist"][0]["text"] == "write test"


@pytest.mark.unit
def test_parse_flags_needs_stronger_model():
    raw = json.dumps(
        {
            "tasks": [
                {
                    "id": "t1",
                    "title": "Rewrite engine",
                    "description": "...",
                    "depends_on": [],
                    "checklist": [{"text": "do it"}],
                    "needs_stronger_model": True,
                },
            ]
        }
    )
    plan = parse_review_response(raw)
    assert plan["tasks"][0]["needs_stronger_model"] is True


@pytest.mark.unit
def test_parse_rejects_malformed_json_no_silent_pass():
    with pytest.raises(PlanReviewError):
        parse_review_response("not json at all")


@pytest.mark.unit
def test_parse_rejects_empty_task_list():
    with pytest.raises(PlanReviewError):
        parse_review_response(json.dumps({"tasks": []}))


def _profile() -> CapabilityProfile:
    return CapabilityProfile(
        model_name="qwen3.6-27b", parameter_count_b=27.0, context_window=32768
    )


def test_prompt_requests_plan_text_and_dependencies():
    prompt = build_review_prompt("PLAN BODY", _profile(), "no history", 12000)
    assert "plan_text" in prompt
    assert "depends_on" in prompt
    assert "verbatim" in prompt.lower() or "exact" in prompt.lower()


def test_parser_defaults_plan_text_to_description():
    raw = '{"tasks": [{"id": "t1", "title": "A", "description": "do A"}]}'
    plan = parse_review_response(raw)
    assert plan["tasks"][0]["plan_text"] == "do A"


def test_prompt_uses_fewest_leaves_and_keeps_tests_with_impl():
    prompt = build_review_prompt("PLAN BODY", _profile(), "no history", 12000)
    assert "FEWEST" in prompt or "fewest" in prompt
    assert "TOGETHER" in prompt or "together" in prompt or "same leaf" in prompt
    # Old "SMALLEST" wording must not appear
    assert "SMALLEST" not in prompt


def test_parser_preserves_supplied_plan_text():
    raw = (
        '{"tasks": [{"id": "t1", "title": "A", "description": "do A", '
        '"plan_text": "run(signal: AbortSignal): Promise<T>"}]}'
    )
    plan = parse_review_response(raw)
    assert "AbortSignal" in plan["tasks"][0]["plan_text"]


def test_parse_tolerates_leading_prose_before_fenced_json():
    raw = (
        "Good, asyncio_mode is already set. Now I have all the context I need.\n\n"
        '```json\n{"tasks": [{"id": "t1", "title": "A", "description": "d"}]}\n```'
    )
    out = parse_review_response(raw)
    assert out["tasks"][0]["title"] == "A"


def test_parse_tolerates_prose_without_fence():
    raw = 'Here is the plan: {"tasks": [{"id": "t1", "title": "B", "description": "d"}]} done.'
    out = parse_review_response(raw)
    assert out["tasks"][0]["title"] == "B"


def test_parse_still_accepts_bare_json():
    raw = '{"tasks": [{"id": "t1", "title": "C", "description": "d"}]}'
    out = parse_review_response(raw)
    assert out["tasks"][0]["title"] == "C"


def test_parse_raises_planreviewerror_on_garbage():
    with pytest.raises(PlanReviewError):
        parse_review_response("this is not json at all, no braces")


@pytest.mark.unit
def test_prompt_includes_hard_constraints_with_rendered_limits():
    profile = CapabilityProfile(
        model_name="qwen3",
        parameter_count_b=30,
        context_window=8192,
        strengths="single-file",
        weaknesses="refactors",
        max_task_complexity="medium",
        max_files_touched=4,
        max_loc_delta=250,
        max_checklist_items=8,
        max_dep_depth=2,
    )
    prompt = build_review_prompt(
        plan_text="Build a thing",
        profile=profile,
        history_summary="(no prior run history for this model)",
        per_leaf_token_budget=3200,
    )
    assert "HARD CONSTRAINTS" in prompt
    assert "at most 4 files" in prompt
    assert "~250 lines" in prompt
    assert "no more than 8 checklist items" in prompt
    assert "Dependency depth no deeper than 2" in prompt
    # The prompt used to demand a verification ">40 characters", a number
    # nothing enforces: _DEFAULT_VERIFICATION_MIN_LEN is 5, so `pytest` alone
    # validates clean while a perfectly good `pytest tests/test_x.py::test_y`
    # was told it was illegal. It now states the rule the validator applies.
    assert "RUNNABLE command" in prompt


@pytest.mark.unit
def test_prompt_requests_extended_leaf_fields():
    prompt = build_review_prompt(
        plan_text="PLAN",
        profile=PROFILE,
        history_summary="none",
        per_leaf_token_budget=4000,
    )
    assert '"files"' in prompt
    assert '"task_type"' in prompt
    assert '"estimated_loc"' in prompt
    assert '"verification"' in prompt


@pytest.mark.unit
def test_parse_accepts_extended_leaf_fields():
    raw = json.dumps(
        {
            "tasks": [
                {
                    "id": "t1",
                    "title": "Add endpoint",
                    "description": "Add a new API endpoint",
                    "depends_on": [],
                    "checklist": [{"text": "write handler"}],
                    "needs_stronger_model": False,
                    "files": ["src/api/endpoint.py", "tests/test_endpoint.py"],
                    "task_type": "feature",
                    "estimated_loc": 85,
                    "verification": "curl the new endpoint and assert 200",
                },
            ]
        }
    )
    plan = parse_review_response(raw)
    assert plan["tasks"][0]["files"] == [
        "src/api/endpoint.py",
        "tests/test_endpoint.py",
    ]
    assert plan["tasks"][0]["task_type"] == "feature"
    assert plan["tasks"][0]["estimated_loc"] == 85
    assert "curl" in plan["tasks"][0]["verification"]


# Local helper for the leaf-type-block tests below, deliberately named
# differently from the module's existing `_profile()` (line 91) so this
# addition cannot shadow it and change what the earlier tests exercise.
def _leaf_type_profile() -> CapabilityProfile:
    return CapabilityProfile(
        model_name="test-model",
        parameter_count_b=30,
        context_window=8192,
    )


@pytest.mark.unit
def test_prompt_lists_every_leaf_type():
    prompt = build_review_prompt(
        "plan body", _leaf_type_profile(), "(no history)", 3276
    )
    for leaf_type in LeafType:
        assert leaf_type.value in prompt


@pytest.mark.unit
def test_prompt_demands_the_base_sections():
    prompt = build_review_prompt(
        "plan body", _leaf_type_profile(), "(no history)", 3276
    )
    for section in ("Goal", "Files", "Steps", "Acceptance"):
        assert section in prompt


@pytest.mark.unit
def test_prompt_json_example_carries_a_leaf_type_key():
    prompt = build_review_prompt(
        "plan body", _leaf_type_profile(), "(no history)", 3276
    )
    assert '"leaf_type"' in prompt


@pytest.mark.unit
def test_prompt_still_carries_the_hard_constraint_numbers():
    profile = _leaf_type_profile()
    prompt = build_review_prompt("plan body", profile, "(no history)", 3276)
    assert str(profile.max_files_touched) in prompt
    assert str(profile.max_loc_delta) in prompt
    assert str(profile.max_dep_depth) in prompt


# ---------------------------------------------------------------------------
# The prompt's worked example, graded by the real validator
#
# Nothing below hand-writes a leaf. The leaf comes out of the rendered prompt
# through the prompt's OWN parser, and the source plan is rebuilt from that
# leaf's own Steps body. A fixture that already carried the labels would prove
# only that the labels satisfy the label rule, which is how the contradiction
# these tests pin shipped in the first place.
# ---------------------------------------------------------------------------


def _example_leaf(prompt: str) -> LeafTask:
    """Parse the prompt's JSON example the way a brain response is parsed.

    ``parse_review_response`` slices from the first ``{`` to the last ``}``, so
    every caller must render the prompt with a BRACE-FREE plan body or the
    slice starts inside the user's plan instead of the example.
    """
    return LeafTask.model_validate(parse_review_response(prompt)["tasks"][0])


def _plan_containing(leaf: LeafTask) -> str:
    """Rebuild the source plan the example claims to have been copied from.

    The Steps body goes back under ``### Task N: <title>``, the heading shape
    ``core/execute_plan_decompose._PLAN_TASK_HEADER_RE`` defines and every plan
    in ``docs/superpowers/plans/`` uses.
    """
    body = leaf.plan_text.split("Steps:\n", 1)[1].split("\nAcceptance:")[0]
    return (
        "# Plan: harden the HTTP client\n\n"
        f"### Task 1: {leaf.title}\n\n"
        f"{body}\n\n"
        "### Task 2: Wire the helper into fetch_page\n\n"
        "Call `retry_on_429` from `fetch_page` in `src/client.py`.\n"
    )


@pytest.mark.unit
def test_the_prompts_own_worked_example_validates_clean():
    """A brain that copies the example must pass, HARD and SOFT.

    The prompt used to order a VERBATIM excerpt of the plan while injecting,
    forty lines earlier, a template rule that HARD-requires line-leading
    Goal/Files/Steps/Acceptance labels the plan never carried. Obeying the last
    instruction failed every leaf, and the JSON example showed
    ``"plan_text": "..."``, which demonstrates neither shape.
    """
    profile = _leaf_type_profile()
    prompt = build_review_prompt(
        "A plan with no braces in it.", profile, "(none)", 12000
    )
    leaf = _example_leaf(prompt)

    # The example is a real leaf, not a row of ellipses.
    assert leaf.leaf_type is LeafType.FUNCTION_ADD
    assert missing_sections(leaf.plan_text, leaf.leaf_type) == []

    source = _plan_containing(leaf)
    result = validate_leaves({}, profile, source, [leaf])
    assert [(v.rule, v.message) for v in result.hard] == []
    assert [(v.rule, v.message) for v in result.soft] == []


@pytest.mark.unit
def test_labels_are_what_separate_a_valid_leaf_from_the_bare_excerpt():
    """The prompt's stated resolution is the real one, not decoration.

    Two leaves cut from ONE example, differing only in whether the labels are
    present. The bare excerpt is what the old "copy the relevant lines"
    instruction produced; it must fail the template rule, and the labelled
    skeleton carrying the identical lines must pass it. Same source plan, same
    leaf_type, same verification: the labels are the only variable.
    """
    profile = _leaf_type_profile()
    prompt = build_review_prompt(
        "A plan with no braces in it.", profile, "(none)", 12000
    )
    example = _example_leaf(prompt)
    source = _plan_containing(example)
    body = example.plan_text.split("Steps:\n", 1)[1].split("\nAcceptance:")[0]

    bare = example.model_copy(update={"plan_text": body})
    bare_result = validate_leaves({}, profile, source, [bare])
    assert [v.rule for v in bare_result.hard] == ["leaf_template"]
    assert "Goal" in bare_result.hard[0].message

    # Positive control: the identical lines, under the labels, pass.
    labelled_result = validate_leaves({}, profile, source, [example])
    assert [v.rule for v in labelled_result.hard] == []


@pytest.mark.unit
def test_prompt_tells_the_brain_what_the_file_overlap_rule_wants():
    """The dependency instruction and the file_overlap rule used to disagree.

    ``_check_file_overlap`` warns whenever two leaves share a file with no dep
    edge, while the prompt said not to add an edge "merely to impose an order
    on independent work". Obeying the prompt tripped the rule and the re-ask
    could not converge, because the only fix was the thing it forbade.
    """
    prompt = build_review_prompt("plan body", _leaf_type_profile(), "(none)", 3276)
    assert "SAME FILE are never independent" in prompt
    assert "merge them into a single leaf" in prompt


@pytest.mark.unit
def test_prompt_names_the_escalate_task_types_the_validator_rejects_on():
    """``escalate_task_types`` is a HARD rule the brain was never told about.

    ``_check_escalate_mismatch`` HARD-rejects a leaf whose task_type is in the
    list unless it set needs_stronger_model, and the prompt never rendered the
    list, so the constraint was unsatisfiable by construction: it would fail
    both attempts and reject the whole plan. Latent only because the shipped
    ``config/praxis.yaml`` ships an empty list.
    """
    profile = CapabilityProfile(
        model_name="qwen3",
        parameter_count_b=30,
        context_window=8192,
        escalate_task_types=["refactor", "architecture"],
    )
    prompt = build_review_prompt("plan body", profile, "(none)", 3276)
    assert '"refactor", "architecture"' in prompt
    assert '"needs_stronger_model": true' in prompt

    # A leaf that obeys the rendered rule clears the HARD check it names.
    obedient = LeafTask(
        id="t1",
        title="Rename the loader",
        plan_text="Goal: g\nFiles: src/a.py\nSteps:\n1. do it\nAcceptance: `pytest`",
        task_type="refactor",
        needs_stronger_model=True,
        verification="Run `pytest tests/test_a.py` and confirm it passes",
    )
    result = validate_leaves({}, profile, "x", [obedient])
    assert [v.rule for v in result.hard] == []


@pytest.mark.unit
def test_the_escalate_block_is_absent_when_no_type_escalates():
    """A stock install must not be handed a rule with an empty list in it.

    ``config/praxis.yaml`` ships ``escalate_task_types: []``; rendering the
    block anyway would state a constraint naming nothing and invite the brain
    to escalate at random.
    """
    prompt = build_review_prompt("plan body", _leaf_type_profile(), "(none)", 3276)
    assert "BEYOND this worker" not in prompt
    # Positive control: the same call site DOES render it when the list is set.
    escalating = _leaf_type_profile().model_copy(
        update={"escalate_task_types": ["chore"]}
    )
    assert "BEYOND this worker" in build_review_prompt(
        "plan body", escalating, "(none)", 3276
    )
