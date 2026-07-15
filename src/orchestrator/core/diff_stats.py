"""Parse a unified diff and return (files_changed, loc_changed)."""

from __future__ import annotations


def diff_stats(diff: str) -> tuple[int, int]:
    """Return (number of files changed, number of content lines changed).

    Counts each `+++ ` line as one file. Every line starting with a single
    `+` or `-` (excluding the `+++ ` / `--- ` headers) increments the
    lines-of-code counter.
    """
    files = 0
    loc = 0

    for line in diff.splitlines():
        if line.startswith("+++ "):
            files += 1
            continue
        if line.startswith("--- "):
            continue
        if line.startswith(("+", "-")):
            loc += 1

    return (files, loc)
