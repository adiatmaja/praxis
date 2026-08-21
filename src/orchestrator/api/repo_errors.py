"""One handler for the repo-access failure family, shared by every route in it.

Every route that reaches a target repository ends at the same two calls,
``git_ops.clone_with_token`` and ``git_ops.commit_and_push``, both of which run
``subprocess.run(..., check=True)``. So they all fail the same way: a
``CalledProcessError`` whose ``str()`` is only
``Command '[...]' returned non-zero exit status 128.`` while the actual reason
(bad repo url, unreachable remote, absent or expired token, unwritable
workspace mount) sits unread on ``.stderr``.

Six routes handled that and four did not, so the four answered a bare 500:
"the server is broken, file a bug", for an install that is simply missing a
credential. This module exists so the answer is decided in ONE place. A remedy
that lives in six copies is a remedy that will be fixed in five of them, which
is the shape of defect this file is here to stop.
"""

from __future__ import annotations

import logging
import subprocess  # noqa: S404
from collections.abc import Awaitable
from typing import TypeVar

from fastapi import HTTPException, status


logger = logging.getLogger(__name__)

T = TypeVar("T")


def repo_failure_detail(exc: BaseException) -> str:
    """Render a repo-access failure with the part the operator can act on.

    ``CalledProcessError.stderr`` is where git puts the reason; ``str(exc)``
    is where it puts the exit code. Callers get the reason.
    """
    if isinstance(exc, subprocess.CalledProcessError):
        raw = exc.stderr
        if isinstance(raw, bytes):
            raw = raw.decode(errors="replace")
        reason = (raw or "").strip() or str(exc)
    else:
        reason = str(exc)
    return f"Could not access repository: {reason}"


async def guard_repo_access(awaitable: Awaitable[T], *, what: str) -> T:
    """Await a repo-touching call, mapping its known failures to 502.

    Args:
        awaitable: The call to run.
        what: A short phrase for the log line, e.g. ``"spec commit"``.

    Returns:
        Whatever the awaitable returns.

    Raises:
        HTTPException: 404 when the repository was reachable but the document
            was not there, which is the caller's mistake and not the remote's.
            502 with the git reason attached for any other failure: 502 rather
            than 500 because the thing that failed is the remote or the
            credential in front of it, not this server.
    """
    try:
        return await awaitable
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc
    except subprocess.CalledProcessError as exc:
        detail = repo_failure_detail(exc)
        logger.warning("%s failed: %s", what, detail)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=detail
        ) from exc
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 - every remaining failure is the repo's
        detail = repo_failure_detail(exc)
        logger.warning("%s failed: %s", what, detail)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=detail
        ) from exc
