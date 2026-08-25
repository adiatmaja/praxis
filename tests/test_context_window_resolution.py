"""The pre-dispatch budget gate must divide by a window somebody knows.

Reported live: two dispatches against an agy/Gemini project failed two seconds
after dispatch with ``context for this task exceeds the local model's window``.
The instructions body was 14 KB. Nothing about the task was too big: praxis
sized the worker's window by asking LM STUDIO, which has never heard of a
Gemini model, took the resulting ``None`` and collapsed it onto a hardcoded
``8192``. ``8192 * (1 - 0.6)`` is 3276 tokens, so every floor context over
roughly 13 100 characters was rejected against a model whose real window is on
the order of a million tokens.

The invariant these tests pin: the gate runs on a KNOWN window or it does not
run at all. ``None`` from the probe carries two different facts ("LM Studio is
not serving this model" and "this model is not served by LM Studio at all") and
neither of them is 8192. A fabricated number is the "blank shell exits 0" of
this subsystem: it produces a confident verdict nobody earned.

These tests are organized by HOW THE WINDOW IS KNOWN, not by harness, and that
is deliberate. "Only probe local harnesses" is the obvious-looking fix and it
is a second copy of the same bug: OpenCode is a harness, not a model host, and
an OpenCode project pointed at a hosted OpenAI-compatible provider is a
supported configuration whose model LM Studio has never heard of either.
``test_a_hosted_endpoint_on_opencode_resolves_to_unknown`` exists so nobody
reintroduces a harness-identity gate later.

The skip case is the one that can go vacuous, because a task that dispatches
because the gate was skipped looks exactly like a task that dispatches because
the gate ran and approved. Every skip test here asserts BOTH facts: the task
proceeded, and the skip was recorded saying which harness and model could not
be established.
"""

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from orchestrator.core.context_window import (
    DeclaredWindows,
    ResolvedWindow,
    parse_declared_windows,
    resolve_context_window,
)
from orchestrator.core.harnesses import (
    may_use_lm_studio,
    should_attempt_lm_studio_probe,
)
from orchestrator.models.schemas import TaskStatus


AGY_MODEL = "Gemini 3.7 Flash (High)"
LOCAL_MODEL = "qwen3.8-27b"

# The reported body, to the character. 14 409 chars is ~3 602 estimated tokens,
# comfortably over the 3 276 the fabricated 8192 allowed and nowhere near any
# real Gemini window.
REPORTED_INSTRUCTIONS = "x" * 14409

_DISPATCH_LOGGER = "orchestrator.core.orchestrator_dispatch"


def _lm_studio_serving(*models: dict[str, Any]) -> MagicMock:
    """An httpx.AsyncClient double answering LM Studio's native model list.

    Patched onto ``agent_manager.httpx`` rather than onto the probe itself:
    going through the real probe exercises the "model not in the list" branch
    that produced the reported failure instead of asserting on a stub's return
    value.
    """
    response = MagicMock()
    response.json.return_value = {"data": list(models)}
    response.raise_for_status.return_value = None
    client = MagicMock()
    client.get = AsyncMock(return_value=response)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=client)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return MagicMock(return_value=ctx)


def _configure(
    orch: Any,
    *,
    lm_studio_url: str,
    declared: Any = None,
) -> None:
    """Pin the dispatch path's settings reads to real values.

    ``orchestrator_fixture`` hands out a bare ``AsyncMock``, whose unconfigured
    coroutines return truthy ``MagicMock``s; an unset ``difficulty_config``
    makes every leaf read as flagged and an unset ``lm_studio_url`` makes the
    probe target a mock object.
    """
    orch._effective_settings.difficulty_config.return_value = {
        "weights": {},
        "bias": 0.0,
        "reject_below": 0.35,
        "flag_below": 0.55,
    }
    orch._effective_settings.auto_delegate_enabled.return_value = False
    orch._effective_settings.lm_studio_url.return_value = lm_studio_url
    orch._effective_settings.declared_context_windows.return_value = (
        declared if declared is not None else DeclaredWindows()
    )


