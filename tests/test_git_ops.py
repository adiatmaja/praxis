"""Git operation tests."""
# ruff: noqa: S101

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from orchestrator.core.git_ops import (
    GitOps,
    clone_with_token,
    commit_and_push,
)


@pytest.mark.unit
@patch("orchestrator.core.git_ops.subprocess.run")
def test_clone_with_token_keeps_token_in_env_not_url(mock_run: object) -> None:
    clone_with_token("https://github.com/u/r", "/tmp/ws", "tok123", depth=20)
    call = mock_run.call_args  # type: ignore[attr-defined]
    argv = call.args[0]
    # Clean URL in argv, token never embedded
    assert "https://github.com/u/r" in argv
    assert not any("tok123" in part for part in argv)
    assert "--depth" in argv
    assert "20" in argv
    # Token supplied via env, and a credential helper is configured
    assert call.kwargs["env"]["GH_TOKEN"] == "tok123"
    assert "credential.helper=" in argv


@pytest.mark.unit
@patch("orchestrator.core.git_ops.subprocess.run")
def test_commit_and_push_stages_commits_pushes(mock_run: object) -> None:
    commit_and_push("/tmp/ws", "tok123", "msg", paths=["a.md"])
    cmds = [c.args[0] for c in mock_run.call_args_list]  # type: ignore[attr-defined]
    assert any("add" in c and "a.md" in c for c in cmds)
    assert any("commit" in c for c in cmds)
    push = next(c for c in cmds if "push" in c)
    assert not any("tok123" in part for part in push)


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
async def test_get_pr_diff_targets_repo(mock_run: AsyncMock) -> None:
    mock_run.return_value = (0, "diff", "")
    git = GitOps(github_token="ghp_test")

    await git.get_pr_diff("/tmp/workspace", 2, repo="owner/name")

    cmd = mock_run.call_args.args[0]
    assert "--repo" in cmd
    assert "owner/name" in cmd


@pytest.mark.unit
@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (
            "https://github.com/adiatmaja/openclaw-telegram/pull/2",
            "adiatmaja/openclaw-telegram",
        ),
        ("https://github.com/u/a", "u/a"),
        ("https://github.com/u/a.git", "u/a"),
        ("git@github.com:u/a.git", "u/a"),
        ("https://example.com/u/a", None),
    ],
)
def test_repo_slug(url: str, expected: str | None) -> None:
    assert GitOps.repo_slug(url) == expected


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


# ---------------------------------------------------------------------------
# remote_branch_exists
# ---------------------------------------------------------------------------


@pytest.mark.unit
@patch("orchestrator.core.git_ops.GitOps._run_command")
async def test_remote_branch_exists_returns_true_when_ref_present(
    mock_run: AsyncMock,
) -> None:
    mock_run.return_value = (
        0,
        "abc123\trefs/heads/my-branch",
        "",
    )
    git = GitOps(github_token="ghp_test")
    result = await git.remote_branch_exists("https://github.com/u/r", "my-branch")
    assert result is True
    cmd = mock_run.call_args.args[0]
    assert "ls-remote" in cmd
    assert "--heads" in cmd
    assert "my-branch" in cmd


@pytest.mark.unit
@patch("orchestrator.core.git_ops.GitOps._run_command")
async def test_remote_branch_exists_returns_false_when_empty_stdout(
    mock_run: AsyncMock,
) -> None:
    mock_run.return_value = (0, "", "")
    git = GitOps(github_token="ghp_test")
    result = await git.remote_branch_exists("https://github.com/u/r", "no-such")
    assert result is False


@pytest.mark.unit
@patch("orchestrator.core.git_ops.GitOps._run_command")
async def test_remote_branch_exists_raises_on_nonzero_exit(
    mock_run: AsyncMock,
) -> None:
    mock_run.return_value = (128, "", "fatal: not found")
    git = GitOps(github_token="ghp_test")
    with pytest.raises(RuntimeError, match="git ls-remote failed"):
        await git.remote_branch_exists("https://github.com/u/r", "main")


