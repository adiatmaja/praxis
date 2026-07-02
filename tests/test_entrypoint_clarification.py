import shutil
import subprocess
from pathlib import Path

import pytest


PARSE_SNIPPET = r"""
set -euo pipefail
OUTPUT_LOG="$1"
STATUS="completed"
QUESTION=""
report_status=$(grep -oE '^Status:[[:space:]]*[A-Z_]+' "$OUTPUT_LOG" | tail -n1 | sed -E 's/^Status:[[:space:]]*//') || true
case "${report_status}" in
    BLOCKED|NEEDS_CONTEXT)
        STATUS="needs_clarification"
        QUESTION=$(awk '/^Concerns/{flag=1;next}/^====/{flag=0}flag' "$OUTPUT_LOG" | sed '/^[[:space:]]*$/d')
        ;;
esac
printf 'STATUS=%s\n' "$STATUS"
printf 'QUESTION=%s\n' "$QUESTION"
"""

_BASH = shutil.which("bash")
pytestmark = pytest.mark.skipif(_BASH is None, reason="bash unavailable")


def _to_posix(p: Path) -> str:
    """Convert a Windows absolute path to a POSIX path usable by Git Bash."""
    s = str(p)
    if len(s) >= 2 and s[1] == ":":
        return "/" + s[0].lower() + s[2:].replace("\\", "/")
    return s.replace("\\", "/")


def _run(tmp_path: Path, report: str) -> str:
    log = tmp_path / "aider.log"
    log.write_text(report, encoding="utf-8")
    # Pass the snippet via stdin (bash -s) so no file-path conversion is needed
    # for the script itself; only the log path is converted to POSIX.
    out = subprocess.run(
        [_BASH, "-s", "--", _to_posix(log)],
        input=PARSE_SNIPPET,
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout


def test_blocked_report_yields_clarification_and_question(tmp_path):
    report = (
        "Status: NEEDS_CONTEXT\n\n"
        "Concerns (if Status is DONE_WITH_CONCERNS, BLOCKED, or NEEDS_CONTEXT):\n"
        "Two auth helpers exist; which should the new endpoint call?\n"
        "========================================================================\n"
    )
    out = _run(tmp_path, report)
    assert "STATUS=needs_clarification" in out
    assert "which should the new endpoint call?" in out


def test_done_report_stays_completed(tmp_path):
    out = _run(tmp_path, "Status: DONE\n")
    assert "STATUS=completed" in out
    assert "QUESTION=\n" in out


# ---------------------------------------------------------------------------
# Structural presence tests: assert each real entrypoint contains the
# clarification wiring (no Docker required).
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).parent.parent


def _read_entrypoint(harness: str) -> str:
    return (REPO_ROOT / "docker" / f"{harness}-agent" / "entrypoint.sh").read_text()


@pytest.mark.parametrize("harness", ["opencode", "openhands"])
def test_entrypoint_has_needs_clarification_status(harness):
    content = _read_entrypoint(harness)
    assert 'STATUS="needs_clarification"' in content, (
        f"{harness} entrypoint missing needs_clarification STATUS assignment"
    )


@pytest.mark.parametrize("harness", ["opencode", "openhands"])
def test_entrypoint_has_concerns_awk_extraction(harness):
    content = _read_entrypoint(harness)
    assert "awk '/^Concerns/" in content, (
        f"{harness} entrypoint missing awk Concerns block extraction"
    )


@pytest.mark.parametrize("harness", ["opencode", "openhands"])
def test_entrypoint_has_question_in_payload(harness):
    content = _read_entrypoint(harness)
    # In the shell file the payload uses escaped quotes: \"question\":${question_json}
    assert r"\"question\":${question_json}" in content, (
        f"{harness} entrypoint missing question field in send_callback payload"
    )


@pytest.mark.parametrize("harness", ["opencode", "openhands"])
def test_entrypoint_syntax_valid(harness):
    """bash -n syntax check on each entrypoint."""
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash unavailable")
    path = REPO_ROOT / "docker" / f"{harness}-agent" / "entrypoint.sh"
    result = subprocess.run([bash, "-n", str(path)], capture_output=True, text=True)
    assert result.returncode == 0, (
        f"{harness} entrypoint failed bash -n: {result.stderr}"
    )
