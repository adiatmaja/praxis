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
    )

    assert result == ["agent/failed", "plan/merged"]


def test_never_touches_unknown_branches() -> None:
    branches = ["feat/mystery", "fix/something"]
    result = dead_branches(
        branches,
        open_pr_branches=set(),
        terminal_failed=set(),
        merged_plan=set(),
    )

    assert result == []
