"""Redact secrets and cap the size of caller-supplied worker context.

The MCP caller (e.g. Claude Code) is asked to pass only task-relevant memory,
but this is the one untrusted-for-secrets channel that gets written into the
agent's repo clone. We never trust curation alone: redact obvious secret shapes
and hard-cap length before the text reaches the worker container.
"""

from __future__ import annotations

import re


_DEFAULT_MAX_CHARS = 12_000

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


def scrub_context(text: str | None, max_chars: int = _DEFAULT_MAX_CHARS) -> str | None:
    """Return ``text`` with secrets redacted and length capped, or None if empty.

    Args:
        text: Raw caller-supplied context, or None.
        max_chars: Hard cap on output length (excluding the truncation notice).

    Returns:
        Scrubbed text, or None when the input is None/blank.
    """
    if text is None or not text.strip():
        return None

    scrubbed = _PRIVATE_KEY.sub("[REDACTED PRIVATE KEY]", text)
    scrubbed = _TOKEN_SHAPES.sub("[REDACTED]", scrubbed)
    scrubbed = _ENV_ASSIGN.sub(lambda m: f"{m.group(1)}=[REDACTED]", scrubbed)

    if len(scrubbed) > max_chars:
        scrubbed = scrubbed[:max_chars] + "\n\n[context truncated by Praxis]"
    return scrubbed
