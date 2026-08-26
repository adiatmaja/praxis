"""Tests for the shared execute_plan_decompose helper."""

from __future__ import annotations

import re
from typing import Any
from unittest.mock import patch

import pytest

from orchestrator.core.context_scrub import INTAKE_ABUSE_CEILING_CHARS
from orchestrator.core.execute_plan_decompose import (
    count_plan_tasks,
    decompose_plan,
    drop_verification_only_leaves,
    normalize_slugs,
)
from orchestrator.core.leaf_validator import validate_leaves
from orchestrator.core.plan_review import PlanReviewError
from orchestrator.models.schemas import LeafTask
from tests.conftest import FAKE_GITHUB_TOKEN


class _FakeRouter:
    def __init__(self, raw: str) -> None:
        self._raw = raw
        self.calls: list[tuple[str, str]] = []

    async def run(self, call_site: str, prompt: str, project_id: Any = None) -> str:
        self.calls.append((call_site, prompt))
        return self._raw


class _FakeProfile:
    context_window = 8192
    model_name = "test-model"
    parameter_count_b = 7.0
    strengths = "coding"
    weaknesses = "math"
    max_task_complexity = "medium"
    max_files_touched = 5
    max_loc_delta = 300
    max_checklist_items = 12
    max_dep_depth = 3
    escalate_task_types = []


class _FakeEffective:
    async def capability_profile(
        self,
        project_id: Any,
        model: str,
        harness: Any = None,
        project_context_window: Any = None,
    ) -> _FakeProfile:
        return _FakeProfile()


async def test_decompose_plan_returns_normalized_opus_plan():
    raw = (
        '{"tasks":[{"id":"t1","title":"A","description":"d",'
        '"depends_on":[],"files":["src/a.py"],"task_type":"feature",'
        '"estimated_loc":50,"verification":"Run pytest and confirm all tests pass",'
        '"plan_text":"## Goal\\nAdd A.\\n## Files\\nsrc/a.py\\n## Steps\\n'
        '1. Implement it.\\n## Acceptance\\nRun `pytest` and confirm it passes"},'
        '{"id":"t2","title":"B","description":"d",'
        '"depends_on":["t1"],"files":["src/b.py"],"task_type":"feature",'
        '"estimated_loc":60,"verification":"Run pytest and confirm all tests pass",'
        '"plan_text":"## Goal\\nAdd B.\\n## Files\\nsrc/b.py\\n## Steps\\n'
        '1. Implement it.\\n## Acceptance\\nRun `pytest` and confirm it passes"'
        "}]}"
    )
    router = _FakeRouter(raw)
    opus_plan = await decompose_plan(
        plan="build a thing",
        model="qwen3.6-27b",
        context="some ctx",
        router=router,
        effective_settings=_FakeEffective(),
        project_id="p1",
    )
    tasks = opus_plan["tasks"]
    assert all("slug" in t for t in tasks)
    assert tasks[1]["depends_on"] == [tasks[0]["slug"]]
    assert len(router.calls) == 1
    assert router.calls[0][0] == "plan_review"
    # ABSENT, not an empty list. The key is the signal that a graph carried
    # findings, so a consumer asking ``"validation_warnings" in opus_plan``
    # must not be told yes by a decomposition that found nothing.
    assert "validation_warnings" not in opus_plan


async def test_decompose_plan_threads_context_onto_tasks():
    raw = (
        '{"tasks":[{"id":"t1","title":"X","description":"d","depends_on":[],'
        '"files":["src/x.py"],"task_type":"feature","estimated_loc":40,'
        '"verification":"Run pytest and confirm all tests pass",'
        '"plan_text":"## Goal\\nAdd X.\\n## Files\\nsrc/x.py\\n## Steps\\n'
        '1. Implement it.\\n## Acceptance\\nRun `pytest` and confirm it passes"'
        "}]}"
    )
    router = _FakeRouter(raw)
    opus_plan = await decompose_plan(
        plan="do something",
        model="qwen3.6-27b",
        context="important context here",
        router=router,
        effective_settings=_FakeEffective(),
        project_id=None,
    )
    assert opus_plan["tasks"][0].get("context_text") == "important context here"


async def test_decompose_plan_no_context_skips_context_text():
    raw = (
        '{"tasks":[{"id":"t1","title":"X","description":"d","depends_on":[],'
        '"files":["src/x.py"],"task_type":"feature","estimated_loc":40,'
        '"verification":"Run pytest and confirm all tests pass",'
        '"plan_text":"## Goal\\nAdd X.\\n## Files\\nsrc/x.py\\n## Steps\\n'
        '1. Implement it.\\n## Acceptance\\nRun `pytest` and confirm it passes"'
        "}]}"
    )
    router = _FakeRouter(raw)
    opus_plan = await decompose_plan(
        plan="do something",
        model="qwen3.6-27b",
        context=None,
        router=router,
        effective_settings=_FakeEffective(),
        project_id=None,
    )
    assert "context_text" not in opus_plan["tasks"][0]


class _FlakyRouter:
    """Returns garbage on the first call, valid JSON on the second."""

    def __init__(self, good_raw: str) -> None:
        self._good = good_raw
        self.calls = 0

    async def run(self, call_site: str, prompt: str, project_id: Any = None) -> str:
        self.calls += 1
        if self.calls == 1:
            return "not json, the model rambled and produced nothing parseable"
        return self._good


