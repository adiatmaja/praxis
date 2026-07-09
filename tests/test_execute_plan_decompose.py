"""Tests for the shared execute_plan_decompose helper."""

from __future__ import annotations

from typing import Any

import pytest

from orchestrator.core.execute_plan_decompose import (
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


class _FakeEffective:
    async def capability_profile(self, project_id: Any, model: str) -> _FakeProfile:
        return _FakeProfile()


async def test_decompose_plan_returns_normalized_opus_plan():
    raw = (
        '{"tasks":[{"id":"t1","title":"A","description":"d",'
        '"depends_on":[]},{"id":"t2","title":"B","description":"d",'
        '"depends_on":["t1"]}]}'
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
    raw = '{"tasks":[{"id":"t1","title":"X","description":"d","depends_on":[]}]}'
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
    raw = '{"tasks":[{"id":"t1","title":"X","description":"d","depends_on":[]}]}'
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
        '{"tasks": [{"id": "t1", "title": "A", "description": "d", "depends_on": []}]}'
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
    raw = '{"tasks":[{"id":"t1","title":"X","description":"d","depends_on":[]}]}'
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
    raw = '{"tasks":[{"id":"t1","title":"X","description":"d","depends_on":[]}]}'
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
    raw = '{"tasks":[{"id":"t1","title":"X","description":"d","depends_on":[]}]}'
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
