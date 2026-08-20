"""Render a submitted specification as a repository spec doc.

Spec 2 made markdown docs the source of truth and dropped the ``plans.spec``
column, but nothing was ever written in its place: a submitted spec was
validated and then discarded, leaving ``plans.spec_path`` NULL and the planner
with nothing but the repository name.  These helpers produce the doc that
closes that gap.  They are deliberately pure so the path and body are testable
without a repository.
"""

from __future__ import annotations

import re
from datetime import date


SPEC_DOC_DIR = "docs/superpowers/specs"

# Keeps a generated filename short enough to stay readable in `praxis plans`
# and in the repo listing, without truncating so hard that two specs submitted
# on the same day become indistinguishable at a glance.
_MAX_SLUG_LENGTH = 48
_MAX_TITLE_LENGTH = 72


def spec_title(spec: str) -> str:
    """Return a one-line human title for a submitted spec."""

    first_line = next(
        (line.strip() for line in spec.splitlines() if line.strip()),
        "Submitted spec",
    )
    # A markdown heading in the submitted text is already a title; unwrap it
    # rather than nesting "# # Add auth".
    first_line = first_line.lstrip("#").strip() or "Submitted spec"
    if len(first_line) > _MAX_TITLE_LENGTH:
        return first_line[:_MAX_TITLE_LENGTH].rstrip() + "..."
    return first_line


def spec_slug(spec: str) -> str:
    """Return a URL-safe slug derived from the first meaningful line."""

    slug = re.sub(r"[^a-z0-9]+", "-", spec_title(spec).lower()).strip("-")
    if len(slug) > _MAX_SLUG_LENGTH:
        slug = slug[:_MAX_SLUG_LENGTH].rstrip("-")
    return slug or "spec"


def spec_doc_path(spec: str, *, today: date, unique: str) -> str:
    """Return the repo-relative path the spec doc is committed to.

    Args:
        spec: The raw specification text as submitted.
        today: Date used for the filename prefix, matching the convention used
            by brainstormed specs (``<date>-<slug>``).
        unique: Short collision-breaking suffix, so two specs submitted on one
            day with a similar first line do not overwrite one another.

    Returns:
        A path under ``docs/superpowers/specs`` so the doc is picked up as a
        spec by the lifecycle listing, which categorizes by folder name.
    """

    return f"{SPEC_DOC_DIR}/{today.isoformat()}-{spec_slug(spec)}-{unique}.md"


def render_spec_doc(spec: str) -> str:
    """Return the markdown body for a submitted spec.

    The submitted text is embedded verbatim: it is the contract the planner
    reads back, so nothing here may reword, wrap, or summarize it.
    """

    return (
        "---\n"
        "type: spec\n"
        "source: praxis-submit\n"
        "---\n\n"
        f"# {spec_title(spec)}\n\n"
        f"{spec.strip()}\n"
    )
