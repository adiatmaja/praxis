"""Redact secrets and cap the size of caller-supplied worker context.

The MCP caller (e.g. Claude Code) is asked to pass only task-relevant memory,
but this is the one untrusted-for-secrets channel that gets written into the
agent's repo clone. We never trust curation alone: redact obvious secret shapes
and hard-cap length before the text reaches the worker container.

The cap used to be a flat 12 000 characters for every worker, regardless of the
model's real context window. That truncated a legitimately-sized 14 KB spec on
a cloud model whose window is on the order of a million tokens, glueing a
truncation notice into the middle of the leaf contract or the caller-supplied
context of a task that would have fit easily. It is a *different* defect than
the one ``core/context_window.py`` fixes (that one is a silent 8192
fabrication); this one is visible - the notice says so - but it is still a bad
default, not a lie, so the fix here is to size the cap, not to remove it.

``resolve_scrub_cap`` is the single place that turns a resolved context window
(``core/context_window.resolve_context_window``, or a caller's own equivalent
resolution such as ``effective_settings.capability_profile().context_window``)
into a character cap. It reuses ``token_budget.worker_budget`` (the same
reserve-fraction policy every other worker-context seam already applies) and
``token_budget._CHARS_PER_TOKEN`` (the same chars/token assumption the budget
gate already assumes) rather than inventing a second ratio. A window at or
below the shipped local-model default (8192, see
``effective_settings.capability_profile``'s docstring) keeps today's flat
12 000-character cap exactly, so a stock LM Studio install is not silently
resized by this change; only a window meaningfully larger than that earns a
larger cap.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from orchestrator.core.token_budget import (
    _CHARS_PER_TOKEN,  # the SAME tokens<->chars ratio the budget gate uses
    worker_budget,
)


_DEFAULT_MAX_CHARS = 12_000

#: The shipped ``capability.default.context_window`` (effective_settings.py:
#: "8192 as shipped, sized for a local open-weight worker"), and the same
#: figure ``core/context_window.py`` names as the fallback every cloud
#: dispatch used to collapse onto before that module existed. Any resolved
#: window at or below this keeps ``_DEFAULT_MAX_CHARS`` byte-for-byte: only a
#: window meaningfully larger than a typical local model's earns a larger cap,
#: so an untouched LM Studio install is not silently re-sized.
_TYPICAL_LOCAL_MODEL_WINDOW_TOKENS = 8192

# KEY=secret / KEY: secret on a single line (value looks secret-ish: long/opaque).
_ENV_ASSIGN = re.compile(
    r"(?m)^\s*([A-Z0-9_]{2,})\s*[=:]\s*(?!https?://|/[A-Za-z]|[A-Za-z]:\\)\S{8,}\s*$",
)
# Common opaque token shapes.
_TOKEN_SHAPES = re.compile(
    r"(?x)"
    r"(ghp_[A-Za-z0-9]{20,})"  # GitHub PAT
    r"|(github_pat_[A-Za-z0-9_]{20,})"
    r"|(sk-[A-Za-z0-9-]{16,})"  # OpenAI/Anthropic-style (allows hyphens)
    r"|(AKIA[0-9A-Z]{16})"  # AWS access key id
    r"|(xox[baprs]-[A-Za-z0-9-]{10,})"  # Slack
    r"|(eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,})"  # JWT
)
_PRIVATE_KEY = re.compile(
    r"-----BEGIN [A-Z ]{0,20}PRIVATE KEY-----.*?-----END [A-Z ]{0,20}PRIVATE KEY-----",
    re.DOTALL,
)


@dataclass(frozen=True)
class ScrubCap:
    """A resolved character cap for one worker-context field, and why.

    ``reason`` is folded into the truncation notice when the cap actually
    binds, so an operator reading a truncated task sees the cause (a small or
    unresolved window) rather than a bare, unexplained number.
    """

    max_chars: int
    reason: str


def resolve_scrub_cap(context_window: int | None) -> ScrubCap:
    """Size the truncation cap to the worker's actual context window.

    Args:
        context_window: The resolved window in tokens, or None when nobody
            could establish one (see ``core/context_window.resolve_context_window``,
            whose ``tokens is None`` means exactly this - never substitute a
            guess for it here either).

    Returns:
        The cap to pass to :func:`scrub_context`, and a human-readable reason.
    """
    if context_window is None:
        return ScrubCap(
            _DEFAULT_MAX_CHARS,
            "the worker's context window is unknown, so Praxis used its "
            "conservative default; declare one under `context_windows` in "
            "the settings file, or set the project's context_window, to "
            "raise this cap",
        )
    if context_window <= _TYPICAL_LOCAL_MODEL_WINDOW_TOKENS:
        return ScrubCap(
            _DEFAULT_MAX_CHARS,
            f"sized to the worker's {context_window}-token context window",
        )
    # The SAME injected-context budget every other worker-context seam already
    # computes (the window minus the reserve held back for the agent's own
    # reasoning), converted to characters at the SAME ratio the budget gate
    # assumes. ``max`` only matters for a window just above the threshold
    # where rounding could otherwise dip under the default; in practice it
    # never fires once ``context_window`` clears the threshold above.
    computed = worker_budget(context_window) * _CHARS_PER_TOKEN
    return ScrubCap(
        max(computed, _DEFAULT_MAX_CHARS),
        f"sized to the worker's {context_window}-token context window",
    )


def scrub_context(
    text: str | None,
    max_chars: int = _DEFAULT_MAX_CHARS,
    *,
    cap_reason: str | None = None,
) -> str | None:
    """Return ``text`` with secrets redacted and length capped, or None if empty.

    Args:
        text: Raw caller-supplied context, or None.
        max_chars: Hard cap on output length (excluding the truncation notice).
            Callers that know the worker's resolved context window should pass
            ``resolve_scrub_cap(context_window).max_chars`` rather than the
            unsized default.
        cap_reason: Human-readable reason for ``max_chars``, folded into the
            truncation notice so it names the cause, not just the number.
            Pass ``resolve_scrub_cap(context_window).reason``. Defaults to a
            generic phrase when omitted.

    Returns:
        Scrubbed text, or None when the input is None/blank.
    """
    if text is None or not text.strip():
        return None

    scrubbed = _PRIVATE_KEY.sub("[REDACTED PRIVATE KEY]", text)
    scrubbed = _TOKEN_SHAPES.sub("[REDACTED]", scrubbed)
    scrubbed = _ENV_ASSIGN.sub(lambda m: f"{m.group(1)}=[REDACTED]", scrubbed)

    if len(scrubbed) > max_chars:
        reason = cap_reason or "Praxis's default cap for worker context"
        scrubbed = (
            scrubbed[:max_chars]
            + f"\n\n[context truncated by Praxis at {max_chars} characters: "
            f"{reason}. Shorten this text or split the task.]"
        )
    return scrubbed
