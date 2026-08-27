"""A DEPLOYMENT fault must not be recorded as a fact about the worker.

Two real container failures, measured live on 2026-08-27 against task
``54fa9978-fa53-42dc-b951-5b33e4b19d33``, are quoted VERBATIM below. Both were
charged to the worker's capability record, and neither says anything about
whether the model can write the code it was handed:

1. the endpoint does not serve the model Praxis asked for at all - and Praxis
   itself chose that model, by promoting a leaf to ``glm-4.7`` through adaptive
   triage's ``escalate``, onto a rung this endpoint cannot serve;
2. the endpoint has a smaller window loaded than the assembled prompt needs.

They land in the same defect class this repo closed at three other seats on
2026-08-26: **a fact about the DEPLOYMENT recorded as a fact about the WORKER.**

The two are NOT resolved the same way, and this file exists as much to pin the
difference as to test the new predicate.

**Neither is a ``_PROVIDER_SIGNALS`` entry, and that is the load-bearing
decision here.** ``is_provider_error`` returning True does two things at once:
it stops the failure being charged to the worker (right for both), and it
re-queues the task WITHOUT consuming a retry (wrong for both, because neither
condition changes on a retry). On the reconcile path that re-queue is bounded by
``PROVIDER_ERROR_RESPAWN_CAP``; on the CALLBACK path - the one both harness
entrypoints actually take, the ``elif provider_error_run:`` branch in
``api/internal.py``, which ``rg -n "provider_error_run" src/`` locates - it is
bounded by nothing at all. (Located by a grep rather than a line number on
purpose: the line number quoted here was invalidated by an unrelated fix in the
same working tree, within hours of being written.) Measured on 2026-08-27 with a throwaway probe: twelve
consecutive provider-error callbacks against ``max_retries=3`` left the task at
``status=pending attempt=1`` every single time. So classifying a PERMANENT
condition as a transient provider error converts a task that fails in three
attempts into one that respawns a container every loop tick forever, while the
plan reads ACTIVE with a null ``error``.

Case 1 is therefore a THIRD category - permanent misconfiguration - which
``permanent_worker_config_error`` names and which must fail the task terminally
with a reason naming the model and the endpoint.

Case 2 is deliberately left in the ordinary failure path. "The prompt did not
fit the window" is exactly the signal adaptive splitting exists to consume, and
``split`` has never yet been observed on a real repository. Peeling it off here
would suppress the one shape most likely to produce one. Its real defect is
upstream: the pre-dispatch budget gate measures only the Bible sections and
cannot see most of what the harness actually sends.
"""

from __future__ import annotations

import pytest

from orchestrator.core.provider_errors import (
    is_provider_error,
    permanent_worker_config_error,
)


#: Verbatim from ``agent_runs.logs``, run 7c7e1e1f-33c4-4f91-a345-ce8f9f9cc463
#: of task 54fa9978-fa53-42dc-b951-5b33e4b19d33, INCLUDING the SGR escapes.
#:
#: The escapes are the point. The first version of this fixture was copied out
#: of a prose report that had stripped them, which put ``Error: `` and
#: ``Invalid model identifier`` adjacent and at the start of a line. Against
#: that shape the predicate passed every test and matched ZERO real logs: the
#: bytes OpenCode actually writes put ``\x1b[0m`` between the two halves and
#: ``\x1b[91m\x1b[1m`` before the anchor, so both the anchor and the phrase
#: missed. A fixture for this predicate that carries no escapes proves only
#: that the sanitizer works.
MODEL_NOT_SERVED_LOG = (
    '\x1b[91m\x1b[1mError: \x1b[0mInvalid model identifier "glm-4.7". '
    "Please specify a valid downloaded model (e.g., qwen/qwen3.8-27b@q4_k_m, "
    "qwen/qwen3.8-27b, qwen3.8-27b)."
)

#: Verbatim from the same run's logs, escapes included. Note the missing space
#: in ``9997>=``: copied exactly, because a paraphrase would not exercise the
#: parse.
CONTEXT_OVERFLOW_LOG = (
    '\x1b[91m\x1b[1mError: \x1b[0m{"message":"The number of tokens to keep '
    "from the initial prompt is greater than the context length (n_keep: "
    "9997>= n_ctx: 4096). Try to load the model with a larger context length, "
    'or provide a shorter input."}'
)

#: What both shipped entrypoints emit around whatever the harness printed.
_LOG_PREAMBLE = (
    "--- Cloning repository ---\n"
    "Cloning into 'workspace'...\n"
    "--- Running OpenCode (headless) ---\n"
)
_LOG_TAIL = "\nStatus: failed\n"


def _as_container_log(harness_line: str) -> str:
    """Wrap a harness error line in the surrounding container transcript.

    The predicates are handed the WHOLE container log, never one line, so every
    fixture here is a transcript. A predicate that only worked on the bare line
    would pass a one-line fixture and do nothing in production.
    """
    return f"{_LOG_PREAMBLE}{harness_line}{_LOG_TAIL}"


