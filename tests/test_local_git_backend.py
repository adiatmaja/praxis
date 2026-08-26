"""LocalGitBackend against a real bare repo. No mocks: git is the contract."""

import subprocess
import tempfile

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


def _clone_for_writing(bare, dest, tmp_path):
    """Clone the bare repo into ``dest`` and configure it to make commits."""
    _git("clone", str(bare), str(dest), cwd=tmp_path)
    _git("config", "user.email", "t@example.com", cwd=dest)
    _git("config", "user.name", "t", cwd=dest)
    _git("config", "commit.gpgsign", "false", cwd=dest)
    return dest


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
async def test_get_diff_ignores_base_commits_made_after_the_branch_point(
    bare_repo, ref, tmp_path
):
    """The diff is against the merge base, not the moving tip of base.

    A two-dot ``base..branch`` diff would report the base-only file as a
    deletion, which the reviewer brain reads as the worker destroying it.
    """
    work = _clone_for_writing(bare_repo, tmp_path / "advance", tmp_path)
    (work / "base_only.txt").write_text("base only\n", encoding="utf-8")
    _git("add", "base_only.txt", cwd=work)
    _git("commit", "-m", "base moves on", cwd=work)
    _git("push", "origin", "main", cwd=work)

    diff = await LocalGitBackend(str(bare_repo)).get_diff(ref)

    assert "+two" in diff
    assert "base_only.txt" not in diff


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
async def test_merge_surfaces_gits_stdout_when_the_branch_conflicts(
    bare_repo, tmp_path
):
    """git writes CONFLICT to stdout, so a stderr-only error message is blank."""
    work = _clone_for_writing(bare_repo, tmp_path / "conflict", tmp_path)
    _git("checkout", "-b", "agent/y", cwd=work)
    (work / "a.txt").write_text("their side\n", encoding="utf-8")
    _git("commit", "-am", "their edit", cwd=work)
    _git("push", "origin", "agent/y", cwd=work)
    _git("checkout", "main", cwd=work)
    (work / "a.txt").write_text("our side\n", encoding="utf-8")
    _git("commit", "-am", "our edit", cwd=work)
    _git("push", "origin", "main", cwd=work)

    conflicting = PullRequestRef(backend="local", branch="agent/y", base="main")

    with pytest.raises(RuntimeError, match="CONFLICT"):
        await LocalGitBackend(str(bare_repo)).merge(conflicting)


@pytest.mark.integration
async def test_merge_survives_a_failed_branch_delete(bare_repo, ref, monkeypatch):
    """The base push is the operation that matters; the delete is cleanup.

    Raising here left the task un-merged and every retry hit the no-op
    ``merge --squash`` then a failing ``commit``, forever.
    """
    backend = LocalGitBackend(str(bare_repo))
    real_run = backend._run

    async def failing_delete(cmd: list[str], cwd: str | None = None):
        if "--delete" in cmd:
            return (1, "", "remote: refusing to delete the current branch")
        return await real_run(cmd, cwd=cwd)

    monkeypatch.setattr(backend, "_run", failing_delete)
    before = int(_git("rev-list", "--count", "main", cwd=bare_repo))

    await backend.merge(ref)

    assert int(_git("rev-list", "--count", "main", cwd=bare_repo)) == before + 1
    assert "agent/x" in _git("branch", "--list", cwd=bare_repo)


@pytest.mark.integration
async def test_merge_leaves_no_temp_clone_behind(bare_repo, ref, monkeypatch, tmp_path):
    """git marks .git/objects/pack read-only, which defeats a plain rmtree."""
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))

    await LocalGitBackend(str(bare_repo)).merge(ref)

    assert list(scratch.glob("praxis-local-merge-*")) == []


@pytest.mark.integration
async def test_a_file_url_and_a_plain_path_behave_identically(bare_repo, ref):
    from_path = await LocalGitBackend(str(bare_repo)).get_diff(ref)
    from_url = await LocalGitBackend(
        "file:///" + str(bare_repo).replace("\\", "/").lstrip("/")
    ).get_diff(ref)
    assert from_path == from_url


@pytest.mark.integration
async def test_base_contains_a_branch_base_has_already_absorbed(bare_repo, tmp_path):
    """A plan branch that merely TRAILS base has nothing left to integrate.

    Every leaf closed ``no_changes``/``superseded``, so the plan branch never
    got a commit of its own; base then moved on. The head SHAs are no longer
    equal, so the identical-SHA fact cannot fire, and ``gh pr create`` refuses
    with "No commits between ...". Reported as a failure, that writes
    ``plans.error`` -- a ONE-WAY signal -- over a plan that did everything
    right.

    ``ls-remote`` cannot answer ancestry, which is why the question belongs on
    the backend seam at all: a bare repo has no ``gh`` either.
    """
    _git("branch", "plan/x", "main", cwd=bare_repo)
    work = _clone_for_writing(bare_repo, tmp_path / "advance", tmp_path)
    (work / "later.txt").write_text("later\n", encoding="utf-8")
    _git("add", "later.txt", cwd=work)
    _git("commit", "-m", "base moves on", cwd=work)
    _git("push", "origin", "main", cwd=work)

    backend = LocalGitBackend(str(bare_repo))

    assert await backend.base_contains("main", "plan/x") is True
    # The control, and the direction that must NOT be reported as nothing to
    # integrate: a branch carrying work base has never seen.
    assert await backend.base_contains("main", "agent/x") is False


@pytest.mark.integration
async def test_a_squash_merged_branch_is_not_an_ancestor_of_base(bare_repo):
    """The documented cost of using ancestry: a squash merge rewrites history.

    ``LocalGitBackend.merge`` squash-merges, so after a local merge the work is
    on base but the branch's own commits are not ancestors of it. This answers
    False and the caller falls through to the ordinary creation attempt, exactly
    as it does today. That costs coverage in local mode, never correctness.
    """
    merged_sha = _git("rev-parse", "refs/heads/agent/x", cwd=bare_repo)
    backend = LocalGitBackend(str(bare_repo))

    await backend.merge(PullRequestRef(backend="local", branch="agent/x", base="main"))
    # merge() deletes the source branch, so name the same commits again.
    _git("branch", "plan/squashed", merged_sha, cwd=bare_repo)

    assert await backend.base_contains("main", "plan/squashed") is False


@pytest.mark.integration
async def test_base_contains_says_unknown_when_the_branch_is_not_there(bare_repo):
    """Neither True nor False: ``git`` could not answer the question at all.

    Exit 0 and exit 1 are the two ANSWERS ``merge-base --is-ancestor`` gives.
    Anything else (a ref that does not resolve, exit 128) is a failed lookup,
    and folding that into False would be a fabricated answer of the kind that
    only shows up as a wrong outcome much later.
    """
    assert (
        await LocalGitBackend(str(bare_repo)).base_contains("main", "no/such") is None
    )


@pytest.mark.integration
async def test_head_sha_returns_the_branch_head(bare_repo):
    """The dispatch-time base sha, read straight from the bare repo."""
    expected = _git("rev-parse", "refs/heads/agent/x", cwd=bare_repo)

    assert await LocalGitBackend(str(bare_repo)).head_sha("agent/x") == expected


@pytest.mark.integration
async def test_head_sha_is_none_for_a_branch_that_does_not_exist(bare_repo):
    """The ordinary first-dispatch case: the branch has not been pushed yet.

    None, not an exception: an absent branch is a fact the caller acts on (it
    records the base branch head instead), and raising here would strand every
    first dispatch.
    """
    assert await LocalGitBackend(str(bare_repo)).head_sha("agent/never") is None
