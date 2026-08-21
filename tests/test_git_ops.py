"""Git operation tests."""
# ruff: noqa: S101

from __future__ import annotations

import os
import subprocess
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from orchestrator.core import git_ops as git_ops_mod
from orchestrator.core.git_ops import (
    GitOps,
    _nothing_staged,
    checkout_branch,
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
def test_checkout_branch_fetches_then_checks_out_fetch_head(mock_run: object) -> None:
    # A plain ``git fetch origin <branch>`` only advances FETCH_HEAD; it never
    # creates a local/remote-tracking ref, so ``git checkout <branch>`` used to
    # fail with exit 1 and silently no-op the plan verify gate. The checkout
    # must therefore materialize the branch from FETCH_HEAD with ``-B``.
    checkout_branch("/tmp/ws", "plan/execute-foo-abc123", "tok123")
    cmds = [c.args[0] for c in mock_run.call_args_list]  # type: ignore[attr-defined]

    fetch = next(c for c in cmds if "fetch" in c)
    assert fetch[-2:] == ["origin", "plan/execute-foo-abc123"]
    # Token supplied via env for the fetch, never embedded in argv.
    fetch_call = next(
        c
        for c in mock_run.call_args_list  # type: ignore[attr-defined]
        if "fetch" in c.args[0]
    )
    assert fetch_call.kwargs["env"]["GH_TOKEN"] == "tok123"

    checkout = next(c for c in cmds if "checkout" in c)
    assert checkout[-3:] == ["-B", "plan/execute-foo-abc123", "FETCH_HEAD"]
    # Both steps fail loudly (check=True) so a genuine checkout error raises.
    for call in mock_run.call_args_list:  # type: ignore[attr-defined]
        assert call.kwargs["check"] is True


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
    git = GitOps("ghp_test")

    await git.clone_repo("https://github.com/user/repo.git", "/tmp/workspace")

    cmd = mock_run.call_args.args[0]
    assert cmd == ["git", "clone", "https://github.com/user/repo.git", "/tmp/workspace"]


@pytest.mark.unit
@patch("orchestrator.core.git_ops.GitOps._run_command")
async def test_create_branch(mock_run: AsyncMock) -> None:
    mock_run.return_value = (0, "", "")
    git = GitOps("ghp_test")

    await git.create_branch("/tmp/workspace", "plan/2026-06-01-auth", "main")

    assert mock_run.call_count == 4


@pytest.mark.unit
@patch("orchestrator.core.git_ops.GitOps._run_command")
async def test_push_branch(mock_run: AsyncMock) -> None:
    mock_run.return_value = (0, "", "")
    git = GitOps("ghp_test")

    await git.push_branch("/tmp/workspace", "agent/login")

    assert "push" in mock_run.call_args.args[0]


@pytest.mark.unit
@patch("orchestrator.core.git_ops.GitOps._run_command")
async def test_create_pr(mock_run: AsyncMock) -> None:
    mock_run.return_value = (0, "https://github.com/user/repo/pull/1\n", "")
    git = GitOps("ghp_test")

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
    git = GitOps("ghp_test")

    await git.merge_pr("/tmp/workspace", 1)

    cmd = mock_run.call_args.args[0]
    assert "--squash" in cmd
    assert "--delete-branch" in cmd


@pytest.mark.unit
@patch("orchestrator.core.git_ops.GitOps._run_command")
async def test_comment_on_pr(mock_run: AsyncMock) -> None:
    mock_run.return_value = (0, "", "")
    git = GitOps("ghp_test")

    await git.comment_on_pr("/tmp/workspace", 1, "Needs fixes")

    assert "comment" in mock_run.call_args.args[0]


@pytest.mark.unit
@patch("orchestrator.core.git_ops.GitOps._run_command")
async def test_get_pr_diff(mock_run: AsyncMock) -> None:
    mock_run.return_value = (0, "diff --git a/file.py ...", "")
    git = GitOps("ghp_test")

    diff = await git.get_pr_diff("/tmp/workspace", 1)

    assert "diff --git" in diff


@pytest.mark.unit
@patch("orchestrator.core.git_ops.GitOps._run_command")
async def test_get_pr_diff_targets_repo(mock_run: AsyncMock) -> None:
    mock_run.return_value = (0, "diff", "")
    git = GitOps("ghp_test")

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
    git = GitOps("ghp_test")

    files = await git.get_changed_files("/tmp/workspace", "main", "agent/login")

    assert files == ["a.py", "b.py"]


@pytest.mark.unit
@patch("orchestrator.core.git_ops.GitOps._run_command")
async def test_command_failure_raises(mock_run: AsyncMock) -> None:
    mock_run.return_value = (1, "", "fatal: not a git repository")
    git = GitOps("ghp_test")

    with pytest.raises(RuntimeError, match="Git command failed"):
        await git.clone_repo("https://github.com/user/repo.git", "/tmp/workspace")


@pytest.mark.unit
async def test_extract_pr_number() -> None:
    git = GitOps("ghp_test")

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
    git = GitOps("ghp_test")
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
    git = GitOps("ghp_test")
    result = await git.remote_branch_exists("https://github.com/u/r", "no-such")
    assert result is False


@pytest.mark.unit
@patch("orchestrator.core.git_ops.GitOps._run_command")
async def test_remote_branch_exists_raises_on_nonzero_exit(
    mock_run: AsyncMock,
) -> None:
    mock_run.return_value = (128, "", "fatal: not found")
    git = GitOps("ghp_test")
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
    git = GitOps("ghp_test")
    result = await git.remote_branch_exists("https://github.com/u/r", "my-branch")
    assert result is False


# ---------------------------------------------------------------------------
# remote_head_sha
# ---------------------------------------------------------------------------


@pytest.mark.unit
@patch("orchestrator.core.git_ops.GitOps._run_command")
async def test_remote_head_sha_returns_sha(mock_run: AsyncMock) -> None:
    mock_run.return_value = (0, "abc1234def5678\trefs/heads/main", "")
    git = GitOps("ghp_test")
    sha = await git.remote_head_sha("https://github.com/o/r", "main")
    assert sha == "abc1234def5678"
    cmd = mock_run.call_args.args[0]
    assert "ls-remote" in cmd
    assert "--heads" in cmd
    assert "main" in cmd


@pytest.mark.unit
@patch("orchestrator.core.git_ops.GitOps._run_command")
async def test_remote_head_sha_missing_branch_returns_none(
    mock_run: AsyncMock,
) -> None:
    mock_run.return_value = (0, "", "")
    git = GitOps("ghp_test")
    assert await git.remote_head_sha("https://github.com/o/r", "nope") is None


@pytest.mark.unit
@patch("orchestrator.core.git_ops.GitOps._run_command")
async def test_remote_head_sha_raises_on_git_error(mock_run: AsyncMock) -> None:
    mock_run.return_value = (128, "", "fatal: repository not found")
    git = GitOps("ghp_test")
    with pytest.raises(RuntimeError, match="git ls-remote failed"):
        await git.remote_head_sha("https://github.com/o/r", "main")


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

    git = GitOps("ghp_real")
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

    git = GitOps("ghp_real")
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

    git = GitOps("ghp_real")
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

    git = GitOps("ghp_real")
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
    git = GitOps("ghp_test")

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
    git = GitOps("ghp_test")

    async def fake_run_checked(cmd: list[str], cwd: str | None = None) -> str:
        return ""

    monkeypatch.setattr(git, "_run_checked", fake_run_checked)

    commits = await git.branch_commit_log(".", "main", "agent/x")
    assert commits == []


@pytest.mark.unit
async def test_branch_commit_log_passes_correct_git_args(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    git = GitOps("ghp_test")
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


# ---------------------------------------------------------------------------
# Credential provider construction tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_gitops_accepts_str_token_for_backcompat() -> None:
    from orchestrator.core.github_credentials import PatCredentialProvider

    ops = GitOps("ghp_legacy")
    assert isinstance(ops._provider, PatCredentialProvider)


@pytest.mark.unit
def test_gitops_accepts_provider() -> None:
    from orchestrator.core.github_credentials import PatCredentialProvider

    provider = PatCredentialProvider("ghp_x")
    ops = GitOps(provider)
    assert ops._provider is provider


@pytest.mark.unit
async def test_remote_branch_exists_resolves_repo_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from orchestrator.core.github_credentials import PatCredentialProvider

    class _Recording(PatCredentialProvider):
        def __init__(self) -> None:
            super().__init__("ghs_scoped")
            self.seen: list[str] = []

        async def token_for_repo(self, repo_url: str) -> str:
            self.seen.append(repo_url)
            return await super().token_for_repo(repo_url)

    provider = _Recording()
    ops = GitOps(provider)

    captured: dict[str, object] = {}

    async def fake_run(
        cmd: list[str],
        cwd: str | None = None,
        token: str | None = None,
    ) -> tuple[int, str, str]:
        captured["token"] = token
        return (0, "abc123\trefs/heads/main", "")

    monkeypatch.setattr(ops, "_run_command", fake_run)

    result = await ops.remote_branch_exists("https://github.com/o/r", "main")
    assert result is True
    assert provider.seen == ["https://github.com/o/r"]
    assert captured["token"] == "ghs_scoped"


@pytest.mark.unit
@patch("orchestrator.core.git_ops.GitOps._run_command")
async def test_open_integration_pr_shells_gh_with_repo(mock_run: AsyncMock) -> None:
    mock_run.return_value = (0, "https://github.com/o/r/pull/42", "")
    git = GitOps("ghp_test")

    url = await git.open_integration_pr(
        repo_url="https://github.com/o/r",
        base="main",
        head="plan/feature-x",
        title="Integrate plan/feature-x",
        body="Auto-opened by Praxis on plan completion.",
    )

    assert url == "https://github.com/o/r/pull/42"
    cmd = mock_run.call_args.args[0]
    flat = " ".join(cmd)
    assert "pr" in flat
    assert "create" in flat
    assert "--repo" in flat
    assert "o/r" in flat
    assert "--base" in flat
    assert "main" in flat
    assert "--head" in flat
    assert "plan/feature-x" in flat


# ---------------------------------------------------------------------------
# remote_commit_meta
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_remote_commit_meta_returns_subject_and_date(monkeypatch):
    import httpx

    from orchestrator.core.git_ops import GitOps

    git = GitOps("placeholder")

    async def fake_token(_repo):
        return "tok"

    monkeypatch.setattr(git, "_token_for_repo", fake_token)

    class FakeResp:
        status_code = 200

        def json(self):
            return {
                "commit": {
                    "message": "security: fix CodeQL\n\nbody",
                    "committer": {"date": "2026-07-06T05:19:58Z"},
                }
            }

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, headers=None):
            return FakeResp()

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)

    meta = await git.remote_commit_meta("o/r", "abc1234")
    assert meta == {
        "subject": "security: fix CodeQL",
        "committed_at": "2026-07-06T05:19:58Z",
    }


