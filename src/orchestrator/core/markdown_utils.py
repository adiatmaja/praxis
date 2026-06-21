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


_FRONTMATTER_TYPE = re.compile(r"^---\n(.*?)\n---", re.DOTALL)
_TYPE_LINE = re.compile(r"^type:\s*(spec|plan)\s*$", re.MULTILINE)
_TASKS_HEADING = re.compile(r"^##\s+Tasks\b", re.MULTILINE | re.IGNORECASE)


def extract_frontmatter_field(text: str, field: str) -> str | None:
    """Return a top-level YAML front-matter scalar field, or None.

    Only parses the leading ``---``-delimited block. Strips surrounding
    single or double quotes from the value.
    """
    fm = _FRONTMATTER_TYPE.search(text)
    if not fm:
        return None
    pattern = re.compile(rf"^{re.escape(field)}:\s*(.+?)\s*$", re.MULTILINE)
    match = pattern.search(fm.group(1))
    if not match:
        return None
    return match.group(1).strip().strip("\"'")


def classify_by_marker(path: str, text: str) -> str | None:
    """Deterministic classification; None when ambiguous."""
    fm = _FRONTMATTER_TYPE.search(text)
    if fm:
        type_match = _TYPE_LINE.search(fm.group(1))
        if type_match:
            return type_match.group(1)
    normalized = path.replace("\\", "/")
    if "/plans/" in normalized:
        return "plan"
    if "/specs/" in normalized:
        return "spec"
    if _TASKS_HEADING.search(text) and checklist_progress(text)[1] > 0:
        return "plan"
    return None
