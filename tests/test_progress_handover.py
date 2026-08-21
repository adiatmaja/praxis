import pytest

from orchestrator.core.progress_handover import (
    ChecklistItem,
    Commit,
    render_handover,
)


@pytest.mark.unit
def test_fresh_run_all_todo():
    """A branch with no commits says so; it does not say "resume here".

    Three states have to render differently: nothing done, something done, and
    "I could not find out". They all used to render as "PROGRESS (resume
    here)" over an unticked checklist.
    """
    items = [ChecklistItem("Add model"), ChecklistItem("Add test")]
    out = render_handover(items, commits=[], worker_note=None)
    assert "PLAN (no commits on this branch yet)" in out
    assert "resume here" not in out
    assert "-> in progress: Add model" in out
    assert "[ ] Add test" in out
    assert "[x]" not in out


@pytest.mark.unit
def test_an_unreadable_history_is_not_reported_as_no_progress():
    """`None` means the history could NOT BE READ, and that is not "nothing
    was done".

    Rendering the second as the first is what tells a resumed worker to redo
    completed work. There is also no basis for an "-> in progress" arrow, since
    nothing establishes which item is current.
    """
    items = [ChecklistItem("Add model"), ChecklistItem("Add test")]
    out = render_handover(items, commits=None, worker_note=None)
    assert "commit history unavailable" in out
    assert "no commits on this branch yet" not in out
    assert "-> in progress" not in out
    assert "[ ] Add model" in out


@pytest.mark.unit
def test_a_single_synthesised_item_is_not_rendered_as_a_checklist():
    """One item holding the whole description cannot be named in a subject.

    Rendering it as a one-box checklist made the working agreement's "name the
    item in the commit subject" unfollowable, so the box could never be ticked.
    """
    long_item = ChecklistItem(
        "Add exponential backoff to the HTTP client so transient 502s retry."
    )
    out = render_handover([long_item], commits=[], worker_note=None)
    assert out.splitlines()[2].startswith("Not started: ")
    assert "- [ ]" not in out


@pytest.mark.unit
def test_commit_subject_marks_item_done():
    items = [ChecklistItem("Add model"), ChecklistItem("Add test")]
    commits = [Commit(sha="abc1234", subject="agent: Add model")]
    out = render_handover(items, commits=commits, worker_note=None)
    assert "[x] Add model (abc1234)" in out
    assert "-> in progress: Add test" in out


@pytest.mark.unit
def test_worker_note_rendered_untrusted_and_never_marks_done():
    items = [ChecklistItem("Add model")]
    out = render_handover(items, commits=[], worker_note="I think the model is done")
    assert "(worker note, unverified)" in out
    assert "I think the model is done" in out
    assert "[x]" not in out


@pytest.mark.unit
def test_substring_match_is_case_insensitive_and_trimmed():
    items = [ChecklistItem("Add the User model")]
    commits = [Commit(sha="deadbee", subject="agent: add the user MODEL done")]
    out = render_handover(items, commits=commits, worker_note=None)
    assert "[x] Add the User model (deadbee)" in out
