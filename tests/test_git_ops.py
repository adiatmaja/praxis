"""Git operation tests."""
# ruff: noqa: S101

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from orchestrator.core.git_ops import GitOps


@pytest.mark.unit
@patch("orchestrator.core.git_ops.GitOps._run_command")
async def test_clone_repo(mock_run: AsyncMock) -> None:
    mock_run.return_value = (0, "", "")
    git = GitOps(github_token="ghp_test")

    await git.clone_repo("https://github.com/user/repo.git", "/tmp/workspace")

    cmd = mock_run.call_args.args[0]
    assert cmd == ["git", "clone", "https://github.com/user/repo.git", "/tmp/workspace"]


@pytest.mark.unit
@patch("orchestrator.core.git_ops.GitOps._run_command")
async def test_create_branch(mock_run: AsyncMock) -> None:
    mock_run.return_value = (0, "", "")
    git = GitOps(github_token="ghp_test")

    await git.create_branch("/tmp/workspace", "plan/2026-06-01-auth", "main")

    assert mock_run.call_count == 3


@pytest.mark.unit
@patch("orchestrator.core.git_ops.GitOps._run_command")
async def test_push_branch(mock_run: AsyncMock) -> None:
    mock_run.return_value = (0, "", "")
    git = GitOps(github_token="ghp_test")

    await git.push_branch("/tmp/workspace", "agent/login")

    assert "push" in mock_run.call_args.args[0]


@pytest.mark.unit
@patch("orchestrator.core.git_ops.GitOps._run_command")
async def test_create_pr(mock_run: AsyncMock) -> None:
    mock_run.return_value = (0, "https://github.com/user/repo/pull/1\n", "")
    git = GitOps(github_token="ghp_test")

    pr_url = await git.create_pr(
        "/tmp/workspace",
        title="feat: login page",
        body="Implements login",
        base="plan/2026-06-01-auth",
        head="agent/login",
    )

    assert pr_url == "https://github.com/user/repo/pull/1"


@pytest.mark.unit
@patch("orchestrator.core.git_ops.GitOps._run_command")
async def test_merge_pr_squash(mock_run: AsyncMock) -> None:
    mock_run.return_value = (0, "", "")
    git = GitOps(github_token="ghp_test")

    await git.merge_pr("/tmp/workspace", 1)

    cmd = mock_run.call_args.args[0]
    assert "--squash" in cmd
    assert "--delete-branch" in cmd


@pytest.mark.unit
@patch("orchestrator.core.git_ops.GitOps._run_command")
async def test_comment_on_pr(mock_run: AsyncMock) -> None:
    mock_run.return_value = (0, "", "")
    git = GitOps(github_token="ghp_test")

    await git.comment_on_pr("/tmp/workspace", 1, "Needs fixes")

    assert "comment" in mock_run.call_args.args[0]


@pytest.mark.unit
@patch("orchestrator.core.git_ops.GitOps._run_command")
async def test_get_pr_diff(mock_run: AsyncMock) -> None:
    mock_run.return_value = (0, "diff --git a/file.py ...", "")
    git = GitOps(github_token="ghp_test")

    diff = await git.get_pr_diff("/tmp/workspace", 1)

    assert "diff --git" in diff


@pytest.mark.unit
@patch("orchestrator.core.git_ops.GitOps._run_command")
async def test_get_changed_files(mock_run: AsyncMock) -> None:
    mock_run.return_value = (0, "a.py\nb.py\n", "")
    git = GitOps(github_token="ghp_test")

    files = await git.get_changed_files("/tmp/workspace", "main", "agent/login")

    assert files == ["a.py", "b.py"]


@pytest.mark.unit
@patch("orchestrator.core.git_ops.GitOps._run_command")
async def test_command_failure_raises(mock_run: AsyncMock) -> None:
    mock_run.return_value = (1, "", "fatal: not a git repository")
    git = GitOps(github_token="ghp_test")

    with pytest.raises(RuntimeError, match="Git command failed"):
        await git.clone_repo("https://github.com/user/repo.git", "/tmp/workspace")


@pytest.mark.unit
async def test_extract_pr_number() -> None:
    git = GitOps(github_token="ghp_test")

    assert await git.extract_pr_number("https://github.com/user/repo/pull/42") == 42
