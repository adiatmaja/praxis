"""Tests for the shared execute_plan_decompose helper."""

from __future__ import annotations

from typing import Any

from orchestrator.core.execute_plan_decompose import decompose_plan


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