async def _project_using(
    orch: Any,
    *,
    harness: str,
    model: str,
    context_window: int | None = None,
) -> dict[str, Any]:
    """Repoint the fixture project at a harness/model pair and re-read the row."""
    await orch._tq._db.execute(
        "UPDATE projects SET harness = ?, model_name = ?, context_window = ? "
        "WHERE id = ?",
        (harness, model, context_window, "proj1"),
    )
    row = await orch._tq.get_project("proj1")
    assert row is not None
    return dict(row)


async def _plan_with_description(orch: Any, description: str) -> str:
    """Create and activate a one-leaf plan carrying ``description``."""
    plan_id = await orch._tq.create_plan("proj1", "big")
    await orch._tq.activate_plan(
        plan_id,
        {
            "tasks": [
                {
                    "id": "big",
                    "slug": "big",
                    "title": "Big",
                    "description": description,
                    "depends_on": [],
                }
            ]
        },
        "plan/big",
    )
    return plan_id


async def _status_of(orch: Any, plan_id: str) -> tuple[str, str | None]:
    rows = await orch._tq.get_tasks_for_plan(plan_id)
    return rows[0]["status"], rows[0]["review_feedback"]


# ---------------------------------------------------------------------------
# The reported failure, end to end
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_a_14kb_agy_dispatch_is_not_failed_by_the_budget_gate(
    orchestrator_fixture,
):
    """The reported case, driven through ``dispatch_pending_tasks``.

    Red before the fix with exactly the reported symptom: FAILED, carrying
    "context for this task exceeds the local model's window", no container.

    Declared windows are the SHIPPED ones, so this is the production path and
    not the skip path: agy resolves to a declared million-token window and the
    gate runs and approves. Delete the declared-window lookup and this goes red
    only if the skip path is also removed, which is why the next test pins the
    number the gate actually used.
    """
    orch, _task_id, _project = orchestrator_fixture
    project = await _project_using(orch, harness="agy", model=AGY_MODEL)
    _configure(
        orch,
        lm_studio_url="http://host.docker.internal:1234",
        declared=parse_declared_windows(None),
    )
    plan_id = await _plan_with_description(orch, REPORTED_INSTRUCTIONS)
    orch._agents.spawn_agent.return_value = "container-1"

    with patch(
        "orchestrator.core.agent_manager.httpx.AsyncClient",
        _lm_studio_serving({"id": LOCAL_MODEL, "loaded_context_length": 112277}),
    ):
        await orch.dispatch_pending_tasks(plan_id, project)

    status, feedback = await _status_of(orch, plan_id)
    assert status == TaskStatus.IN_PROGRESS
    assert feedback is None
    orch._agents.spawn_agent.assert_awaited_once()


@pytest.mark.unit
async def test_the_agy_dispatch_used_a_declared_window_not_a_skipped_gate(
    orchestrator_fixture, caplog
):
    """A POSITIVE fact about the reported case: the gate ran, at 1 000 000.

    Without this, the test above passes identically whether the window was
    resolved or the gate was skipped, and "the fix works" would be unfalsifiable
    for the exact case that was reported.
    """
    orch, _task_id, _project = orchestrator_fixture
    project = await _project_using(orch, harness="agy", model=AGY_MODEL)
    _configure(
        orch,
        lm_studio_url="http://host.docker.internal:1234",
        declared=parse_declared_windows(None),
    )
    plan_id = await _plan_with_description(orch, REPORTED_INSTRUCTIONS)
    orch._agents.spawn_agent.return_value = "container-1"

    with caplog.at_level(logging.INFO, logger=_DISPATCH_LOGGER):
        await orch.dispatch_pending_tasks(plan_id, project)

    assert "1000000 tokens" in caplog.text
    assert "Skipping the pre-dispatch context budget gate" not in caplog.text


