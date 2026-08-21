"""Reconstruct a worker's progress from ground truth (git + checklist).

This is a handover, NOT a model-written summary: completed items are derived
only from real commit subjects, so a weak worker cannot hallucinate progress.
The worker may contribute a single, clearly-marked, untrusted "current intent"
line which never marks an item done.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ChecklistItem:
    """One ordered step of a leaf task."""

    text: str


@dataclass
class Commit:
    """A commit on the task branch."""

    sha: str
    subject: str


def _is_done(item: ChecklistItem, commits: list[Commit]) -> str | None:
    """Return the short sha of the first commit naming ``item``, else None."""
    needle = item.text.strip().lower()
    for c in commits:
        if needle in c.subject.strip().lower():
            return c.sha[:7]
    return None


def render_handover(
    items: list[ChecklistItem],
    commits: list[Commit] | None,
    worker_note: str | None,
) -> str:
    """Render the PROGRESS section from checklist + commits + optional note.

    Three states, and they must not be rendered the same way. The section used
    to say "resume here" over an all-unticked checklist in all three, which is
    an assertion the caller often could not support: "nothing has been done" and
    "I could not find out what has been done" are different facts, and the
    second one rendered as the first is what tells a resumed worker to redo
    completed work.

    Args:
        items: The leaf's checklist, in order.
        commits: Commits on the task branch, oldest first; ``[]`` when the
            branch genuinely has none, and ``None`` when the history could NOT
            BE READ. The distinction is the point of this signature.
        worker_note: The worker's own untrusted "current intent" line.

    Returns:
        The PROGRESS section, headed to match which of the three states holds.
    """
    if commits is None:
        header = "# PLAN (commit history unavailable; verify before redoing work)"
    elif not commits:
        header = "# PLAN (no commits on this branch yet)"
    else:
        header = "# PROGRESS (resume here)"
    lines = [header, ""]

    # A single item is the fallback the dispatcher builds from the whole task
    # description when a leaf declares no checklist, which is the common case.
    # Rendering that as a one-box checklist made the working agreement's "name
    # the item in the commit subject" unfollowable, because the item text is a
    # whole paragraph no subject line can contain.
    single_fallback = len(items) == 1 and not commits
    if single_fallback:
        lines.append(f"Not started: {items[0].text}")
    else:
        in_progress_emitted = False
        for item in items:
            sha = _is_done(item, commits or [])
            if sha:
                lines.append(f"- [x] {item.text} ({sha})")
            elif commits is None:
                # No arrow: with no history there is no basis for saying which
                # item is the current one.
                lines.append(f"- [ ] {item.text}")
            elif not in_progress_emitted:
                lines.append(f"- [ ] -> in progress: {item.text}")
                in_progress_emitted = True
            else:
                lines.append(f"- [ ] {item.text}")

    if commits:
        lines += ["", f"Last action: {commits[-1].subject} ({commits[-1].sha[:7]})"]
    if worker_note and worker_note.strip():
        lines += ["", f"> (worker note, unverified) {worker_note.strip()}"]
    return "\n".join(lines)
