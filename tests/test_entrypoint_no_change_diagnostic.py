"""A no-change worker run must EXPLAIN itself, not merely assert one.

The invariant, stated once: when the harness exits cleanly and the branch ends
up with nothing to review, the entrypoint prints the evidence that separates
"the harness emitted nothing" from "the harness said something and still shipped
no patch" from "the harness refused". That evidence is the harness return code,
the byte size of ``OUTPUT_LOG``, the parsed ``report_status`` (or an explicit
``none`` when the harness printed no ``Status:`` line) and the tail of the
transcript. The agy entrypoint adds the two envelope fields only it has,
``status`` and ``num_turns``.

It fails SILENTLY. The container is destroyed seconds after this line runs, so
anything not printed here is gone for good: observed live 2026-08-14, three
consecutive attempts each printed ``[PRAXIS PHASE] understanding`` and then one
bare sentence, and nothing anywhere recorded which of the three shapes it was.

Since run #5 the entrypoint also SPLITS that evidence into two outcomes,
because they are not the same event and calling both ``failed`` broke plans:

- Transcript non-empty: the harness ran and the tree already satisfied the
  task. Reported as ``no_changes``, exit 0. The orchestrator then decides
  whether that is a legitimate no-op by running the project's verify command
  against the base branch; the container never makes that call itself.
- Transcript empty: nothing ran. Still ``failed``, exit 1. Without this half,
  a dead worker would silently close every leaf it touched.

The first case is the one that mattered: with a plan split into "write the
module" and "write its tests", task 1 routinely writes both files, task 2
correctly has nothing to change, and the old branch retried it three times to
the identical correct answer before failing the whole plan with the repository
already in the state the spec asked for. Measured in 4 of 4 plans across BOTH
harnesses.

Note what a unit test cannot see here: "empty diff -> failed" passes both
before the fix and after a bad one. The assertions below are on the STATUS
carried to the callback, per case, which is the thing that actually changed.

A `rev-list` failure while counting commits ahead of ``BASE_BRANCH`` must not
make this silent failure worse: a bare assignment there aborts the whole
script under ``set -euo pipefail`` before a single diagnostic line prints, so
the guard around it is exercised here too.

That guard captures ``rev-list``'s stdout AND stderr together (``2>&1``), so a
successful exit alone does not prove the captured value is a clean count: a
stray advisory line on stderr lands in the same string. Trusting that blindly
with ``-gt`` is not a numeric comparison at all -- bash exits 2 ("integer
expression expected"), the surrounding ``if`` reads that as FALSE, and a
worker's real commits get reported as no changes, which is the exact
misclassification this diagnostic exists to prevent. The entrypoint therefore
validates the captured value is PURELY numeric before trusting it as a count;
this module treats "any stderr on an otherwise-successful rev-list makes the
count untrustworthy" as correct rather than attempting to strip and parse it,
and pins that choice: a successful rev-list with a stray stderr line must
still land on the diagnostic path, with the raw captured text visible.

Greps cannot pin this. Asserting the diagnostic strings appear in the file, or
appear near the guard, measures PRESENCE, not EXECUTION: the block sits behind
two nested conditions, and closing the outer one early, inverting the inner one,
or naming a variable the script never set (which under the file's own ``set -u``
aborts before a single line is printed) all leave such a test green. So the real
no-change region is sliced out of the shipped file and EXECUTED under bash with
``git`` replaced by a spy, and every assertion reads the resulting stdout.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]

# Per-harness facts the diagnostic depends on. The rc variable name is listed
# per harness ON PURPOSE and only that one is defined in the preamble: the
# sliced region runs under `set -u`, so a block that reads the OTHER harness's
# name (the obvious copy-paste when this was mirrored) dies with "unbound
# variable" instead of quietly printing nothing.
HARNESSES = {
    "opencode": {
        "dir": "opencode-agent",
        "rc_var": "opencode_rc",
        "sentence": "No changes produced by OpenCode",
        "has_envelope": False,
    },
    "agy": {
        "dir": "agy-agent",
        "rc_var": "agy_rc",
        "sentence": "No changes produced by agy",
        "has_envelope": True,
    },
}

# 40 lines so "the last 30" is distinguishable from "all of them": line-011 must
# survive and line-010 must not. A `cat` in place of the tail passes a
# presence-only assertion and fails this one.
_LOG_LINES = [f"line-{i:03d}" for i in range(1, 41)]
_LOG_TEXT = "".join(f"{line}\n" for line in _LOG_LINES)
_LOG_BYTES = len(_LOG_TEXT.encode("utf-8"))

_GOOD_ENVELOPE = (
    '{"conversation_id": "conv-7", "status": "completed", "response": "hi",'
    ' "duration_seconds": 12.5, "num_turns": 7, "usage": {"input": 1}}'
)

# The fallback path in the agy entrypoint copies RAW_LOG straight to OUTPUT_LOG
# when the envelope will not parse, so this shape really does reach the
# diagnostic in production.
_BAD_ENVELOPE = "not json at all\n[PRAXIS PHASE] understanding\n"

# `git diff --cached --quiet` exits 0 when the index is CLEAN, which is the
# no-change path; `rev-list --count` decides whether the worker committed its
# own work, and can itself be made to fail (PRAXIS_GIT_REV_LIST_EXIT) to prove
# the guard around it does not trip `set -e` and vanish. PRAXIS_GIT_REV_LIST_WARN
# makes an otherwise-SUCCESSFUL rev-list also emit a stray stderr line, which
# is the shape that would corrupt the merged `2>&1` capture with a real count
# still present. All are knobs so the non-diagnostic outcomes, the outright
# rev-list failure, and the successful-but-untrustworthy-output outcome can
# each be pinned.
_SPY_GIT = """\
#!/usr/bin/env bash
printf '%s\\n' "git $*" >> "$PRAXIS_SPY_LOG"
case "${1:-}" in
    diff) exit "${PRAXIS_GIT_DIFF_EXIT:-0}" ;;
    rev-list)
        if [ "${PRAXIS_GIT_REV_LIST_EXIT:-0}" != "0" ]; then
            printf '%s\\n' "${PRAXIS_GIT_REV_LIST_STDERR:-fatal: bad revision}" >&2
            exit "${PRAXIS_GIT_REV_LIST_EXIT}"
        fi
        if [ -n "${PRAXIS_GIT_REV_LIST_WARN:-}" ]; then
            printf '%s\\n' "${PRAXIS_GIT_REV_LIST_WARN}" >&2
        fi
        printf '%s\\n' "${PRAXIS_GIT_AHEAD:-0}"
        ;;
