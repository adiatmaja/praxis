"""Tests for the deterministic leaf validator (core/leaf_validator)."""

from __future__ import annotations

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
