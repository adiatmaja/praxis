#!/usr/bin/env python3
"""Print the OpenCode session id from `opencode session list --format json`.

Reads JSON on stdin, prints one session id on stdout, exits 0. On any problem
(malformed JSON, no sessions, unexpected shape) prints nothing and exits 1, so
the entrypoint can treat session capture as best-effort and carry on.

A fresh container holds exactly one session. The newest-by-created ordering
only matters when the sessions volume is reused across tasks.
"""

from __future__ import annotations

import json
import sys


def main() -> int:
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 1

    if isinstance(data, dict):
        data = data.get("sessions", [])
    if not isinstance(data, list) or not data:
        return 1

    def created(entry: object) -> float:
        if not isinstance(entry, dict):
            return -1.0
        time_block = entry.get("time")
        if isinstance(time_block, dict):
            try:
                return float(time_block.get("created", -1))
            except (TypeError, ValueError):
                return -1.0
        return -1.0

    newest = max(data, key=created)
    if not isinstance(newest, dict):
        return 1
    session_id = newest.get("id")
    if not isinstance(session_id, str) or not session_id:
        return 1

    print(session_id)
    return 0


if __name__ == "__main__":
    sys.exit(main())
