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


@pytest.mark.unit
def test_delete_and_replace_refactor_not_flagged():
    """A file with many deletions but equal or more additions is a refactor, not truncation."""
    # 60 deleted lines, 65 added lines => net addition => not flagged
    diff = "\n".join(
        ["--- a/dispatch.py", "+++ b/dispatch.py"]
        + [f"-OLD{i}" for i in range(60)]
        + [f"+NEW{i}" for i in range(65)]
    )
    flagged = destructive_deletions(diff, threshold=40)
    assert "dispatch.py" not in flagged


@pytest.mark.unit
def test_diff_with_more_deletions_than_additions_is_flagged():
    """Net-deletion diff with large per-file removal is flagged."""
    # 60 deleted, 5 added => net deletion => flagged
    diff = "\n".join(
        ["--- a/big_file.py", "+++ b/big_file.py"]
        + [f"-DEL{i}" for i in range(60)]
        + [f"+ADD{i}" for i in range(5)]
    )
    flagged = destructive_deletions(diff, threshold=40)
    assert "big_file.py" in flagged


@pytest.mark.unit
def test_multiple_files_mixed_refactor():
    """Only net-shrinking files with large deletions are flagged in a mixed diff."""
    diff = "\n".join(
        # file1: 50 del, 55 add => refactor, not flagged
        ["--- a/file1.py", "+++ b/file1.py"]
        + [f"-D{i}" for i in range(50)]
        + [f"+A{i}" for i in range(55)]
        +
        # file2: 50 del, 2 add => truncation, flagged
        ["--- a/file2.py", "+++ b/file2.py"]
        + [f"-X{i}" for i in range(50)]
        + ["+Y=1", "+Z=2"]
    )
    flagged = destructive_deletions(diff, threshold=40)
    # net total: 100 del, 57 add => net deletion; file2 is flagged, file1 has near-match adds
    assert "file2.py" in flagged
    assert "file1.py" not in flagged


@pytest.mark.unit
def test_threshold_below_deletion_count_not_exceeded():
    # 30 deletions, threshold=40 => not flagged regardless of additions
    diff = "\n".join(
        ["--- a/small.py", "+++ b/small.py"] + [f"-L{i}" for i in range(30)]
    )
    assert destructive_deletions(diff, threshold=40) == []
