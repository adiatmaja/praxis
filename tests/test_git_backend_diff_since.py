"""``get_diff_since``: the diff a task's own commits produced, not the branch's.

Both backends must have it. The bench runs entirely on ``LocalGitBackend``, so
an implementation on only one of them is worse than none: it would scope the
review on GitHub and silently keep the whole-branch diff everywhere the
benchmark measures.

The case that matters most is an orphaned base SHA. It is reachable through a
force push, a rebuilt-from-base retry, or a swept and recreated branch. It must
NEVER yield an empty diff, because an empty diff reviews as a trivially passing
change and would let a broken change through the gate. Both backends fall back
to the whole pull request and say so.
"""
# ruff: noqa: S101

import subprocess

import pytest

from orchestrator.core.git_backend import (
    GitHubBackend,
    LocalGitBackend,
    PullRequestRef,
)
from tests.test_git_backend import GITHUB_PR_URL, _git_ops_mock


def _git(*args: str, cwd) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.fixture
def two_task_repo(tmp_path):
    """A bare repo whose ONE branch carries two tasks' commits.

    This is the single-branch (auto-delegate) shape that the whole plan exists
    for: ``first.txt`` is task one's file and ``second.txt`` is task two's, both
    on ``plan/x``, and the pull request diff contains both.
    """
    work = tmp_path / "work"
    bare = tmp_path / "repo.git"
    work.mkdir()
    _git("init", "-b", "main", cwd=work)
    _git("config", "user.email", "t@example.com", cwd=work)
    _git("config", "user.name", "t", cwd=work)
    _git("config", "commit.gpgsign", "false", cwd=work)
    (work / "base.txt").write_text("base\n", encoding="utf-8")
    _git("add", "base.txt", cwd=work)
    _git("commit", "-m", "base", cwd=work)
    _git("clone", "--bare", str(work), str(bare), cwd=tmp_path)
    _git("remote", "add", "origin", str(bare), cwd=work)

    _git("checkout", "-b", "plan/x", cwd=work)
    (work / "first.txt").write_text("task one\n", encoding="utf-8")
    _git("add", "first.txt", cwd=work)
    _git("commit", "-m", "task one", cwd=work)
    after_first = _git("rev-parse", "HEAD", cwd=work)

    (work / "second.txt").write_text("task two\n", encoding="utf-8")
    _git("add", "second.txt", cwd=work)
    _git("commit", "-m", "task two", cwd=work)
    _git("push", "origin", "main", cwd=work)
    _git("push", "origin", "plan/x", cwd=work)
    return bare, after_first, work


@pytest.fixture
def local_ref():
    return PullRequestRef(backend="local", branch="plan/x", base="main")


@pytest.mark.integration
async def test_local_diff_since_shows_only_the_later_commits(two_task_repo, local_ref):
    """The regression this plan exists to remove, at the backend level."""
    bare, after_first, _work = two_task_repo
    backend = LocalGitBackend(str(bare))

    whole = await backend.get_diff(local_ref)
    scoped = await backend.get_diff_since(local_ref, after_first)

    assert "first.txt" in whole
    assert "second.txt" in whole
    assert "second.txt" in scoped
    assert "first.txt" not in scoped


@pytest.mark.integration
async def test_local_diff_since_an_orphaned_sha_falls_back_to_the_whole_diff(
    two_task_repo, local_ref, caplog
):
    """A SHA that is not an ancestor must not read as "nothing changed".

    Forty zeros is a well-formed SHA this repository has never seen, which is
    what a swept and recreated branch leaves behind.
    """
    bare, _after_first, _work = two_task_repo
    backend = LocalGitBackend(str(bare))

    with caplog.at_level("WARNING", logger="orchestrator.core.git_backend"):
        scoped = await backend.get_diff_since(local_ref, "0" * 40)

    assert "first.txt" in scoped
    assert "second.txt" in scoped
    assert any("whole pull request" in r.getMessage() for r in caplog.records)


@pytest.mark.integration
async def test_local_diff_since_a_diverged_sha_falls_back_to_the_whole_diff(
    two_task_repo, local_ref, caplog
):
    """A real commit that is not on this branch is the force-push case.

    It is a harder case than an unknown SHA: git can resolve it, so a naive
    two-dot diff would succeed and return a diff against an unrelated line of
    history rather than failing.
    """
    bare, _after_first, work = two_task_repo
    _git("checkout", "-b", "sidetrack", "main", cwd=work)
    (work / "elsewhere.txt").write_text("elsewhere\n", encoding="utf-8")
    _git("add", "elsewhere.txt", cwd=work)
    _git("commit", "-m", "elsewhere", cwd=work)
    diverged = _git("rev-parse", "HEAD", cwd=work)
    _git("push", "origin", "sidetrack", cwd=work)
    backend = LocalGitBackend(str(bare))

    with caplog.at_level("WARNING", logger="orchestrator.core.git_backend"):
        scoped = await backend.get_diff_since(local_ref, diverged)

    assert "first.txt" in scoped
    assert "second.txt" in scoped
    assert "elsewhere.txt" not in scoped
    assert any("whole pull request" in r.getMessage() for r in caplog.records)


@pytest.mark.unit
async def test_github_diff_since_compares_the_base_sha_with_the_pr_head():
    """The GitHub half of the same capability."""
    git = _git_ops_mock()
    git.pr_head_sha.return_value = "headsha"
    git.compare_merge_base.return_value = "basesha"
    git.compare_diff.return_value = "diff --git a/second.txt b/second.txt"
    backend = GitHubBackend(git, "https://github.com/o/r")

    diff = await backend.get_diff_since(
        PullRequestRef.from_url(GITHUB_PR_URL), "basesha"
    )

    assert diff == "diff --git a/second.txt b/second.txt"
    git.pr_head_sha.assert_awaited_once_with(42, repo="o/r")
    git.compare_diff.assert_awaited_once_with("o/r", "basesha", "headsha")
    git.get_pr_diff.assert_not_awaited()


@pytest.mark.unit
async def test_github_diff_since_an_orphaned_sha_falls_back_to_the_whole_diff(caplog):
    """Not an ancestor: GitHub's merge base is not the sha we recorded.

    The compare endpoint would still answer, from the merge base rather than
    from the recorded SHA, so this cannot be left to fail loudly on its own.
    """
    git = _git_ops_mock()
    git.pr_head_sha.return_value = "headsha"
    git.compare_merge_base.return_value = "someothersha"
    git.get_pr_diff.return_value = "the whole pr diff"
    backend = GitHubBackend(git, "https://github.com/o/r")

    with caplog.at_level("WARNING", logger="orchestrator.core.git_backend"):
        diff = await backend.get_diff_since(
            PullRequestRef.from_url(GITHUB_PR_URL), "basesha"
        )

    assert diff == "the whole pr diff"
    git.compare_diff.assert_not_awaited()
    assert any("whole pull request" in r.getMessage() for r in caplog.records)


@pytest.mark.unit
async def test_github_diff_since_falls_back_when_the_compare_call_fails(caplog):
    """An API failure degrades to today's behavior, never to an empty diff."""
    git = _git_ops_mock()
    git.pr_head_sha.side_effect = RuntimeError("gh api exploded")
    git.get_pr_diff.return_value = "the whole pr diff"
    backend = GitHubBackend(git, "https://github.com/o/r")

    with caplog.at_level("WARNING", logger="orchestrator.core.git_backend"):
        diff = await backend.get_diff_since(
            PullRequestRef.from_url(GITHUB_PR_URL), "basesha"
        )

    assert diff == "the whole pr diff"
    assert any("whole pull request" in r.getMessage() for r in caplog.records)
