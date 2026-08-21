"""SSoT for how praxis states local-model thinking, explicitly, every time.

Why this file exists
--------------------
Praxis builds OpenAI-compatible payloads by hand at two call sites
(:mod:`orchestrator.core.llm_router` and :mod:`orchestrator.core.plan_derive`).
Neither used to mention ``reasoning_effort``, and what the server does with a
silent payload has now changed TWICE underneath us:

- qwen3.6-27b did not reason by default, so an absent key meant "no thinking".
- qwen3.8-27b inverted that: on 2026-08-15 an absent key meant MAXIMUM effort.
- On 2026-08-21 the same endpoint inverted it BACK: an absent key now means
  zero, byte-identical to ``none``.

That history, not any one of those defaults, is the reason this module exists.
The server's default is not a stable API. A payload that says nothing about
thinking is a payload whose behaviour is decided by whichever LM Studio build
happens to be running, and it will change again without an error, a warning or
a failing test.

Measured against the configured endpoint (qwen3.8-27b via LM Studio at
pcllm.sigmasolusi.com) using praxis's own payload shape, ``temperature: 0`` and
no ``max_tokens``. Temperature 0 makes the endpoint deterministic here:

    reasoning_effort   2026-08-15    2026-08-21
    none                        0             0
    low                       317           188
    medium                    389           139
    high                      354           188
    (omitted)                 354             0

Two things to read off that table before trusting either column. Omission
flipped from "identical to ``high``" to "identical to ``none``". And the levels
are NOT monotonic in either column: on 2026-08-21 ``medium`` thinks MORE than
``high``, and ``low`` and ``high`` are indistinguishable. Do not reason about
these labels as an ordered scale; measure them.

The blast radius is real, and it also moved. Re-running ``plan_derive``'s exact
``response_format: json_schema`` payload:

- 2026-08-15: the key OMITTED returned ``finish_reason: stop`` and EMPTY
  content, unparseable, raising ``JSONDecodeError`` out of ``derive_opus_plan``.
- 2026-08-21: omitted and ``none`` both return a clean task list, while
  ``low``, ``medium`` and ``high`` ALL return empty content and raise.

So the durable fact underneath both measurements is not about omission at all:
**structured json_schema extraction breaks whenever the model thinks**. That is
why ``plan_derive`` pins ``effort_param(None)`` rather than inheriting anything,
and why raising the effort on that call site to "improve" it would break it.

``low`` has never been an off switch (317, then 188). ``none`` is the level to
name when zero is wanted, because it has measured zero on every probe, while
omission has measured both zero and maximum.

The rule this encodes, unchanged across both inversions: NEVER express a
thinking level as an absent key. Any new LM Studio payload must state
``reasoning_effort`` explicitly. ``tests/test_thinking_explicit.py`` gates that
invariant, and it is a structural gate, so it survives the default flipping
again.
"""

from __future__ import annotations


#: The level to NAME when zero thinking is wanted. It has measured zero on
#: every probe; omission has measured both zero and maximum, so it is not a
#: synonym even on the days the numbers agree.
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