async def test_decompose_retries_once_on_parse_failure():
    good = (
        '{"tasks": [{"id": "t1", "title": "A", "description": "d", "depends_on": [],'
        '"files": ["src/a.py"], "task_type": "feature", "estimated_loc": 50,'
        '"verification": "Run pytest and confirm all tests pass",'
        '"plan_text": "## Goal\\nAdd A.\\n## Files\\nsrc/a.py\\n## Steps\\n'
        '1. Implement it.\\n## Acceptance\\nRun `pytest` and confirm it passes"'
        "}]}"
    )
    router = _FlakyRouter(good)
    opus_plan = await decompose_plan(
        plan="build a thing",
        model="qwen3.6-27b",
        context=None,
        router=router,
        effective_settings=_FakeEffective(),
        project_id="p1",
    )
    assert router.calls == 2  # first failed, retried, second succeeded
    assert opus_plan["tasks"][0]["slug"]


async def test_decompose_raises_after_all_retries_exhausted():
    class _AlwaysBad:
        def __init__(self) -> None:
            self.calls = 0

        async def run(self, call_site: str, prompt: str, project_id: Any = None) -> str:
            self.calls += 1
            return "never valid json"

    router = _AlwaysBad()
    with pytest.raises(PlanReviewError):
        await decompose_plan(
            plan="x",
            model="m",
            context=None,
            router=router,
            effective_settings=_FakeEffective(),
            project_id=None,
        )
    assert router.calls == 2  # attempted twice, then gave up


async def test_decompose_threads_local_context_as_repo_memory():
    raw = (
        '{"tasks":[{"id":"t1","title":"X","description":"d","depends_on":[],'
        '"files":["src/x.py"],"task_type":"feature","estimated_loc":40,'
        '"verification":"Run pytest and confirm all tests pass",'
        '"plan_text":"## Goal\\nAdd X.\\n## Files\\nsrc/x.py\\n## Steps\\n'
        '1. Implement it.\\n## Acceptance\\nRun `pytest` and confirm it passes"'
        "}]}"
    )
    router = _FakeRouter(raw)
    opus_plan = await decompose_plan(
        plan="do something",
        model="qwen3.6-27b",
        context=None,
        router=router,
        effective_settings=_FakeEffective(),
        project_id=None,
        local_context="local notes",
    )
    assert opus_plan["tasks"][0].get("repo_memory") == "local notes"


async def test_decompose_no_local_context_skips_repo_memory():
    raw = (
        '{"tasks":[{"id":"t1","title":"X","description":"d","depends_on":[],'
        '"files":["src/x.py"],"task_type":"feature","estimated_loc":40,'
        '"verification":"Run pytest and confirm all tests pass",'
        '"plan_text":"## Goal\\nAdd X.\\n## Files\\nsrc/x.py\\n## Steps\\n'
        '1. Implement it.\\n## Acceptance\\nRun `pytest` and confirm it passes"'
        "}]}"
    )
    router = _FakeRouter(raw)
    opus_plan = await decompose_plan(
        plan="do something",
        model="qwen3.6-27b",
        context=None,
        router=router,
        effective_settings=_FakeEffective(),
        project_id=None,
    )
    assert "repo_memory" not in opus_plan["tasks"][0]


async def test_decompose_scrubs_local_context_server_side():
    """Secret tokens in local_context are scrubbed before threading onto tasks."""
    raw = (
        '{"tasks":[{"id":"t1","title":"X","description":"d","depends_on":[],'
        '"files":["src/x.py"],"task_type":"feature","estimated_loc":40,'
        '"verification":"Run pytest and confirm all tests pass",'
        '"plan_text":"## Goal\\nAdd X.\\n## Files\\nsrc/x.py\\n## Steps\\n'
        '1. Implement it.\\n## Acceptance\\nRun `pytest` and confirm it passes"'
        "}]}"
    )
    router = _FakeRouter(raw)
    opus_plan = await decompose_plan(
        plan="do something",
        model="qwen3.6-27b",
        context=None,
        router=router,
        effective_settings=_FakeEffective(),
        project_id=None,
        local_context=f"token: {FAKE_GITHUB_TOKEN}",
    )
    assert FAKE_GITHUB_TOKEN not in opus_plan["tasks"][0].get("repo_memory", "")


# ---------------------------------------------------------------------------
# drop_verification_only_leaves unit tests (Fix 1)
# ---------------------------------------------------------------------------


def _make_task(
    slug: str,
    title: str,
    description: str = "",
    plan_text: str = "",
    depends_on: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "slug": slug,
        "title": title,
        "description": description,
        "plan_text": plan_text,
        "depends_on": depends_on or [],
    }


def test_drop_verification_only_leaves_removes_verify_leaf_and_rewrites_depends_on():
    """A verification-only leaf is dropped; dangling depends_on refs are removed."""
    tasks = [
        _make_task("impl", "Implement feature X", "Add the new endpoint"),
        _make_task(
            "verify",
            "Run the test suite",
            "Run pytest and commit only if formatting changed",
            depends_on=["impl"],
        ),
        _make_task("docs", "Update docs", "Write changelog", depends_on=["verify"]),
    ]
    opus_plan: dict[str, Any] = {"tasks": tasks}
    result = drop_verification_only_leaves(opus_plan)
    slugs = [t["slug"] for t in result["tasks"]]
    assert "verify" not in slugs
    assert "impl" in slugs
    assert "docs" in slugs
    # The dangling dep on the dropped leaf must be gone
    docs_task = next(t for t in result["tasks"] if t["slug"] == "docs")
    assert "verify" not in docs_task["depends_on"]


def test_drop_verification_only_leaves_all_real_leaves_untouched():
    """A plan with only real implementation leaves passes through unchanged."""
    tasks = [
        _make_task("feat-a", "Add validation logic", "Validate user input"),
        _make_task(
            "feat-b",
            "Add tests",
            "Write unit tests for validation",
            depends_on=["feat-a"],
        ),
    ]
    opus_plan: dict[str, Any] = {"tasks": list(tasks)}
    result = drop_verification_only_leaves(opus_plan)
    assert [t["slug"] for t in result["tasks"]] == ["feat-a", "feat-b"]


