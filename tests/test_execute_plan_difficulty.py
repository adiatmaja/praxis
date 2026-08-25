"""Difficulty scoring runs after F3 and gates dispatch.

Below ``reject_below`` the leaf goes back to the planner with its failing
features named, sharing F3's round budget rather than getting a fresh one.
Between ``reject_below`` and ``flag_below`` the leaf dispatches, flagged.  At
or above ``flag_below`` it is a normal leaf.

Fixture note: every fixture here is deliberately F3-LEGAL (at most
``max_files_touched`` files, at most ``max_loc_delta`` estimated LOC, a
runnable acceptance command, every required plan_text section).  F3 runs
BEFORE the difficulty gate, so an F3-illegal fixture would be rejected by the
validator and never reach the gate under test, and the test would pass or fail
for the wrong reason.  The bands below were measured, not assumed.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock

import pytest

from orchestrator.core.difficulty import DEFAULT_BIAS, DEFAULT_WEIGHTS
from orchestrator.core.execute_plan_decompose import (
    _DECOMPOSE_ATTEMPTS,
    decompose_plan,
)
from orchestrator.core.plan_review import PlanReviewError
from orchestrator.models.schemas import CapabilityProfile


PLAN = """### Task 1: Add the widget

Add a widget to the module.
"""

_ACCEPTANCE = "Run `uv run pytest tests/test_a.py`"

# Five files and 300 estimated LOC sit exactly AT the profile limits, which
# F3 checks with a strict ">" and therefore accepts.
_FIVE_FILES = ["src/a.py", "src/b.py", "src/c.py", "src/d.py", "src/e.py"]


def _plan_text(steps: str = "1. Wire it up.\n") -> str:
    """Build a template-complete plan_text.

    The Goal says "Implement" on purpose: ``drop_verification_only_leaves``
    reads title + description + plan_text, and the runnable acceptance command
    ("uv run pytest") trips its verify-only pattern.  Without a real-work
    phrase to guard it, every fixture leaf here is silently dropped before it
    can be scored and each test then asserts against an empty task list.
    """
    return (
        "## Goal\nImplement a widget.\n"
        "## Files\nsrc/a.py\n"
        f"## Steps\n{steps}"
        f"## Acceptance\n{_ACCEPTANCE}"
    )


def _leaf(**overrides: Any) -> dict[str, Any]:
    """A healthy leaf: one file, small, typed, runnable acceptance."""
    base: dict[str, Any] = {
        "id": "t1",
        "title": "Add the widget",
        "description": "Implement a widget.",
        "plan_text": _plan_text(),
        "depends_on": [],
        "checklist": [{"text": "add it"}],
        "files": ["src/a.py"],
        "task_type": "feature",
        "estimated_loc": 40,
        "verification": "Run `uv run pytest tests/test_a.py` and confirm it passes",
        "leaf_type": "function_add",
    }
    base.update(overrides)
    return base


def _flagged_leaf(**overrides: Any) -> dict[str, Any]:
    """F3-legal but predicted borderline: at both limits, untyped shape."""
    return _leaf(
        files=list(_FIVE_FILES), estimated_loc=300, leaf_type="generic", **overrides
    )


def _hopeless_leaf(**overrides: Any) -> dict[str, Any]:
    """F3-legal but predicted hopeless: the flagged leaf plus a bloated context."""
    return _flagged_leaf(
        plan_text=_plan_text("1. do a thing in great detail. " * 900 + "\n"),
        **overrides,
    )


def _response(*leaves: dict[str, Any]) -> str:
    return json.dumps({"tasks": list(leaves)})


class _Router:
    """Returns each canned response in turn, then repeats the last one."""

    def __init__(self, *responses: str) -> None:
        self.responses = list(responses)
        self.prompts: list[str] = []

    async def run(
        self,
        call_site: str,
        prompt: str,
        project_id: Any = None,
        cwd: Any = None,
    ) -> str:
        self.prompts.append(prompt)
        return self.responses[min(len(self.prompts) - 1, len(self.responses) - 1)]


def _settings() -> AsyncMock:
    settings = AsyncMock()
    settings.capability_profile.return_value = CapabilityProfile(
        model_name="m", parameter_count_b=30, context_window=8192
    )
    settings.difficulty_config.return_value = {
        "weights": DEFAULT_WEIGHTS,
        "bias": DEFAULT_BIAS,
        "reject_below": 0.35,
        "flag_below": 0.55,
    }
    return settings


def _event_types(emitter: AsyncMock) -> list[str]:
    return [
        call.args[0].event_type for call in emitter.emit.await_args_list if call.args
    ]


def _events_of(emitter: AsyncMock, event_type: str) -> list[Any]:
    return [
        call.args[0]
        for call in emitter.emit.await_args_list
        if call.args and call.args[0].event_type == event_type
    ]


@pytest.mark.unit
async def test_a_healthy_leaf_dispatches_and_carries_its_score():
    router = _Router(_response(_leaf()))
    plan = await decompose_plan(PLAN, "m", None, router, _settings(), project_id="p1")
    task = plan["tasks"][0]
    assert 0.0 <= task["difficulty_score"] <= 1.0
    assert task["difficulty_score"] >= 0.55
    assert task["difficulty_flagged"] is False


@pytest.mark.unit
async def test_a_borderline_leaf_is_flagged_but_still_dispatched():
    router = _Router(_response(_flagged_leaf()))
    plan = await decompose_plan(PLAN, "m", None, router, _settings(), project_id="p1")
    assert len(plan["tasks"]) == 1
    task = plan["tasks"][0]
    assert 0.35 <= task["difficulty_score"] < 0.55
    assert task["difficulty_flagged"] is True


@pytest.mark.unit
async def test_a_hopeless_leaf_is_re_asked_with_its_failing_features():
    router = _Router(_response(_hopeless_leaf()), _response(_leaf()))
    plan = await decompose_plan(PLAN, "m", None, router, _settings(), project_id="p1")
    assert len(router.prompts) == 2
    second = router.prompts[1]
    assert "difficulty" in second.lower()
    # context_ratio is what actually drags this F3-legal leaf under the floor:
    # its bloated plan_text. loc_ratio cannot exceed 1.0 on an F3-legal leaf.
    assert "context_ratio" in second
    # The corrected second answer is what gets returned.
    assert plan["tasks"][0]["difficulty_score"] >= 0.55
    assert plan["tasks"][0]["difficulty_flagged"] is False


@pytest.mark.unit
async def test_a_hopeless_leaf_twice_rejects_the_whole_plan():
    router = _Router(_response(_hopeless_leaf()))
    with pytest.raises(PlanReviewError, match="difficulty"):
        await decompose_plan(PLAN, "m", None, router, _settings(), project_id="p1")
    # The gate shares F3's budget: never more brain calls than F3 alone would make.
    assert len(router.prompts) == _DECOMPOSE_ATTEMPTS


@pytest.mark.unit
async def test_the_difficulty_gate_never_gets_its_own_extra_rounds():
    """A permanently hopeless plan costs exactly the shared budget, no more.

    A separate budget for the difficulty gate would let a pathological plan
    re-ask the brain far past F3's cap without anything failing loudly, so the
    call count is pinned here rather than merely bounded.
    """
    router = _Router(_response(_hopeless_leaf()))
    with pytest.raises(PlanReviewError):
        await decompose_plan(PLAN, "m", None, router, _settings(), project_id="p1")
    assert len(router.prompts) == _DECOMPOSE_ATTEMPTS == 2


@pytest.mark.unit
async def test_f3_and_difficulty_feedback_share_the_same_re_ask():
    """One leaf failing both gates is re-asked once, carrying both critiques.

    Feeding back only the difficulty critique would silently drop the F3
    violations and burn the shared round on half the information.
    """
    both_bad = _hopeless_leaf(verification="Check it manually by eyeball")
    router = _Router(_response(both_bad), _response(_leaf()))
    plan = await decompose_plan(PLAN, "m", None, router, _settings(), project_id="p1")
    assert len(router.prompts) == 2
    second = router.prompts[1]
    assert "HARD violations" in second
    assert "[verification]" in second
    assert "difficulty" in second.lower()
    assert plan["tasks"][0]["difficulty_score"] >= 0.55


@pytest.mark.unit
async def test_scoring_emits_one_event_per_leaf():
    emitter = AsyncMock()
    router = _Router(_response(_leaf()))
    await decompose_plan(
        PLAN,
        "m",
        None,
        router,
        _settings(),
        project_id="p1",
        plan_id="plan1",
        emitter=emitter,
    )
    assert "leaf_difficulty_scored" in _event_types(emitter)
    scored = _events_of(emitter, "leaf_difficulty_scored")
    assert len(scored) == 1
    assert scored[0].leaf_slug == "add-the-widget"
    assert scored[0].flagged is False
    # The event is the calibration loop's training data: it carries the real
    # feature vector, not an empty placeholder.
    assert scored[0].features["files_touched"] == 1.0
    assert scored[0].features["loc_ratio"] == pytest.approx(40 / 300)


@pytest.mark.unit
async def test_a_flagged_leaf_is_emitted_as_flagged():
    emitter = AsyncMock()
    router = _Router(_response(_flagged_leaf()))
    await decompose_plan(
        PLAN,
        "m",
        None,
        router,
        _settings(),
        project_id="p1",
        plan_id="plan1",
        emitter=emitter,
    )
    scored = _events_of(emitter, "leaf_difficulty_scored")
    assert len(scored) == 1
    assert scored[0].flagged is True
    assert scored[0].p_success < 0.55


@pytest.mark.unit
async def test_a_predispatch_rejection_emits_its_own_event():
    emitter = AsyncMock()
    router = _Router(_response(_hopeless_leaf()))
    with pytest.raises(PlanReviewError):
        await decompose_plan(
            PLAN,
            "m",
            None,
            router,
            _settings(),
            project_id="p1",
            plan_id="plan1",
            emitter=emitter,
        )
    assert "leaf_rejected_predispatch" in _event_types(emitter)
    rejected = _events_of(emitter, "leaf_rejected_predispatch")
    assert len(rejected) == 1
    assert rejected[0].leaf_slug == "add-the-widget"
    assert rejected[0].p_success < 0.35
    assert "context_ratio" in rejected[0].failing_features
    # A rejected leaf never also reports as dispatchable.
    assert "leaf_difficulty_scored" not in _event_types(emitter)


@pytest.mark.unit
async def test_settings_without_a_difficulty_config_falls_back_to_defaults():
    """A settings object predating the gate still gets the gate, on defaults.

    ``EffectiveSettings`` always supplies ``difficulty_config``; older test
    doubles and any caller-supplied settings shim may not.  Falling back to the
    module defaults keeps the gate live instead of silently disabling it.
    """

    class _NoDifficultyConfig:
        async def capability_profile(
            self,
            project_id: Any,
            model: str,
            harness: Any = None,
            project_context_window: Any = None,
        ) -> Any:
            # ASSERTED, not accepted into oblivion. A fake that swallows a new
            # argument cannot notice production has stopped passing it, and
            # ``project_id`` is exactly such an argument: ``decompose_plan``
            # hardcoded ``project_id=None`` here until the context-window work,
            # so the per-project capability override could never apply. This
            # stub is now the thing that would catch a regression to that.
            assert project_id == "p1"
            assert model == "m"
            # ``harness`` and ``project_context_window`` are legitimately None:
            # this test's caller supplies neither, and the subject is the
            # difficulty gate's fallback, not window resolution. Stated so the
            # next reader knows the omission is the caller's, not the fake's.
            assert harness is None
            assert project_context_window is None
            return CapabilityProfile(
                model_name="m", parameter_count_b=30, context_window=8192
            )

    router = _Router(_response(_hopeless_leaf()))
    with pytest.raises(PlanReviewError, match="difficulty"):
        await decompose_plan(
            PLAN, "m", None, router, _NoDifficultyConfig(), project_id="p1"
        )