@pytest.mark.unit
async def test_the_reported_case_survives_a_real_effective_settings_object(
    orchestrator_fixture, db, test_settings, caplog
):
    """The anti-vacuity test for this whole fix.

    Every other dispatch test here reads its declarations off an ``AsyncMock``,
    and that single mocked line would hide a TOTAL failure: misname the method
    the dispatcher calls and the mock answers anyway, every test stays green,
    and production raises ``AttributeError`` on the first dispatch. The
    declared-window lookup would also be entirely fictional if the real
    ``EffectiveSettings`` did not have it.

    So this one wires the REAL object - real settings file, real database - and
    drives the reported 14 KB agy case through it end to end. Nothing about the
    window is stubbed.
    """
    from orchestrator.core.effective_settings import EffectiveSettings

    orch, _task_id, _project = orchestrator_fixture
    project = await _project_using(orch, harness="agy", model=AGY_MODEL)
    orch._effective_settings = EffectiveSettings(test_settings, db)
    plan_id = await _plan_with_description(orch, REPORTED_INSTRUCTIONS)
    orch._agents.spawn_agent.return_value = "container-1"

    with caplog.at_level(logging.INFO, logger=_DISPATCH_LOGGER):
        await orch.dispatch_pending_tasks(plan_id, project)

    status, feedback = await _status_of(orch, plan_id)
    assert status == TaskStatus.IN_PROGRESS
    assert feedback is None
    orch._agents.spawn_agent.assert_awaited_once()
    # The gate RAN, at a real declared number read off the real settings layer.
    assert "1000000 tokens" in caplog.text


# ---------------------------------------------------------------------------
# The window is known by PROBE: the endpoint is LM Studio and it has the model
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_a_loaded_model_is_probed_and_the_real_value_is_used(
    orchestrator_fixture, caplog
):
    """The regression risk: existing local-model behaviour must be unchanged."""
    orch, _task_id, _project = orchestrator_fixture
    project = await _project_using(orch, harness="opencode", model=LOCAL_MODEL)
    _configure(orch, lm_studio_url="http://host.docker.internal:1234")
    plan_id = await _plan_with_description(orch, "small")
    orch._agents.spawn_agent.return_value = "container-1"

    with (
        caplog.at_level(logging.INFO, logger=_DISPATCH_LOGGER),
        patch(
            "orchestrator.core.context_window.detect_context_limit",
            new_callable=AsyncMock,
            return_value=112277,
        ) as probe,
    ):
        await orch.dispatch_pending_tasks(plan_id, project)

    probe.assert_awaited_once_with("http://host.docker.internal:1234", LOCAL_MODEL)
    assert "112277 tokens" in caplog.text
    assert "Skipping the pre-dispatch context budget gate" not in caplog.text


@pytest.mark.unit
async def test_a_probed_window_still_gates_exactly_as_before(
    orchestrator_fixture,
):
    """A probed 8192 window still refuses a 14 KB pack, with the same message.

    This is the behaviour that must NOT change. The defect was never that the
    gate is wrong at 8192; it is that 8192 was invented for models that do not
    have it.
    """
    orch, _task_id, _project = orchestrator_fixture
    project = await _project_using(orch, harness="opencode", model=LOCAL_MODEL)
    _configure(orch, lm_studio_url="http://host.docker.internal:1234")
    plan_id = await _plan_with_description(orch, REPORTED_INSTRUCTIONS)
    orch._agents.spawn_agent.return_value = "container-1"

    with patch(
        "orchestrator.core.agent_manager.httpx.AsyncClient",
        _lm_studio_serving({"id": LOCAL_MODEL, "loaded_context_length": 8192}),
    ):
        await orch.dispatch_pending_tasks(plan_id, project)

    status, feedback = await _status_of(orch, plan_id)
    assert status == TaskStatus.FAILED
    assert feedback == (
        "context for this task exceeds the local model's window; split the task"
    )
    orch._agents.spawn_agent.assert_not_awaited()


