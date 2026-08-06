"""One real bare repo through the whole review-and-merge path.

No containers: the worker's output is simulated by pushing a branch, which is
exactly what the entrypoint does. Everything after that is the real loop.
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from orchestrator.core.git_backend import PullRequestRef, resolve_backend
from orchestrator.core.preflight import preflight_remote


_ENTRYPOINT_SH = (
    Path(__file__).resolve().parents[1] / "docker" / "opencode-agent" / "entrypoint.sh"
)


def _extract_url_encode(entrypoint_path: Path) -> str:
    """Pull the shipped ``url_encode`` shell function out of entrypoint.sh.

    Extracting the real function (rather than hand-copying its body into the
    test) is the point: this test proves the shipped shell code and the
    Python parser agree, so a future edit to either side that breaks the
    contract fails here.
    """
    text = entrypoint_path.read_text(encoding="utf-8")
    match = re.search(r"^url_encode\(\) \{\n(?:.*\n)*?^\}\n", text, re.MULTILINE)
    assert match, "url_encode() function not found in entrypoint.sh"
    return match.group(0)


def _git(*args, cwd):
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def seeded(tmp_path):
    work, bare = tmp_path / "w", tmp_path / "r.git"
    work.mkdir()
    _git("init", "-b", "main", cwd=work)
    _git("config", "user.email", "t@e.com", cwd=work)
    _git("config", "user.name", "t", cwd=work)
    _git("config", "commit.gpgsign", "false", cwd=work)
    (work / "app.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    _git("add", ".", cwd=work)
    _git("commit", "-m", "base", cwd=work)
    _git("clone", "--bare", str(work), str(bare), cwd=tmp_path)
    _git("remote", "add", "origin", str(bare), cwd=work)
    _git("push", "origin", "main", cwd=work)
    # Simulate the worker: a branch with a fix.
    _git("checkout", "-b", "agent/fix", cwd=work)
    (work / "app.py").write_text("def f():\n    return 2\n", encoding="utf-8")
    _git("commit", "-am", "fix the return", cwd=work)
    _git("push", "origin", "agent/fix", cwd=work)
    return bare


@pytest.mark.integration
async def test_the_full_local_path_preflight_diff_merge(seeded):
    repo_url = str(seeded)

    # 1. Preflight passes with no credential at all.
    assert (
        await preflight_remote(
            git=None,
            repo_url=repo_url,
            base="main",
            branch="agent/fix",
            credential_configured=False,
        )
        == []
    )

    # 2. The backend resolves to local and produces a reviewable diff.
    backend = resolve_backend(repo_url, git_ops=None)
    assert backend.name == "local"
    ref = PullRequestRef(backend="local", branch="agent/fix", base="main")
    diff = await backend.get_diff(ref)
    assert "return 2" in diff
    assert "return 1" in diff

    # 3. Merge lands the change on main and removes the branch.
    await backend.merge(ref)
    assert "agent/fix" not in _git("branch", "--list", cwd=seeded)
    blob = _git("show", "main:app.py", cwd=seeded)
    assert "return 2" in blob


@pytest.mark.integration
async def test_a_pr_url_survives_a_round_trip_through_storage(seeded):
    ref = PullRequestRef(backend="local", branch="agent/fix", base="main")
    stored = ref.to_url()
    assert PullRequestRef.from_url(stored) == ref
    backend = resolve_backend(str(seeded), git_ops=None)
    assert "return 2" in await backend.get_diff(PullRequestRef.from_url(stored))


@pytest.mark.integration
def test_the_shipped_url_encode_agrees_with_the_python_parser():
    """The shell half and the Python half of the praxis-local:// contract.

    Extracts the real `url_encode` function out of the shipped
    entrypoint.sh and runs it under bash on a branch name containing a
    character that actually matters ('&'), then asserts
    PullRequestRef.from_url parses the resulting URL back to the exact
    (branch, base) pair. This is the seam where the shell and the Python
    parser have to agree; nothing else in the phase tests both halves
    together against the real file.

    A bare ``["bash", ...]`` in ``subprocess.run`` finds the WSL launcher on
    Windows rather than Git Bash and fails regardless of the script, so bash
    is resolved with ``shutil.which`` and invoked by its absolute path.
    """
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash not available on PATH")

    branch = "agent/fix&more"
    base = "main"
    script = (
        "set -e\n"
        + _extract_url_encode(_ENTRYPOINT_SH)
        + 'printf "praxis-local://pr?branch=%s&base=%s" '
        '"$(url_encode "$1")" "$(url_encode "$2")"\n'
    )
    result = subprocess.run(
        [bash, "-c", script, "_", branch, base],
        check=True,
        capture_output=True,
        text=True,
    )
    url = result.stdout.strip()

    ref = PullRequestRef.from_url(url)
    assert ref.branch == branch
    assert ref.base == base
