"""Flag PRs that delete large chunks from existing files.

A weak worker can silently truncate a config/source file. The reviewer brain
should catch this, but this deterministic guard is a cheap hard backstop.
"""

from __future__ import annotations

import re


_OLD = re.compile(r"^--- a/(.+)$")


def destructive_deletions(diff: str, threshold: int = 40) -> list[str]:
    """Return paths from which more than ``threshold`` lines were removed.

    Only counts files that existed before (``--- a/...``, not ``/dev/null``).
    """
    removals: dict[str, int] = {}
    current: str | None = None
    for line in diff.splitlines():
        m = _OLD.match(line)
        if m:
            current = m.group(1)
            removals.setdefault(current, 0)
            continue
        if line.startswith("--- /dev/null"):
            current = None
            continue
        if current and line.startswith("-") and not line.startswith("---"):
            removals[current] += 1
    return [path for path, n in removals.items() if n > threshold]