# ---------------------------------------------------------------------------
# Unknown is a third state: skipped, said, and never 8192
#
# Every route into it is here, because the routes have nothing in common except
# that the probe did not come back with a number: LM Studio is up but is not
# serving the model, the endpoint is a hosted provider that is not LM Studio at
# all, and there is no endpoint. Harness identity is not one of the axes.
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_a_hosted_endpoint_on_opencode_resolves_to_unknown(
    orchestrator_fixture, caplog
):
    """OpenCode is a harness, not a model host.

    An operator can point OpenCode at a hosted OpenAI-compatible provider, and
    LM Studio has never heard of that model either. This is the case a
    harness-identity gate ("only probe cloud harnesses are skipped") gets
    exactly wrong: it would probe, miss, and be right back at a fabricated
    number. The probe IS attempted here - the endpoint is unclassifiable from
    its URL and guessing is the defect - and what saves it is that its nothing
    resolves to unknown.
    """
    orch, _task_id, _project = orchestrator_fixture
    project = await _project_using(orch, harness="opencode", model="glm-4.7")
    _configure(orch, lm_studio_url="https://api.z.ai/v1")
    plan_id = await _plan_with_description(orch, REPORTED_INSTRUCTIONS)
    orch._agents.spawn_agent.return_value = "container-1"

    with (
        caplog.at_level(logging.WARNING, logger=_DISPATCH_LOGGER),
        patch(
            "orchestrator.core.agent_manager.httpx.AsyncClient",
            # A hosted provider answering the LM Studio path with something
            # unrecognizable, which is the friendliest thing one can do.
            _lm_studio_serving({"id": "glm-4.7", "object": "model"}),
        ),
    ):
        await orch.dispatch_pending_tasks(plan_id, project)

    status, feedback = await _status_of(orch, plan_id)
    assert status == TaskStatus.IN_PROGRESS
    assert feedback is None
    orch._agents.spawn_agent.assert_awaited_once()
    assert "Skipping the pre-dispatch context budget gate" in caplog.text
    assert "glm-4.7" in caplog.text


@pytest.mark.unit
async def test_a_hosted_endpoint_with_a_declared_window_is_never_probed(
    orchestrator_fixture, caplog
):
    """The remedy for the case above is a declaration, on any harness.

    Declared windows are the primary mechanism for every API-served model, not
    a cloud-harness special case.
    """
    orch, _task_id, _project = orchestrator_fixture
    project = await _project_using(orch, harness="opencode", model="glm-4.7")
    _configure(
        orch,
        lm_studio_url="https://api.z.ai/v1",
        declared=parse_declared_windows({"models": {"glm-4.7": 128_000}}),
    )
    plan_id = await _plan_with_description(orch, REPORTED_INSTRUCTIONS)
    orch._agents.spawn_agent.return_value = "container-1"

    with (
        caplog.at_level(logging.INFO, logger=_DISPATCH_LOGGER),
        patch(
            "orchestrator.core.context_window.detect_context_limit",
            new_callable=AsyncMock,
        ) as probe,
    ):
        await orch.dispatch_pending_tasks(plan_id, project)

    probe.assert_not_awaited()
    assert "128000 tokens" in caplog.text
    orch._agents.spawn_agent.assert_awaited_once()


@pytest.mark.unit
async def test_no_endpoint_configured_is_unknown_and_is_not_probed(
    orchestrator_fixture, caplog
):
    orch, _task_id, _project = orchestrator_fixture
    project = await _project_using(orch, harness="opencode", model=LOCAL_MODEL)
    _configure(orch, lm_studio_url="")
    plan_id = await _plan_with_description(orch, "small")
    orch._agents.spawn_agent.return_value = "container-1"

    with (
        caplog.at_level(logging.WARNING, logger=_DISPATCH_LOGGER),
        patch(
            "orchestrator.core.context_window.detect_context_limit",
            new_callable=AsyncMock,
        ) as probe,
    ):
        await orch.dispatch_pending_tasks(plan_id, project)

    probe.assert_not_awaited()
    assert "Skipping the pre-dispatch context budget gate" in caplog.text
    orch._agents.spawn_agent.assert_awaited_once()


