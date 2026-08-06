"""LocalGitBackend against a real bare repo. No mocks: git is the contract."""

import subprocess

import pytest

from orchestrator.core.git_backend import LocalGitBackend, PullRequestRef


def _git(*args: str, cwd) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.fixture
def bare_repo(tmp_path):
    """A bare repo with a `main` commit and an `agent/x` branch on top."""
    work = tmp_path / "work"
    bare = tmp_path / "repo.git"
    work.mkdir()
    _git("init", "-b", "main", cwd=work)
    _git("config", "user.email", "t@example.com", cwd=work)
    _git("config", "user.name", "t", cwd=work)
    _git("config", "commit.gpgsign", "false", cwd=work)
    (work / "a.txt").write_text("one\n", encoding="utf-8")
    _git("add", "a.txt", cwd=work)
    _git("commit", "-m", "base", cwd=work)
    _git("clone", "--bare", str(work), str(bare), cwd=tmp_path)
    _git("remote", "add", "origin", str(bare), cwd=work)
    _git("checkout", "-b", "agent/x", cwd=work)
    (work / "a.txt").write_text("one\ntwo\n", encoding="utf-8")
    _git("commit", "-am", "add two", cwd=work)
    _git("push", "origin", "agent/x", cwd=work)
    _git("push", "origin", "main", cwd=work)
    return bare


@pytest.fixture
def ref():
    return PullRequestRef(backend="local", branch="agent/x", base="main")


@pytest.mark.integration
async def test_get_diff_returns_the_branch_changes(bare_repo, ref):
    diff = await LocalGitBackend(str(bare_repo)).get_diff(ref)
    assert "+two" in diff
    assert "a.txt" in diff


@pytest.mark.integration
async def test_checkout_produces_a_working_tree_at_the_branch(bare_repo, ref, tmp_path):
    dest = tmp_path / "checkout"
    await LocalGitBackend(str(bare_repo)).checkout(ref, str(dest))
    assert (dest / "a.txt").read_text(encoding="utf-8") == "one\ntwo\n"


@pytest.mark.integration
async def test_merge_squashes_into_base_and_deletes_the_branch(
    bare_repo, ref, tmp_path
):
    await LocalGitBackend(str(bare_repo)).merge(ref)
    heads = _git("branch", "--list", cwd=bare_repo)
    assert "agent/x" not in heads
    dest = tmp_path / "after"
    _git("clone", str(bare_repo), str(dest), cwd=tmp_path)
    assert (dest / "a.txt").read_text(encoding="utf-8") == "one\ntwo\n"


@pytest.mark.integration
async def test_merge_produces_exactly_one_new_commit_on_base(bare_repo, ref):
    before = int(_git("rev-list", "--count", "main", cwd=bare_repo))
    await LocalGitBackend(str(bare_repo)).merge(ref)
    after = int(_git("rev-list", "--count", "main", cwd=bare_repo))
    assert after == before + 1


@pytest.mark.integration
async def test_a_file_url_and_a_plain_path_behave_identically(bare_repo, ref):
    from_path = await LocalGitBackend(str(bare_repo)).get_diff(ref)
    from_url = await LocalGitBackend(
        "file:///" + str(bare_repo).replace("\\", "/").lstrip("/")
    ).get_diff(ref)
    assert from_path == from_url
