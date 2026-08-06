"""The entrypoints must skip gh and credential setup when GIT_BACKEND=local.

These are static assertions over the shell source: an entrypoint change needs
an agent IMAGE REBUILD, so a source-level test is the cheap early signal.
"""

from pathlib import Path

import pytest


ENTRYPOINTS = [
    Path(__file__).resolve().parents[1] / "docker" / h / "entrypoint.sh"
    for h in ("opencode-agent", "agy-agent")
]


@pytest.mark.unit
@pytest.mark.parametrize("path", ENTRYPOINTS, ids=lambda p: p.parent.name)
def test_entrypoint_reads_git_backend(path):
    assert "GIT_BACKEND" in path.read_text(encoding="utf-8")


@pytest.mark.unit
@pytest.mark.parametrize("path", ENTRYPOINTS, ids=lambda p: p.parent.name)
def test_credential_helper_is_guarded_by_the_backend(path):
    text = path.read_text(encoding="utf-8")
    helper_line = next(
        (i for i, line in enumerate(text.splitlines()) if "credential.helper" in line),
        None,
    )
    assert helper_line is not None
    window = "\n".join(text.splitlines()[max(helper_line - 6, 0) : helper_line])
    assert "GIT_BACKEND" in window, (
        "the credential helper must be inside a github-only guard"
    )


@pytest.mark.unit
@pytest.mark.parametrize("path", ENTRYPOINTS, ids=lambda p: p.parent.name)
def test_pr_creation_is_guarded_by_the_backend(path):
    text = path.read_text(encoding="utf-8")
    pr_line = next(
        (i for i, line in enumerate(text.splitlines()) if "gh pr create" in line),
        None,
    )
    assert pr_line is not None
    window = "\n".join(text.splitlines()[max(pr_line - 25, 0) : pr_line])
    assert "GIT_BACKEND" in window, "gh pr create must be inside a github-only guard"


@pytest.mark.unit
@pytest.mark.parametrize("path", ENTRYPOINTS, ids=lambda p: p.parent.name)
def test_pr_reuse_lookup_is_guarded_by_the_backend(path):
    """`gh pr view` is a gh call too, so it must be inside the same guard.

    Guarding only `gh pr create` would still shell out to `gh pr view` first,
    which in local mode has no credential and no GitHub remote. It fails
    quietly (`2>/dev/null`, `|| else`), so the run would look healthy and then
    open a PR against a repo that does not exist.
    """
    text = path.read_text(encoding="utf-8")
    view_line = next(
        (i for i, line in enumerate(text.splitlines()) if "gh pr view" in line),
        None,
    )
    assert view_line is not None
    window = "\n".join(text.splitlines()[max(view_line - 25, 0) : view_line])
    assert "GIT_BACKEND" in window, "gh pr view must be inside a github-only guard"


@pytest.mark.unit
@pytest.mark.parametrize("path", ENTRYPOINTS, ids=lambda p: p.parent.name)
def test_local_mode_reports_a_praxis_local_pr_url(path):
    assert "praxis-local://pr" in path.read_text(encoding="utf-8")


def _slice_function(source: str, name: str) -> str:
    """Return the shell source of ``name``, from its header to its closing brace."""
    lines = source.splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.startswith(f"{name}()"))
    end = next(i for i in range(start, len(lines)) if lines[i] == "}")
    return "\n".join(lines[start : end + 1])


@pytest.mark.unit
@pytest.mark.parametrize("path", ENTRYPOINTS, ids=lambda p: p.parent.name)
def test_local_pr_url_round_trips_through_the_orchestrator_parser(path):
    """Execute the REAL encoder and PR_URL line, then parse with the REAL parser.

    Every other test here only reads the source. This one runs the shipped
    shell, which is the point: ``bash -n`` proves syntax, not behavior.

    The contract is ``PullRequestRef.from_url``. A branch containing ``&``
    terminates the ``([^&]+)`` group early and makes the whole URL
    unparseable, and a raw ``%`` is mis-decoded into a different branch name.
    Either way the worker still reports success and the reviewable ref is
    silently lost, so both characters are pinned here.
    """
    import shutil
    import subprocess

    from orchestrator.core.git_backend import PullRequestRef

    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash is not available on this host")

    source = path.read_text(encoding="utf-8")
    pr_line = next(
        ln for ln in source.splitlines() if 'PR_URL="praxis-local://pr' in ln
    )
    script = "\n".join(
        [
            _slice_function(source, "url_encode"),
            'BRANCH="$1"',
            'BASE_BRANCH="$2"',
            pr_line.strip(),
            'printf %s "$PR_URL"',
        ]
    )

    cases = [
        ("agent/my-leaf", "plan/2026-08-06-bench"),
        ("agent/my leaf", "main"),
        ("feat/a&b", "plan/x&y"),
        ("feat/100%-done", "main"),
        ("plain", "main"),
    ]
    for branch, base in cases:
        result = subprocess.run(
            [bash, "-c", script, "entrypoint", branch, base],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
        ref = PullRequestRef.from_url(result.stdout)
        assert ref.backend == "local"
        assert ref.branch == branch, f"{result.stdout} lost the branch"
        assert ref.base == base, f"{result.stdout} lost the base"


@pytest.mark.unit
@pytest.mark.parametrize("path", ENTRYPOINTS, ids=lambda p: p.parent.name)
def test_entrypoint_is_valid_shell(path):
    """A syntax check only; it proves nothing about behavior.

    Resolve bash via ``shutil.which`` and invoke the ABSOLUTE path. Passing a
    bare ``"bash"`` to ``subprocess`` on Windows is not the same lookup:
    ``CreateProcess`` searches ``System32`` before ``PATH``, so it finds the
    WSL launcher shim and fails with ``execvpe(/bin/bash)`` regardless of the
    script's contents, i.e. a permanent false failure.
    """
    import shutil
    import subprocess

    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash is not available on this host")
    result = subprocess.run([bash, "-n", str(path)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
