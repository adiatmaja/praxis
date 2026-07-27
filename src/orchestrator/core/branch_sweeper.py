"""Pure decision logic for identifying dead/stale git branches."""

from collections.abc import Iterable


_PROTECTED_PREFIXES = ("main", "master", "release")


def _is_protected(branch: str) -> bool:
    """Check if a branch name matches any protected prefix."""
    return any(branch == p or branch.startswith(p) for p in _PROTECTED_PREFIXES)


def dead_branches(
    branches: Iterable[str],
    *,
    open_pr_branches: set[str],
    terminal_failed: set[str],
    merged_plan: set[str],
) -> list[str]:
    """Return a list of branch names that are provably dead and safe to prune.

    A branch is dead if it is in terminal_failed or merged_plan, and is NOT protected
    or bearing an open PR.
    """
    dead: list[str] = []
    for b in branches:
        if _is_protected(b) or b in open_pr_branches:
            continue
        if b in terminal_failed or b in merged_plan:
            dead.append(b)
    return dead
