import pytest

from orchestrator.core.branch_sweeper import dead_branches


def test_keeps_protected_and_live() -> None:
    branches = [
        "main",
        "master",
        "release-1.0",
        "agent/live",
        "agent/failed",
        "plan/merged",
    ]
    open_pr_branches = {"agent/live"}
    terminal_failed = {"agent/failed"}
    merged_plan = {"plan/merged"}

    result = dead_branches(
        branches,
        open_pr_branches=open_pr_branches,
        terminal_failed=terminal_failed,
        merged_plan=merged_plan,
        live_branches=set(),
        protected_branches=set(),
        carrying_merged_work=set(),
    )

    assert result == ["agent/failed", "plan/merged"]


def test_never_touches_unknown_branches() -> None:
    branches = ["feat/mystery", "fix/something"]
    result = dead_branches(
        branches,
        open_pr_branches=set(),
        terminal_failed=set(),
        merged_plan=set(),
        live_branches=set(),
        protected_branches=set(),
        carrying_merged_work=set(),
    )

    assert result == []


def test_a_branch_something_is_still_using_is_never_dead() -> None:
    """Scenario A, at the decision level.

    In single-branch mode a terminally failed task and a still-running sibling
    share one work branch, so the branch appears in ``terminal_failed`` while a
    container is still pushing to it. A live signal must veto a terminal one.
    """
    result = dead_branches(
        ["daily/dev-session"],
        open_pr_branches=set(),
        terminal_failed={"daily/dev-session"},
        merged_plan=set(),
        live_branches={"daily/dev-session"},
        protected_branches=set(),
        carrying_merged_work=set(),
    )

    assert result == []


def test_a_live_signal_beats_a_merged_plan_signal_too() -> None:
    """Liveness vetoes ``merged_plan`` as well, not just ``terminal_failed``.

    A plan can complete while a later task is still parked on the same shared
    branch; the branch is not free just because one plan finished with it.
    """
    result = dead_branches(
        ["daily/dev-session"],
        open_pr_branches=set(),
        terminal_failed=set(),
        merged_plan={"daily/dev-session"},
        live_branches={"daily/dev-session"},
        protected_branches=set(),
        carrying_merged_work=set(),
    )

    assert result == []


def test_the_repositorys_real_default_branch_is_protected() -> None:
    """Scenario B, at the decision level.

    The hardcoded main/master/release* prefixes are a guess about naming. A
    repository whose default branch is ``develop`` gets that branch nominated
    after one failed task, and nothing else in the ledger objects.
    """
    result = dead_branches(
        ["develop", "agent/failed"],
        open_pr_branches=set(),
        terminal_failed={"develop", "agent/failed"},
        merged_plan=set(),
        live_branches=set(),
        protected_branches={"develop"},
        carrying_merged_work=set(),
    )

    assert result == ["agent/failed"]


def test_a_genuinely_dead_agent_branch_is_still_reclaimed() -> None:
    """The guards must not be bought by disabling the sweeper.

    Nothing live, nothing protected, no open PR: this is precisely the branch
    the sweeper exists to reclaim, and it must still go.
    """
    result = dead_branches(
        ["main", "develop", "agent/genuinely-dead", "daily/dev-session"],
        open_pr_branches=set(),
        terminal_failed={"agent/genuinely-dead"},
        merged_plan=set(),
        live_branches={"daily/dev-session"},
        protected_branches={"develop"},
        carrying_merged_work=set(),
    )

    assert result == ["agent/genuinely-dead"]


def test_liveness_and_protection_are_required_arguments() -> None:
    """A caller cannot omit them and silently get an unguarded sweep.

    Fail-safe means the absence of a liveness signal is never readable as
    "nothing is live".
    """
    with pytest.raises(TypeError, match="live_branches"):
        dead_branches(  # type: ignore[call-arg]
            ["agent/failed"],
            open_pr_branches=set(),
            terminal_failed={"agent/failed"},
            merged_plan=set(),
            protected_branches=set(),
        )
