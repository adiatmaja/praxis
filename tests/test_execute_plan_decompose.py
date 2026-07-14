"""Tests for the shared execute_plan_decompose helper."""

from __future__ import annotations

from typing import Any

import pytest

from orchestrator.core.execute_plan_decompose import (
    count_plan_tasks,
    decompose_plan,
    drop_verification_only_leaves,
)
from orchestrator.core.plan_review import PlanReviewError
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
    async def capability_profile(self, project_id: Any, model: str) -> _FakeProfile:
        return _FakeProfile()


async def test_decompose_plan_returns_normalized_opus_plan():
    raw = (
        '{"tasks":[{"id":"t1","title":"A","description":"d",'
        '"depends_on":[],"files":["src/a.py"],"task_type":"feature",'
        '"estimated_loc":50,"verification":"Run pytest and confirm all tests pass"},'
        '{"id":"t2","title":"B","description":"d",'
        '"depends_on":["t1"],"files":["src/b.py"],"task_type":"feature",'
        '"estimated_loc":60,"verification":"Run pytest and confirm all tests pass"}]}'
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
    assert router.calls
    assert router.calls[0][0] == "plan_review"


async def test_decompose_plan_threads_context_onto_tasks():
    raw = '{"tasks":[{"id":"t1","title":"X","description":"d","depends_on":[],"files":["src/x.py"],"task_type":"feature","estimated_loc":40,"verification":"Run pytest and confirm all tests pass"}]}'
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
    raw = '{"tasks":[{"id":"t1","title":"X","description":"d","depends_on":[],"files":["src/x.py"],"task_type":"feature","estimated_loc":40,"verification":"Run pytest and confirm all tests pass"}]}'
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
        '"verification": "Run pytest and confirm all tests pass"}]}'
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
    raw = '{"tasks":[{"id":"t1","title":"X","description":"d","depends_on":[],"files":["src/x.py"],"task_type":"feature","estimated_loc":40,"verification":"Run pytest and confirm all tests pass"}]}'
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
    raw = '{"tasks":[{"id":"t1","title":"X","description":"d","depends_on":[],"files":["src/x.py"],"task_type":"feature","estimated_loc":40,"verification":"Run pytest and confirm all tests pass"}]}'
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
    raw = '{"tasks":[{"id":"t1","title":"X","description":"d","depends_on":[],"files":["src/x.py"],"task_type":"feature","estimated_loc":40,"verification":"Run pytest and confirm all tests pass"}]}'
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
    raw = '{"tasks":[{"id":"t1","title":"A","description":"d","depends_on":[],"files":["src/a.py"],"task_type":"feature","estimated_loc":50,"verification":"Run pytest and confirm all tests pass"}]}'
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
        '{"tasks":[{"id":"t1","title":"A","description":"d","depends_on":[],"files":["src/a.py"],"task_type":"feature","estimated_loc":50,"verification":"Run pytest and confirm all tests pass"},'
        '{"id":"t2","title":"B","description":"d","depends_on":[],"files":["src/b.py"],"task_type":"feature","estimated_loc":60,"verification":"Run pytest and confirm all tests pass"}]}'
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
    raw = '{"tasks":[{"id":"t1","title":"A","description":"d","depends_on":[],"files":["src/a.py"],"task_type":"feature","estimated_loc":50,"verification":"Run pytest and confirm all tests pass"}]}'
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
        '"estimated_loc":50,"verification":"Run pytest and confirm all tests pass"}]}'
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


async def test_decompose_attaches_validation_warnings_on_soft_only():
    """When only SOFT violations remain, validation_warnings is attached and proceeds."""
    # Task with a vague phrase ("refactor") triggers a SOFT violation
    raw = (
        '{"tasks":[{"id":"t1","title":"Refactor module","description":"d",'
        '"depends_on":[],"files":["src/a.py"],"task_type":"refactor",'
        '"estimated_loc":50,"verification":"Run pytest and confirm all tests pass"}]}'
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
    assert "validation_warnings" in opus_plan
    warnings = opus_plan["validation_warnings"]
    assert any(w["rule"] == "vague_phrase" for w in warnings)


async def test_decompose_plan_id_and_emitter_params_accepted():
    """Optional plan_id and emitter params are accepted (inert for now)."""
    raw = (
        '{"tasks":[{"id":"t1","title":"A","description":"d",'
        '"depends_on":[],"files":["src/a.py"],"task_type":"feature",'
        '"estimated_loc":50,"verification":"Run pytest and confirm all tests pass"}]}'
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
        '"estimated_loc":50,"verification":"Run pytest and confirm all tests pass"}]}'
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
        '"estimated_loc":50,"verification":"Run pytest and confirm all tests pass"},'
        '{"id":"t2","title":"B","description":"d",'
        '"depends_on":["t1"],"files":["src/b.py"],"task_type":"feature",'
        '"estimated_loc":60,"verification":"Run pytest and confirm all tests pass"}]}'
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


async def test_decompose_no_emit_when_emitter_is_none():
    """No events emitted when emitter is None (None-safe guard)."""
    raw = (
        '{"tasks":[{"id":"t1","title":"A","description":"d",'
        '"depends_on":[],"files":["src/a.py"],"task_type":"feature",'
        '"estimated_loc":50,"verification":"Run pytest and confirm all tests pass"}]}'
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
        '"estimated_loc":50,"verification":"Run pytest and confirm all tests pass"}]}'
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