@pytest.mark.unit
async def test_a_cloud_only_harness_is_not_probed_at_all(orchestrator_fixture):
    """An optimization, asserted on the CALL: agy can never be served this way.

    This is the ONLY thing harness identity is allowed to decide, and it saves
    a round trip rather than a wrong answer.
    """
    orch, _task_id, _project = orchestrator_fixture
    project = await _project_using(orch, harness="agy", model=AGY_MODEL)
    _configure(
        orch,
        lm_studio_url="http://host.docker.internal:1234",
        declared=parse_declared_windows(None),
    )
    plan_id = await _plan_with_description(orch, "small")
    orch._agents.spawn_agent.return_value = "container-1"

    with patch(
        "orchestrator.core.context_window.detect_context_limit",
        new_callable=AsyncMock,
    ) as probe:
        await orch.dispatch_pending_tasks(plan_id, project)

    probe.assert_not_awaited()


@pytest.mark.unit
async def test_an_unknown_window_skips_the_gate_and_says_so(
    orchestrator_fixture, caplog
):
    """Both halves asserted, because either alone is satisfied by a bug.

    "The task dispatched" alone is satisfied by a gate that was silently
    removed. "A warning was logged" alone is satisfied by a gate that logged
    and then failed the task anyway. The pair is the claim.
    """
    orch, _task_id, _project = orchestrator_fixture
    project = await _project_using(orch, harness="opencode", model="unknown-model")
    _configure(orch, lm_studio_url="http://host.docker.internal:1234")
    plan_id = await _plan_with_description(orch, REPORTED_INSTRUCTIONS)
    orch._agents.spawn_agent.return_value = "container-1"

    with (
        caplog.at_level(logging.WARNING, logger=_DISPATCH_LOGGER),
        patch(
            "orchestrator.core.context_window.detect_context_limit",
            new_callable=AsyncMock,
            return_value=None,
        ),
    ):
        await orch.dispatch_pending_tasks(plan_id, project)

    status, feedback = await _status_of(orch, plan_id)
    assert status == TaskStatus.IN_PROGRESS
    assert feedback is None
    orch._agents.spawn_agent.assert_awaited_once()
    assert "Skipping the pre-dispatch context budget gate" in caplog.text
    assert "unknown-model" in caplog.text


@pytest.mark.unit
async def test_an_unknown_window_keeps_the_whole_pack_rather_than_trimming_to_8192(
    orchestrator_fixture,
):
    """8192 must be UNREACHABLE, not merely non-fatal.

    A gate that kept "raise" but quietly trimmed the droppable tail to a
    3 276-token budget would pass the test above while still silently
    truncating a cloud worker's pack.

    The assertion is 11 000 characters and not the full 14 409 because
    ``context_scrub`` caps every section at 12 000 chars (heading included) and
    says so in the text. That cap is a separate, VISIBLE mechanism and is
    deliberately not pinned to the character here; 11 000 chars is still 2 750
    tokens of goal alone, which the other floors push over the 3 276-token
    budget the fabricated 8192 produced.
    """
    orch, _task_id, _project = orchestrator_fixture
    project = await _project_using(orch, harness="agy", model="undeclared-gemini")
    _configure(orch, lm_studio_url="")
    plan_id = await _plan_with_description(orch, REPORTED_INSTRUCTIONS)
    orch._agents.spawn_agent.return_value = "container-1"

    await orch.dispatch_pending_tasks(plan_id, project)

    bible = orch._agents.spawn_agent.await_args.kwargs["bible_text"]
    assert "x" * 11_000 in bible
    assert "SCOPE DISCIPLINE" in bible
    # Droppable, and lowest priority but one: the first thing a 3 276-token
    # budget would have discarded.
    assert "WORKING AGREEMENT" in bible


