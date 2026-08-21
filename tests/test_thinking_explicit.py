"""Structural gate: no LM Studio payload may stay silent about thinking.

What an absent ``reasoning_effort`` means is decided by the server, and it is
not a stable API: on the configured endpoint it meant MAXIMUM effort on
2026-08-15 and ZERO on 2026-08-21, with nothing in Praxis changing between.
Praxis hand-builds OpenAI-compatible payloads, so a new call site that simply
says nothing inherits whichever default is current, with no error and no
failing test, and silently changes meaning the next time it flips.

This gate is deliberately STRUCTURAL rather than a measurement, which is what
lets it survive that flipping: it asserts only that a level is stated.

This asserts the invariant over EVERY payload rather than the two that were
fixed by hand. It deliberately does not assert WHICH level a payload uses: the
level is a tuning decision, silence is the bug.

See ``orchestrator.core.thinking`` for the measurements behind this.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest


SRC = Path(__file__).resolve().parents[1] / "src" / "orchestrator"

# Every hand-built chat payload is anchored by this URL suffix.
_ENDPOINT = re.compile(r"chat/completions")
# How far past the URL the payload literal can reasonably extend.
_WINDOW = 40
_STATES_THINKING = re.compile(r"reasoning_effort|effort_param")


def _payload_sites() -> list[tuple[Path, int, str]]:
    """Return (file, lineno, following-source-window) per chat payload site.

    Comment lines are STRIPPED from the window. Every one of these call sites
    carries a comment explaining the thinking default, and matching against
    prose would let a payload be silent while its own warning comment satisfied
    the gate -- verified: without this, deleting the real parameter still
    passed.
    """
    sites: list[tuple[Path, int, str]] = []
    for path in sorted(SRC.rglob("*.py")):
        lines = path.read_text(encoding="utf-8").splitlines()
        for i, line in enumerate(lines):
            if _ENDPOINT.search(line):
                window = [
                    ln
                    for ln in lines[i : i + _WINDOW]
                    if not ln.lstrip().startswith("#")
                ]
                sites.append((path, i + 1, "\n".join(window)))
    return sites


def test_every_lm_studio_payload_states_reasoning_effort() -> None:
    sites = _payload_sites()

    # Guard the guard: a detector that silently stops matching would pass
    # forever while checking nothing, which reads as coverage but is worse
    # than no test at all.
    assert len(sites) >= 2, (
        f"expected at least 2 chat/completions payload sites, found {len(sites)}; "
        "the detector has drifted and this gate is no longer checking anything"
    )

    offenders = [
        f"{path.relative_to(SRC)}:{lineno}"
        for path, lineno, window in sites
        if not _STATES_THINKING.search(window)
    ]
    assert offenders == [], (
        "these LM Studio payloads say nothing about reasoning_effort, so their "
        "thinking level is whatever the current LM Studio build defaults to, "
        "which has inverted before: " + ", ".join(offenders)
    )


@pytest.mark.parametrize(
    ("effort", "expected"),
    [
        (None, "none"),
        ("", "none"),
        ("high", "high"),
        ("medium", "medium"),
    ],
)
def test_effort_param_always_populates_the_key(
    effort: str | None, expected: str
) -> None:
    """Both branches are explicit; the off case never degrades to ``{}``."""
    from orchestrator.core.thinking import effort_param

    assert effort_param(effort) == {"reasoning_effort": expected}