def test_drop_verification_only_leaves_borderline_add_tests_leaf_not_dropped():
    """A leaf whose title merely mentions 'add tests' is NOT treated as verify-only."""
    tasks = [
        _make_task(
            "add-tests",
            "Add unit tests for the new parser",
            "Write pytest tests covering edge cases in parser.py",
        ),
    ]
    opus_plan: dict[str, Any] = {"tasks": tasks}
    result = drop_verification_only_leaves(opus_plan)
    assert len(result["tasks"]) == 1
    assert result["tasks"][0]["slug"] == "add-tests"


def test_drop_verification_only_leaves_verification_only_in_plan_text():
    """A leaf with 'verification only' in plan_text is dropped."""
    tasks = [
        _make_task("impl", "Implement login", "Build login endpoint"),
        _make_task(
            "ci-check",
            "CI check",
            "No code changes expected",
            plan_text="Verification only: run mypy and ruff, no source changes.",
            depends_on=["impl"],
        ),
    ]
    opus_plan: dict[str, Any] = {"tasks": tasks}
    result = drop_verification_only_leaves(opus_plan)
    assert len(result["tasks"]) == 1
    assert result["tasks"][0]["slug"] == "impl"


def test_drop_verification_only_leaves_multiple_verify_leaves():
    """Multiple verification-only leaves are all dropped; graph stays valid."""
    tasks = [
        _make_task("impl", "Build feature", "Write the code"),
        _make_task(
            "lint",
            "Run ruff and mypy",
            "Run ruff check and mypy, no source edits",
            depends_on=["impl"],
        ),
        _make_task(
            "pytest-run",
            "Run the test suite",
            "Execute pytest, commit only if coverage report changed",
            depends_on=["impl"],
        ),
        _make_task(
            "deploy",
            "Deploy to staging",
            "Push docker image",
            depends_on=["lint", "pytest-run"],
        ),
    ]
    opus_plan: dict[str, Any] = {"tasks": tasks}
    result = drop_verification_only_leaves(opus_plan)
    slugs = [t["slug"] for t in result["tasks"]]
    assert "lint" not in slugs
    assert "pytest-run" not in slugs
    assert "impl" in slugs
    assert "deploy" in slugs
    deploy_task = next(t for t in result["tasks"] if t["slug"] == "deploy")
    assert "lint" not in deploy_task["depends_on"]
    assert "pytest-run" not in deploy_task["depends_on"]


# ---------------------------------------------------------------------------
# count_plan_tasks unit tests (Fix 3, Part A)
# ---------------------------------------------------------------------------


def test_count_plan_tasks_zero_when_no_headers():
    plan = "Some free-form plan with no task headers.\n\n## Overview\nblah"
    assert count_plan_tasks(plan) == 0


def test_count_plan_tasks_counts_several():
    plan = "### Task 1\ndo a thing\n\n### Task 2\ndo another\n\n### Task 3\nfinish up\n"
    assert count_plan_tasks(plan) == 3


def test_count_plan_tasks_allows_trailing_text_and_case_insensitive():
    plan = (
        "### Task 1: Scaffold\nx\n\n"
        "### task 2 - add tests\ny\n\n"
        "### TASK 8: Finalize\nz\n"
    )
    assert count_plan_tasks(plan) == 3


# ---------------------------------------------------------------------------
# decompose_warning tests (Fix 3, Part A)
# ---------------------------------------------------------------------------


async def test_decompose_warning_set_when_leaves_fewer_than_authored():
    """Router drops a leaf (2 authored -> 1 leaf): decompose_warning is attached."""
    raw = (
        '{"tasks":[{"id":"t1","title":"A","description":"d","depends_on":[],'
        '"files":["src/a.py"],"task_type":"feature","estimated_loc":50,'
        '"verification":"Run pytest and confirm all tests pass",'
        '"plan_text":"## Goal\\nAdd the leaf.\\n## Files\\nsrc/a.py\\n## Steps\\n'
        '1. Implement it.\\n## Acceptance\\nRun `pytest` and confirm it passes"'
        "}]}"
    )
    router = _FakeRouter(raw)
    plan = "### Task 1\nfirst\n\n### Task 2\nsecond\n"
    opus_plan = await decompose_plan(
        plan=plan,
        model="qwen3.6-27b",
        context=None,
        router=router,
        effective_settings=_FakeEffective(),
        project_id="p1",
    )
    assert opus_plan["decompose_warning"] == {
        "authored_task_count": 2,
        "leaf_count": 1,
    }


async def test_decompose_warning_absent_when_leaves_equal_authored():
    raw = (
        '{"tasks":[{"id":"t1","title":"A","description":"d","depends_on":[],'
        '"files":["src/a.py"],"task_type":"feature","estimated_loc":50,'
        '"verification":"Run pytest and confirm all tests pass",'
        '"plan_text":"## Goal\\nAdd A.\\n## Files\\nsrc/a.py\\n## Steps\\n'
        '1. Implement it.\\n## Acceptance\\nRun `pytest` and confirm it passes"},'
        '{"id":"t2","title":"B","description":"d","depends_on":[],'
        '"files":["src/b.py"],"task_type":"feature","estimated_loc":60,'
        '"verification":"Run pytest and confirm all tests pass",'
        '"plan_text":"## Goal\\nAdd B.\\n## Files\\nsrc/b.py\\n## Steps\\n'
        '1. Implement it.\\n## Acceptance\\nRun `pytest` and confirm it passes"'
        "}]}"
    )
    router = _FakeRouter(raw)
    plan = "### Task 1\nfirst\n\n### Task 2\nsecond\n"
    opus_plan = await decompose_plan(
        plan=plan,
        model="qwen3.6-27b",
        context=None,
        router=router,
        effective_settings=_FakeEffective(),
        project_id="p1",
    )
    assert "decompose_warning" not in opus_plan


