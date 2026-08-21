"""What the harness entrypoints REPORT has to be true of what they did.

These two scripts are the product's mouth. They post the status the whole state
machine acts on, and their `echo` lines are literally what `praxis logs
<task-id>` shows an operator. So every echo is an assertion and every posted
status is a verdict, and the failure mode here is the same one that costs a
walkthrough its score everywhere else: a surface reporting something that did
not happen.

Two rules shape this file.

**Shell must be EXECUTED.** `bash -n` proves syntax and nothing else, and every
defect below is a RUNTIME one: a capture that came back empty, a string that
was not a number, a command that exited non-zero. Each scenario drives the real
code with stubbed `git`/`gh`/`curl`/`json_escape` whose exit codes and stdout
are the variable under test.

**The slice is extracted from the SHIPPED file, never copied into this file.**
A test holding its own copy of the snippet passes forever while the script it
describes drifts away underneath it, which is the defect one layer up from the
ones being fixed.

Every scenario runs against BOTH entrypoints. They are line-for-line identical
across most of this surface, so a fix landing on one and not the other is
itself an instance of the class.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[1]
ENTRYPOINTS = {
    "opencode": REPO / "docker" / "opencode-agent" / "entrypoint.sh",
    "agy": REPO / "docker" / "agy-agent" / "entrypoint.sh",
}
EXTRACTOR = REPO / "docker" / "agy-agent" / "extract_session.py"


def _check_bash() -> str | None:
    bash_path = shutil.which("bash")
    if not bash_path:
        return None
    try:
        res = subprocess.run(  # noqa: S603
            [bash_path, "-c", "echo 1"], capture_output=True, text=True, timeout=5
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return bash_path if res.returncode == 0 else None


_BASH = _check_bash()
pytestmark = pytest.mark.skipif(
    _BASH is None, reason="bash unavailable or non-functional"
)


def _slice(harness: str, start: str, seen: str, stop: str) -> str:
    """Return the shipped lines from ``start`` through the ``stop`` after ``seen``.

    Three anchors rather than two because every block here contains an inner
    ``fi``/``esac`` that matches the closing pattern early. ``seen`` is a line
    that can only appear inside the block, so ``stop`` is only armed once the
    extraction is genuinely past the inner structure.

    Raises:
        AssertionError: If an anchor does not match. A silently empty slice
            would run zero lines and pass every assertion below.
    """
    lines = ENTRYPOINTS[harness].read_text(encoding="utf-8").splitlines()
    out: list[str] = []
    started = armed = False
    for line in lines:
        if not started and line == start:
            started = True
        if not started:
            continue
        out.append(line)
        if line.strip().startswith(seen) or seen in line:
            armed = True
        elif armed and line == stop:
            break
    assert started, f"{harness}: start anchor never matched: {start!r}"
    assert armed, f"{harness}: mid anchor never matched: {seen!r}"
    assert out[-1] == stop, f"{harness}: stop anchor never matched: {stop!r}"
    return "\n".join(out)


def _run(script: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    """Execute a script through bash, never with the praxis repo as cwd.

    A hand-run entrypoint slice has previously committed to the developer's own
    repository, so the working directory is always a tmp_path.
    """
    return subprocess.run(  # noqa: S603
        [_BASH, "-s"],
        input=script,
        capture_output=True,
        text=True,
        cwd=str(cwd),
        timeout=60,
        check=False,
    )


def _callback_slice(harness: str) -> str:
    return _slice(
        harness,
        start="# Escape a value for JSON, or render the bare word `null` if it cannot be.",
        seen="send_callback() {",
        stop="trap cleanup EXIT",
    )


def _checkpoint_slice(harness: str) -> str:
    return _slice(
        harness,
        start="    ahead=0",
        seen="checkpoint rev-list failed",
        stop="    fi",
    )


def _pr_slice(harness: str) -> str:
    return _slice(
        harness,
        start='echo "--- Creating PR ---"',
        seen='echo "PR created: ',
        stop="fi",
    )


HARNESSES = pytest.mark.parametrize("harness", sorted(ENTRYPOINTS))


@pytest.mark.integration
@HARNESSES
def test_a_failed_callback_reports_one_status_code_not_two(
    harness: str, tmp_path: Path
) -> None:
    """`curl` writes "000" to stdout AND exits non-zero on a connection failure.

    With `|| echo "000"` INSIDE the command substitution both landed in the
    variable, and the operator read `failed (HTTP 000000)`: a status code that
    does not exist, for the one failure they most need to recognise.
    """
    script = (
        "set -euo pipefail\n"
        'curl() { echo -n "000"; return 7; }\n'
        'STATUS="completed"; PR_URL="https://x/1"; TASK_ID="t1"\n'
        'CALLBACK_URL="http://o"; QUESTION=""; CAPTURED_SESSION_ID=""\n'
        'TOKENS_USED=""; CALLBACK_MAX_ATTEMPTS=1\n'
        f"{_callback_slice(harness)}\n"
        "send_callback\n"
    )
    out = _run(script, tmp_path).stdout
    assert "(HTTP 000)" in out
    assert "000000" not in out


@pytest.mark.integration
@HARNESSES
def test_an_escaping_failure_degrades_a_field_not_the_delivery(
    harness: str, tmp_path: Path
) -> None:
    """`json_escape` shells out to python3, and the call sites were plain
    assignments under `set -e`.

    One failing interpreter aborted `send_callback` MIDWAY, so no callback was
    posted at all while the run's own log still ended "PR created: <url>" and
    "agent completed". The task hung to the reconcile sweep with its last two
    lines asserting success. Delete `escape_or_null` and only this goes red.
    """
    marker = tmp_path / "delivered"
    script = (
        "set -euo pipefail\n"
        f'MARK="{marker.as_posix()}"\n'
        'curl() { echo delivered >> "${MARK}"; echo -n "200"; return 0; }\n'
        "json_escape() { return 1; }\n"
        'STATUS="completed"; PR_URL="https://x/1"; TASK_ID="t1"\n'
        'CALLBACK_URL="http://o"; QUESTION=""; CAPTURED_SESSION_ID=""\n'
        'TOKENS_USED=""; CALLBACK_MAX_ATTEMPTS=1\n'
        f"{_callback_slice(harness)}\n"
        # Redefined AFTER the slice so the stub wins: escape_or_null resolves
        # json_escape at call time. Filtering it out of the slice instead is
        # what a copied snippet would tempt you into, and it silently ate the
        # closing brace of every other function in the block.
        "json_escape() { return 1; }\n"
        "trap - EXIT\n"
        "send_callback\necho REACHED_END\n"
    )
    result = _run(script, tmp_path)
    assert "REACHED_END" in result.stdout
    assert marker.exists(), "the callback was never posted"
    assert "json escaping failed" in result.stdout + result.stderr


@pytest.mark.integration
@HARNESSES
def test_an_empty_rev_list_suppresses_the_session_id(
    harness: str, tmp_path: Path
) -> None:
    """A session id may only be reported once its checkpoint is on the remote.

    `git rev-list --count` can exit 0 printing nothing, and `[ "" -gt 0 ]` is
    not false, it is `integer expression expected`, which bash then reads as
    false. The push was skipped while `checkpoint_ok` stayed 1, so the id was
    reported anyway: the next turn resumes the model's memory of edits that
    only ever existed in a destroyed container, against a branch rebuilt from
    base. The commit block fifty lines below already validated this; the
    checkpoint block did not.
    """
    script = (
        "set -euo pipefail\n"
        'BASE_BRANCH="plan/y"\ncheckpoint_ok=1\n'
        'git() { echo -n ""; return 0; }\n'
        f"{_checkpoint_slice(harness)}\n"
        'echo "RESULT ok=${checkpoint_ok} ahead=${ahead}"\n'
    )
    result = _run(script, tmp_path)
    assert "RESULT ok=0" in result.stdout
    assert "integer expression" not in result.stderr


@pytest.mark.integration
@HARNESSES
def test_a_numeric_rev_list_still_permits_the_checkpoint_push(
    harness: str, tmp_path: Path
) -> None:
    """The other branch, so a guard that simply always suppresses cannot pass."""
    script = (
        "set -euo pipefail\n"
        'BASE_BRANCH="plan/y"\ncheckpoint_ok=1\n'
        "git() { echo 3; return 0; }\n"
        f"{_checkpoint_slice(harness)}\n"
        'echo "RESULT ok=${checkpoint_ok} ahead=${ahead}"\n'
    )
    assert "RESULT ok=1 ahead=3" in _run(script, tmp_path).stdout


@pytest.mark.integration
@HARNESSES
def test_an_empty_pr_url_is_never_reported_as_a_created_pr(
    harness: str, tmp_path: Path
) -> None:
    """`gh pr create` can exit 0 having printed NOTHING.

    The old code echoed "PR created: " with an empty url and ran on to a normal
    exit, so the callback said `completed` with `pr_url: null` while the log an
    operator reads through `praxis logs` said the PR existed and the agent had
    finished. The orchestrator's `completed_without_pr` guard turns that into a
    failure, so the dashboard was right and the log was wrong about one run.
    """
    script = (
        "set -euo pipefail\n"
        'BRANCH="agent/x"; BASE_BRANCH="plan/y"; MODEL="m"; TASK_SUMMARY="s"\n'
        'gh() { echo -n ""; return 0; }\n'
        f"{_pr_slice(harness)}\n"
        "echo RAN_ON\n"
    )
    result = _run(script, tmp_path)
    assert "printed no PR URL" in result.stdout
    assert "PR created: " not in result.stdout
    assert "RAN_ON" not in result.stdout
    assert result.returncode != 0


@pytest.mark.integration
@HARNESSES
def test_a_reused_pr_is_not_reported_as_a_creation(
    harness: str, tmp_path: Path
) -> None:
    """ "Creating PR" then "Reusing existing open PR" then "PR created" is two
    lines of three asserting something that did not happen, and on a retry it
    made a reused PR indistinguishable from a fresh one."""
    script = (
        "set -euo pipefail\n"
        'BRANCH="agent/x"; BASE_BRANCH="plan/y"; MODEL="m"; TASK_SUMMARY="s"\n'
        'gh() { if [ "$1 $2" = "pr list" ]; then echo "https://x/7";'
        ' else echo "https://x/NEW"; fi; return 0; }\n'
        f"{_pr_slice(harness)}\n"
    )
    out = _run(script, tmp_path).stdout
    assert "Reusing existing open PR: https://x/7" in out
    assert "PR created" not in out


@pytest.mark.integration
@HARNESSES
def test_a_real_creation_is_still_reported(harness: str, tmp_path: Path) -> None:
    """The success branch, so the two guards above cannot pass by silencing it."""
    script = (
        "set -euo pipefail\n"
        'BRANCH="agent/x"; BASE_BRANCH="plan/y"; MODEL="m"; TASK_SUMMARY="s"\n'
        'gh() { if [ "$1 $2" = "pr list" ]; then echo -n "";'
        ' else echo "https://x/NEW"; fi; return 0; }\n'
        f"{_pr_slice(harness)}\n"
    )
    assert "PR created: https://x/NEW" in _run(script, tmp_path).stdout


@pytest.mark.integration
def test_the_agy_extractor_fails_closed_on_an_unrecognized_body_key() -> None:
    """An envelope it cannot fully read must exit non-zero, printing nothing.

    Exiting 0 with only the conversation id handed the entrypoint an EMPTY
    transcript and suppressed the RAW_LOG fallback that exists for exactly this
    case. Downstream that is not a degraded run but a wrong one: the `Status:`
    grep finds no BLOCKED line so a worker's question is destroyed and a PR of
    half-done work goes to review, and the no-changes block reads zero bytes so
    a satisfied tree is called a failed run. The file's own docstring and
    `docs/gotchas.md` both already claimed it failed closed.
    """
    import sys

    result = subprocess.run(  # noqa: S603
        [sys.executable, str(EXTRACTOR)],
        input='{"conversation_id":"c1","status":"ok","result":"Status: BLOCKED"}',
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert result.stdout.strip() == ""


@pytest.mark.integration
def test_the_agy_extractor_still_splits_a_recognized_envelope() -> None:
    """The success branch, so "always exit 1" cannot pass the test above."""
    import sys

    result = subprocess.run(  # noqa: S603
        [sys.executable, str(EXTRACTOR)],
        input='{"conversation_id":"c1","response":"Status: DONE"}',
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert result.stdout.splitlines()[0] == "c1"
    assert "Status: DONE" in result.stdout