def test_a_model_the_endpoint_does_not_serve_is_a_permanent_config_fault() -> None:
    """Case 1: the endpoint answered, and refused the model by name."""
    reason = permanent_worker_config_error(_as_container_log(MODEL_NOT_SERVED_LOG))
    assert reason is not None


def test_the_permanent_fault_reason_names_the_model_and_the_endpoint() -> None:
    """A terminal reason a human reads must say WHICH model at WHICH endpoint.

    Without both, the operator is told "misconfigured" and left to find the
    pair themselves - and the pair is the whole content of the fault. The model
    is parsed out of the endpoint's own wording rather than taken from the
    caller, so the reason quotes what the endpoint actually rejected.
    """
    reason = permanent_worker_config_error(
        _as_container_log(MODEL_NOT_SERVED_LOG),
        endpoint="https://pcllm.sigmasolusi.com",
    )
    assert reason is not None
    assert "glm-4.7" in reason
    assert "https://pcllm.sigmasolusi.com" in reason


def test_the_reason_is_still_usable_when_the_caller_has_no_endpoint() -> None:
    """``endpoint`` is optional, and its absence must not produce ``None`` prose.

    An f-string interpolating an absent endpoint would put the literal "None"
    in front of an operator as though it were a URL.
    """
    reason = permanent_worker_config_error(_as_container_log(MODEL_NOT_SERVED_LOG))
    assert reason is not None
    assert "None" not in reason
    assert "glm-4.7" in reason


def test_a_permanent_fault_is_not_a_transient_provider_error() -> None:
    """The tripwire. This test PASSES before the fix as well as after it.

    That is stated rather than hidden, because a test which is green on both
    sides of a change is the exact shape of an inert guard. Its value is not
    RED-first; it is that adding ``"Invalid model identifier"`` to
    ``_PROVIDER_SIGNALS`` - the obvious-looking one-line fix for the reported
    defect - turns it red. That "fix" would stop the mis-attribution and buy an
    unbounded respawn loop against a model that will never exist, because the
    callback consumer applies no cap (see this module's docstring).
    """
    assert not is_provider_error(_as_container_log(MODEL_NOT_SERVED_LOG))


def test_a_context_window_overflow_is_not_a_transient_provider_error() -> None:
    """Case 2 stays in the ordinary failure path, and this pins that choice.

    Same tripwire shape as above, for the opposite reason: retrying an
    oversized prompt against a fixed window is a guaranteed re-failure, and
    peeling it off here would also hide it from adaptive triage, which is the
    only thing that can answer it with ``split``.
    """
    assert not is_provider_error(_as_container_log(CONTEXT_OVERFLOW_LOG))


def test_a_context_window_overflow_is_not_a_permanent_config_fault_either() -> None:
    """And it must not be swept into the new category on a later pass.

    The wording is tempting - a 4096-token window IS a deployment setting - but
    ``permanent_worker_config_error`` is terminal, and a terminal verdict here
    would destroy the same ``split`` signal for a second time by another route.
    """
    assert (
        permanent_worker_config_error(_as_container_log(CONTEXT_OVERFLOW_LOG)) is None
    )


@pytest.mark.parametrize(
    "harness_line",
    [
        "Error: build failed: 3 tests failed in tests/test_thing.py",
        "TypeError: Invalid argument passed to identifier()",
        "Error: Forbidden",
        "npm ERR! code ECONNRESET",
    ],
)
def test_ordinary_worker_and_gateway_failures_are_not_permanent_faults(
    harness_line: str,
) -> None:
    """Including the two gateway lines, which belong to the OTHER predicate.

    A permanent-fault predicate that also fired on ``Error: Forbidden`` would
    convert every transient WAF block into a terminal failure.
    """
    assert permanent_worker_config_error(_as_container_log(harness_line)) is None


@pytest.mark.parametrize(
    "harness_line",
    [
        # A worker whose LEAF is about this repo's own model handling. Praxis
        # is dogfooded on itself, so its container logs quote its own source.
        '+    r\'^\\s*Error: Invalid model identifier "(?P<model>[^"]+)"\',',
        "  the endpoint replies Error: Invalid model identifier when the model "
        "is missing, so we",
        'assert "Invalid model identifier" in reason',
    ],
)
def test_the_phrase_quoted_inside_worker_output_is_not_a_fault(
    harness_line: str,
) -> None:
    """A bare substring scan over a whole transcript is a false-positive engine.

    This repo has been bitten by exactly that before: the protected-base check
    ANDed two unanchored substrings over the full worker transcript and fired
    on a log where they came from different places. The recorded fix was to
    anchor on the tool's exact wording, and that is what is done here - the
    line must BEGIN with the endpoint's error, so a diff line, a prose mention
    and a quoted assertion all fail to match.
    """
    assert permanent_worker_config_error(_as_container_log(harness_line)) is None
