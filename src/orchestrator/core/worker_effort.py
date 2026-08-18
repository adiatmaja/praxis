"""Resolve the thinking-effort signal sent to an implementation worker.

Why this file exists
--------------------
:mod:`orchestrator.core.thinking` encodes the rule for BRAIN payloads: never
express "no thinking" as an absent key, because an omitted ``reasoning_effort``
means MAXIMUM effort on qwen3.8, not off. Workers had no equivalent. OpenCode's
provider config carried no effort at all while agy took its effort baked into
the Gemini model string, so the same task ran under two different and
undeclared thinking regimes depending on which harness picked it up.

This module is the worker-side half of that rule. It reads the harness's
declared ``effort_channel`` (see :mod:`orchestrator.core.harnesses`) and answers
one question: what, if anything, should the spawn environment state?

Returning ``None`` is meaningful and is NOT the same as "off". It means the
harness has no knob to turn, so praxis must not pretend to have set one.
"""

from __future__ import annotations

import logging

from orchestrator.core.harnesses import REGISTRY


logger = logging.getLogger(__name__)

#: Effort levels accepted by the OpenAI-compatible providers praxis drives.
VALID_EFFORTS: frozenset[str] = frozenset({"none", "low", "medium", "high"})

#: Used when the operator expressed no preference. "none" is the only level
#: measured to yield zero reasoning tokens on the configured endpoint; see the
#: measurement table in :mod:`orchestrator.core.thinking`.
DEFAULT_WORKER_EFFORT = "none"


def resolve_worker_effort(harness_id: str, configured: str | None) -> str | None:
    """Return the effort value to place in the spawn environment.

    Args:
        harness_id: The harness that will run the task.
        configured: The operator's requested level, or None for no preference.

    Returns:
        An explicit level for harnesses praxis drives through a request option,
        or None when the harness has no separate effort knob.

    Raises:
        ValueError: If ``configured`` is not a supported level.
    """
    if configured is not None and configured not in VALID_EFFORTS:
        message = (
            f"unsupported reasoning effort {configured!r}; "
            f"expected one of {sorted(VALID_EFFORTS)}"
        )
        raise ValueError(message)

    spec = REGISTRY.get(harness_id)
    if spec is None:
        logger.warning(
            "Unknown harness %s; sending no effort signal to the worker",
            harness_id,
        )
        return None

    if spec.effort_channel != "request_option":
        return None

    return configured or DEFAULT_WORKER_EFFORT
