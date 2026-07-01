"""Pure policy functions deciding whether a reviewed task may auto-merge.

These functions perform no I/O so they can be unit-tested in isolation. The
core security rule lives here: auto-merge may never target a protected branch.
"""

from __future__ import annotations

import fnmatch
from typing import Any


# Branch-name patterns always treated as protected, regardless of project
# default. Matched case-insensitively with shell-style globbing.
_PROTECTED_PATTERNS: tuple[str, ...] = ("main", "master", "release*")


def is_protected_branch(branch: str | None, default_branch: str) -> bool:
    """Return True if ``branch`` must never be auto-merged into.

    A branch is protected when it is empty/unknown, equals the project's
    configured default branch, or matches a built-in protected pattern
    (``main``, ``master``, ``release*``). Matching is case-insensitive.

    Args:
        branch: The merge-target branch name, or None if unknown.
        default_branch: The project's configured default branch.

    Returns:
        True if the branch is protected (auto-merge forbidden).
    """
    if not branch:
        return True
    candidate = branch.strip().lower()
    if candidate == default_branch.strip().lower():
        return True
    return any(
        fnmatch.fnmatch(candidate, pattern.lower()) for pattern in _PROTECTED_PATTERNS
    )


def auto_merge_eligible(project: dict[str, Any], base_branch: str | None) -> bool:
    """Return True if a reviewed task in ``project`` may auto-merge to ``base_branch``.

    Eligible only when the project opted into ``auto_merge`` AND the merge
    target is not a protected branch. An unknown base branch is treated as
    protected (fail safe).

    Args:
        project: The project row (must expose ``auto_merge`` and
            ``default_branch``).
        base_branch: The PR's merge-target branch, or None if unknown.

    Returns:
        True if auto-merge is permitted; False means the gated path applies.
    """
    if not bool(project.get("auto_merge", 0)):
        return False
    return not is_protected_branch(base_branch, str(project.get("default_branch", "")))
