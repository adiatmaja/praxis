"""Flag PRs that delete large chunks from existing files and detect provider errors.

A weak worker can silently truncate a config/source file. The reviewer brain
should catch this, but this deterministic guard is a cheap hard backstop.

The guard is ADVISORY when the brain already returned PASS: in that case we
surface the flagged files as extra context for a second targeted review pass
rather than unconditionally flipping the verdict. A hard flip only happens when
the brain itself returned FAIL (belt-and-suspenders) or when the caller
explicitly requests hard-block mode.

Delete-and-replace patterns (net additions >= raw deletions) are exempt
because a refactor that moves code from N files into M new files is not
destructive, even when individual file deletion counts are high.
"""

from __future__ import annotations

import re


_OLD = re.compile(r"^--- a/(.+)$")


def destructive_deletions(diff: str, threshold: int = 40) -> list[str]:
    """Return paths with a NET loss above ``threshold`` lines.

    A file is only flagged when its raw deletion count exceeds ``threshold``
    AND the entire diff has more deletions than additions (i.e. the PR is a
    net shrink, not a delete-and-replace refactor). Files introduced from
    ``/dev/null`` are never flagged.

    Args:
        diff: Unified diff text.
        threshold: Minimum per-file raw deletion count to consider flagging.

    Returns:
        List of file paths that are genuinely destructive.
    """
    removals: dict[str, int] = {}
    additions: dict[str, int] = {}
    current: str | None = None
    total_additions = 0
    total_deletions = 0

    for line in diff.splitlines():
        m = _OLD.match(line)
        if m:
            current = m.group(1)
            removals.setdefault(current, 0)
            additions.setdefault(current, 0)
            continue
        if line.startswith("--- /dev/null"):
            current = None
            continue
        if current and line.startswith("-") and not line.startswith("---"):
            removals[current] += 1
            total_deletions += 1
        elif current and line.startswith("+") and not line.startswith("+++"):
            additions[current] += 1
            total_additions += 1

    # If the diff as a whole adds at least as many lines as it removes, treat
    # it as a delete-and-replace refactor: no single file counts as destructive.
    if total_additions >= total_deletions:
        return []

    flagged = []
    for path, n in removals.items():
        if n <= threshold:
            continue
        # Per-file: if additions roughly match deletions, it is a rewrite, not
        # a truncation (allow 30% slack so minor consolidations still pass).
        file_adds = additions.get(path, 0)
        if file_adds >= n * 0.7:
            continue
        flagged.append(path)
    return flagged
