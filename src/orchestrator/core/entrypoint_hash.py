"""Content hash of a harness entrypoint, shared by the build and the doctor.

The staleness check used to compare an image's build timestamp against the
entrypoint's filesystem mtime.  ``git clone`` stamps every file at clone
time, so the source was always "newer" than any cached or pulled layer and
a correct fresh install reported a stale image.  Docker's own build cache
already keys on content, so this module makes the check agree with it.
"""

from __future__ import annotations

import hashlib
from pathlib import Path


#: Docker label carrying the hash of the entrypoint baked into an image.
LABEL_KEY = "org.praxis.entrypoint-sha256"


def hash_entrypoint(path: Path) -> str | None:
    """Return the sha256 of an entrypoint's normalized content.

    Line endings are normalized to LF before hashing: a Windows checkout may
    hold CRLF while the image always holds LF, and that difference is not a
    staleness signal.

    Args:
        path: Path to the ``entrypoint.sh`` to hash.

    Returns:
        The hex digest, or ``None`` when the file cannot be read.  ``None``
        means "cannot be judged" and callers must not treat it as a mismatch.
    """
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    normalized = raw.replace(b"\r\n", b"\n")
    return hashlib.sha256(normalized).hexdigest()