@pytest.mark.unit
async def test_git_ops_remote_validation_raises():
    git = GitOps("placeholder")

    # Invalid repo_slug
    with pytest.raises(ValueError, match="Invalid repository slug format"):
        await git.remote_file_exists("invalid/slug/path", "main", "file.txt")

    # Invalid path (directory traversal)
    with pytest.raises(ValueError, match="Invalid repository-relative path format"):
        await git.remote_file_exists("owner/repo", "main", "../traversal.txt")

    # Invalid sha
    with pytest.raises(ValueError, match="Invalid commit sha format"):
        await git.remote_commit_meta("owner/repo", "not-a-sha-123")


# ---------------------------------------------------------------------------
# merge_pr transient-retry tests
# ---------------------------------------------------------------------------

# GitHub's 504 as gh renders it when it surfaces the response body. Seen on two
# of three merges during newcomer walkthrough #4, where GitHub HAD performed the
# merge. Note it says "resubmitting", not "try again".
_GATEWAY_TIMEOUT_STDERR = (
    "non-200 OK status code: 504 Gateway Timeout body: "
    '"{\\"message\\": \\"We couldn\'t respond to your request in time. '
    'Sorry about that. Please try resubmitting your request.\\"}"'
)

# gh's OTHER rendering of the same failure, a bare status line. Neither
# "gateway timeout" nor "resubmitting your request" appears here, so only the
# "http 504" pattern can match it. Kept distinct so that pattern is pinned.
_GATEWAY_TIMEOUT_STDERR_HTTP = (
    "HTTP 504 (https://api.github.com/repos/o/r/pulls/39/merge)"
)