# ---------------------------------------------------------------------------
# Precedence
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_a_project_override_beats_a_declared_window(orchestrator_fixture, caplog):
    orch, _task_id, _project = orchestrator_fixture
    project = await _project_using(
        orch, harness="agy", model=AGY_MODEL, context_window=250_000
    )
    _configure(
        orch,
        lm_studio_url="",
        declared=parse_declared_windows(None),
    )
    plan_id = await _plan_with_description(orch, "small")
    orch._agents.spawn_agent.return_value = "container-1"

    with caplog.at_level(logging.INFO, logger=_DISPATCH_LOGGER):
        await orch.dispatch_pending_tasks(plan_id, project)

    assert "250000 tokens (project override)" in caplog.text


@pytest.mark.unit
async def test_a_declared_window_beats_the_probe(orchestrator_fixture, caplog):
    """An opencode project IS probed, so this pins the order rather than the gate."""
    orch, _task_id, _project = orchestrator_fixture
    project = await _project_using(orch, harness="opencode", model=LOCAL_MODEL)
    _configure(
        orch,
        lm_studio_url="http://host.docker.internal:1234",
        declared=parse_declared_windows({"models": {LOCAL_MODEL: 262_144}}),
    )
    plan_id = await _plan_with_description(orch, "small")
    orch._agents.spawn_agent.return_value = "container-1"

    with (
        caplog.at_level(logging.INFO, logger=_DISPATCH_LOGGER),
        patch(
            "orchestrator.core.context_window.detect_context_limit",
            new_callable=AsyncMock,
            return_value=8192,
        ) as probe,
    ):
        await orch.dispatch_pending_tasks(plan_id, project)

    assert "262144 tokens" in caplog.text
    probe.assert_not_awaited()


@pytest.mark.unit
async def test_an_escalated_leaf_is_budgeted_for_the_harness_that_will_run_it(
    orchestrator_fixture, caplog
):
    """The pack is sized for the implementer, not for the project default.

    An escalated leaf carries its own harness and model. Budgeting it against
    the project's defaults sizes the pack for a model that will never see it.
    """
    orch, _task_id, _project = orchestrator_fixture
    project = await _project_using(orch, harness="opencode", model=LOCAL_MODEL)
    _configure(
        orch,
        lm_studio_url="http://host.docker.internal:1234",
        declared=parse_declared_windows(None),
    )
    plan_id = await _plan_with_description(orch, "small")
    rows = await orch._tq.get_tasks_for_plan(plan_id)
    await orch._tq._db.execute(
        "UPDATE tasks SET implement_harness = ?, implement_model = ? WHERE id = ?",
        ("agy", AGY_MODEL, rows[0]["id"]),
    )
    orch._agents.spawn_agent.return_value = "container-1"

    with (
        caplog.at_level(logging.INFO, logger=_DISPATCH_LOGGER),
        patch(
            "orchestrator.core.context_window.detect_context_limit",
            new_callable=AsyncMock,
            return_value=8192,
        ) as probe,
    ):
        await orch.dispatch_pending_tasks(plan_id, project)

    assert "agy/Gemini 3.7 Flash (High)" in caplog.text
    assert "1000000 tokens" in caplog.text
    probe.assert_not_awaited()


# ---------------------------------------------------------------------------
# The resolver in isolation
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_resolver_reports_unknown_rather_than_a_number():
    resolved = await resolve_context_window(
        harness_id="agy",
        model_name="nobody-declared-this",
        declared=DeclaredWindows(),
        lm_studio_url="http://x:1234",
    )
    assert resolved.tokens is None
    assert resolved.known is False
    assert resolved.source == "unknown"


@pytest.mark.unit
@pytest.mark.parametrize(
    "harness_id", ["opencode", "agy", None, "not-a-harness"], ids=repr
)
async def test_a_probe_returning_nothing_is_unknown_on_every_harness(harness_id):
    """Rule 3, stated per harness so it cannot become harness-conditional."""
    with patch(
        "orchestrator.core.context_window.detect_context_limit",
        new_callable=AsyncMock,
        return_value=None,
    ):
        resolved = await resolve_context_window(
            harness_id=harness_id,
            model_name="not-loaded-anywhere",
            declared=DeclaredWindows(),
            lm_studio_url="http://x:1234",
        )
    assert resolved.known is False
    assert resolved.tokens is None


