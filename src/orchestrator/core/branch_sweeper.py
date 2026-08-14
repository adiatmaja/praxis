"""Pure decision logic for identifying dead/stale git branches.

The asymmetry here is total and the code is written around it. Deleting a
branch something is still using destroys work that exists only in a container
or only on that remote ref, and no part of the system can put it back. Failing
to delete a branch that really is dead leaves one stale ref on a remote. So
every signal in this module is read in the fail-safe direction: a branch is
deleted only when it is POSITIVELY known to be finished with, and any signal
that something might still be using it wins outright.
"""

from collections.abc import Iterable


_PROTECTED_PREFIXES = ("main", "master", "release")


def _is_protected(branch: str, protected_branches: set[str]) -> bool:
    """Check whether a branch is protected by name or by configuration.

    ``_PROTECTED_PREFIXES`` is a guess about naming conventions and catches
    only the common cases. ``protected_branches`` carries facts about the
    actual repositories (their configured default branches), which is the
    signal that covers a repo whose trunk is called ``develop`` or ``trunk``.
    """
    if branch in protected_branches:
        return True
    return any(branch == p or branch.startswith(p) for p in _PROTECTED_PREFIXES)


def dead_branches(
    branches: Iterable[str],
    *,
    open_pr_branches: set[str],
    terminal_failed: set[str],
    merged_plan: set[str],
    live_branches: set[str],
    protected_branches: set[str],
) -> list[str]:
    """Return a list of branch names that are provably dead and safe to prune.

    A branch is dead only if it is in ``terminal_failed`` or ``merged_plan``
    AND nothing objects: it is not protected, bears no open PR, and is not in
    use by anything still running.

    Args:
        branches: Every branch currently on the remote.
        open_pr_branches: Branches with an open pull request.
        terminal_failed: Branches whose task or plan failed terminally.
        merged_plan: Branches of plans that finished and were merged.
        live_branches: Branches an unfinished plan or task is still using. In
            single-branch (auto-delegate) mode many tasks share one work
            branch, so one of them failing puts that SHARED branch into
            ``terminal_failed`` while siblings are still pushing to it. A
            sibling that has not opened a PR yet is invisible to
            ``open_pr_branches``, which is why this signal is separate and why
            it vetoes both terminal sets rather than only one.
        protected_branches: Branches that must never be deleted regardless of
            any other signal, e.g. each project's configured default branch.
            A plan with no plan branch runs directly on the project default,
            so without this a single failed task nominates the repository's
            trunk for deletion.

    Returns:
        The subset of ``branches`` that is safe to delete, in input order.

    Both liveness arguments are REQUIRED rather than defaulting to empty: an
    omitted signal must never be readable as "nothing is live" or "nothing is
    protected", because that reading is the one that deletes work.
    """
    dead: list[str] = []
    for b in branches:
        if _is_protected(b, protected_branches):
            continue
        if b in open_pr_branches or b in live_branches:
            continue
        if b in terminal_failed or b in merged_plan:
            dead.append(b)
    return dead
