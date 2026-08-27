"""Tests for the deterministic leaf validator (core/leaf_validator)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from orchestrator.core.leaf_validator import (
    ValidationResult,
    Violation,
    _section_for_task,
    format_violations_feedback,
    is_runnable_verification,
    validate_leaves,
    verification_defect,
)
from orchestrator.models.schemas import CapabilityProfile, LeafTask


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _profile(**overrides) -> CapabilityProfile:
    """Return a minimal CapabilityProfile with optional overrides."""
    return CapabilityProfile(
        model_name="test-model",
        parameter_count_b=7,
        context_window=8192,
        **overrides,
    )


def _source_plan(leaf_tasks: list[LeafTask]) -> str:
    """Build a minimal source plan text from leaf tasks."""
    parts = ["# Plan"]
    for lt in leaf_tasks:
        parts.append(f"## {lt.title}")
        parts.append(lt.plan_text)
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Violation
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_violation_defaults():
    v = Violation(rule="dangling_dep", task_id="t1")
    assert v.rule == "dangling_dep"
    assert v.task_id == "t1"
    assert v.severity == "hard"
    assert v.message == ""


@pytest.mark.unit
def test_violation_soft():
    v = Violation(rule="vague_phrase", task_id="t2", severity="soft")
    assert v.severity == "soft"


@pytest.mark.unit
def test_violation_with_message():
    v = Violation(rule="max_loc", task_id="t3", message="too large")
    assert v.message == "too large"


# ---------------------------------------------------------------------------
# ValidationResult
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_validation_result_initial():
    vr = ValidationResult()
    assert vr.hard == []
    assert vr.soft == []
    assert vr.dispatchable is True
    assert vr.clean is True


@pytest.mark.unit
def test_validation_result_hard_violation():
    vr = ValidationResult()
    vr.add(Violation(rule="dangling_dep", task_id="t1", severity="hard"))
    assert len(vr.hard) == 1
    assert vr.dispatchable is False
    assert vr.clean is False


@pytest.mark.unit
def test_validation_result_soft_violation():
    vr = ValidationResult()
    vr.add(Violation(rule="vague_phrase", task_id="t1", severity="soft"))
    assert len(vr.soft) == 1
    assert vr.dispatchable is True
    assert vr.clean is False


@pytest.mark.unit
def test_validation_result_both_violations():
    vr = ValidationResult()
    vr.add(Violation(rule="dangling_dep", task_id="t1", severity="hard"))
    vr.add(Violation(rule="vague_phrase", task_id="t1", severity="soft"))
    assert len(vr.hard) == 1
    assert len(vr.soft) == 1
    assert vr.dispatchable is False
    assert vr.clean is False


# ---------------------------------------------------------------------------
# format_violations_feedback
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_format_violations_feedback_clean():
    vr = ValidationResult()
    assert format_violations_feedback(vr) == ""


@pytest.mark.unit
def test_format_violations_feedback_has_violations():
    vr = ValidationResult()
    vr.add(
        Violation(
            rule="dangling_dep",
            task_id="t1",
            severity="hard",
            message="depends on missing t99",
        )
    )
    feedback = format_violations_feedback(vr)
    assert "HARD" in feedback
    assert "dangling_dep" in feedback
    assert "t1" in feedback


@pytest.mark.unit
def test_format_violations_feedback_soft_only():
    vr = ValidationResult()
    vr.add(
        Violation(
            rule="vague_phrase",
            task_id="t2",
            severity="soft",
            message="contains vague language",
        )
    )
    feedback = format_violations_feedback(vr)
    assert "SOFT" in feedback
    assert "vague_phrase" in feedback


# ---------------------------------------------------------------------------
# validate_leaves — clean plan (no violations)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_validate_leaves_clean_plan():
    leaves = [
        LeafTask(
            id="t1",
            title="Add config loader",
            plan_text=(
                "## Goal\nAdd a config loader.\n"
                "## Files\nsrc/config.py\n"
                "## Steps\n1. def load_config(path: str) -> dict: ...\n"
                "## Acceptance\nRun `pytest tests/test_config.py`"
            ),
            depends_on=[],
            files=["src/config.py"],
            estimated_loc=50,
            verification="pytest tests/test_config.py",
        ),
        LeafTask(
            id="t2",
            title="Wire config",
            plan_text=(
                "## Goal\nWire config into settings module.\n"
                "## Files\nsrc/settings.py\n"
                "## Steps\n1. Wire config into settings module.\n"
                "## Acceptance\nRun `pytest tests/test_settings.py`"
            ),
            depends_on=["t1"],
            files=["src/settings.py"],
            estimated_loc=30,
            verification="pytest tests/test_settings.py",
        ),
    ]
    profile = _profile()
    source = _source_plan(leaves)
    opus_plan = {"plan_summary": "test", "plan_slug": "test", "tasks": []}

    result = validate_leaves(opus_plan, profile, source, leaves)
    assert result.clean is True
    assert result.dispatchable is True
    assert result.hard == []


# ---------------------------------------------------------------------------
# HARD: dangling_dep
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_validate_leaves_dangling_dep():
    leaves = [
        LeafTask(
            id="t1",
            title="Task one",
            plan_text="do something",
            depends_on=["t999"],
        ),
    ]
    profile = _profile()
    source = _source_plan(leaves)
    opus_plan = {"plan_summary": "x", "plan_slug": "x", "tasks": []}

    result = validate_leaves(opus_plan, profile, source, leaves)
    assert any(v.rule == "dangling_dep" for v in result.hard)
    assert result.dispatchable is False


# ---------------------------------------------------------------------------
# HARD: dep_cycle
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_validate_leaves_dep_cycle():
    leaves = [
        LeafTask(id="t1", title="A", plan_text="a", depends_on=["t2"]),
        LeafTask(id="t2", title="B", plan_text="b", depends_on=["t1"]),
    ]
    profile = _profile()
    source = _source_plan(leaves)
    opus_plan = {"plan_summary": "x", "plan_slug": "x", "tasks": []}

    result = validate_leaves(opus_plan, profile, source, leaves)
    assert any(v.rule == "dep_cycle" for v in result.hard)
    assert result.dispatchable is False


@pytest.mark.unit
def test_validate_leaves_dep_cycle_longer():
    leaves = [
        LeafTask(id="t1", title="A", plan_text="a", depends_on=["t3"]),
        LeafTask(id="t2", title="B", plan_text="b", depends_on=["t1"]),
        LeafTask(id="t3", title="C", plan_text="c", depends_on=["t2"]),
    ]
    profile = _profile()
    source = _source_plan(leaves)
    opus_plan = {"plan_summary": "x", "plan_slug": "x", "tasks": []}

    result = validate_leaves(opus_plan, profile, source, leaves)
    assert any(v.rule == "dep_cycle" for v in result.hard)


# ---------------------------------------------------------------------------
# HARD: dep_depth
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_validate_leaves_dep_depth_exceeded():
    leaves = [
        LeafTask(id="t1", title="A", plan_text="a", depends_on=[]),
        LeafTask(id="t2", title="B", plan_text="b", depends_on=["t1"]),
        LeafTask(id="t3", title="C", plan_text="c", depends_on=["t2"]),
        LeafTask(id="t4", title="D", plan_text="d", depends_on=["t3"]),
    ]
    profile = _profile(max_dep_depth=2)
    source = _source_plan(leaves)
    opus_plan = {"plan_summary": "x", "plan_slug": "x", "tasks": []}

    result = validate_leaves(opus_plan, profile, source, leaves)
    assert any(v.rule == "dep_depth" for v in result.hard)


@pytest.mark.unit
def test_validate_leaves_dep_depth_within_limit():
    leaves = [
        LeafTask(id="t1", title="A", plan_text="a", depends_on=[]),
        LeafTask(id="t2", title="B", plan_text="b", depends_on=["t1"]),
        LeafTask(id="t3", title="C", plan_text="c", depends_on=["t2"]),
    ]
    profile = _profile(max_dep_depth=3)
    source = _source_plan(leaves)
    opus_plan = {"plan_summary": "x", "plan_slug": "x", "tasks": []}

    result = validate_leaves(opus_plan, profile, source, leaves)
    assert not any(v.rule == "dep_depth" for v in result.hard)


@pytest.mark.unit
def test_validate_leaves_dep_depth_default_profile_limits():
    # 1) Passing depth-4 chain (5 tasks)
    passing_leaves = [
        LeafTask(id="t1", title="A", plan_text="a", depends_on=[]),
        LeafTask(id="t2", title="B", plan_text="b", depends_on=["t1"]),
        LeafTask(id="t3", title="C", plan_text="c", depends_on=["t2"]),
        LeafTask(id="t4", title="D", plan_text="d", depends_on=["t3"]),
        LeafTask(id="t5", title="E", plan_text="e", depends_on=["t4"]),
    ]
    profile = _profile()  # default max_dep_depth = 4
    source = _source_plan(passing_leaves)
    opus_plan = {"plan_summary": "x", "plan_slug": "x", "tasks": []}

    result = validate_leaves(opus_plan, profile, source, passing_leaves)
    assert not any(v.rule == "dep_depth" for v in result.hard)

    # 2) Rejected depth-5 chain (6 tasks)
    rejected_leaves = [
        LeafTask(id="t1", title="A", plan_text="a", depends_on=[]),
        LeafTask(id="t2", title="B", plan_text="b", depends_on=["t1"]),
        LeafTask(id="t3", title="C", plan_text="c", depends_on=["t2"]),
        LeafTask(id="t4", title="D", plan_text="d", depends_on=["t3"]),
        LeafTask(id="t5", title="E", plan_text="e", depends_on=["t4"]),
        LeafTask(id="t6", title="F", plan_text="f", depends_on=["t5"]),
    ]
    source_rejected = _source_plan(rejected_leaves)
    result_rejected = validate_leaves(
        opus_plan, profile, source_rejected, rejected_leaves
    )
    assert any(v.rule == "dep_depth" for v in result_rejected.hard)


# ---------------------------------------------------------------------------
# HARD: max_files
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_validate_leaves_max_files_exceeded():
    leaves = [
        LeafTask(
            id="t1",
            title="Big task",
            plan_text="do many things",
            files=[f"src/file{i}.py" for i in range(10)],
        ),
    ]
    profile = _profile(max_files_touched=5)
    source = _source_plan(leaves)
    opus_plan = {"plan_summary": "x", "plan_slug": "x", "tasks": []}

    result = validate_leaves(opus_plan, profile, source, leaves)
    assert any(v.rule == "max_files" for v in result.hard)


# ---------------------------------------------------------------------------
# HARD: max_loc
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_validate_leaves_max_loc_exceeded():
    leaves = [
        LeafTask(
            id="t1",
            title="Big task",
            plan_text="big implementation",
            estimated_loc=500,
        ),
    ]
    profile = _profile(max_loc_delta=200)
    source = _source_plan(leaves)
    opus_plan = {"plan_summary": "x", "plan_slug": "x", "tasks": []}

    result = validate_leaves(opus_plan, profile, source, leaves)
    assert any(v.rule == "max_loc" for v in result.hard)


@pytest.mark.unit
def test_validate_leaves_max_loc_none():
    """estimated_loc=None should not trigger max_loc."""
    leaves = [
        LeafTask(
            id="t1",
            title="Task",
            plan_text="something",
            estimated_loc=None,
        ),
    ]
    profile = _profile(max_loc_delta=10)
    source = _source_plan(leaves)
    opus_plan = {"plan_summary": "x", "plan_slug": "x", "tasks": []}

    result = validate_leaves(opus_plan, profile, source, leaves)
    assert not any(v.rule == "max_loc" for v in result.hard)


# ---------------------------------------------------------------------------
# HARD: verification (missing / short / not-runnable)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_validate_leaves_verification_missing():
    leaves = [
        LeafTask(
            id="t1",
            title="Task",
            plan_text="do something",
            verification=None,
        ),
    ]
    profile = _profile()
    source = _source_plan(leaves)
    opus_plan = {"plan_summary": "x", "plan_slug": "x", "tasks": []}

    result = validate_leaves(opus_plan, profile, source, leaves)
    assert any(v.rule == "verification" for v in result.hard)


@pytest.mark.unit
def test_validate_leaves_verification_short():
    leaves = [
        LeafTask(
            id="t1",
            title="Task",
            plan_text="do something",
            verification="ok",
        ),
    ]
    profile = _profile()
    source = _source_plan(leaves)
    opus_plan = {"plan_summary": "x", "plan_slug": "x", "tasks": []}

    result = validate_leaves(opus_plan, profile, source, leaves)
    assert any(v.rule == "verification" for v in result.hard)


@pytest.mark.unit
def test_validate_leaves_verification_not_runnable():
    leaves = [
        LeafTask(
            id="t1",
            title="Task",
            plan_text="do something",
            verification="check manually",
        ),
    ]
    profile = _profile()
    source = _source_plan(leaves)
    opus_plan = {"plan_summary": "x", "plan_slug": "x", "tasks": []}

    result = validate_leaves(opus_plan, profile, source, leaves)
    assert any(v.rule == "verification" for v in result.hard)


@pytest.mark.unit
def test_validate_leaves_verification_runnable_with_review_prose():
    # A real `uv run pytest` command must NOT be flagged non-runnable just
    # because its surrounding prose or file paths contain "review"/"inspect".
    leaves = [
        LeafTask(
            id="t1",
            title="Task",
            plan_text="do something",
            verification=(
                "Run `uv run pytest tests/test_orchestrator_review.py -q`; "
                "confirms no regression in existing review tests."
            ),
        ),
    ]
    profile = _profile()
    source = _source_plan(leaves)
    opus_plan = {"plan_summary": "x", "plan_slug": "x", "tasks": []}

    result = validate_leaves(opus_plan, profile, source, leaves)
    assert not any(v.rule == "verification" for v in result.hard)


@pytest.mark.unit
def test_validate_leaves_verification_valid():
    leaves = [
        LeafTask(
            id="t1",
            title="Task",
            plan_text="do something",
            verification="pytest tests/test_foo.py",
        ),
    ]
    profile = _profile()
    source = _source_plan(leaves)
    opus_plan = {"plan_summary": "x", "plan_slug": "x", "tasks": []}

    result = validate_leaves(opus_plan, profile, source, leaves)
    assert not any(v.rule == "verification" for v in result.hard)


# ---------------------------------------------------------------------------
# is_runnable_verification: the public predicate the dispatch path reuses
# ---------------------------------------------------------------------------

# One corpus, used by BOTH the predicate test and the agreement test below, so
# a value can never be exercised against one and not the other.
_VERIFICATION_CORPUS: list[tuple[str | None, bool]] = [
    (None, False),
    ("", False),
    ("   ", False),
    ("ok", False),
    ("manual review", False),
    # Each blacklist word needs at least one entry where it is the ONLY reason
    # the value is rejected, or deleting its pattern is invisible here. "review
    # the diff by eye" covers neither "by eye" nor "eyeball" exclusively.
    ("confirm the totals by eye", False),
    ("eyeball the output", False),
    ("review the diff by eye", False),
    ("inspect the rendered output", False),
    ("check it visually", False),
    ("read through the new module", False),
    ("verify manually that the page loads", False),
    ("pytest tests/test_foo.py", True),
    ("uv run pytest tests/test_x.py passes and returns 0", True),
    ("`make test`", True),
    # No runnable token and no manual verb: the validator is deliberately
    # permissive here, and the predicate must not be stricter than the rule it
    # mirrors, or a leaf that PASSED validation would be demoted at dispatch.
    ("the endpoint answers 422 for a bad payload", True),
    (
        "Run `uv run pytest tests/test_orchestrator_review.py -q`; "
        "confirms no regression in existing review tests.",
        True,
    ),
]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("value", "expected"),
    _VERIFICATION_CORPUS,
    ids=[str(v)[:40] for v, _ in _VERIFICATION_CORPUS],
)
def test_is_runnable_verification(value: str | None, expected: bool):
    assert is_runnable_verification(value) is expected


@pytest.mark.unit
@pytest.mark.parametrize(
    ("value", "expected_message"),
    [
        (None, "missing verification command"),
        ("", "missing verification command"),
        ("   ", "missing verification command"),
        ("\t\n", "missing verification command"),
        ("ok", "missing or too short verification command"),
        ("four", "missing or too short verification command"),
        ("manual review", "verification is not runnable: 'manual review'"),
        ("pytest tests/test_foo.py", None),
    ],
    ids=["none", "empty", "spaces", "tab-newline", "two", "four", "prose", "ok"],
)
def test_verification_defect_reports_which_defect(
    value: str | None, expected_message: str | None
):
    """The three messages are distinct diagnoses, not interchangeable text.

    They are what the brain is re-asked with on an informed retry, so swapping
    "missing" for "too short" sends it to fix the wrong thing.
    """
    assert verification_defect(value) == expected_message


@pytest.mark.unit
@pytest.mark.parametrize(
    ("value", "expected"),
    _VERIFICATION_CORPUS,
    ids=[str(v)[:40] for v, _ in _VERIFICATION_CORPUS],
)
def test_the_predicate_agrees_with_the_hard_verification_rule(
    value: str | None, expected: bool
):
    """The predicate and the HARD rule must be the SAME decision.

    ``orchestrator_dispatch`` uses the predicate to decide whether an
    unvalidated ``verification`` may become a leaf's acceptance floor. If the
    two ever diverge, either a leaf the validator accepted gets demoted at
    dispatch, or one it rejected gets promoted.
    """
    leaves = [
        LeafTask(
            id="t1",
            title="Task",
            plan_text="do something",
            verification=value,
        ),
    ]
    profile = _profile()
    source = _source_plan(leaves)
    opus_plan = {"plan_summary": "x", "plan_slug": "x", "tasks": []}

    result = validate_leaves(opus_plan, profile, source, leaves)
    flagged = any(v.rule == "verification" for v in result.hard)
    assert flagged is (not expected)


# ---------------------------------------------------------------------------
# HARD: escalate_mismatch
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_validate_leaves_escalate_mismatch():
    leaves = [
        LeafTask(
            id="t1",
            title="Hard task",
            plan_text="complex architecture change",
            task_type="architecture",
            needs_stronger_model=False,
        ),
    ]
    profile = _profile(escalate_task_types=["architecture"])
    source = _source_plan(leaves)
    opus_plan = {"plan_summary": "x", "plan_slug": "x", "tasks": []}

    result = validate_leaves(opus_plan, profile, source, leaves)
    assert any(v.rule == "escalate_mismatch" for v in result.hard)


@pytest.mark.unit
def test_validate_leaves_escalate_mismatch_no_mismatch():
    leaves = [
        LeafTask(
            id="t1",
            title="Easy task",
            plan_text="simple fix",
            task_type="bugfix",
            needs_stronger_model=False,
        ),
    ]
    profile = _profile(escalate_task_types=["architecture"])
    source = _source_plan(leaves)
    opus_plan = {"plan_summary": "x", "plan_slug": "x", "tasks": []}

    result = validate_leaves(opus_plan, profile, source, leaves)
    assert not any(v.rule == "escalate_mismatch" for v in result.hard)


# ---------------------------------------------------------------------------
# SOFT: plan_text_verbatim
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_validate_leaves_plan_text_verbatim_pass():
    source = (
        "# Plan\n\n## Add config loader\n\ndef load_config(path: str) -> dict: ...\n"
    )
    leaves = [
        LeafTask(
            id="t1",
            title="Add config loader",
            plan_text="def load_config(path: str) -> dict: ...",
        ),
    ]
    profile = _profile()
    opus_plan = {"plan_summary": "x", "plan_slug": "x", "tasks": []}

    result = validate_leaves(opus_plan, profile, source, leaves)
    assert not any(v.rule == "plan_text_verbatim" for v in result.soft)


@pytest.mark.unit
def test_validate_leaves_plan_text_verbatim_fail():
    source = (
        "# Plan\n\n## Add config loader\n\ndef load_config(path: str) -> dict: ...\n"
    )
    leaves = [
        LeafTask(
            id="t1",
            title="Add config loader",
            plan_text="completely different text not in source",
        ),
    ]
    profile = _profile()
    opus_plan = {"plan_summary": "x", "plan_slug": "x", "tasks": []}

    result = validate_leaves(opus_plan, profile, source, leaves)
    assert any(v.rule == "plan_text_verbatim" for v in result.soft)


# ---------------------------------------------------------------------------
# SOFT: file_overlap
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_validate_leaves_file_overlap():
    leaves = [
        LeafTask(
            id="t1",
            title="A",
            plan_text="a",
            files=["src/shared.py"],
            depends_on=[],
        ),
        LeafTask(
            id="t2",
            title="B",
            plan_text="b",
            files=["src/shared.py"],
            depends_on=[],
        ),
    ]
    profile = _profile()
    source = _source_plan(leaves)
    opus_plan = {"plan_summary": "x", "plan_slug": "x", "tasks": []}

    result = validate_leaves(opus_plan, profile, source, leaves)
    assert any(v.rule == "file_overlap" for v in result.soft)


@pytest.mark.unit
def test_validate_leaves_file_overlap_with_dep_ok():
    """Overlap is allowed when there is a dependency edge."""
    leaves = [
        LeafTask(
            id="t1",
            title="A",
            plan_text="a",
            files=["src/shared.py"],
            depends_on=[],
        ),
        LeafTask(
            id="t2",
            title="B",
            plan_text="b",
            files=["src/shared.py"],
            depends_on=["t1"],
        ),
    ]
    profile = _profile()
    source = _source_plan(leaves)
    opus_plan = {"plan_summary": "x", "plan_slug": "x", "tasks": []}

    result = validate_leaves(opus_plan, profile, source, leaves)
    assert not any(v.rule == "file_overlap" for v in result.soft)


# ---------------------------------------------------------------------------
# SOFT: checklist_size
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_validate_leaves_checklist_size_exceeded():
    leaves = [
        LeafTask(
            id="t1",
            title="Big checklist",
            plan_text="many steps",
            checklist=[{"text": f"step {i}"} for i in range(20)],
        ),
    ]
    profile = _profile(max_checklist_items=10)
    source = _source_plan(leaves)
    opus_plan = {"plan_summary": "x", "plan_slug": "x", "tasks": []}

    result = validate_leaves(opus_plan, profile, source, leaves)
    assert any(v.rule == "checklist_size" for v in result.soft)


@pytest.mark.unit
def test_validate_leaves_checklist_size_within():
    leaves = [
        LeafTask(
            id="t1",
            title="Small checklist",
            plan_text="few steps",
            checklist=[{"text": f"step {i}"} for i in range(5)],
        ),
    ]
    profile = _profile(max_checklist_items=10)
    source = _source_plan(leaves)
    opus_plan = {"plan_summary": "x", "plan_slug": "x", "tasks": []}

    result = validate_leaves(opus_plan, profile, source, leaves)
    assert not any(v.rule == "checklist_size" for v in result.soft)


# ---------------------------------------------------------------------------
# SOFT: vague_phrase
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_validate_leaves_vague_phrase():
    leaves = [
        LeafTask(
            id="t1",
            title="Improve stuff",
            plan_text="make things better and optimize",
        ),
    ]
    profile = _profile()
    source = _source_plan(leaves)
    opus_plan = {"plan_summary": "x", "plan_slug": "x", "tasks": []}

    result = validate_leaves(opus_plan, profile, source, leaves)
    assert any(v.rule == "vague_phrase" for v in result.soft)


@pytest.mark.unit
def test_validate_leaves_vague_phrase_clean():
    leaves = [
        LeafTask(
            id="t1",
            title="Add user model",
            plan_text="Create User model with name and email fields",
        ),
    ]
    profile = _profile()
    source = _source_plan(leaves)
    opus_plan = {"plan_summary": "x", "plan_slug": "x", "tasks": []}

    result = validate_leaves(opus_plan, profile, source, leaves)
    assert not any(v.rule == "vague_phrase" for v in result.soft)


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_validate_leaves_empty_leaves():
    profile = _profile()
    source = "# Plan"
    opus_plan = {"plan_summary": "x", "plan_slug": "x", "tasks": []}

    result = validate_leaves(opus_plan, profile, source, [])
    assert result.clean is True
    assert result.dispatchable is True


@pytest.mark.unit
def test_validate_leaves_no_leaves_arg():
    """When leaves arg is omitted (None), should handle gracefully."""
    profile = _profile()
    source = "# Plan"
    opus_plan = {"plan_summary": "x", "plan_slug": "x", "tasks": []}

    result = validate_leaves(opus_plan, profile, source)
    assert result.clean is True


# ---------------------------------------------------------------------------
# HARD: leaf_template
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_leaf_template_rule_passes_a_complete_generic_leaf():
    from orchestrator.core.leaf_validator import validate_leaves
    from orchestrator.models.schemas import CapabilityProfile, LeafTask

    leaf = LeafTask(
        id="t1",
        title="Add helper",
        plan_text=(
            "## Goal\nAdd a helper.\n"
            "## Files\nsrc/a.py\n"
            "## Steps\n1. Write it.\n"
            "## Acceptance\nRun `uv run pytest tests/test_a.py`"
        ),
        verification="Run `uv run pytest tests/test_a.py` and confirm it passes",
        leaf_type="generic",
    )
    result = validate_leaves(
        {},
        CapabilityProfile(model_name="m", parameter_count_b=30, context_window=8192),
        leaf.plan_text,
        [leaf],
    )
    assert [v for v in result.hard if v.rule == "leaf_template"] == []


@pytest.mark.unit
def test_leaf_template_rule_hard_rejects_a_missing_section():
    from orchestrator.core.leaf_validator import validate_leaves
    from orchestrator.models.schemas import CapabilityProfile, LeafTask

    leaf = LeafTask(
        id="t1",
        title="Add helper",
        plan_text="## Goal\nAdd a helper.\n## Files\nsrc/a.py",
        verification="Run `uv run pytest tests/test_a.py` and confirm it passes",
        leaf_type="generic",
    )
    result = validate_leaves(
        {},
        CapabilityProfile(model_name="m", parameter_count_b=30, context_window=8192),
        leaf.plan_text,
        [leaf],
    )
    violations = [v for v in result.hard if v.rule == "leaf_template"]
    assert len(violations) == 1
    assert "Steps" in violations[0].message
    assert "Acceptance" in violations[0].message


@pytest.mark.unit
def test_leaf_template_rule_enforces_the_type_specific_section():
    from orchestrator.core.leaf_validator import validate_leaves
    from orchestrator.models.schemas import CapabilityProfile, LeafTask

    leaf = LeafTask(
        id="t1",
        title="Fix the crash",
        plan_text=(
            "## Goal\nStop the crash.\n"
            "## Files\nsrc/a.py\n"
            "## Steps\n1. Guard the None.\n"
            "## Acceptance\nRun `uv run pytest tests/test_a.py`"
        ),
        verification="Run `uv run pytest tests/test_a.py` and confirm it passes",
        leaf_type="bugfix_repro",
    )
    result = validate_leaves(
        {},
        CapabilityProfile(model_name="m", parameter_count_b=30, context_window=8192),
        leaf.plan_text,
        [leaf],
    )
    violations = [v for v in result.hard if v.rule == "leaf_template"]
    assert len(violations) == 1
    assert "Reproduction" in violations[0].message


# ---------------------------------------------------------------------------
# _section_for_task: which plan section a leaf is graded against
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_section_for_task_matches_praxis_own_task_heading():
    """``### Task N: <title>`` is the heading shape Praxis plans actually use.

    ``execute_plan_decompose._PLAN_TASK_HEADER_RE`` defines it as THE authored
    header, and every plan in ``docs/superpowers/plans/`` follows it. Anchoring
    the title straight after the hashes never matched one, so the module that
    knows the format and the module that parses it disagreed.
    """
    source = (
        "# Plan\n\n"
        "### Task 1: Add the config loader\n\n"
        "def load_config(path: str) -> dict: ...\n\n"
        "### Task 2: Wire it in\n\n"
        "Call load_config from settings.\n"
    )
    leaf = LeafTask(id="t1", title="Add the config loader", plan_text="x")
    assert _section_for_task(source, leaf) == "def load_config(path: str) -> dict: ..."

    # Positive control: the plain ``## <title>`` shape still works.
    plain = "# Plan\n\n## Add the config loader\n\ndef load_config(path: str) -> dict: ...\n"
    assert _section_for_task(plain, leaf) == "def load_config(path: str) -> dict: ..."


@pytest.mark.unit
def test_section_for_task_matches_a_title_ending_in_a_non_word_character():
    """A trailing ``\\b`` could never match a title ending in ')', '"', '.' or '?'.

    ``\\b`` after a non-word character demands a word character next, and the
    end of a heading line never supplies one, so those titles silently fell
    through to the no-match path.
    """
    source = "# Plan\n\n## Add retry_on_429()\n\nbody line one\n"
    leaf = LeafTask(id="t1", title="Add retry_on_429()", plan_text="x")
    assert _section_for_task(source, leaf) == "body line one"


@pytest.mark.unit
def test_section_for_task_says_unknown_instead_of_returning_the_whole_plan():
    """No heading names this leaf, so there is no section to grade it against.

    Returning the WHOLE document instead made the ratio measure what fraction
    of the plan this one leaf is, not whether the leaf quoted its own section,
    so on any multi-task plan every leaf read as a mismatch however faithfully
    it copied. ``_check_plan_text_verbatim`` skips an empty section, which is
    the same "say unknown rather than guess" answer ``core/context_window``
    and ``core/verify_gate.normalize_verify_cmd`` give.
    """
    source = (
        "# Plan\n\n"
        "### Task 1: Something else entirely\n\n"
        "unrelated body\n\n"
        "### Task 2: Also unrelated\n\n"
        "more unrelated body\n"
    )
    leaf = LeafTask(id="t1", title="Add the config loader", plan_text="x")
    assert _section_for_task(source, leaf) == ""

    # And the rule stays silent rather than inventing a verdict from the whole
    # document. Positive control below proves the rule can still fire.
    profile = _profile()
    result = validate_leaves({}, profile, source, [leaf])
    assert [v.rule for v in result.soft if v.rule == "plan_text_verbatim"] == []

    named = LeafTask(
        id="t2", title="Something else entirely", plan_text="nothing like the body"
    )
    fired = validate_leaves({}, profile, source, [named])
    assert [v.rule for v in fired.soft if v.rule == "plan_text_verbatim"] == [
        "plan_text_verbatim"
    ]


@pytest.mark.unit
def test_verbatim_rule_accepts_a_short_section_copied_under_labels():
    """The labelled skeleton is the shape the decompose prompt now mandates.

    The symmetric ratio measures how much of plan_text IS the section, so the
    Goal/Files/Steps/Acceptance labels count against it. On a short section
    they outweigh the copied lines: a FAITHFUL copy of a one-line section
    scores about 0.36 against a 0.70 threshold. Line coverage asks what the
    prompt asks -- are the plan's own lines in there -- and a paraphrase of the
    same section must still be caught.
    """
    source = (
        "# Plan\n\n### Task 1: Raise the client timeout\n\n"
        "Bump the timeout to 30 seconds.\n"
    )
    profile = _profile()

    faithful = LeafTask(
        id="t1",
        title="Raise the client timeout",
        plan_text=(
            "Goal: the client timeout is 30 seconds.\n"
            "Files: src/config.py\n"
            "Steps:\n"
            "Bump the timeout to 30 seconds.\n"
            "Acceptance: `pytest tests/test_config.py` passes."
        ),
    )
    result = validate_leaves({}, profile, source, [faithful])
    assert [v.rule for v in result.soft if v.rule == "plan_text_verbatim"] == []

    # Positive control: same labels, same section, but the line is summarized
    # rather than copied. That is exactly what the rule exists to catch.
    paraphrased = faithful.model_copy(
        update={
            "plan_text": (
                "Goal: the client is more patient.\n"
                "Files: src/config.py\n"
                "Steps:\n"
                "Make it wait longer.\n"
                "Acceptance: `pytest tests/test_config.py` passes."
            )
        }
    )
    warned = validate_leaves({}, profile, source, [paraphrased])
    assert [v.rule for v in warned.soft if v.rule == "plan_text_verbatim"] == [
        "plan_text_verbatim"
    ]


# ---------------------------------------------------------------------------
# SOFT: vague_phrase, and the type that makes one of its words precise
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_refactor_is_not_vague_in_a_refactor_rename_leaf():
    """``refactor_rename`` is a first-class leaf type, so the word is accurate.

    The rule fired on a textbook rename leaf and the feedback read "contains
    vague phrase matching '\\brefactor\\b'" on a leaf that genuinely IS a
    refactor. Nothing the brain can write fixes that, so the informed re-ask
    never converges; it only ends when the round budget runs out.

    The exemption is one pattern for one type, not an amnesty: the same leaf
    with any other vague word must still be warned, and the same text under
    any other leaf_type must still be warned.
    """
    profile = _profile()
    rename_text = (
        "Goal: load_config is named read_config everywhere.\n"
        "Files: src/config.py\n"
        "Steps:\n1. Refactor load_config to read_config.\n"
        "Renames: load_config -> read_config\n"
        "Acceptance: `pytest tests/test_config.py` passes."
    )
    fields = {
        "id": "t1",
        "title": "Refactor: rename load_config to read_config",
        "plan_text": rename_text,
        "verification": "Run `pytest tests/test_config.py` and confirm it passes",
    }

    exempt = LeafTask(**fields, leaf_type="refactor_rename")
    result = validate_leaves({}, profile, rename_text, [exempt])
    assert [v.rule for v in result.soft if v.rule == "vague_phrase"] == []

    # Control 1: the exemption is scoped to the ONE word, not to the rule.
    also_polish = LeafTask(
        **{**fields, "plan_text": rename_text + "\nThen polish the module."},
        leaf_type="refactor_rename",
    )
    still_warned = validate_leaves({}, profile, rename_text, [also_polish])
    assert [v.rule for v in still_warned.soft if v.rule == "vague_phrase"] == [
        "vague_phrase"
    ]

    # Control 2: the exemption is scoped to the ONE type. Identical text under
    # `generic` claims no rename table, so "refactor" really is unspecific.
    generic = LeafTask(**fields, leaf_type="generic")
    warned = validate_leaves({}, profile, rename_text, [generic])
    assert [v.rule for v in warned.soft if v.rule == "vague_phrase"] == ["vague_phrase"]


@pytest.mark.unit
def test_leaf_template_violation_is_hard_not_soft():
    from orchestrator.core.leaf_validator import validate_leaves
    from orchestrator.models.schemas import CapabilityProfile, LeafTask

    leaf = LeafTask(
        id="t1",
        title="Add helper",
        plan_text="Goal: do a thing",
        verification="Run `uv run pytest` and confirm it passes cleanly",
        leaf_type="generic",
    )
    result = validate_leaves(
        {},
        CapabilityProfile(model_name="m", parameter_count_b=30, context_window=8192),
        leaf.plan_text,
        [leaf],
    )
    assert any(v.rule == "leaf_template" for v in result.hard)
    assert not any(v.rule == "leaf_template" for v in result.soft)
    assert result.dispatchable is False


@pytest.mark.unit
def test_verbatim_rule_grades_a_leaf_whose_title_names_no_heading():
    """The silent skip is where a fabricating decomposition walked through.

    ``_section_for_task`` resolves a leaf's section by looking for the
    DECOMPOSER-AUTHORED title inside a plan heading, and the decomposer writes
    that title for the worker, not for matching. Measured on production
    artefacts on 2026-08-27: 0 of 3 sections resolved on the plan whose leaf
    deleted the repository's acceptance test and specified sixteen replacement
    tests of its own invention, and 1 of 3 on a faithful decomposition of a
    well-formed three-task plan. ``validation_warnings`` on both carried only
    ``file_overlap``. So the check that grades drift was disabled BY drifting,
    and the leaf that most needed grading was the one that could not be graded.

    The section path is unchanged and still preferred, because it is precise
    when it resolves. This covers the FALLBACK: when no heading names the leaf,
    grade its plan_text against the whole document instead of skipping.

    Both leaves below carry the same unresolvable title, so the only difference
    between firing and not firing is whether the text came from the plan.
    """
    source = (
        "# Plan\n\n"
        "## Task 1: Ship the loader\n\n"
        "Goal: `src/loader.py` exposes `load_config`.\n"
        "Files: `src/loader.py`\n"
        "Steps:\n"
        "- Add `load_config(path: str) -> dict` to `src/loader.py`, raising\n"
        "  `ConfigError` when the file is absent.\n"
        "- Parse TOML only; reject every other extension by suffix.\n"
        "Acceptance: `pytest tests/test_loader.py` passes.\n"
    )
    profile = _profile()
    unresolvable_title = "Loader scaffolding and config parsing"

    # Control: the title genuinely resolves no section, so the OLD code skipped.
    assert (
        _section_for_task(
            source, LeafTask(id="c", title=unresolvable_title, plan_text="x")
        )
        == ""
    )

    # A faithful leaf UNWRAPS the plan's hard-wrapped lines onto one line, which
    # is what a real decomposer emits; it must not be warned about.
    faithful = LeafTask(
        id="t1",
        title=unresolvable_title,
        plan_text=(
            "Goal: `src/loader.py` exposes `load_config`.\n"
            "Files: src/loader.py\n"
            "Steps:\n"
            "- Add `load_config(path: str) -> dict` to `src/loader.py`, raising "
            "`ConfigError` when the file is absent.\n"
            "- Parse TOML only; reject every other extension by suffix.\n"
            "Acceptance: `pytest tests/test_loader.py` passes."
        ),
    )
    clean = validate_leaves({}, profile, source, [faithful])
    assert [v.rule for v in clean.soft if v.rule == "plan_text_verbatim"] == []

    # A fabricating leaf: same unresolvable title, content the plan never named.
    fabricated = LeafTask(
        id="t1",
        title=unresolvable_title,
        plan_text=(
            "Goal: `src/loader.py` and its tests exist.\n"
            "Files: src/loader.py, tests/test_loader.py\n"
            "Steps:\n"
            "- Rewrite `tests/test_loader.py` to contain these three tests: "
            "test_reads_toml, test_missing_file, test_bad_suffix.\n"
            "- Each test asserts only that the call returns without raising.\n"
            "Acceptance: `pytest tests/test_loader.py` passes."
        ),
    )
    fired = validate_leaves({}, profile, source, [fabricated])
    assert [v.rule for v in fired.soft if v.rule == "plan_text_verbatim"] == [
        "plan_text_verbatim"
    ]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("case", "expect_fires"),
    [("fabricated", True), ("faithful", False)],
)
def test_verbatim_rule_on_the_real_decompositions(case: str, expect_fires: bool):
    """Regression fixture: the two REAL decompositions this rule was built from.

    Extracted programmatically from ``plans.pending_input`` and
    ``plans.opus_plan`` on 2026-08-27, never retyped from a report - a
    sanitized fixture shipped an inert guard once already, and a fixture that
    looks too clean is the tell.

    ``fabricated`` is the decomposition whose leaf deleted the repository's
    acceptance test and specified sixteen replacement tests of its own
    invention (playground PR #103). Under the old code it scored ZERO
    violations, because ``_section_for_task`` resolved no section for any of
    its three leaves and the rule silently skipped them all.

    ``faithful`` is the same decomposer, the same repository and the same day,
    on a plan that did not name the test file. It must stay silent: a rule that
    fires on both is worthless, and this pair is what keeps the threshold
    honest if anyone tunes it.
    """
    cases = json.loads(
        (
            Path(__file__).parent
            / "fixtures"
            / "decompose"
            / "plan_text_backing_cases.json"
        ).read_text(encoding="utf-8")
    )
    fixture = cases[case]
    leaves = [
        LeafTask(id=leaf["id"], title=leaf["title"], plan_text=leaf["plan_text"])
        for leaf in fixture["leaves"]
    ]

    result = validate_leaves({}, _profile(), fixture["source_plan"], leaves)
    fired = [v for v in result.soft if v.rule == "plan_text_verbatim"]

    if expect_fires:
        assert len(fired) == len(leaves), fixture["note"]
    else:
        assert fired == [], fixture["note"]


def _corpus() -> list[dict]:
    """The wider plan_text-backing corpus (15 real decompositions, 32 leaves)."""
    return json.loads(
        (
            Path(__file__).parent
            / "fixtures"
            / "decompose"
            / "plan_text_backing_corpus.json"
        ).read_text(encoding="utf-8")
    )


def _verbatim_fires(plan: dict) -> list[str]:
    """Return the leaf ids ``plan_text_verbatim`` fires on for one dumped plan."""
    leaves = [
        LeafTask(id=leaf["id"], title=leaf["title"], plan_text=leaf["plan_text"])
        for leaf in plan["leaves"]
    ]
    result = validate_leaves({}, _profile(), plan["source_plan"], leaves)
    return [v.task_id for v in result.soft if v.rule == "plan_text_verbatim"]


@pytest.mark.unit
def test_verbatim_rule_does_not_separate_on_the_wider_corpus():
    """MEASURED REFUTATION: this rule is not evidence of fabrication.

    ``plan_text_backing_cases.json`` holds TWO plans and on those two the rule
    separates perfectly (3 of 3 fabricated, 0 of 3 faithful). That is what it
    was built and tuned on, and it does NOT generalise. Measured on 2026-08-27
    over every execute-plan decomposition this install had produced plus seven
    plans run live to vary the SHAPE (one-section 1:N, prose, code blocks,
    generic headings the decomposer must retitle, a well-formed multi-task
    plan, a fabrication bait, and a verbatim REPLAY of the plan that produced
    the round-7 fabrication): 34 leaves over 16 real decompositions, labelled
    by reading each leaf's Files/Acceptance/Steps against its plan.

    The rule fires on 19 of the 31 FAITHFUL leaves. Its precision on this
    corpus is 3 in 22. The dominant cause is not judgment but markup: a plan
    writes ``- Define `IntSetError(ValueError)`, raised for ...`` and the
    decomposer emits the same sentence for the worker with the backticks
    stripped, so a substring test misses a line-for-line copy. Probe D's three
    leaves are literally the plan's own bullets and all three fire.

    So promoting it to HARD is refuted: it would have blocked faithful
    decompositions far more often than the one it was built to catch. Anyone
    tuning the metric must re-measure HERE, not on the two-plan fixture.
    """
    corpus = _corpus()
    faithful_fires = 0
    faithful_leaves = 0
    fabricated_fires = 0
    fabricated_leaves = 0
    for plan in corpus:
        fires = len(_verbatim_fires(plan))
        if plan["label"] == "faithful":
            faithful_fires += fires
            faithful_leaves += len(plan["leaves"])
        else:
            fabricated_fires += fires
            fabricated_leaves += len(plan["leaves"])

    assert (faithful_leaves, fabricated_leaves) == (31, 3)
    assert fabricated_fires == 3
    assert faithful_fires == 19, (
        "The measured false-positive count changed. That is not a test to "
        "update in passing: re-measure the whole corpus and rewrite the "
        "docstring with the new numbers before deciding anything about this "
        "rule's severity."
    )


@pytest.mark.unit
def test_verbatim_rule_fires_on_a_line_for_line_copy_of_the_plan():
    """The clearest single false positive, kept as its own guard.

    Probe D (plan ``f91dc84e``, 2026-08-27): a three-section plan whose leaf
    Steps are the plan's own bullets, word for word, with only the markdown
    inline-code backticks removed - which is what a worker-facing prompt should
    do. All three leaves are warned as "largely not present in the source
    plan". Nothing about that decomposition is wrong.
    """
    plan = next(p for p in _corpus() if p["plan_id"] == "f91dc84e")
    assert plan["label"] == "faithful"
    assert len(_verbatim_fires(plan)) == 3


@pytest.mark.unit
def test_replaying_the_fabricating_plan_no_longer_grabs_the_contract_file():
    """The round-7 prompt fix, tested on the input that defeated it.

    Plan ``2ea05b85`` said ``src/playground/test_hm.py`` was the contract and
    forbade editing it, and the decomposer put that file into leaf 1's ``files``
    with sixteen replacement tests of its own. The decompose prompt then gained
    the prevention half. On 2026-08-27 the SAME plan document was submitted
    again, verbatim, through ``execute_plan`` (plan ``8d4ee3b1``): no leaf
    declares the test file and both carry the plan's real acceptance command.

    One sample against a non-deterministic decomposer is evidence, not proof -
    but it is evidence on the one input that matters, and it is the reason the
    ``files``-authorisation rule was not shipped off the back of a single
    fabricating leaf. That same replay also shows the rule's cost: leaf 2 adds
    ``src/playground/__init__.py``, which the plan authorises nowhere and which
    already exists in the repository, so a document-scoped files rule would
    fire on a faithful leaf here.
    """
    plan = next(p for p in _corpus() if p["plan_id"] == "8d4ee3b1")
    declared = {path for leaf in plan["leaves"] for path in (leaf["files"] or [])}
    assert "src/playground/test_hm.py" not in declared
    assert "src/playground/__init__.py" in declared
