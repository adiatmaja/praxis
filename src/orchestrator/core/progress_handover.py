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
    commits: list[Commit],
    worker_note: str | None,
) -> str:
    """Render the PROGRESS section from checklist + commits + optional note."""
    lines = ["# PROGRESS (resume here)", ""]
    in_progress_emitted = False
    for item in items:
        sha = _is_done(item, commits)
        if sha:
            lines.append(f"- [x] {item.text} ({sha})")
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