@pytest.mark.unit
@pytest.mark.parametrize(
    "override", [0, -1, "131072", 8192.0, True, None], ids=lambda v: repr(v)
)
async def test_a_non_positive_int_override_is_not_a_window(override):
    """A NULL column, a typo, or a mock must all read as "not declared".

    ``True`` is in here on purpose: ``isinstance(True, int)`` is True in
    Python, and a boolean sliding through as a 1-token window would make every
    task fail the gate.
    """
    resolved = await resolve_context_window(
        harness_id="agy",
        model_name=AGY_MODEL,
        project_override=override,
        declared=DeclaredWindows(),
    )
    assert resolved.source != "project override"


@pytest.mark.unit
def test_parse_declared_windows_ignores_junk_and_keeps_the_defaults(caplog):
    with caplog.at_level(logging.WARNING, logger="orchestrator.core.context_window"):
        declared = parse_declared_windows(
            {"models": {"m": "lots"}, "harnesses": {"agy": 0}}
        )
    assert declared.for_model("m") is None
    # The junk harness entry did not erase the shipped default.
    assert declared.for_harness("agy") == 1_000_000
    assert "not a positive integer" in caplog.text


@pytest.mark.unit
def test_a_yaml_declaration_overrides_the_shipped_default():
    declared = parse_declared_windows({"models": {AGY_MODEL: 2_000_000}})
    assert declared.for_model(AGY_MODEL) == 2_000_000


@pytest.mark.unit
def test_a_non_mapping_yaml_block_falls_back_to_the_shipped_defaults():
    for raw in (None, "context_windows", [1, 2, 3]):
        declared = parse_declared_windows(raw)
        assert declared.for_model(AGY_MODEL) == 1_000_000


@pytest.mark.unit
def test_resolved_window_known_tracks_tokens():
    assert ResolvedWindow(1, "x").known is True
    assert ResolvedWindow(None, "unknown").known is False


# ---------------------------------------------------------------------------
# The shared probe predicate: an optimization, never the correctness mechanism
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    ("harness_id", "expected"),
    [
        ("opencode", True),
        ("agy", False),
        (None, True),  # None means "the default harness", which is opencode
        ("not-a-harness", False),
    ],
)
def test_may_use_lm_studio_reads_the_registry(harness_id, expected):
    assert may_use_lm_studio(harness_id) is expected


@pytest.mark.unit
@pytest.mark.parametrize("endpoint", ["", None])
def test_no_endpoint_means_no_probe_whatever_the_harness(endpoint):
    """Both halves of the predicate, and this is the half agy's old string missed.

    ``harness_id != "agy"`` probed an OpenCode project with no endpoint at all.
    """
    assert should_attempt_lm_studio_probe("opencode", endpoint) is False


@pytest.mark.unit
def test_a_hosted_endpoint_is_still_worth_attempting():
    """Because nothing can tell it apart from LM Studio by its URL.

    Pattern-matching the hostname here would be a new guess in the exact place
    a guess caused the defect. The probe is attempted and its nothing becomes
    unknown; that is the design, and this test pins it so a later "clever"
    URL classifier has to argue with it.
    """
    assert should_attempt_lm_studio_probe("opencode", "https://api.z.ai/v1") is True


@pytest.mark.unit
def test_the_probe_predicate_follows_the_registry_flag_not_the_harness_name(
    monkeypatch,
):
    """Flip the registry entry and the answer flips: no hardcoded 'agy'.

    Hardcode the name again in ``may_use_lm_studio`` and this is the test that
    goes red.
    """
    from dataclasses import replace

    from orchestrator.core import harnesses

    monkeypatch.setitem(
        harnesses.REGISTRY,
        "agy",
        replace(harnesses.REGISTRY["agy"], supports_local_llm=True),
    )
    assert should_attempt_lm_studio_probe("agy", "http://x:1234") is True