async def test_decompose_warning_absent_when_no_authored_headers():
    """No '### Task N' headers means authored count is 0; never warn."""
    raw = (
        '{"tasks":[{"id":"t1","title":"A","description":"d","depends_on":[],'
        '"files":["src/a.py"],"task_type":"feature","estimated_loc":50,'
        '"verification":"Run pytest and confirm all tests pass",'
        '"plan_text":"## Goal\\nAdd the leaf.\\n## Files\\nsrc/a.py\\n## Steps\\n'
        '1. Implement it.\\n## Acceptance\\nRun `pytest` and confirm it passes"'
        "}]}"
    )
    router = _FakeRouter(raw)
    opus_plan = await decompose_plan(
        plan="free-form plan with no task headers",
        model="qwen3.6-27b",
        context=None,
        router=router,
        effective_settings=_FakeEffective(),
        project_id="p1",
    )
    assert "decompose_warning" not in opus_plan


# ---------------------------------------------------------------------------
# Docs-finalization leaf protection (Fix 3, Part B)
# ---------------------------------------------------------------------------


def test_drop_keeps_docs_finalization_leaf_that_also_runs_suite():
    """A terminal leaf editing CLAUDE.md is retained even though it runs the suite."""
    tasks = [
        _make_task("impl", "Implement feature", "Write the code"),
        _make_task(
            "finalize",
            "Finalize docs",
            "Update CLAUDE.md gotcha index (add 3 lines) then run the full suite",
            depends_on=["impl"],
        ),
    ]
    opus_plan: dict[str, Any] = {"tasks": tasks}
    result = drop_verification_only_leaves(opus_plan)
    slugs = [t["slug"] for t in result["tasks"]]
    assert "finalize" in slugs
    assert "impl" in slugs


def test_drop_still_removes_pure_verification_leaf():
    """A pure 'run the suite, commit only if formatting changed' leaf is dropped."""
    tasks = [
        _make_task("impl", "Implement feature", "Write the code"),
        _make_task(
            "verify",
            "Run the test suite",
            "Run the test suite and commit only if formatting changed",
            depends_on=["impl"],
        ),
    ]
    opus_plan: dict[str, Any] = {"tasks": tasks}
    result = drop_verification_only_leaves(opus_plan)
    slugs = [t["slug"] for t in result["tasks"]]
    assert "verify" not in slugs
    assert "impl" in slugs


# ---------------------------------------------------------------------------
# Validate-and-repair loop tests
# ---------------------------------------------------------------------------


class _FeedbackRouter:
    """Returns bad JSON on first call, then valid JSON with the second prompt."""

    def __init__(self, bad_raw: str, good_raw: str) -> None:
        self._bad = bad_raw
        self._good = good_raw
        self.calls: list[tuple[str, str]] = []

    async def run(self, call_site: str, prompt: str, project_id: Any = None) -> str:
        self.calls.append((call_site, prompt))
        if len(self.calls) == 1:
            return self._bad
        return self._good


async def test_decompose_retries_on_validation_failure_with_feedback():
    """When validation finds violations, the brain is re-invoked with feedback."""
    # First response: missing verification (HARD violation)
    bad = (
        '{"tasks":[{"id":"t1","title":"A","description":"d",'
        '"depends_on":[],"files":["src/a.py"],"task_type":"feature",'
        '"estimated_loc":50}]}'
    )
    # Second response: includes verification
    good = (
        '{"tasks":[{"id":"t1","title":"A","description":"d",'
        '"depends_on":[],"files":["src/a.py"],"task_type":"feature",'
        '"estimated_loc":50,"verification":"Run pytest and confirm all tests pass",'
        '"plan_text":"## Goal\\nAdd A.\\n## Files\\nsrc/a.py\\n## Steps\\n'
        '1. Implement it.\\n## Acceptance\\nRun `pytest` and confirm it passes"'
        "}]}"
    )
    router = _FeedbackRouter(bad, good)
    opus_plan = await decompose_plan(
        plan="build a thing",
        model="qwen3.6-27b",
        context=None,
        router=router,
        effective_settings=_FakeEffective(),
        project_id="p1",
    )
    assert len(router.calls) == 2
    assert "HARD violations" in router.calls[1][1]
    assert opus_plan["tasks"][0].get("verification")


async def test_decompose_raises_plan_rejected_on_persistent_hard_violations():
    """When HARD violations persist after the informed round, PlanReviewError is raised."""
    # Both responses have missing verification
    bad = (
        '{"tasks":[{"id":"t1","title":"A","description":"d",'
        '"depends_on":[],"files":["src/a.py"],"task_type":"feature",'
        '"estimated_loc":50}]}'
    )

    class _AlwaysBadRouter:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []

        async def run(self, call_site: str, prompt: str, project_id: Any = None) -> str:
            self.calls.append((call_site, prompt))
            return bad

    router = _AlwaysBadRouter()
    with pytest.raises(PlanReviewError, match="plan_rejected"):
        await decompose_plan(
            plan="x",
            model="m",
            context=None,
            router=router,
            effective_settings=_FakeEffective(),
            project_id=None,
        )
    assert len(router.calls) == 2
    # This fixture now trips two HARD rules, so pin the one the test was
    # written for: a regression that dropped the verification check must not
    # be able to hide behind leaf_template.
    assert "[verification]" in router.calls[1][1]


