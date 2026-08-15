"""SSoT for how praxis turns local-model thinking off.

Why this file exists
--------------------
Praxis builds OpenAI-compatible payloads by hand at two call sites
(:mod:`orchestrator.core.llm_router` and :mod:`orchestrator.core.plan_derive`).
Neither used to mention ``reasoning_effort``, which was harmless while the
local seat ran qwen3.6-27b: that build did not reason by default, so an absent
key meant "no thinking".

qwen3.8-27b inverts that default. An ABSENT ``reasoning_effort`` means MAXIMUM
effort, not off, so both payloads silently changed behaviour on the model swap
with no error, no warning and no failing test.

Measured against the configured endpoint on 2026-08-15 (qwen3.8-27b via
LM Studio at pcllm.sigmasolusi.com, three samples per level, using praxis's own
payload shape: ``temperature: 0`` and no ``max_tokens``). Temperature 0 makes
the endpoint deterministic here, so within-level spread was 0 for every row:

    reasoning_effort   reasoning_tokens
    none                     0
    low                    317
    medium                 389
    high                   354
    (omitted)              354   <-- identical to `high`: omission is NOT off

The blast radius is not theoretical. Re-running ``plan_derive``'s exact
``response_format: json_schema`` payload with the key omitted returned
``finish_reason: stop`` and EMPTY content, which is unparseable JSON: the
promote-plan.md path would raise ``JSONDecodeError`` out of ``derive_opus_plan``.
The same payload at ``reasoning_effort: none`` returned a clean 8-task object.

Note that ``low`` is NOT an off switch (317 tokens). ``none`` is the only level
measured to reach zero, which is why it is the constant below.

The rule this encodes: NEVER express "no thinking" as an absent key. Any new
LM Studio payload must state ``reasoning_effort`` explicitly -- a payload that
says nothing about thinking is a payload asking for maximum thinking.
``tests/test_thinking_explicit.py`` gates that invariant.
"""

from __future__ import annotations


#: The only level measured to yield zero reasoning tokens on this endpoint.
NO_THINK_EFFORT = "none"


def effort_param(effort: str | None) -> dict[str, str]:
    """Build the thinking half of an LM Studio payload, explicitly.

    Both branches are explicit on purpose. Returning ``{}`` for the "off" case
    would reintroduce the exact silence this module exists to prevent.

    Args:
        effort: The caller's requested reasoning level, or None when the
            caller has no opinion. None means OFF, not "let the server decide".

    Returns:
        A dict carrying a ``reasoning_effort`` key, always populated.
    """
    return {"reasoning_effort": effort or NO_THINK_EFFORT}
