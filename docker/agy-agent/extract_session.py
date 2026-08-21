#!/usr/bin/env python3
"""Split an agy `--output-format json` envelope into id + response text.

Reads the JSON envelope on stdin. Prints the conversation id on the FIRST line
(empty if absent) and the response body on the remaining lines, then exits 0.
Exits 1 printing nothing on ANY shape it cannot fully read, which now includes
a well-formed envelope carrying no recognized body key: the entrypoint's
fallback to plain text mode is what keeps the transcript, and it only runs on a
non-zero exit.

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

    if not body:
        # FAIL CLOSED. A well-formed envelope whose body sits under a key not
        # in _RESPONSE_KEYS is exactly the shape this file admits it cannot be
        # sure of, and returning 0 here handed the entrypoint an EMPTY
        # transcript while suppressing the RAW_LOG fallback that exists for
        # this case. Downstream that is not a degraded run, it is a wrong one:
        # the `Status:` grep finds no BLOCKED line and the worker's question is
        # destroyed, and the no-changes block reads zero bytes and calls a
        # satisfied tree a failed run. Losing the conversation id costs a
        # session resume; losing the transcript costs the verdict.
        return 1

    print(conversation_id)
    print(body)
    return 0


if __name__ == "__main__":
    sys.exit(main())