@pytest.mark.unit
@patch("orchestrator.core.git_ops.GitOps._run_command")
async def test_remote_branch_exists_no_partial_match(
    mock_run: AsyncMock,
) -> None:
    # The output contains a ref for a *different* branch — must not match.
    mock_run.return_value = (
        0,
        "abc123\trefs/heads/my-branch-extra",
        "",
    )
    git = GitOps(github_token="ghp_test")
    result = await git.remote_branch_exists("https://github.com/u/r", "my-branch")
    assert result is False


# ---------------------------------------------------------------------------
# remote_file_exists
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_remote_file_exists_returns_true_on_200() -> None:
    mock_response = MagicMock()
    mock_response.status_code = 200

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.get = AsyncMock(return_value=mock_response)

    git = GitOps(github_token="ghp_real")
    with patch("orchestrator.core.git_ops.httpx.AsyncClient", return_value=mock_client):
        result = await git.remote_file_exists("u/r", "main", "docs/plan.md")

    assert result is True
    call_kwargs = mock_client.get.call_args
    # Check auth header was sent
    assert "Authorization" in call_kwargs.kwargs["headers"]
    assert "ghp_real" in call_kwargs.kwargs["headers"]["Authorization"]


@pytest.mark.unit
async def test_remote_file_exists_returns_false_on_404() -> None:
    mock_response = MagicMock()
    mock_response.status_code = 404

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.get = AsyncMock(return_value=mock_response)

    git = GitOps(github_token="ghp_real")
    with patch("orchestrator.core.git_ops.httpx.AsyncClient", return_value=mock_client):
        result = await git.remote_file_exists("u/r", "main", "missing.md")

    assert result is False


@pytest.mark.unit
async def test_remote_file_exists_raises_on_unexpected_status() -> None:
    mock_response = MagicMock()
    mock_response.status_code = 500

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.get = AsyncMock(return_value=mock_response)

    git = GitOps(github_token="ghp_real")
    with (
        patch("orchestrator.core.git_ops.httpx.AsyncClient", return_value=mock_client),
        pytest.raises(RuntimeError, match="unexpected GitHub API status 500"),
    ):
        await git.remote_file_exists("u/r", "main", "plan.md")


@pytest.mark.unit
async def test_remote_file_exists_raises_on_network_error() -> None:
    import httpx

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.get = AsyncMock(side_effect=httpx.ConnectError("connection refused"))

    git = GitOps(github_token="ghp_real")
    with (
        patch("orchestrator.core.git_ops.httpx.AsyncClient", return_value=mock_client),
        pytest.raises(RuntimeError, match="network error checking file on GitHub"),
    ):
        await git.remote_file_exists("u/r", "main", "plan.md")


# ---------------------------------------------------------------------------
# branch_commit_log
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_branch_commit_log_parses_sha_and_subject(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    git = GitOps(github_token="ghp_test")

    async def fake_run_checked(cmd: list[str], cwd: str | None = None) -> str:
        return "abc123def\x1fagent: Add model\n789ghijkl\x1fagent: Add test\n"

    monkeypatch.setattr(git, "_run_checked", fake_run_checked)

    from orchestrator.core.progress_handover import Commit

    commits = await git.branch_commit_log(".", "main", "agent/x")
    assert commits == [
        Commit(sha="abc123def", subject="agent: Add model"),
        Commit(sha="789ghijkl", subject="agent: Add test"),
    ]


@pytest.mark.unit
async def test_branch_commit_log_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    git = GitOps(github_token="ghp_test")

    async def fake_run_checked(cmd: list[str], cwd: str | None = None) -> str:
        return ""

    monkeypatch.setattr(git, "_run_checked", fake_run_checked)

    commits = await git.branch_commit_log(".", "main", "agent/x")
    assert commits == []


@pytest.mark.unit
async def test_branch_commit_log_passes_correct_git_args(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    git = GitOps(github_token="ghp_test")
    captured: list[tuple[list[str], str | None]] = []

    async def fake_run_checked(cmd: list[str], cwd: str | None = None) -> str:
        captured.append((cmd, cwd))
        return ""

    monkeypatch.setattr(git, "_run_checked", fake_run_checked)
    await git.branch_commit_log("/repo", "main", "agent/feat")

    assert len(captured) == 1
    cmd, cwd = captured[0]
    assert cmd == ["git", "log", "--reverse", "--format=%H%x1f%s", "main..agent/feat"]
    assert cwd == "/repo"