async def test_decompose_attaches_validation_warnings_on_soft_only():
    """A SOFT-only draft is accepted on the FIRST call, warnings attached.

    The call COUNT is the bound, and it was the one thing this test never
    asserted.  The accept predicate used to be ``ValidationResult.clean``,
    which counts soft findings, so a draft with zero HARD violations bought a
    second brain call it could not use: the re-ask has no authority to reject,
    so the second answer was accepted with its own soft findings attached
    exactly as the first would have been.  Assert the count, or any soft rule
    added later silently doubles the price of every decomposition it fires on.
    """
    # Task with a vague phrase ("refactor") triggers a SOFT violation
    raw = (
        '{"tasks":[{"id":"t1","title":"Refactor module","description":"d",'
        '"depends_on":[],"files":["src/a.py"],"task_type":"refactor",'
        '"estimated_loc":50,"verification":"Run pytest and confirm all tests pass",'
        '"plan_text":"## Goal\\nRefactor the module.\\n## Files\\nsrc/a.py\\n'
        "## Steps\\n1. Refactor it.\\n## Acceptance\\nRun `pytest` and confirm "
        'it passes"'
        "}]}"
    )
    router = _FakeRouter(raw)
    opus_plan = await decompose_plan(
        plan="refactor something",
        model="qwen3.6-27b",
        context=None,
        router=router,
        effective_settings=_FakeEffective(),
        project_id="p1",
    )
    assert len(router.calls) == 1
    assert "validation_warnings" in opus_plan
    warnings = opus_plan["validation_warnings"]
    assert any(w["rule"] == "vague_phrase" for w in warnings)


async def test_decompose_plan_id_and_emitter_params_accepted():
    """Optional plan_id and emitter params are accepted (inert for now)."""
    raw = (
        '{"tasks":[{"id":"t1","title":"A","description":"d",'
        '"depends_on":[],"files":["src/a.py"],"task_type":"feature",'
        '"estimated_loc":50,"verification":"Run pytest and confirm all tests pass",'
        '"plan_text":"## Goal\\nAdd A.\\n## Files\\nsrc/a.py\\n## Steps\\n'
        '1. Implement it.\\n## Acceptance\\nRun `pytest` and confirm it passes"'
        "}]}"
    )
    router = _FakeRouter(raw)
    opus_plan = await decompose_plan(
        plan="build a thing",
        model="qwen3.6-27b",
        context=None,
        router=router,
        effective_settings=_FakeEffective(),
        project_id="p1",
        plan_id="plan-123",
        emitter=None,
    )
    assert opus_plan["tasks"]


# ---------------------------------------------------------------------------
# Capability event emitter tests (S1)
# ---------------------------------------------------------------------------


class _CapturingEmitter:
    """Fake emitter that records emitted events."""

    def __init__(self) -> None:
        self.events: list[Any] = []

    async def emit(self, event: Any) -> str:
        self.events.append(event)
        return "fake-id"


async def test_decompose_emits_decompose_input_event():
    """DecomposeInputEvent is emitted before the brain call."""
    raw = (
        '{"tasks":[{"id":"t1","title":"A","description":"d",'
        '"depends_on":[],"files":["src/a.py"],"task_type":"feature",'
        '"estimated_loc":50,"verification":"Run pytest and confirm all tests pass",'
        '"plan_text":"## Goal\\nAdd A.\\n## Files\\nsrc/a.py\\n## Steps\\n'
        '1. Implement it.\\n## Acceptance\\nRun `pytest` and confirm it passes"'
        "}]}"
    )
    router = _FakeRouter(raw)
    emitter = _CapturingEmitter()
    await decompose_plan(
        plan="build a thing",
        model="qwen3.6-27b",
        context=None,
        router=router,
        effective_settings=_FakeEffective(),
        project_id="p1",
        plan_id="plan-123",
        emitter=emitter,
    )
    input_events = [e for e in emitter.events if e.event_type == "decompose_input"]
    assert len(input_events) == 1
    assert input_events[0].plan_id == "plan-123"
    assert input_events[0].model_name == "qwen3.6-27b"


async def test_decompose_emits_leaf_validated_on_clean():
    """LeafValidatedEvent emitted per leaf when validation is clean."""
    raw = (
        '{"tasks":[{"id":"t1","title":"A","description":"d",'
        '"depends_on":[],"files":["src/a.py"],"task_type":"feature",'
        '"estimated_loc":50,"verification":"Run pytest and confirm all tests pass",'
        '"plan_text":"## Goal\\nAdd A.\\n## Files\\nsrc/a.py\\n## Steps\\n'
        '1. Implement it.\\n## Acceptance\\nRun `pytest` and confirm it passes"},'
        '{"id":"t2","title":"B","description":"d",'
        '"depends_on":["t1"],"files":["src/b.py"],"task_type":"feature",'
        '"estimated_loc":60,"verification":"Run pytest and confirm all tests pass",'
        '"plan_text":"## Goal\\nAdd B.\\n## Files\\nsrc/b.py\\n## Steps\\n'
        '1. Implement it.\\n## Acceptance\\nRun `pytest` and confirm it passes"'
        "}]}"
    )
    router = _FakeRouter(raw)
    emitter = _CapturingEmitter()
    await decompose_plan(
        plan="build a thing",
        model="qwen3.6-27b",
        context=None,
        router=router,
        effective_settings=_FakeEffective(),
        project_id="p1",
        plan_id="plan-456",
        emitter=emitter,
    )
    validated = [e for e in emitter.events if e.event_type == "leaf_validated"]
    assert len(validated) == 2
    assert all(e.plan_id == "plan-456" for e in validated)


