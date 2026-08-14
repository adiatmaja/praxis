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
The run still looks like an ordinary failure, and
``core.failure_taxonomy.counts_against_worker`` charges it to the worker either
way.

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
# own work. Both are knobs so the two non-diagnostic outcomes can be pinned too.
_SPY_GIT = """\
#!/usr/bin/env bash
printf '%s\\n' "git $*" >> "$PRAXIS_SPY_LOG"
case "${1:-}" in
    diff) exit "${PRAXIS_GIT_DIFF_EXIT:-0}" ;;
    rev-list) printf '%s\\n' "${PRAXIS_GIT_AHEAD:-0}" ;;
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
        'BRANCH="agent/no-change-leaf"',
        'BASE_BRANCH="plan/2026-08-14-diagnostics"',
        'STATUS="completed"',
        'trap \'printf "__PRAXIS_STATUS__=%s\\n" "${STATUS}"\' EXIT',
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
    return subprocess.run([bash, "-c", script], capture_output=True, text=True, env=env)


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
    assert result.returncode == 1, (
        f"the no-change path must still fail; stdout={result.stdout!r}"
    )
    assert "__PRAXIS_STATUS__=failed" in result.stdout, result.stdout
    assert HARNESSES[harness]["sentence"] in result.stdout

    assert "harness_rc=0" in result.stdout, f"no harness rc reported: {result.stdout!r}"
    assert f"output_log_bytes={_LOG_BYTES}" in result.stdout, (
        f"no output-log size reported: {result.stdout!r}"
    )
    assert "report_status=none" in result.stdout, (
        f"an unparsed status must be reported as an explicit none: {result.stdout!r}"
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

    assert result.returncode == 1, result.stdout
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

    assert result.returncode == 1, result.stdout
    assert "harness_rc=7" in result.stdout, (
        f"the rc must be read from the harness variable: {result.stdout!r}"
    )


@pytest.mark.integration
@pytest.mark.parametrize("harness", list(HARNESSES))
def test_an_empty_transcript_is_reported_as_zero_bytes(harness, tmp_path):
    """ "The harness emitted nothing" is the shape the whole task is about."""
    result = _run_no_change(harness, tmp_path, log_text="")

    assert result.returncode == 1, result.stdout
    assert "output_log_bytes=0" in result.stdout, result.stdout
    assert "report_status=none" in result.stdout, result.stdout


@pytest.mark.integration
def test_agy_reports_its_envelope_status_and_turn_count(tmp_path):
    """agy has an envelope; opencode does not. Only agy is asked for it."""
    result = _run_no_change("agy", tmp_path)

    assert result.returncode == 1, result.stdout
    assert "envelope_status=completed" in result.stdout, result.stdout
    assert "envelope_num_turns=7" in result.stdout, result.stdout


@pytest.mark.integration
def test_agy_survives_an_unparseable_envelope(tmp_path):
    """The fallback path really does reach here with no JSON at all.

    The diagnostic must not trip ``set -e`` (that would skip its own tail and
    the ``exit 1``) and must say so explicitly rather than print a blank.
    """
    result = _run_no_change("agy", tmp_path, envelope=_BAD_ENVELOPE)

    assert "unbound variable" not in result.stderr, result.stderr
    assert result.returncode == 1, result.stdout
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