@pytest.mark.unit
async def test_merge_pr_retries_on_transient_error_then_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleep_calls: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    monkeypatch.setattr(git_ops_mod, "_merge_sleep", fake_sleep)

    # Patch _run_checked so _token_for_workspace doesn't go to subprocess.
    # We intercept merge_pr's internal _run_command calls directly by patching
    # the token resolution and the run loop separately.
    git = GitOps("ghp_test")

    # Make token resolution trivially succeed.
    async def fake_token_for_workspace(workspace: str) -> str:
        return "ghp_test"

    monkeypatch.setattr(git, "_token_for_workspace", fake_token_for_workspace)

    merge_call_count = 0

    original_run_command = git._run_command

    async def fake_run_command(
        cmd: list[str],
        cwd: str | None = None,
        token: str | None = None,
    ) -> tuple[int, str, str]:
        nonlocal merge_call_count
        if "merge" in cmd:
            merge_call_count += 1
            if merge_call_count == 1:
                return (1, "", "Base branch was modified. Please try again.")
            return (0, "", "")
        if "view" in cmd:
            return (0, "", "")
        return await original_run_command(cmd, cwd=cwd, token=token)

    monkeypatch.setattr(git, "_run_command", fake_run_command)

    await git.merge_pr("/tmp/workspace", 42)

    assert merge_call_count == 2
    assert len(sleep_calls) == 1