async def test_decompose_emits_plan_rejected_on_hard_violations():
    """PlanRejectedEvent emitted when HARD violations persist after all rounds."""
    bad = (
        '{"tasks":[{"id":"t1","title":"A","description":"d",'
        '"depends_on":[],"files":["src/a.py"],"task_type":"feature",'
        '"estimated_loc":50}]}'
    )

    class _AlwaysBadRouter:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []

        async def run(self, call_site: str, prompt: str, project_id: Any = None) -> str:
            self.calls.append((call_site, prompt))
            return bad

    router = _AlwaysBadRouter()
    emitter = _CapturingEmitter()
    with pytest.raises(PlanReviewError, match="plan_rejected"):
        await decompose_plan(
            plan="x",
            model="m",
            context=None,
            router=router,
            effective_settings=_FakeEffective(),
            project_id=None,
            plan_id="plan-789",
            emitter=emitter,
        )
    rejected = [e for e in emitter.events if e.event_type == "plan_rejected"]
    assert len(rejected) == 1
    assert rejected[0].plan_id == "plan-789"
    assert rejected[0].rounds == 2
    assert len(rejected[0].violations) > 0
    # This fixture now trips two HARD rules, so pin the one the test was
    # written for rather than accepting any non-empty violation list.
    assert any(v.startswith("[verification]") for v in rejected[0].violations)


async def test_decompose_no_emit_when_emitter_is_none():
    """No events emitted when emitter is None (None-safe guard)."""
    raw = (
        '{"tasks":[{"id":"t1","title":"A","description":"d",'
        '"depends_on":[],"files":["src/a.py"],"task_type":"feature",'
        '"estimated_loc":50,"verification":"Run pytest and confirm all tests pass",'
        '"plan_text":"## Goal\\nAdd A.\\n## Files\\nsrc/a.py\\n## Steps\\n'
        '1. Implement it.\\n## Acceptance\\nRun `pytest` and confirm it passes"'
        "}]}"
    )
    router = _FakeRouter(raw)
    opus_plan = await decompose_plan(
        plan="build a thing",
        model="qwen3.6-27b",
        context=None,
        router=router,
        effective_settings=_FakeEffective(),
        project_id="p1",
        plan_id="plan-123",
        emitter=None,
    )
    assert opus_plan["tasks"]


async def test_decompose_no_emit_when_plan_id_is_none():
    """No events emitted when plan_id is None (None-safe guard)."""
    raw = (
        '{"tasks":[{"id":"t1","title":"A","description":"d",'
        '"depends_on":[],"files":["src/a.py"],"task_type":"feature",'
        '"estimated_loc":50,"verification":"Run pytest and confirm all tests pass",'
        '"plan_text":"## Goal\\nAdd A.\\n## Files\\nsrc/a.py\\n## Steps\\n'
        '1. Implement it.\\n## Acceptance\\nRun `pytest` and confirm it passes"'
        "}]}"
    )
    router = _FakeRouter(raw)
    emitter = _CapturingEmitter()
    opus_plan = await decompose_plan(
        plan="build a thing",
        model="qwen3.6-27b",
        context=None,
        router=router,
        effective_settings=_FakeEffective(),
        project_id="p1",
        plan_id=None,
        emitter=emitter,
    )
    assert opus_plan["tasks"]
    assert len(emitter.events) == 0


# ---------------------------------------------------------------------------
# fetch_recent_outcomes wiring test (Part B)
# ---------------------------------------------------------------------------


