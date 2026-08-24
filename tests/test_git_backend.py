"""The backend seam: GitHub behavior is unchanged, local is new.

Everything above this seam (merge gate, verify gates, review flow) must be
identical for both backends; only the PR plumbing differs.

The GitHub tests assert the FULL delegated call, not just that a call happened.
``gh pr`` resolves against the orchestrator's own cwd when ``--repo`` is
missing, so ``repo=`` is the difference between merging the project's PR and
merging something in Praxis's own checkout.
"""

import os
from unittest.mock import AsyncMock

import pytest

from orchestrator.core.git_backend import (
    GitHubBackend,
    PullRequestRef,
    is_local_repo_url,
    local_repo_path,
    resolve_backend,
)
from orchestrator.core.git_ops import GitOps


GITHUB_PR_URL = "https://github.com/o/r/pull/42"


def _git_ops_mock() -> AsyncMock:
    """A GitOps double that fails loudly if a method is renamed or misspelled."""
    return AsyncMock(spec=GitOps)


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
def test_a_unc_path_is_a_local_repo():
    assert is_local_repo_url(r"\\server\share\repo.git") is True


@pytest.mark.unit
def test_a_home_relative_path_is_a_local_repo():
    assert is_local_repo_url("~/bench/astropy.git") is True


@pytest.mark.unit
def test_local_repo_path_expands_a_home_relative_path(monkeypatch, tmp_path):
    home = str(tmp_path / "home").replace("\\", "/")
    monkeypatch.setenv("HOME", home)
    monkeypatch.setenv("USERPROFILE", home)

    assert local_repo_path("~/bench/a.git") == f"{home}/bench/a.git"
    assert os.path.isabs(local_repo_path("~/bench/a.git"))


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
def test_pull_request_ref_round_trips_url_metacharacters_in_branch_and_base():
    """``&``, ``=`` and a space must survive the ``praxis-local://`` encoding."""
    ref = PullRequestRef(
        backend="local",
        branch="agent/fix a&b=c",
        base="release&main",
        number=None,
    )
    url = ref.to_url()

    assert "agent/fix a&b=c" not in url
    assert PullRequestRef.from_url(url) == ref


@pytest.mark.unit
def test_pull_request_ref_parses_a_real_github_pr_url():
    ref = PullRequestRef.from_url(GITHUB_PR_URL)
    assert ref.backend == "github"
    assert ref.number == 42


@pytest.mark.unit
def test_to_url_rejects_an_unknown_backend():
    ref = PullRequestRef(
        backend="Github", branch="x", base="main", number=42, repo="o/r"
    )
    with pytest.raises(ValueError, match="backend"):
        ref.to_url()


@pytest.mark.unit
def test_to_url_rejects_a_github_ref_with_no_repo():
    """Otherwise it renders ``https://github.com/None/pull/42`` into ``pr_url``."""
    ref = PullRequestRef(backend="github", branch="x", base="main", number=42)
    with pytest.raises(ValueError, match="repo"):
        ref.to_url()


@pytest.mark.unit
def test_to_url_still_renders_a_local_ref_that_has_no_repo():
    """A local ref never carries a repo; the github guard must not catch it."""
    ref = PullRequestRef(backend="local", branch="agent/x", base="main")
    assert ref.to_url().startswith("praxis-local://pr?")


@pytest.mark.unit
def test_from_url_rejects_a_null_pr_url():
    """``tasks.pr_url`` is nullable and the contract promises ValueError."""
    with pytest.raises(ValueError, match="pull-request reference"):
        PullRequestRef.from_url(None)  # type: ignore[arg-type]


@pytest.mark.unit
def test_from_url_rejects_an_empty_pr_url():
    with pytest.raises(ValueError, match="pull-request reference"):
        PullRequestRef.from_url("")


@pytest.mark.unit
async def test_github_backend_get_diff_delegates_to_git_ops_with_repo():
    git = _git_ops_mock()
    git.get_pr_diff.return_value = "diff --git a/x b/x"
    backend = GitHubBackend(git)
    ref = PullRequestRef.from_url(GITHUB_PR_URL)

    diff = await backend.get_diff(ref)

    assert diff == "diff --git a/x b/x"
    git.get_pr_diff.assert_awaited_once_with(".", 42, repo="o/r")