@pytest.mark.unit
async def test_merge_pr_non_transient_error_raises_immediately(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleep_calls: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    monkeypatch.setattr(git_ops_mod, "_merge_sleep", fake_sleep)

    git = GitOps("ghp_test")

    async def fake_token_for_workspace(workspace: str) -> str:
        return "ghp_test"

    monkeypatch.setattr(git, "_token_for_workspace", fake_token_for_workspace)

    merge_call_count = 0

    async def fake_run_command(
        cmd: list[str],
        cwd: str | None = None,
        token: str | None = None,
    ) -> tuple[int, str, str]:
        nonlocal merge_call_count
        if "merge" in cmd:
            merge_call_count += 1
            return (1, "", "Not found: repository or object does not exist")
        return (0, "", "")

    monkeypatch.setattr(git, "_run_command", fake_run_command)

    with pytest.raises(RuntimeError, match="Git command failed"):
        await git.merge_pr("/tmp/workspace", 7)

    assert merge_call_count == 1
    assert sleep_calls == []


@pytest.mark.unit
async def test_merge_pr_exhausts_retries_and_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleep_calls: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    monkeypatch.setattr(git_ops_mod, "_merge_sleep", fake_sleep)
    monkeypatch.setattr(git_ops_mod, "_MERGE_MAX_ATTEMPTS", 3)

    git = GitOps("ghp_test")

    async def fake_token_for_workspace(workspace: str) -> str:
        return "ghp_test"

    monkeypatch.setattr(git, "_token_for_workspace", fake_token_for_workspace)

    merge_call_count = 0

    async def fake_run_command(
        cmd: list[str],
        cwd: str | None = None,
        token: str | None = None,
    ) -> tuple[int, str, str]:
        nonlocal merge_call_count
        if "merge" in cmd:
            merge_call_count += 1
            return (1, "", "Pull request is not mergeable. Try again.")
        return (0, "", "")

    monkeypatch.setattr(git, "_run_command", fake_run_command)

    with pytest.raises(RuntimeError, match="Git command failed"):
        await git.merge_pr("/tmp/workspace", 99)

    assert merge_call_count == 3
    assert len(sleep_calls) == 2  # sleep between attempts, not after last


@pytest.mark.unit
async def test_merge_pr_succeeds_when_the_pr_is_already_merged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 504 AFTER a successful merge must not be reported as a failure.

    Observed twice in three merges during newcomer walkthrough #4: gh timed out
    while GitHub had already merged, leaving the task stuck at the gate.
    """

    async def fake_sleep(seconds: float) -> None:
        return None

    monkeypatch.setattr(git_ops_mod, "_merge_sleep", fake_sleep)
    git = GitOps("ghp_test")

    async def fake_token_for_workspace(workspace: str) -> str:
        return "ghp_test"

    monkeypatch.setattr(git, "_token_for_workspace", fake_token_for_workspace)

    async def fake_run_command(
        cmd: list[str],
        cwd: str | None = None,
        token: str | None = None,
    ) -> tuple[int, str, str]:
        if "merge" in cmd:
            return (1, "", _GATEWAY_TIMEOUT_STDERR)
        if "view" in cmd:
            return (0, '{"state":"MERGED"}', "")
        return (0, "", "")

    monkeypatch.setattr(git, "_run_command", fake_run_command)

    # Must NOT raise: GitHub says the PR is merged.
    await git.merge_pr("/tmp/workspace", 39)


@pytest.mark.unit
async def test_merge_pr_still_raises_when_the_pr_is_not_merged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The idempotency check must not swallow a genuine failure."""

    async def fake_sleep(seconds: float) -> None:
        return None

    monkeypatch.setattr(git_ops_mod, "_merge_sleep", fake_sleep)
    git = GitOps("ghp_test")

    async def fake_token_for_workspace(workspace: str) -> str:
        return "ghp_test"

    monkeypatch.setattr(git, "_token_for_workspace", fake_token_for_workspace)

    async def fake_run_command(
        cmd: list[str],
        cwd: str | None = None,
        token: str | None = None,
    ) -> tuple[int, str, str]:
        if "merge" in cmd:
            return (1, "", "Not found: repository or object does not exist")
        if "view" in cmd:
            return (0, '{"state":"OPEN"}', "")
        return (0, "", "")

    monkeypatch.setattr(git, "_run_command", fake_run_command)

    with pytest.raises(RuntimeError, match="Git command failed"):
        await git.merge_pr("/tmp/workspace", 7)


@pytest.mark.unit
@pytest.mark.parametrize(
    "merge_stderr",
    [_GATEWAY_TIMEOUT_STDERR, _GATEWAY_TIMEOUT_STDERR_HTTP],
    ids=["response-body-wording", "http-status-line"],
)
async def test_merge_pr_retries_a_gateway_timeout_instead_of_raising_at_once(
    monkeypatch: pytest.MonkeyPatch, merge_stderr: str
) -> None:
    """A 504 must reach the retry loop, not raise on the first attempt.

    This is the ONLY test that can see _TRANSIENT_MERGE_PATTERNS for a 504.
    GitHub is asked first and answers OPEN here, so the merged-check cannot
    short-circuit, which leaves the pattern list as the sole thing deciding
    retry-versus-raise. The attempt count is therefore the assertion: without
    the 504 patterns the very first failure raises and only one merge is tried.
    """
    sleep_calls: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    monkeypatch.setattr(git_ops_mod, "_merge_sleep", fake_sleep)
    monkeypatch.setattr(git_ops_mod, "_MERGE_MAX_ATTEMPTS", 3)
    git = GitOps("ghp_test")

    async def fake_token_for_workspace(workspace: str) -> str:
        return "ghp_test"

    monkeypatch.setattr(git, "_token_for_workspace", fake_token_for_workspace)

    merge_call_count = 0

    async def fake_run_command(
        cmd: list[str],
        cwd: str | None = None,
        token: str | None = None,
    ) -> tuple[int, str, str]:
        nonlocal merge_call_count
        if "merge" in cmd:
            merge_call_count += 1
            return (1, "", merge_stderr)
        if "view" in cmd:
            # Never merged, so the idempotency shortcut cannot mask the
            # pattern check.
            return (0, '{"state":"OPEN"}', "")
        return (0, "", "")

    monkeypatch.setattr(git, "_run_command", fake_run_command)

    with pytest.raises(RuntimeError, match="Git command failed"):
        await git.merge_pr("/tmp/workspace", 39)

    assert merge_call_count == 3
    assert len(sleep_calls) == 2


@pytest.mark.unit
async def test_merge_pr_succeeds_when_the_merge_landed_on_the_final_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A merge that lands on the LAST retry must not be reported as a failure.

    Exercises the second _pr_is_merged call site, the one guarding the raise
    after the retry loop is exhausted. Deleting that block turns a merge GitHub
    actually performed back into an error on the merge gate.
    """
    sleep_calls: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    monkeypatch.setattr(git_ops_mod, "_merge_sleep", fake_sleep)
    monkeypatch.setattr(git_ops_mod, "_MERGE_MAX_ATTEMPTS", 3)
    git = GitOps("ghp_test")

    async def fake_token_for_workspace(workspace: str) -> str:
        return "ghp_test"

    monkeypatch.setattr(git, "_token_for_workspace", fake_token_for_workspace)

    merge_call_count = 0
    view_call_count = 0

    async def fake_run_command(
        cmd: list[str],
        cwd: str | None = None,
        token: str | None = None,
    ) -> tuple[int, str, str]:
        nonlocal merge_call_count, view_call_count
        if "merge" in cmd:
            merge_call_count += 1
            return (1, "", _GATEWAY_TIMEOUT_STDERR)
        if "view" in cmd:
            view_call_count += 1
            # OPEN for the in-loop check of every attempt, MERGED only for the
            # post-loop check. That is what forces the second call site.
            if view_call_count <= 3:
                return (0, '{"state":"OPEN"}', "")
            return (0, '{"state":"MERGED"}', "")
        return (0, "", "")

    monkeypatch.setattr(git, "_run_command", fake_run_command)

    # Must NOT raise: the merge landed, gh just never got to say so.
    await git.merge_pr("/tmp/workspace", 71)

    assert merge_call_count == 3
    # The 4th view is the post-loop check; reaching it is what proves which
    # call site returned success.
    assert view_call_count == 4


@pytest.mark.unit
@pytest.mark.parametrize("view_mode", ["spawn-fails", "nonzero-exit"])
async def test_merge_pr_raises_when_github_cannot_be_asked(
    monkeypatch: pytest.MonkeyPatch, view_mode: str
) -> None:
    """Not being able to ask GitHub is not evidence of a merge.

    Covers both ways the state read can fail to answer: the gh subprocess never
    starting (missing binary, workspace already cleaned up) and gh exiting
    non-zero. Both must fail CLOSED, surfacing the original merge error rather
    than swallowing it or leaking the spawn error to the caller.
    """

    async def fake_sleep(seconds: float) -> None:
        return None

    monkeypatch.setattr(git_ops_mod, "_merge_sleep", fake_sleep)
    git = GitOps("ghp_test")

    async def fake_token_for_workspace(workspace: str) -> str:
        return "ghp_test"

    monkeypatch.setattr(git, "_token_for_workspace", fake_token_for_workspace)

    async def fake_run_command(
        cmd: list[str],
        cwd: str | None = None,
        token: str | None = None,
    ) -> tuple[int, str, str]:
        if "merge" in cmd:
            return (1, "", "Not found: repository or object does not exist")
        if "view" in cmd:
            if view_mode == "spawn-fails":
                raise OSError(267, "The directory name is invalid")
            # Non-zero exit, yet stdout still parses as MERGED. A failed gh
            # invocation must never be trusted, however plausible its output.
            return (1, '{"state":"MERGED"}', "gh: connection reset")
        return (0, "", "")

    monkeypatch.setattr(git, "_run_command", fake_run_command)

    # The merge error must surface, NOT the OSError and NOT a false success.
    with pytest.raises(RuntimeError, match="Git command failed"):
        await git.merge_pr("/tmp/workspace", 12)


@pytest.mark.unit
def test_nothing_staged_is_true_only_for_a_clean_index(tmp_path) -> None:
    """The fact the whole fix rests on, checked against REAL git.

    `git commit` exits 1 on a clean tree, so `check=True` turned "nothing
    changed" into `CalledProcessError`, which two routes propagated as a bare
    500. The replacement asks `git diff --cached --quiet`, which answers in
    exit codes rather than in prose and so cannot be defeated by a locale that
    translates "nothing to commit". Mocking subprocess here would assert only
    that the mock was called.
    """
    ws = str(tmp_path)
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e"}
    env.update({"GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e"})
    subprocess.run(["git", "init", "-q", ws], check=True, env=env)
    subprocess.run(
        ["git", "-C", ws, "commit", "-q", "--allow-empty", "-m", "base"],
        check=True,
        env=env,
    )

    assert _nothing_staged(ws) is True

    (tmp_path / "f.txt").write_text("x", encoding="utf-8")
    subprocess.run(["git", "-C", ws, "add", "f.txt"], check=True, env=env)
    assert _nothing_staged(ws) is False


@pytest.mark.unit
@patch("orchestrator.core.git_ops._nothing_staged", return_value=True)
@patch("orchestrator.core.git_ops.subprocess.run")
def test_commit_and_push_reports_a_no_op_instead_of_raising(
    mock_run: object,
    probe: object,  # noqa: ARG001 - patch decorator arg, not a fixture
) -> None:
    """ "Nothing changed" is a FACT the caller must be able to report.

    Saving a spec without editing it, and approving a context draft the planner
    produced empty, both answered 500. Revert to `check=True` on the commit and
    only these go red.
    """
    assert commit_and_push("/tmp/ws", "tok123", "msg") is False
    cmds = [c.args[0] for c in mock_run.call_args_list]  # type: ignore[attr-defined]
    assert any("add" in c for c in cmds)
    # Neither the commit nor the push was attempted.
    assert not any("commit" in c for c in cmds)
    assert not any("push" in c for c in cmds)


@pytest.mark.unit
@patch("orchestrator.core.git_ops._nothing_staged", return_value=False)
@patch("orchestrator.core.git_ops.subprocess.run")
def test_commit_and_push_reports_true_when_it_committed(
    mock_run: object,
    probe: object,  # noqa: ARG001 - patch decorator arg, not a fixture
) -> None:
    """The other branch, so "always False" cannot pass the test above."""
    assert commit_and_push("/tmp/ws", "tok123", "msg") is True