async def test_decompose_uses_fetched_outcome_history_when_db_provided(db):
    """When db is passed, decompose_plan feeds real outcome history to the brain."""
    await db.execute(
        """INSERT INTO task_outcomes
           (id, project_id, model_name, outcome, failure_class, source, task_type,
            harness, files_touched, loc_delta, context_tokens_est,
            attempt, split_depth)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            "r1",
            "p1",
            "m",
            "pass",
            None,
            "run",
            "feature",
            "opencode",
            2,
            50,
            1000,
            1,
            0,
        ),
    )

    raw = (
        '{"tasks":[{"id":"t1","title":"A","description":"d",'
        '"depends_on":[],"files":["src/a.py"],"task_type":"feature",'
        '"estimated_loc":50,"verification":"Run pytest and confirm all tests pass",'
        '"plan_text":"## Goal\\nAdd A.\\n## Files\\nsrc/a.py\\n## Steps\\n'
        '1. Implement it.\\n## Acceptance\\nRun `pytest` and confirm it passes"'
        "}]}"
    )
    router = _FakeRouter(raw)

    with patch(
        "orchestrator.core.execute_plan_decompose.build_review_prompt"
    ) as mock_prompt:
        mock_prompt.return_value = "fake-prompt"
        with patch(
            "orchestrator.core.execute_plan_decompose.parse_review_response"
        ) as mock_parse:
            mock_parse.return_value = {
                "tasks": [
                    {
                        "id": "t1",
                        "slug": "a",
                        "title": "A",
                        "description": "d",
                        "depends_on": [],
                        "files": ["src/a.py"],
                        "task_type": "feature",
                        "estimated_loc": 50,
                        "verification": "Run pytest and confirm all tests pass",
                        "plan_text": (
                            "## Goal\nAdd A.\n## Files\nsrc/a.py\n## Steps\n"
                            "1. Implement it.\n## Acceptance\n"
                            "Run `pytest` and confirm it passes"
                        ),
                    }
                ]
            }
            await decompose_plan(
                plan="build a thing",
                model="m",
                context=None,
                router=router,
                effective_settings=_FakeEffective(),
                project_id="p1",
                db=db,
            )

    assert mock_prompt.called
    history_arg = mock_prompt.call_args[0][2]
    assert "no prior run history" not in history_arg
    assert "feature" in history_arg


# ---------------------------------------------------------------------------
# Residual defect (round 2): this intake seam must never size-cap by
# ``profile.context_window`` at all - that lookup is a coarse, model-only
# capability default, not the authoritative, probe-aware resolution
# (core/context_window.resolve_context_window) that core/worker_bible.
# build_bible uses downstream to re-scrub this same context_text/repo_memory.
# Capping here first just cuts the text before build_bible ever runs; a
# re-scrub cannot lengthen a string intake already shortened. So intake now
# applies only a fixed, window-independent abuse ceiling
# (INTAKE_ABUSE_CEILING_CHARS); the real cap is deferred entirely to
# build_bible.
# ---------------------------------------------------------------------------


class _FakeLargeWindowProfile(_FakeProfile):
    """Same shape as ``_FakeProfile``, but a declared million-token window."""

    context_window = 1_000_000


class _FakeLargeWindowEffective:
    async def capability_profile(
        self,
        project_id: Any,
        model: str,
        harness: Any = None,
        project_context_window: Any = None,
    ) -> _FakeLargeWindowProfile:
        return _FakeLargeWindowProfile()


_OVERSIZED_CONTEXT = ("c" * 14_000) + "TAILMARK"


def _single_task_raw() -> str:
    return (
        '{"tasks":[{"id":"t1","title":"X","description":"d","depends_on":[],'
        '"files":["src/x.py"],"task_type":"feature","estimated_loc":40,'
        '"verification":"Run pytest and confirm all tests pass",'
        '"plan_text":"## Goal\\nAdd X.\\n## Files\\nsrc/x.py\\n## Steps\\n'
        '1. Implement it.\\n## Acceptance\\nRun `pytest` and confirm it passes"'
        "}]}"
    )


async def test_decompose_plan_never_pretruncates_context_on_a_small_profile_window():
    """The regression this remediation exists for: even with
    ``_FakeProfile.context_window == 8192`` (the shipped local-model
    default, which used to size the intake cap down to 12 000 chars), a
    14 KB context/local_context must survive intake completely intact -
    this profile lookup is a coarse capability default, not the
    authoritative, probe-aware window resolution, and capping on it at
    intake permanently loses text before ``core/worker_bible.build_bible``
    (the seam that DOES resolve the true window) ever runs. Must go red
    against the pre-fix code and green against this one."""
    router = _FakeRouter(_single_task_raw())
    opus_plan = await decompose_plan(
        plan="do something",
        model="qwen3.6-27b",
        context=_OVERSIZED_CONTEXT,
        local_context=_OVERSIZED_CONTEXT,
        router=router,
        effective_settings=_FakeEffective(),  # context_window == 8192
        project_id=None,
    )
    task = opus_plan["tasks"][0]
    assert task["context_text"] == _OVERSIZED_CONTEXT
    assert task["repo_memory"] == _OVERSIZED_CONTEXT
    assert "truncated by Praxis" not in task["context_text"]
    assert "TAILMARK" in task["context_text"]


async def test_decompose_plan_never_pretruncates_context_on_a_large_profile_window():
    """Same guarantee holds regardless of the profile's declared window -
    proof that intake capping is not merely relaxed for a small window but
    genuinely decoupled from ``profile.context_window`` altogether."""
    router = _FakeRouter(_single_task_raw())
    opus_plan = await decompose_plan(
        plan="do something",
        model="qwen3.6-27b",
        context=_OVERSIZED_CONTEXT,
        local_context=_OVERSIZED_CONTEXT,
        router=router,
        effective_settings=_FakeLargeWindowEffective(),
        project_id=None,
    )
    task = opus_plan["tasks"][0]
    assert task["context_text"] == _OVERSIZED_CONTEXT
    assert task["repo_memory"] == _OVERSIZED_CONTEXT


async def test_decompose_plan_abuse_ceiling_still_caps_a_pathological_payload():
    """The abuse guard is real and unrelated to any window."""
    pathological = "z" * (INTAKE_ABUSE_CEILING_CHARS + 10_000)
    router = _FakeRouter(_single_task_raw())
    opus_plan = await decompose_plan(
        plan="do something",
        model="qwen3.6-27b",
        context=pathological,
        router=router,
        effective_settings=_FakeEffective(),
        project_id=None,
    )
    task = opus_plan["tasks"][0]
    assert "truncated by Praxis" in task["context_text"]
    assert "abuse guard" in task["context_text"]
    run = re.search(r"z{100,}", task["context_text"])
    assert run is not None
    assert len(run.group(0)) == INTAKE_ABUSE_CEILING_CHARS


# ---------------------------------------------------------------------------
# The graph that is RETURNED must be the graph that CLEARED both gates.
#
# The retry loop's own bookkeeping decided this, and it tracked the wrong
# thing: "a graph was parsed" rather than "a graph was accepted". The gap is a
# mixed run, good-then-garbage, which no test covered because the exhaustion
# test uses a router that never parses at all.
# ---------------------------------------------------------------------------


class _Sequenced:
    """Replies with each canned string in turn, one per call."""

    def __init__(self, *replies: str) -> None:
        self._replies = list(replies)
        self.calls = 0

    async def run(self, call_site: str, prompt: str, project_id: Any = None) -> str:
        reply = self._replies[self.calls]
        self.calls += 1
        return reply


class _RecordingEmitter:
    def __init__(self) -> None:
        self.emitted: list[str] = []

    async def emit(self, event: Any) -> None:
        self.emitted.append(type(event).__name__)


# Parses cleanly, then fails HARD validation: leaf_type 'generic' requires
# Goal/Files/Steps/Acceptance sections and this plan_text has none of them.
_PARSES_BUT_INVALID = (
    '{"tasks": [{"id": "t1", "title": "A", "description": "d", "depends_on": [],'
    '"files": ["src/a.py"], "task_type": "feature", "estimated_loc": 50,'
    '"verification": "Run pytest and confirm all tests pass",'
    '"plan_text": "no sections here at all"'
    "}]}"
)


async def test_decompose_refuses_a_graph_that_never_cleared_validation():
    """Attempt 1 rejected, attempt 2 unparseable: the rejected graph must not ship.

    ``parse_review_response`` raising leaves the previous attempt's graph still
    bound, so a guard reading "is the graph None" could not fire, and the two
    raises that would have rejected it sit inside the loop body the parse
    failure's ``continue`` skipped. The plan dispatched with a leaf whose
    ``plan_text`` carries none of the sections its own leaf_type requires.
    """
    router = _Sequenced(_PARSES_BUT_INVALID, "I cannot help with that.")

    with pytest.raises(PlanReviewError):
        await decompose_plan(
            plan="### Task 1: x\nbody",
            model="m",
            context=None,
            router=router,
            effective_settings=_FakeEffective(),
            project_id=None,
        )

    assert router.calls == 2


async def test_decompose_emits_no_leaf_events_for_a_graph_it_never_accepted():
    """The capability ledger must not record a leaf it did not judge.

    Worse than the silent dispatch: the same run emitted ``LeafRejectedEvent``
    for the very leaf it went on to return, so the calibration trail said
    "rejected" about work that was about to be built.
    """
    emitter = _RecordingEmitter()
    router = _Sequenced(_PARSES_BUT_INVALID, "I cannot help with that.")

    with pytest.raises(PlanReviewError):
        await decompose_plan(
            plan="### Task 1: x\nbody",
            model="m",
            context=None,
            router=router,
            effective_settings=_FakeEffective(),
            project_id=None,
            plan_id="plan-1",
            emitter=emitter,
        )

    assert emitter.emitted == ["DecomposeInputEvent"], (
        f"per-leaf events escaped for an unaccepted graph: {emitter.emitted}"
    )


async def test_decompose_still_returns_a_graph_that_parsed_on_the_last_attempt():
    """Positive control for the two refusals above.

    Both assert that decomposition FAILS. A bookkeeping change that simply
    never accepted anything would satisfy both and break every real plan, so
    this pins the mixed run that must still succeed: garbage first, then a
    valid graph on the final attempt.
    """
    good = (
        '{"tasks": [{"id": "t1", "title": "A", "description": "d", "depends_on": [],'
        '"files": ["src/a.py"], "task_type": "feature", "estimated_loc": 50,'
        '"verification": "Run pytest and confirm all tests pass",'
        '"plan_text": "## Goal\\nAdd A.\\n## Files\\nsrc/a.py\\n## Steps\\n'
        '1. Implement it.\\n## Acceptance\\nRun `pytest` and confirm it passes"'
        "}]}"
    )
    emitter = _RecordingEmitter()
    router = _Sequenced("the model rambled", good)

    opus_plan = await decompose_plan(
        plan="build a thing",
        model="qwen3.6-27b",
        context=None,
        router=router,
        effective_settings=_FakeEffective(),
        project_id=None,
        plan_id="plan-1",
        emitter=emitter,
    )

    assert router.calls == 2
    assert opus_plan["tasks"][0]["slug"]
    assert "## Goal" in opus_plan["tasks"][0]["plan_text"]
    assert "LeafValidatedEvent" in emitter.emitted


# ---------------------------------------------------------------------------
# Two tasks may carry the SAME brain id, and the id -> slug map was last-wins.
# ---------------------------------------------------------------------------


def _duplicate_id_plan() -> dict[str, Any]:
    """A graph whose first two tasks share the brain id the third depends on."""
    return {
        "tasks": [
            {"id": "t1", "title": "First", "description": "d", "depends_on": []},
            {"id": "t1", "title": "Second", "description": "d", "depends_on": []},
            {"id": "t9", "title": "Third", "description": "d", "depends_on": ["t1"]},
        ]
    }


@pytest.mark.unit
def test_a_dep_on_an_id_two_tasks_share_resolves_to_neither() -> None:
    """An ambiguous id must resolve to NOTHING, never to whoever came last.

    Nothing requires the brain's ``id`` values to differ: the field is a bare
    ``str`` on ``LeafTask`` and the decompose prompt never asks for uniqueness.
    ``id_to_slug`` was last-wins, so a dependent silently pointed at the SECOND
    task and was ordered after work its author never named. The plan reads
    healthy the whole way through -- the misordered leaf fails on its own
    verification and the outcome is recorded against the worker.
    """
    opus_plan = _duplicate_id_plan()
    normalize_slugs(opus_plan)
    first, second, third = opus_plan["tasks"]

    assert first["slug"] != second["slug"]
    # Left unresolved: an id that names two tasks names no one task.
    assert third["depends_on"] == ["t1"]


@pytest.mark.unit
def test_an_ambiguous_dep_is_rejected_by_f3_rather_than_dispatched() -> None:
    """Unresolved is only safe because the next gate SEES it.

    ``get_dispatchable_tasks`` raises on a dep it cannot resolve, so leaving the
    raw id in place would wedge the plan if it ever reached dispatch. It does
    not: F3's HARD ``dangling_dep`` rule runs first on exactly this graph and
    the decompose loop re-asks with the violation quoted.
    """
    opus_plan = _duplicate_id_plan()
    normalize_slugs(opus_plan)
    # decompose_plan re-keys every id to the task's own unique slug before
    # validating; the ambiguous DEP is what survives that.
    for task in opus_plan["tasks"]:
        task["id"] = task["slug"]
    leaves = [LeafTask.model_validate(t) for t in opus_plan["tasks"]]

    result = validate_leaves(opus_plan, _FakeProfile(), "", leaves)

    assert "dangling_dep" in {v.rule for v in result.hard}
    assert not result.dispatchable
