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
