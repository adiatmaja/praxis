"""Pure helpers for parsing markdown docs."""

from __future__ import annotations

import hashlib
import re


_CHECKBOX = re.compile(r"^\s*-\s\[( |x|X)\]\s", re.MULTILINE)
_H1 = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)


def content_hash(text: str) -> str:
    """Return a stable hex digest of the file content."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def extract_title(text: str) -> str | None:
    """Return the first H1 heading text, or None."""
    match = _H1.search(text)
    return match.group(1) if match else None


def checklist_progress(text: str) -> tuple[int, int]:
    """Return (done, total) markdown task checkboxes."""
    boxes = _CHECKBOX.findall(text)
    done = sum(1 for state in boxes if state in ("x", "X"))
    return done, len(boxes)