@pytest.mark.unit
async def test_github_backend_merge_delegates_to_merge_pr():
    git = _git_ops_mock()
    backend = GitHubBackend(git)

    await backend.merge(PullRequestRef.from_url(GITHUB_PR_URL))

    git.merge_pr.assert_awaited_once_with(".", 42, repo="o/r")


@pytest.mark.unit
async def test_github_backend_comment_delegates_to_comment_on_pr():
    git = _git_ops_mock()
    backend = GitHubBackend(git)

    await backend.comment(PullRequestRef.from_url(GITHUB_PR_URL), "feedback")

    git.comment_on_pr.assert_awaited_once_with(".", 42, "feedback", repo="o/r")


@pytest.mark.unit
async def test_github_backend_checkout_clones_the_pr_head_into_dest():
    git = _git_ops_mock()
    git.clone_pr_head.return_value = "/work/dest"
    backend = GitHubBackend(git)

    result = await backend.checkout(
        PullRequestRef.from_url(GITHUB_PR_URL), "/work/dest"
    )

    assert result == "/work/dest"
    git.clone_pr_head.assert_awaited_once_with(GITHUB_PR_URL, "/work/dest")


# Two ways a repo-less ref reaches GitHubBackend.  The second is the one the
# ``backend == "github"`` guard used to wave through: the backend is chosen from
# the project's ``repo_url`` while the ref is parsed from the task's ``pr_url``,
# so editing a project's repo_url while tasks exist puts a local ref in front of
# the GitHub backend.  Both must fail closed.
_REPO_LESS_REFS = {
    "a_github_ref_with_no_repo": PullRequestRef(
        backend="github", branch="x", base="main", number=42
    ),
    "a_local_ref_handed_to_the_github_backend": PullRequestRef(
        backend="local", branch="agent/a", base="main"
    ),
}


async def _invoke(backend: GitHubBackend, method: str, ref: PullRequestRef) -> None:
    """Call one of the four protocol methods on ``backend``."""
    if method == "get_diff":
        await backend.get_diff(ref)
    elif method == "checkout":
        await backend.checkout(ref, "/work/dest")
    elif method == "comment":
        await backend.comment(ref, "feedback")
    else:
        await backend.merge(ref)


@pytest.mark.unit
@pytest.mark.parametrize("method", ["get_diff", "checkout", "comment", "merge"])
@pytest.mark.parametrize("ref_name", sorted(_REPO_LESS_REFS))
async def test_github_backend_refuses_any_ref_without_a_repo(
    method: str, ref_name: str
):
    """No repo means ``gh`` would target the orchestrator's own cwd.

    The guard keys on ``repo is None`` alone, never on ``ref.backend``: the
    backend and the ref come from two independent sources and can disagree.
    """
    git = _git_ops_mock()
    backend = GitHubBackend(git)

    with pytest.raises(ValueError, match="repo"):
        await _invoke(backend, method, _REPO_LESS_REFS[ref_name])

    git.get_pr_diff.assert_not_awaited()
    git.clone_pr_head.assert_not_awaited()
    git.comment_on_pr.assert_not_awaited()
    git.merge_pr.assert_not_awaited()


@pytest.mark.unit
async def test_github_backend_head_sha_delegates_with_the_repo_url():
    git = _git_ops_mock()
    git.remote_head_sha.return_value = "abc123"
    backend = GitHubBackend(git, "https://github.com/o/r")

    assert await backend.head_sha("plan/x") == "abc123"
    git.remote_head_sha.assert_awaited_once_with("https://github.com/o/r", "plan/x")


@pytest.mark.unit
async def test_github_backend_head_sha_is_none_without_a_repo_url():
    """A branch head has no meaning without a repository, so do not guess one.

    ``GitHubBackend(git_ops)`` with no URL is how many existing call sites and
    doubles build it. Returning None keeps them on the whole-pull-request review
    path instead of resolving a branch against some other repository.
    """
    git = _git_ops_mock()
    backend = GitHubBackend(git)

    assert await backend.head_sha("plan/x") is None
    git.remote_head_sha.assert_not_awaited()
