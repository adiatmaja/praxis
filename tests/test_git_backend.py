"""The backend seam: GitHub behavior is unchanged, local is new.

Everything above this seam (merge gate, verify gates, review flow) must be
identical for both backends; only the PR plumbing differs.
"""

from unittest.mock import AsyncMock

import pytest

from orchestrator.core.git_backend import (
    GitHubBackend,
    PullRequestRef,
    is_local_repo_url,
    resolve_backend,
)


@pytest.mark.unit
def test_a_file_url_is_a_local_repo():
    assert is_local_repo_url("file:///srv/bench/astropy.git") is True


@pytest.mark.unit
def test_an_absolute_path_is_a_local_repo():
    assert is_local_repo_url("/srv/bench/astropy.git") is True


@pytest.mark.unit
def test_a_windows_path_is_a_local_repo():
    assert is_local_repo_url(r"C:\bench\astropy.git") is True


@pytest.mark.unit
def test_an_https_github_url_is_not_local():
    assert is_local_repo_url("https://github.com/o/r") is False


@pytest.mark.unit
def test_an_ssh_github_url_is_not_local():
    assert is_local_repo_url("git@github.com:o/r.git") is False


@pytest.mark.unit
def test_resolve_backend_picks_github_for_a_github_url():
    backend = resolve_backend("https://github.com/o/r", git_ops=AsyncMock())
    assert isinstance(backend, GitHubBackend)
    assert backend.name == "github"


@pytest.mark.unit
def test_resolve_backend_picks_local_for_a_file_url():
    backend = resolve_backend("file:///srv/bench/a.git", git_ops=AsyncMock())
    assert backend.name == "local"


@pytest.mark.unit
def test_pull_request_ref_round_trips_through_its_url_form():
    ref = PullRequestRef(backend="local", branch="agent/x", base="main", number=None)
    assert PullRequestRef.from_url(ref.to_url()) == ref


@pytest.mark.unit
def test_pull_request_ref_parses_a_real_github_pr_url():
    ref = PullRequestRef.from_url("https://github.com/o/r/pull/42")
    assert ref.backend == "github"
    assert ref.number == 42


@pytest.mark.unit
async def test_github_backend_get_diff_delegates_to_git_ops_with_repo():
    git = AsyncMock()
    git.extract_pr_number.return_value = 42
    git.repo_slug.return_value = "o/r"
    git.get_pr_diff.return_value = "diff --git a/x b/x"
    backend = GitHubBackend(git)
    ref = PullRequestRef.from_url("https://github.com/o/r/pull/42")

    diff = await backend.get_diff(ref)

    assert diff == "diff --git a/x b/x"
    git.get_pr_diff.assert_awaited_once()
    assert git.get_pr_diff.await_args.kwargs["repo"] == "o/r"


@pytest.mark.unit
async def test_github_backend_merge_delegates_to_merge_pr():
    git = AsyncMock()
    git.extract_pr_number.return_value = 42
    git.repo_slug.return_value = "o/r"
    backend = GitHubBackend(git)
    await backend.merge(PullRequestRef.from_url("https://github.com/o/r/pull/42"))
    git.merge_pr.assert_awaited_once()


@pytest.mark.unit
async def test_github_backend_comment_delegates_to_comment_on_pr():
    git = AsyncMock()
    git.extract_pr_number.return_value = 42
    git.repo_slug.return_value = "o/r"
    backend = GitHubBackend(git)
    await backend.comment(
        PullRequestRef.from_url("https://github.com/o/r/pull/42"), "feedback"
    )
    git.comment_on_pr.assert_awaited_once()
