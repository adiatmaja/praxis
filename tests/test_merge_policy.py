"""Unit tests for merge-policy pure functions."""

from __future__ import annotations

import pytest

from orchestrator.core.merge_policy import auto_merge_eligible, is_protected_branch


@pytest.mark.parametrize(
    ("branch", "default_branch", "expected"),
    [
        ("main", "main", True),
        ("MAIN", "main", True),
        ("master", "develop", True),
        ("release", "main", True),
        ("release/2.0", "main", True),
        ("release-hotfix", "main", True),
        ("develop", "develop", True),  # matches project default
        ("plan/mcp-foo", "main", False),
        ("agent/add-thing", "main", False),
        ("", "main", True),
        (None, "main", True),
    ],
)
def test_is_protected_branch(
    branch: str | None, default_branch: str, expected: bool
) -> None:
    assert is_protected_branch(branch, default_branch) is expected


def test_auto_merge_eligible_off_by_default() -> None:
    project = {"auto_merge": 0, "default_branch": "main"}
    assert auto_merge_eligible(project, "plan/mcp-foo") is False


def test_auto_merge_eligible_on_for_nonprotected_base() -> None:
    project = {"auto_merge": 1, "default_branch": "main"}
    assert auto_merge_eligible(project, "plan/mcp-foo") is True


def test_auto_merge_eligible_blocked_for_protected_base() -> None:
    project = {"auto_merge": 1, "default_branch": "main"}
    assert auto_merge_eligible(project, "main") is False


def test_auto_merge_eligible_none_base_is_protected() -> None:
    # Unknown base is treated as protected (fail safe).
    project = {"auto_merge": 1, "default_branch": "main"}
    assert auto_merge_eligible(project, None) is False
