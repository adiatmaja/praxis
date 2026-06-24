import pytest

from orchestrator.core.diff_guard import destructive_deletions


@pytest.mark.unit
def test_flags_large_deletion_from_existing_file():
    diff = "\n".join(
        ["--- a/.env.example", "+++ b/.env.example"]
        + [f"-LINE{i}" for i in range(70)]
        + ["+KEY=1"]
    )
    flagged = destructive_deletions(diff, threshold=40)
    assert ".env.example" in flagged


@pytest.mark.unit
def test_ignores_new_file_and_small_edits():
    diff = "\n".join(
        ["--- /dev/null", "+++ b/new.py"] + [f"+LINE{i}" for i in range(70)]
    )
    assert destructive_deletions(diff, threshold=40) == []
