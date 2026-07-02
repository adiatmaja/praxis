"""Expose the running build's commit SHA and process start time.

Lets an operator tell at a glance whether the live server is running current
code. A server started before a feature merged silently runs stale behavior;
stamping the commit on /health and /api/status makes that visible.
"""

from __future__ import annotations

import os
import subprocess
from datetime import UTC, datetime


def _resolve_commit() -> str:
    """Return the build commit: env override, else `git rev-parse`, else 'unknown'."""
    env = os.environ.get("PRAXIS_BUILD_SHA")
    if env:
        return env
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
        return out.stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


_STARTED_AT = datetime.now(UTC).isoformat()
_COMMIT = _resolve_commit()


def build_stamp() -> dict[str, str]:
    """Return `{"commit": <sha>, "started_at": <iso8601>}` for the running process."""
    return {"commit": _COMMIT, "started_at": _STARTED_AT}
