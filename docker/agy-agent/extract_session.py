#!/usr/bin/env python3
"""Split an agy `--output-format json` envelope into id + response text.

Reads the JSON envelope on stdin. Prints the conversation id on the FIRST line
(empty if absent) and the response body on the remaining lines, then exits 0.
On malformed input prints nothing and exits 1, so the entrypoint can fall back
to plain text mode.

Emitting the body on stdout keeps the existing `Status:` grep working unchanged
against the extractor's output.

The key carrying the response body is UNVERIFIED against a real agy build (see
the design spec), so several plausible names are tried in order.
"""

from __future__ import annotations

import json
import sys


_RESPONSE_KEYS = ("response", "text", "output", "content", "message")


def main() -> int:
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 1

    if not isinstance(data, dict):
        return 1

    conversation_id = data.get("conversation_id") or ""
    if not isinstance(conversation_id, str):
        conversation_id = ""

    body = ""
    for key in _RESPONSE_KEYS:
        value = data.get(key)
        if isinstance(value, str) and value:
            body = value
            break

    print(conversation_id)
    if body:
        print(body)
    return 0


if __name__ == "__main__":
    sys.exit(main())