esac
exit 0
"""


def _to_posix(path: Path) -> str:
    """Convert a Windows absolute path to the MSYS form Git Bash reads."""
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


def _entrypoint(harness: str) -> Path:
    return REPO_ROOT / "docker" / HARNESSES[harness]["dir"] / "entrypoint.sh"


def _slice_no_change_region(harness: str) -> str:
    """Return the shipped commit/no-change block, header through its column-0 fi.

    Sliced rather than retyped so the test can never drift from the file: the
    inner ``fi`` is indented, the outer one is not, which is what makes the
    whole if/else pair, and therefore the polarity of both conditions, part of
    what runs here.
    """
    lines = _entrypoint(harness).read_text(encoding="utf-8").splitlines()
    start = _find(
        lines,
        lambda ln: ln.startswith('echo "--- Committing changes'),
        "the commit block header",
    )
    return "\n".join(lines[start : _closing(lines, start, "fi") + 1])


def _run_no_change(
    harness: str,
    tmp_path: Path,
    *,
    rc: int = 0,
    report_status: str = "",
    envelope: str = _GOOD_ENVELOPE,
    log_text: str = _LOG_TEXT,
    diff_exit: str = "0",
    ahead: str = "0",
    rev_list_exit: str = "0",
    rev_list_stderr: str = "fatal: bad revision",
    rev_list_warn: str = "",
):
    """Execute the sliced region with git spied; return the CompletedProcess."""
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash is not available on this host")

    bindir = tmp_path / "bin"
    bindir.mkdir(parents=True)
    spy = bindir / "git"
    # newline="\n" is required: a CRLF shebang makes the spy unrunnable ("bad
    # interpreter"), which would read as the entrypoint's fault.
    spy.write_text(_SPY_GIT, encoding="utf-8", newline="\n")
    spy.chmod(0o755)

    output_log = tmp_path / "output.log"
    output_log.write_text(log_text, encoding="utf-8", newline="")
    raw_log = tmp_path / "raw.log"
    raw_log.write_text(envelope, encoding="utf-8", newline="")

    preamble = [
        "set -euo pipefail",
        # Prepending the spy dir in the parent env is not enough on Windows:
        # Git for Windows' `bash.exe` is a wrapper that rewrites PATH at
        # startup and puts its own `/mingw64/bin` first, so the REAL git.exe
        # answered instead of the spy and the assertions read the host's git.
        f'export PATH="{_to_posix(bindir)}:$PATH"',
        'BRANCH="agent/no-change-leaf"',
        'BASE_BRANCH="plan/2026-08-14-diagnostics"',
        'STATUS="completed"',
        'trap \'printf "__PRAXIS_STATUS__=%s\\n" "${STATUS}"\' EXIT',
        # The no-op path reports through send_callback and then CLEARS the
        # trap, exactly as the clarification path above it does, so the trap
        # marker cannot see it. The stub is what proves the callback was sent
        # AND which status it carried, which is the real contract: an
        # entrypoint that set STATUS and exited without calling this would
        # leave the orchestrator to reconcile an orphan.
        'send_callback() { printf "__PRAXIS_CALLBACK__=%s\\n" "${STATUS}"; }',
        f'OUTPUT_LOG="{_to_posix(output_log)}"',
        f"{HARNESSES[harness]['rc_var']}={rc}",
        f'report_status="{report_status}"',
    ]
    if HARNESSES[harness]["has_envelope"]:
        preamble.append(f'RAW_LOG="{_to_posix(raw_log)}"')
    script = "\n".join([*preamble, _slice_no_change_region(harness)])

    env = {**os.environ}
    env["PATH"] = f"{bindir}{os.pathsep}{env.get('PATH', '')}"
    env["PRAXIS_SPY_LOG"] = _to_posix(tmp_path / "spy.log")
    env["PRAXIS_GIT_DIFF_EXIT"] = diff_exit
    env["PRAXIS_GIT_AHEAD"] = ahead
    env["PRAXIS_GIT_REV_LIST_EXIT"] = rev_list_exit
    env["PRAXIS_GIT_REV_LIST_STDERR"] = rev_list_stderr
    env["PRAXIS_GIT_REV_LIST_WARN"] = rev_list_warn
    # The script goes in on STDIN, never as a `bash -c` argument. Git Bash's
    # MSYS layer silently TRUNCATES a single argument at 8192 bytes: the agy
    # region crossed that ceiling and every agy case started failing with
    # "syntax error: unexpected end of file", which reads as a broken
    # entrypoint. It was not. The same script parsed clean as a file, and
    # echoing argv back showed 9507 bytes sent and 8186 received.
    #
    # Do NOT revert this to `-c` to "simplify". The ceiling is invisible until
    # crossed and it reports itself as the product's fault. This is the same
    # class as the brain's own argv limit (see docs/gotchas.md); nothing here
    # reads stdin, so `-s` costs nothing.
    return subprocess.run(
        [bash, "-s"],
        input=script,
        capture_output=True,
        text=True,
        env=env,
    )


@pytest.mark.integration
@pytest.mark.parametrize("harness", list(HARNESSES))
def test_no_change_path_reports_rc_size_status_and_a_tail(harness, tmp_path):
    """The load-bearing case: harness exited 0, transcript non-empty, no patch.

    ``report_status`` is EMPTY here, which is the shape actually observed live
    (the harness printed phase banners and no ``Status:`` line at all), so the
    diagnostic must say ``none`` rather than print a blank and lose the
    distinction between "no status" and "status parsed as empty".
    """
    result = _run_no_change(harness, tmp_path)

    assert "unbound variable" not in result.stderr, result.stderr
    assert result.returncode == 0, (
        f"a harness that ran and found nothing to do is not a failure; "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert "__PRAXIS_CALLBACK__=no_changes" in result.stdout, result.stdout
    assert "__PRAXIS_CALLBACK__=failed" not in result.stdout, result.stdout
    assert HARNESSES[harness]["sentence"] in result.stdout

    assert "harness_rc=0" in result.stdout, f"no harness rc reported: {result.stdout!r}"
    assert f"output_log_bytes={_LOG_BYTES}" in result.stdout, (
        f"no output-log size reported: {result.stdout!r}"
    )
    assert "report_status=none" in result.stdout, (
        f"an unparsed status must be reported as an explicit none: {result.stdout!r}"
    )
    assert "rev_list_failed" not in result.stdout, (
        f"rev-list succeeded here; the failure line must not appear: {result.stdout!r}"
    )

    assert "line-040" in result.stdout, (
        f"no transcript tail reported: {result.stdout!r}"
    )
    assert "line-011" in result.stdout, "the tail must be 30 lines deep"
    assert "line-010" not in result.stdout, (
        "the tail must stop at 30 lines, not dump the whole transcript"
    )


@pytest.mark.integration
@pytest.mark.parametrize("harness", list(HARNESSES))
def test_a_parsed_status_is_reported_verbatim(harness, tmp_path):
    """ "The harness said DONE and shipped nothing" is a different failure.

    Pinned separately from the empty case because a block that hardcodes
    ``none`` passes the test above and erases exactly the distinction the
    diagnostic exists to record.
    """
    result = _run_no_change(harness, tmp_path, report_status="DONE")

    assert result.returncode == 0, result.stdout
    assert "report_status=DONE" in result.stdout, result.stdout
    assert "report_status=none" not in result.stdout


@pytest.mark.integration
@pytest.mark.parametrize("harness", list(HARNESSES))
def test_the_reported_rc_is_read_not_hardcoded(harness, tmp_path):
    """On the real path the rc is always 0, so a literal ``0`` would pass.

    Both entrypoints exit early on a non-zero harness rc, so production only
    ever reaches this block with rc 0. That makes the value worth PRINTING (it
    proves the harness exited cleanly and still produced nothing, and stops a
    future reader assuming the code was masked) but it also means the happy
    case cannot tell a real read from a hardcoded literal. This case can.
    """
    result = _run_no_change(harness, tmp_path, rc=7)

    assert result.returncode == 0, result.stdout
    assert "harness_rc=7" in result.stdout, (
        f"the rc must be read from the harness variable: {result.stdout!r}"
    )


@pytest.mark.integration
@pytest.mark.parametrize("harness", list(HARNESSES))
def test_rev_list_failure_still_explains_itself(harness, tmp_path):
    """A `git rev-list` failure must not silently abort the script.

    Proved by execution, not asserted: reverting the guard around
    ``ahead=$(git rev-list ...)`` to a bare assignment makes the sliced region
    abort AT THAT LINE under ``set -e`` with rc 128 and prints nothing at all,
    not the sentence, not the rc, not the tail -- the exact silent-abort shape
    the no-change diagnostic exists to eliminate. An undeterminable count is
    treated as "no commits ahead" so the run still falls into the diagnostic
    path, and the rev-list failure itself (rc + stderr) must be reported, not
    merely swallowed into ``ahead=0``.
    """
    result = _run_no_change(
        harness,
        tmp_path,
        rev_list_exit="128",
        rev_list_stderr="fatal: bad revision 'plan/2026-08-14-diagnostics..HEAD'",
    )

    assert "unbound variable" not in result.stderr, result.stderr
    assert result.returncode == 1, (
        f"a rev-list failure must still exit non-zero and explain why: "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert "__PRAXIS_STATUS__=failed" in result.stdout, result.stdout
    assert HARNESSES[harness]["sentence"] in result.stdout, (
        f"the diagnostic sentence must still print when rev-list itself "
        f"fails: {result.stdout!r}"
    )
    assert "harness_rc=0" in result.stdout, result.stdout
    assert "output_log_bytes=" in result.stdout, result.stdout
    assert "line-040" in result.stdout, "the transcript tail must still print"

    assert "rev_list_failed=true" in result.stdout, (
        f"the rev-list failure must be reported, not swallowed: {result.stdout!r}"
    )
    # Anchored to the diagnostic's OWN rc field, not just the substring "rc=128"
    # anywhere in stdout: the WARNING line printed above the diagnostic also
    # carries the real rc, so a bare substring check would stay green even if
    # the diagnostic's own field were hardcoded to something else.
    assert "rev_list_failed=true rc=128" in result.stdout, (
        f"rev-list's own rc must be surfaced in the diagnostic line itself, "
        f"not hardcoded: {result.stdout!r}"
    )
    assert "fatal: bad revision" in result.stdout, (
        f"rev-list's own stderr must be surfaced: {result.stdout!r}"
    )


@pytest.mark.integration
@pytest.mark.parametrize("harness", list(HARNESSES))
def test_rev_list_success_with_stray_stderr_is_not_trusted_as_a_count(
    harness, tmp_path
):
    """A rev-list that exits 0 but ALSO writes to stderr is not a clean count.

    The capture is ``2>&1``, so a real count (``3``) plus an advisory stderr
    line merge into one string. Blindly trusting that with ``-gt`` is not a
    numeric comparison at all: bash reports "integer expression expected" and
    the ``if`` reads the error as FALSE, so a worker's real commits would be
    reported as no changes -- the exact misclassification this diagnostic
    exists to prevent, reintroduced by the guard meant to fix it.

    This module's choice (documented at module level): any stderr on an
    otherwise-successful rev-list makes the output untrustworthy, full stop,
    rather than attempting to strip and re-parse it. So this must land on the
    diagnostic path, not the "worker committed its own work" path, AND the
    raw captured text (both the real count and the stray warning) must be
    visible to the operator, not silently discarded.
    """
    result = _run_no_change(
        harness,
        tmp_path,
        ahead="3",
        rev_list_warn="warning: unable to access some ref, ignoring",
    )

    assert "unbound variable" not in result.stderr, result.stderr
    assert result.returncode == 1, (
        f"a stray stderr line must not be trusted as a clean count: "
        f"stdout={result.stdout!r}"
    )
    assert "__PRAXIS_STATUS__=failed" in result.stdout, result.stdout
    assert HARNESSES[harness]["sentence"] in result.stdout, (
        f"the diagnostic path must be taken, not the committed-work path: "
        f"{result.stdout!r}"
    )
    assert "Worker committed its own work" not in result.stdout, result.stdout

    assert "rev_list_failed=true" in result.stdout, (
        f"the untrustworthy output must be reported, not silently trusted: "
        f"{result.stdout!r}"
    )
    assert "3" in result.stdout, (
        f"the raw captured text (including the real count) must stay "
        f"visible: {result.stdout!r}"
    )
    assert "warning: unable to access some ref" in result.stdout, (
        f"the raw captured text (including the stray warning) must stay "
        f"visible: {result.stdout!r}"
    )


@pytest.mark.integration
@pytest.mark.parametrize("harness", list(HARNESSES))
def test_rev_list_success_with_non_numeric_output_does_not_abort(harness, tmp_path):
    """A rev-list that exits 0 but prints garbage must not crash the script.

    ``[ "not-a-number" -gt 0 ]`` is not a syntax error, but it is not silent
    either: this pins that the script survives it (no ``set -u`` blowup, no
    unhandled non-zero from the comparison escaping the ``if``), still exits
    non-zero, and reports the anomaly with the raw text rather than treating
    the garbage as a valid count or swallowing the whole thing quietly.
    """
    result = _run_no_change(harness, tmp_path, ahead="not-a-number")

    assert "unbound variable" not in result.stderr, result.stderr
    assert result.returncode == 1, (
        f"non-numeric rev-list output must not crash or be trusted: "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert "__PRAXIS_STATUS__=failed" in result.stdout, result.stdout
    assert HARNESSES[harness]["sentence"] in result.stdout, result.stdout

    assert "rev_list_failed=true" in result.stdout, (
        f"non-numeric output must be reported as an anomaly, not silently "
        f"zeroed: {result.stdout!r}"
    )
    assert "not-a-number" in result.stdout, (
        f"the raw non-numeric text must be visible to the operator: {result.stdout!r}"
    )


@pytest.mark.integration
@pytest.mark.parametrize("harness", list(HARNESSES))
def test_an_empty_transcript_stays_a_failure(harness, tmp_path):
    """A harness that emitted nothing did not run, so it is not a no-op.

    This is the half that stops the fix from being a blanket "empty diff is
    fine". Delete the ``output_bytes -gt 0`` condition and a dead worker
    silently closes every leaf it is handed, with the plan reporting success.
    """
    result = _run_no_change(harness, tmp_path, log_text="")

    assert result.returncode == 1, result.stdout
    assert "__PRAXIS_STATUS__=failed" in result.stdout, result.stdout
    assert "__PRAXIS_CALLBACK__=no_changes" not in result.stdout, result.stdout
    assert "output_log_bytes=0" in result.stdout, result.stdout
    assert "report_status=none" in result.stdout, result.stdout


# The three shapes in which the commit count cannot be trusted. Each is a
# separate case because `_run_no_change` owns its tmp_path, and because a
# single test that stopped at the first one would silently stop covering the
# rest.
_UNTRUSTWORTHY_COUNTS = {
    "rev_list_failed": {"rev_list_exit": "128"},
    "stray_stderr": {"ahead": "3", "rev_list_warn": "warning: unable to access"},
    "non_numeric": {"ahead": "not-a-number"},
}


@pytest.mark.integration
@pytest.mark.parametrize("harness", list(HARNESSES))
@pytest.mark.parametrize("shape", list(_UNTRUSTWORTHY_COUNTS))
def test_an_untrustworthy_commit_count_is_never_reported_as_a_no_op(
    harness, shape, tmp_path
):
    """A rev-list we could not read does not prove the worker produced nothing.

    It may have committed work this run cannot see, and closing the leaf would
    discard it permanently: a no-op is terminal, with no PR and no review.
    Drop ``rev_list_error`` from the no-op condition and every one of these
    goes red.
    """
    result = _run_no_change(harness, tmp_path, **_UNTRUSTWORTHY_COUNTS[shape])

    assert result.returncode == 1, result.stdout
    assert "__PRAXIS_CALLBACK__=no_changes" not in result.stdout, result.stdout
    assert "__PRAXIS_STATUS__=failed" in result.stdout, result.stdout


@pytest.mark.integration
@pytest.mark.parametrize("harness", list(HARNESSES))
def test_a_worker_that_committed_its_own_work_is_untouched(harness, tmp_path):
    """The no-op path must not swallow the committed-work path beside it."""
    result = _run_no_change(harness, tmp_path, ahead="2")

    assert result.returncode == 0, result.stdout
    assert "Worker committed its own work (2 commit(s)" in result.stdout
    assert HARNESSES[harness]["sentence"] not in result.stdout
    assert "__PRAXIS_CALLBACK__" not in result.stdout, (
        "the committed-work path continues to the push/PR block; it must not "
        "short-circuit into a callback here"
    )


@pytest.mark.integration
def test_agy_reports_its_envelope_status_and_turn_count(tmp_path):
    """agy has an envelope; opencode does not. Only agy is asked for it."""
    result = _run_no_change("agy", tmp_path)

    assert result.returncode == 0, result.stdout
    assert "envelope_status=completed" in result.stdout, result.stdout
    assert "envelope_num_turns=7" in result.stdout, result.stdout


@pytest.mark.integration
def test_agy_survives_an_unparseable_envelope(tmp_path):
    """The fallback path really does reach here with no JSON at all.

    The diagnostic must not trip ``set -e`` (that would skip its own tail and
    the outcome branch below it) and must say so explicitly rather than print
    a blank.

    It also pins that the envelope is EVIDENCE, not the condition: an
    unparseable envelope still reaches the no-op decision. Gating the outcome
    on ``envelope_status`` is a separate, tracked change; smuggling it in here
    would make the two entrypoints diverge on a defect that is not
    agy-specific, and would silently re-fail the exact runs this fixes.
    """
    result = _run_no_change("agy", tmp_path, envelope=_BAD_ENVELOPE)

    assert "unbound variable" not in result.stderr, result.stderr
    assert result.returncode == 0, result.stdout
    assert "__PRAXIS_CALLBACK__=no_changes" in result.stdout, result.stdout
    assert "envelope_status=unparseable" in result.stdout, result.stdout
    assert "envelope_num_turns=unknown" in result.stdout, result.stdout
    assert "line-040" in result.stdout, "the tail must still print"


@pytest.mark.integration
@pytest.mark.parametrize("harness", list(HARNESSES))
def test_the_diagnostic_stays_inside_the_no_change_branch(harness, tmp_path):
    """Polarity and containment, the half a presence test cannot see.

    A block hoisted out of the inner ``else`` fires on every run and reports a
    failure for a task that succeeded. Both non-diagnostic outcomes are pinned:
    a dirty index (the ordinary commit) and a clean index on a branch the worker
    already advanced itself.
    """
    committed = _run_no_change(harness, tmp_path / "dirty", diff_exit="1")
    assert committed.returncode == 0, committed.stderr
    assert "__PRAXIS_STATUS__=completed" in committed.stdout
    assert HARNESSES[harness]["sentence"] not in committed.stdout
    assert "harness_rc=" not in committed.stdout, committed.stdout

    self_committed = _run_no_change(harness, tmp_path / "ahead", ahead="3")
    assert self_committed.returncode == 0, self_committed.stderr
    assert "__PRAXIS_STATUS__=completed" in self_committed.stdout
    assert "3 commit(s) ahead" in self_committed.stdout
    assert HARNESSES[harness]["sentence"] not in self_committed.stdout
    assert "harness_rc=" not in self_committed.stdout, self_committed.stdout
