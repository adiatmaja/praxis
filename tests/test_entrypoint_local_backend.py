"""The entrypoints must skip gh and credential setup when GIT_BACKEND=local.

The invariant, stated once: in local mode the entrypoint makes ZERO ``gh``
calls and emits a ``pr_url`` that ``PullRequestRef.from_url`` parses back to
the exact ``(branch, base)`` pair; in github mode (and when GIT_BACKEND is
unset) it does the opposite, configuring a credential helper and going through
``gh``. Both halves fail SILENTLY: a stray ``gh`` call is swallowed by
``2>/dev/null``, and a malformed URL still reports ``status=completed`` while
the reviewable change vanishes.

Static greps cannot pin that. Asserting the string ``GIT_BACKEND`` appears
within N lines of a ``gh`` call measures PROXIMITY, not CONTAINMENT, and says
nothing about POLARITY: inverting a guard, or closing it early so every ``gh``
call runs unconditionally, leaves such a test green. So the guards below are
tested by EXECUTING the real sliced regions of the shipped file under bash with
``gh`` and ``git`` replaced by spies. The static tests are kept only as the
cheap early signal, since an entrypoint change needs an agent IMAGE REBUILD.
"""

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from orchestrator.core.git_backend import PullRequestRef


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
def test_the_backend_is_reduced_to_one_boolean_before_any_guard(path):
    """GIT_BACKEND is read exactly twice: to default it, and to derive the flag.

    The two guards used to test opposite sides of DIFFERENT values (the
    credential helper on ``= "github"``, the PR block on ``= "local"``), so any
    third value -- a typo, a future backend, an image lagging the orchestrator
    -- got NO credential helper AND the full ``gh`` path, the worst possible
    combination. Freezing the two call sites here is what keeps them from
    drifting apart again; every other spot must test ``IS_LOCAL_BACKEND``.
    """
    lines = path.read_text(encoding="utf-8").splitlines()
    refs = [
        line
        for line in lines
        if "GIT_BACKEND" in line and not line.lstrip().startswith("#")
    ]
    assert refs == [
        'GIT_BACKEND="${GIT_BACKEND:-github}"',
        'if [ "${GIT_BACKEND}" = "local" ]; then',
    ], "GIT_BACKEND must be collapsed to IS_LOCAL_BACKEND once, then never re-read"


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
    assert "IS_LOCAL_BACKEND" in window, (
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
    # A proximity window, so it has to be wide enough to survive comments being
    # added to the PR block above it. What it actually measures is nearness, not
    # containment; containment is what the EXECUTED tests at the bottom of this
    # file prove, and this stays only as the cheap early signal.
    window = "\n".join(text.splitlines()[max(pr_line - 45, 0) : pr_line])
    assert "IS_LOCAL_BACKEND" in window, (
        "gh pr create must be inside a github-only guard"
    )


@pytest.mark.unit
@pytest.mark.parametrize("path", ENTRYPOINTS, ids=lambda p: p.parent.name)
def test_pr_reuse_lookup_is_guarded_by_the_backend(path):
    """The reuse lookup is a gh call too, so it must be inside the same guard.

    Guarding only `gh pr create` would still shell out to the lookup first,
    which in local mode has no credential and no GitHub remote. It fails
    quietly (`2>/dev/null`, `|| true`), so the run would look healthy and then
    open a PR against a repo that does not exist.

    The lookup is `gh pr list --state open`, never `gh pr view <branch>`;
    which state it asks about is pinned in `test_entrypoint_pr_reuse`.
    """
    text = path.read_text(encoding="utf-8")
    lookup_line = next(
        (i for i, line in enumerate(text.splitlines()) if "gh pr list" in line),
        None,
    )
    assert lookup_line is not None
    window = "\n".join(text.splitlines()[max(lookup_line - 40, 0) : lookup_line])
    assert "IS_LOCAL_BACKEND" in window, (
        "the PR reuse lookup must be inside a github-only guard"
    )


@pytest.mark.unit
@pytest.mark.parametrize("path", ENTRYPOINTS, ids=lambda p: p.parent.name)
def test_local_mode_reports_a_praxis_local_pr_url(path):
    """Anchored to the ASSIGNMENT, not to the string appearing anywhere.

    ``praxis-local://pr`` also occurs inside the ``url_encode`` comment block,
    so a bare substring check stayed green when the real ``PR_URL=`` assignment
    was deleted.
    """
    lines = path.read_text(encoding="utf-8").splitlines()
    assert any(
        line.strip().startswith('PR_URL="praxis-local://pr?branch=') for line in lines
    ), "the local branch must ASSIGN PR_URL, not merely mention the scheme"


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

    The contract is ``PullRequestRef.from_url``. A branch containing ``&``
    terminates the ``([^&]+)`` group early and makes the whole URL
    unparseable, and a raw ``%`` is mis-decoded into a different branch name.
    Either way the worker still reports success and the reviewable ref is
    silently lost, so both characters are pinned here.
    """
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
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash is not available on this host")
    result = subprocess.run([bash, "-n", str(path)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


# --------------------------------------------------------------------------
# Executable guard tests.
#
# Everything above reads the source. Everything below RUNS it: the real
# regions are sliced out of the shipped file, concatenated IN SOURCE ORDER,
# and executed under bash with `gh` and `git` replaced by spies on PATH.
# Source order is load-bearing -- it is what makes "the GIT_BACKEND default
# sits above its first use" testable at all, since moving it below leaves an
# unbound variable under the file's own `set -u`.
# --------------------------------------------------------------------------

_TEST_BRANCH = "agent/local&mode"
_TEST_BASE = "plan/2026-08-06-bench"
_FAKE_PR_URL = "https://github.test/owner/repo/pull/7"

# The reuse lookup must come back EMPTY (no open PR exists yet) or
# `gh pr create` is never reached, leaving the create path untested. `gh pr
# list` prints nothing and exits 0 in that case, which is what the fall-through
# below reproduces.
_SPY_GH = """\
#!/usr/bin/env bash
printf '%s\\n' "gh $*" >> "$PRAXIS_SPY_LOG"
if [ "${1:-}" = "pr" ] && [ "${2:-}" = "create" ]; then
    printf '%s\\n' "PLACEHOLDER_PR_URL"
fi
exit 0
"""

_SPY_GIT = """\
#!/usr/bin/env bash
printf '%s\\n' "git $*" >> "$PRAXIS_SPY_LOG"
exit 0
"""

_PREAMBLE = f"""\
set -euo pipefail
BRANCH="{_TEST_BRANCH}"
BASE_BRANCH="{_TEST_BASE}"
PR_URL=""
MODEL="a-test-model"
GH_TOKEN="placeholder-token"
TASK_SUMMARY="a task summary"
"""

_POSTAMBLE = """
printf '__PRAXIS_PR_URL__=%s\\n' "${PR_URL}"
"""


def _to_posix(path: Path) -> str:
    """Convert a Windows absolute path to the MSYS form Git Bash reads.

    `Path.as_posix()` is NOT good enough for a PATH entry: it yields
    ``C:/Users/...``, and PATH is colon-separated, so the drive letter becomes
    its own entry and the rest becomes a bogus ``/Users/...`` one. The spy dir
    then is not on PATH at all and the real binary answers.
    """
    text = str(path)
    if len(text) >= 2 and text[1] == ":":
        return "/" + text[0].lower() + text[2:].replace("\\", "/")
    return text.replace("\\", "/")


def _find(lines: list[str], predicate, what: str) -> int:
    for i, line in enumerate(lines):
        if predicate(line):
            return i
    message = f"{what} not found in the entrypoint"
    raise AssertionError(message)


def _closing(lines: list[str], start: int, token: str) -> int:
    """Index of the first column-0 ``token`` at or after ``start``."""
    for i in range(start, len(lines)):
        if lines[i] == token:
            return i
    message = f"no closing {token!r} after line {start + 1}"
    raise AssertionError(message)


def _regions(lines: list[str]) -> list[tuple[int, int]]:
    """Line spans of every region the guard invariant depends on.

    Returned sorted by start line so the assembled script preserves SOURCE
    ORDER. That is the whole point of slicing rather than retyping: a region
    that moves in the file moves in the test.
    """
    default = _find(
        lines, lambda ln: ln.startswith("GIT_BACKEND="), "the GIT_BACKEND default"
    )

    flag = _find(
        lines,
        lambda ln: ln.startswith("IS_LOCAL_BACKEND="),
        "the IS_LOCAL_BACKEND derivation",
    )

    enc = _find(lines, lambda ln: ln.startswith("url_encode()"), "url_encode()")

    helper = _find(lines, lambda ln: "credential.helper" in ln, "the credential helper")
    helper_guard = max(i for i in range(helper) if lines[i].startswith("if "))

    local_assign = _find(
        lines,
        lambda ln: 'PR_URL="praxis-local://pr' in ln,
        "the local PR_URL assignment",
    )
    pr_guard = max(i for i in range(local_assign) if lines[i].startswith("if "))
    # The PR block's end is anchored on the trailing banner, NOT on brace
    # matching. Closing the guard early (turning its `else` into a `fi` and
    # dropping the trailing `fi`) leaves every gh call unconditional; a
    # brace-matched slice would stop at the injected `fi` and never include
    # those calls, so the mutation would escape.
    banner = _find(lines, lambda ln: "completed ===" in ln, "the completion banner")

    regions = [
        (default, default),
        (flag, _closing(lines, flag, "fi")),
        (enc, _closing(lines, enc, "}")),
        (helper_guard, _closing(lines, helper, "fi")),
        (pr_guard, banner - 1),
    ]
    return sorted(regions)


def _assemble(path: Path) -> str:
    lines = path.read_text(encoding="utf-8").splitlines()
    body = "\n".join(
        "\n".join(lines[start : end + 1]) for start, end in _regions(lines)
    )
    return _PREAMBLE + body + _POSTAMBLE


def _run_regions(path: Path, tmp_path: Path, backend: str | None):
    """Execute the sliced regions with gh/git spied; return (result, calls, url)."""
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash is not available on this host")

    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "spy.log"
    for name, body in (("gh", _SPY_GH), ("git", _SPY_GIT)):
        spy = bindir / name
        # newline="\n" is required: a CRLF shebang makes the spy unrunnable
        # ("bad interpreter"), which would look like the entrypoint's fault.
        spy.write_text(
            body.replace("PLACEHOLDER_PR_URL", _FAKE_PR_URL),
            encoding="utf-8",
            newline="\n",
        )
        spy.chmod(0o755)

    env = {**os.environ}
    env["PATH"] = f"{bindir}{os.pathsep}{env.get('PATH', '')}"
    env["PRAXIS_SPY_LOG"] = log.as_posix()
    # ...and again INSIDE the script, which is the one that actually holds on
    # Windows.  Git for Windows' `bash.exe` is a wrapper that rewrites PATH at
    # startup, putting its own `/mingw64/bin` ahead of whatever the parent set,
    # so the real `git.exe` shadowed the spy and the run silently exercised the
    # host's git instead.  `gh` has no counterpart in that directory, which is
    # why only the git half failed and only on the CI runner.
    spy_path_line = f'export PATH="{_to_posix(bindir)}:$PATH"\n'
    env.pop("GIT_BACKEND", None)
    if backend is not None:
        env["GIT_BACKEND"] = backend

    result = subprocess.run(
        [bash, "-c", spy_path_line + _assemble(path)],
        capture_output=True,
        text=True,
        env=env,
    )
    calls = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
    urls = re.findall(r"^__PRAXIS_PR_URL__=(.*)$", result.stdout, re.MULTILINE)
    return result, calls, (urls[-1] if urls else None)


@pytest.mark.integration
@pytest.mark.parametrize("path", ENTRYPOINTS, ids=lambda p: p.parent.name)
def test_local_backend_makes_zero_gh_calls_and_emits_a_parseable_ref(path, tmp_path):
    """The load-bearing half, executed rather than grepped.

    A stray ``gh`` call in local mode is swallowed by ``2>/dev/null`` and then
    clobbers ``PR_URL`` to empty, so the failure is invisible in the logs; only
    a spy on PATH can see it. The emitted ref is parsed by the REAL
    ``PullRequestRef.from_url``, which is what the review loop calls.
    """
    result, calls, url = _run_regions(path, tmp_path, "local")

    assert result.returncode == 0, result.stderr
    assert [c for c in calls if c.startswith("gh ")] == [], (
        f"local mode must make ZERO gh calls, got: {calls}"
    )
    assert [c for c in calls if "credential.helper" in c] == [], (
        f"local mode must configure no credential helper, got: {calls}"
    )

    assert url, f"no PR_URL emitted; stdout={result.stdout!r}"
    ref = PullRequestRef.from_url(url)
    assert ref.backend == "local"
    assert ref.branch == _TEST_BRANCH
    assert ref.base == _TEST_BASE


@pytest.mark.integration
@pytest.mark.parametrize("path", ENTRYPOINTS, ids=lambda p: p.parent.name)
def test_github_backend_configures_the_credential_helper_and_creates_a_pr(
    path, tmp_path
):
    """The other side of the same guards.

    Pinning only local mode leaves an inverted guard green, because an
    inversion breaks github mode instead: no credential helper (so every
    private-repo clone and push fails) and a ``praxis-local://`` ref reported
    for a change that has no PR.
    """
    result, calls, url = _run_regions(path, tmp_path, "github")

    assert result.returncode == 0, result.stderr
    assert any("credential.helper" in c for c in calls), (
        f"github mode must configure the credential helper, got: {calls}"
    )
    assert any(c.startswith("gh pr create") for c in calls), (
        f"github mode must create a PR through gh, got: {calls}"
    )
    assert url == _FAKE_PR_URL, f"github mode reported {url!r}"


@pytest.mark.integration
@pytest.mark.parametrize("path", ENTRYPOINTS, ids=lambda p: p.parent.name)
def test_unset_git_backend_behaves_exactly_like_github(path, tmp_path):
    """The default must be applied ABOVE its first use.

    The file runs under ``set -u``, so an unset ``GIT_BACKEND`` read before the
    ``:-github`` default aborts the whole run with ``unbound variable`` before
    the clone, and ``cleanup`` reports ``failed``. Nothing else covers this:
    ``agent_manager.py`` always sets the variable, and the promise being kept
    here is the older-orchestrator case.
    """
    result, calls, url = _run_regions(path, tmp_path, None)

    assert "unbound variable" not in result.stderr, result.stderr
    assert result.returncode == 0, result.stderr
    assert any("credential.helper" in c for c in calls), (
        f"an unset backend must default to github, got: {calls}"
    )
    assert any(c.startswith("gh pr create") for c in calls), (
        f"an unset backend must default to github, got: {calls}"
    )
    assert url == _FAKE_PR_URL, f"an unset backend reported {url!r}"
