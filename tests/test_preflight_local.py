"""Local mode has its own cheap preflight: real bare repo, real branch."""

import subprocess

import pytest

from orchestrator.core.preflight import PreflightError, PreflightKind, preflight_remote


@pytest.fixture
def bare_repo(tmp_path):
    work = tmp_path / "w"
    bare = tmp_path / "r.git"
    work.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=work, check=True)
    subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=work, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=work, check=True)
    (work / "a.txt").write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "add", "a.txt"], cwd=work, check=True)
    subprocess.run(["git", "commit", "-m", "base"], cwd=work, check=True)
    subprocess.run(["git", "clone", "--bare", str(work), str(bare)], check=True)
    return bare


@pytest.mark.integration
async def test_a_valid_local_repo_passes_with_no_warnings(bare_repo):
    warnings = await preflight_remote(
        git=None, repo_url=str(bare_repo), base="main", credential_configured=False
    )
    assert warnings == []


@pytest.mark.integration
async def test_a_missing_local_path_is_422(tmp_path):
    with pytest.raises(PreflightError) as exc:
        await preflight_remote(
            git=None,
            repo_url=str(tmp_path / "nope.git"),
            base="main",
            credential_configured=False,
        )
    assert exc.value.kind is PreflightKind.MISSING_REPO


@pytest.mark.integration
async def test_a_non_bare_directory_is_422(tmp_path):
    # A plain directory that is not a git repo at all would raise NOT_A_REPO
    # via the "not a git repository" branch, never reaching the bare check.
    # Init a REAL (non-bare) repo so the mutation this test guards
    # (removing the bare check) is actually exercised.
    plain = tmp_path / "plain"
    plain.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=plain, check=True)
    with pytest.raises(PreflightError) as exc:
        await preflight_remote(
            git=None, repo_url=str(plain), base="main", credential_configured=False
        )
    assert exc.value.kind is PreflightKind.NOT_A_REPO


@pytest.mark.integration
async def test_a_missing_base_branch_is_422(bare_repo):
    with pytest.raises(PreflightError) as exc:
        await preflight_remote(
            git=None,
            repo_url=str(bare_repo),
            base="does-not-exist",
            credential_configured=False,
        )
    assert exc.value.kind is PreflightKind.MISSING_BRANCH


@pytest.mark.integration
async def test_local_mode_needs_no_github_credential(bare_repo):
    """The whole point: evaluate Praxis with zero GitHub credentials."""
    warnings = await preflight_remote(
        git=None, repo_url=str(bare_repo), base="main", credential_configured=False
    )
    assert not any("credential" in w for w in warnings)


@pytest.mark.unit
def test_the_two_new_kinds_map_to_422():
    from orchestrator.core.preflight import status_and_detail

    for kind in (PreflightKind.MISSING_REPO, PreflightKind.NOT_A_REPO):
        status, _ = status_and_detail(PreflightError(kind, "x"))
        assert status == 422
