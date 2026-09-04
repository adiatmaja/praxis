"""Verify, before a worker is spawned, that the endpoint SERVES the model named.

Why this file exists
--------------------
An OpenAI-compatible endpoint may ignore the ``model`` field entirely.
Measured against the reference LM Studio endpoint on 2026-08-27: ``glm-4.7``
and ``totally-made-up-model-xyz`` both returned HTTP 200 with
``"model": "qwen3.8-27b"``, whatever was loaded. No 404, no error field. So a
worker preset or an escalation rung naming a model nobody serves is not a
failure anyone finds out about: it is a SILENT no-op that still stamps
``tasks.implement_model`` and still writes ``task_outcomes`` rows under that
name, teaching the capability engine a stronger model's success rate from a
weaker model's output. That is why ``implement_escalation`` ships empty.

The model-LIST probe was authoritative all along, but the completions
endpoint is the one the worker actually talks to and the one that JIT-loads
a model by name, so the only check that cannot be fooled is the ``model``
field of a completions RESPONSE. That is what this module asks for, with a
one-token request, and it answers one of three things:

- ``served``: the response names the model that was requested (modulo a
  stripped namespace prefix or an added instance suffix, both benign).
- ``substituted``: the response names a DIFFERENT model. Dispatching would
  record a lie, so the caller refuses to spawn.
- ``unverified``: nothing could be established (endpoint down, no ``model``
  field, an unexpected shape). The caller proceeds: the worker will meet the
  same endpoint and the existing provider-error path handles an outage. This
  probe exists to catch SUBSTITUTION, not reachability, and failing closed
  on an outage would turn a transient into a terminal.

It is a MEASUREMENT of what the endpoint answers, never a guess from the
model list or the hostname.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Literal

import httpx

from orchestrator.core.thinking import effort_param


logger = logging.getLogger(__name__)

Verdict = Literal["served", "substituted", "unverified"]

#: Long enough for LM Studio to JIT-load a model that is named but not yet
#: loaded (tens of seconds on a large model); short enough that a dead
#: endpoint does not stall the dispatch pass for long.
PROBE_TIMEOUT_SECONDS = 90.0


@dataclass(frozen=True)
class ModelProbe:
    """What one probe established."""

    verdict: Verdict
    requested: str
    served: str | None
    detail: str


def normalize_model_name(name: str) -> str:
    """Reduce a model string to the part that identifies the MODEL.

    A stripped namespace prefix (``vendor/x`` -> ``x``) and an added instance
    suffix (``x`` -> ``x:2``) are benign: LM Studio reports both shapes for
    one loaded model. Case is ignored. Anything else that differs is a
    different model.
    """
    text = name.strip().lower()
    if "/" in text:
        text = text.rsplit("/", 1)[1]
    head, sep, tail = text.rpartition(":")
    if sep and tail.isdigit():
        text = head
    return text


def model_matches(requested: str, served: str) -> bool:
    """True when ``served`` names the same model as ``requested``."""
    return normalize_model_name(requested) == normalize_model_name(served)


async def probe_served_model(
    lm_studio_url: str,
    model: str,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    timeout: float = PROBE_TIMEOUT_SECONDS,
) -> ModelProbe:
    """Ask the endpoint for one token and read the model it says it used.

    Args:
        lm_studio_url: The endpoint the worker will be given.
        model: The model string the worker will be told to use.
        transport: Injected for tests; None uses the network.
        timeout: Seconds to wait for the endpoint.

    Returns:
        A :class:`ModelProbe`. ``unverified`` on every failure to establish
        an answer; ``substituted`` only on a positive, differing ``model``
        field in a successful response.
    """
    if not lm_studio_url or not model:
        return ModelProbe("unverified", model, None, "no endpoint or model to probe")
    url = lm_studio_url.rstrip("/") + "/v1/chat/completions"
    # ``reasoning_effort`` is stated EXPLICITLY, never omitted (core/thinking):
    # ``none`` here because the answer is the ``model`` field, not the text,
    # and a thinking model would spend its one token on reasoning.
    body: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": "Reply with the word OK."}],
        "max_tokens": 1,
        "temperature": 0,
        **effort_param("none"),
    }
    try:
        async with httpx.AsyncClient(timeout=timeout, transport=transport) as http:
            resp = await http.post(url, json=body)
            resp.raise_for_status()
            payload = resp.json()
    except Exception as exc:  # noqa: BLE001 - every failure means "nobody knows"
        detail = f"probe of {url} raised {type(exc).__name__}: {exc}"
        logger.warning("Worker model not verified for %r: %s", model, detail)
        return ModelProbe("unverified", model, None, detail)
    served = payload.get("model") if isinstance(payload, dict) else None
    if not isinstance(served, str) or not served.strip():
        detail = f"{url} answered without a model field"
        logger.warning("Worker model not verified for %r: %s", model, detail)
        return ModelProbe("unverified", model, None, detail)
    if model_matches(model, served):
        return ModelProbe("served", model, served, f"{url} serves {served!r}")
    return ModelProbe(
        "substituted",
        model,
        served,
        f"{url} was asked for {model!r} and answered with {served!r}",
    )


def substitution_reason(probe: ModelProbe) -> str:
    """The failure reason stored on a task whose model the endpoint does not serve.

    Operator-facing, on the same shape as the permanent spawn refusals in
    ``orchestrator_dispatch``: retrying changes nothing until the
    configuration or the endpoint does, and the task is failed rather than
    deferred so a human sees it.
    """
    return (
        "agent spawn refused, and retrying will not change that: the worker "
        f"endpoint does not serve the configured model {probe.requested!r}; it "
        f"answered a completion request with model {probe.served!r} instead. "
        "Running the task would record its outcome under a model that never "
        "ran. Load or name a model the endpoint actually serves (verify with "
        "the `model` field of a completions RESPONSE, never the request), "
        "then `praxis retry` this task."
    )
