"""The pre-dispatch model probe: verify the RESPONSE's model, never the request.

An OpenAI-compatible endpoint returns HTTP 200 for a model string it does not
serve and hands back whatever is loaded (measured 2026-08-27: ``glm-4.7`` and
``totally-made-up-model-xyz`` both answered ``"model": "qwen3.8-27b"``). So a
worker dispatched under an unserved model name runs a DIFFERENT model and
Praxis records the outcome under the name it asked for. This probe is what
makes a rung, or a preset, honest: substitution refuses the spawn; an
outage does not, because that is the provider-error path's job.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from orchestrator.core import orchestrator_dispatch
from orchestrator.core.worker_model_probe import (
    ModelProbe,
    model_matches,
    normalize_model_name,
    probe_served_model,
    substitution_reason,
)
from orchestrator.models.schemas import TaskStatus


@pytest.mark.parametrize(
    ("requested", "served", "expected"),
    [
        ("qwen3.8-27b", "qwen3.8-27b", True),
        ("Qwen3.8-27B", "qwen3.8-27b", True),
        ("lmstudio-community/qwen3.8-27b", "qwen3.8-27b", True),
        ("qwen3.8-27b", "qwen3.8-27b:2", True),
        ("vendor/qwen3.8-27b", "qwen3.8-27b:3", True),
        ("glm-4.7", "qwen3.8-27b", False),
        ("totally-made-up-model-xyz", "qwen3.8-27b", False),
        ("qwen3.8-27b", "qwen3.8-27b-instruct", False),
    ],
)
def test_model_matches_tolerates_prefix_and_instance_suffix_only(
    requested: str, served: str, expected: bool
) -> None:
    assert model_matches(requested, served) is expected


def test_normalize_keeps_a_non_numeric_colon_suffix() -> None:
    """``:2`` is an instance suffix; ``:latest`` is part of the name."""
    assert normalize_model_name("x:latest") == "x:latest"
    assert normalize_model_name("x:2") == "x"


def _endpoint(answer: Any, status: int = 200) -> tuple[httpx.MockTransport, list]:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        assert request.url.path.endswith("/v1/chat/completions")
        if isinstance(answer, Exception):
            raise answer
        return httpx.Response(status, json=answer)

    return httpx.MockTransport(handler), seen


async def test_probe_reports_substitution_from_the_response_model_field() -> None:
    transport, seen = _endpoint({"model": "qwen3.8-27b", "choices": []})
    probe = await probe_served_model("http://lm", "glm-4.7", transport=transport)
    assert probe == ModelProbe(
        "substituted",
        "glm-4.7",
        "qwen3.8-27b",
        "http://lm/v1/chat/completions was asked for 'glm-4.7' and answered "
        "with 'qwen3.8-27b'",
    )
    body = seen[0]
    assert body["model"] == "glm-4.7"
    assert body["max_tokens"] == 1
    assert body["reasoning_effort"] == "none", "the payload must state its effort"


async def test_probe_reports_served_on_a_matching_response() -> None:
    transport, _ = _endpoint({"model": "vendor/qwen3.8-27b:2"})
    probe = await probe_served_model("http://lm/", "qwen3.8-27b", transport=transport)
    assert probe.verdict == "served"
    assert probe.served == "vendor/qwen3.8-27b:2"


@pytest.mark.parametrize(
    "answer",
    [
        httpx.ConnectError("refused"),
        {"choices": []},
        {"model": ""},
        ["not", "an", "object"],
    ],
)
async def test_probe_is_unverified_when_nothing_can_be_established(
    answer: Any,
) -> None:
    transport, _ = _endpoint(answer)
    probe = await probe_served_model("http://lm", "qwen3.8-27b", transport=transport)
    assert probe.verdict == "unverified"
    assert probe.served is None


async def test_probe_is_unverified_on_a_non_200() -> None:
    transport, _ = _endpoint({"model": "qwen3.8-27b"}, status=503)
    probe = await probe_served_model("http://lm", "qwen3.8-27b", transport=transport)
    assert probe.verdict == "unverified"


async def test_probe_skips_when_there_is_no_endpoint() -> None:
    transport, seen = _endpoint({"model": "x"})
    probe = await probe_served_model("", "x", transport=transport)
    assert probe.verdict == "unverified"
    assert seen == []


def test_substitution_reason_names_both_models_and_the_remedy() -> None:
    text = substitution_reason(ModelProbe("substituted", "glm-4.7", "qwen3.8-27b", ""))
    assert "'glm-4.7'" in text
    assert "'qwen3.8-27b'" in text
    assert "praxis retry" in text
    assert "RESPONSE" in text


# --- the dispatch seam -----------------------------------------------------


def _configure(orch: Any, url: str = "http://lm") -> None:
    orch._effective_settings.difficulty_config.return_value = {
        "weights": {},
        "bias": 0.0,
        "reject_below": 0.35,
        "flag_below": 0.55,
    }
    orch._effective_settings.auto_delegate_enabled.return_value = False
    orch._effective_settings.lm_studio_url.return_value = url
    orch._effective_settings.declared_context_windows.return_value = None


class _Probe:
    """A stand-in for ``probe_served_model``, recording what it was asked."""

    def __init__(self, verdict: str, served: str | None = "qwen3.8-27b") -> None:
        self.verdict = verdict
        self.served = served
        self.calls: list[tuple[str, str]] = []

    async def __call__(self, url: str, model: str, **_: Any) -> ModelProbe:
        self.calls.append((url, model))
        return ModelProbe(self.verdict, model, self.served, "stub")  # type: ignore[arg-type]


async def _pending_task(orch: Any, task_id: str) -> dict[str, Any]:
    await orch._tq.update_task_status(task_id, TaskStatus.PENDING)
    task = await orch._tq.get_task(task_id)
    assert task is not None
    return dict(task)


@pytest.mark.unit
async def test_a_substituted_model_refuses_the_spawn_and_fails_the_task(
    orchestrator_fixture, captured_events, monkeypatch
) -> None:
    orch, task_id, project = orchestrator_fixture
    _configure(orch)
    probe = _Probe("substituted")
    monkeypatch.setattr(orchestrator_dispatch, "probe_served_model", probe)
    task = await _pending_task(orch, task_id)
    orch._agents.spawn_agent.return_value = "container-1"

    await orch.dispatch_pending_tasks(
        task["plan_id"], dict(project, context_window=32768)
    )

    assert probe.calls == [("http://lm", "qwen3.6-27b")]
    orch._agents.spawn_agent.assert_not_called()
    row = await orch._tq.get_task(task_id)
    assert row["status"] == TaskStatus.FAILED
    assert "'qwen3.6-27b'" in row["review_feedback"]
    assert "'qwen3.8-27b'" in row["review_feedback"]
    assert await orch._tq.get_runs_for_task(task_id) == [], "no run row for no spawn"
    event = next(e for e in captured_events if e["type"] == "worker_model_substituted")
    assert event["task_id"] == task_id
    assert event["requested"] == "qwen3.6-27b"
    assert event["served"] == "qwen3.8-27b"
    outcomes = await orch._tq._db.fetch_all(
        "SELECT * FROM task_outcomes WHERE task_id = ?", (task_id,)
    )
    assert outcomes == [], "a refusal before any worker ran is not attributable"


@pytest.mark.unit
@pytest.mark.parametrize("verdict", ["served", "unverified"])
async def test_served_and_unverified_both_proceed_to_spawn(
    orchestrator_fixture, captured_events, monkeypatch, verdict: str
) -> None:
    orch, task_id, project = orchestrator_fixture
    _configure(orch)
    probe = _Probe(verdict, served=None if verdict == "unverified" else "qwen3.6-27b")
    monkeypatch.setattr(orchestrator_dispatch, "probe_served_model", probe)
    task = await _pending_task(orch, task_id)
    orch._agents.spawn_agent.return_value = "container-1"

    await orch.dispatch_pending_tasks(
        task["plan_id"], dict(project, context_window=32768)
    )

    assert probe.calls == [("http://lm", "qwen3.6-27b")]
    orch._agents.spawn_agent.assert_called_once()
    assert any(e["type"] == "agent_dispatched" for e in captured_events)


@pytest.mark.unit
async def test_a_harness_without_an_endpoint_is_never_probed(
    orchestrator_fixture, monkeypatch
) -> None:
    """agy speaks only to Google: there is no OpenAI-compatible endpoint to ask."""
    orch, task_id, project = orchestrator_fixture
    _configure(orch)
    probe = _Probe("substituted")
    monkeypatch.setattr(orchestrator_dispatch, "probe_served_model", probe)
    await orch._tq._db.execute(
        "UPDATE projects SET harness = 'agy' WHERE id = ?", (project["id"],)
    )
    project = dict(project, harness="agy")
    task = await _pending_task(orch, task_id)
    orch._agents.spawn_agent.return_value = "container-1"

    await orch.dispatch_pending_tasks(
        task["plan_id"], dict(project, context_window=32768)
    )

    assert probe.calls == []
    orch._agents.spawn_agent.assert_called_once()


@pytest.mark.unit
async def test_an_empty_endpoint_url_is_never_probed(
    orchestrator_fixture, monkeypatch
) -> None:
    orch, task_id, project = orchestrator_fixture
    _configure(orch, url="")
    probe = _Probe("substituted")
    monkeypatch.setattr(orchestrator_dispatch, "probe_served_model", probe)
    task = await _pending_task(orch, task_id)
    orch._agents.spawn_agent.return_value = "container-1"

    await orch.dispatch_pending_tasks(
        task["plan_id"], dict(project, context_window=32768)
    )

    assert probe.calls == []
    orch._agents.spawn_agent.assert_called_once()
