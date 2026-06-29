import pytest

from orchestrator.core.progress_handover import (
    ChecklistItem,
    Commit,
    render_handover,
)


@pytest.mark.unit
def test_fresh_run_all_todo():
    items = [ChecklistItem("Add model"), ChecklistItem("Add test")]
    out = render_handover(items, commits=[], worker_note=None)
    assert "PROGRESS (resume here)" in out
    assert "-> in progress: Add model" in out
    assert "[ ] Add test" in out
    assert "[x]" not in out


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
