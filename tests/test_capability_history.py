import pytest

from orchestrator.core.capability_history import summarize_outcomes


@pytest.mark.unit
def test_empty_history_returns_no_history_sentinel():
    assert summarize_outcomes([]) == "(no prior run history for this model)"


@pytest.mark.unit
def test_summary_reports_pass_fail_counts_by_type():
    runs = [
        {"task_type": "test", "files_touched": 1, "loc_delta": 20, "outcome": "pass"},
        {"task_type": "test", "files_touched": 1, "loc_delta": 30, "outcome": "pass"},
        {
            "task_type": "refactor",
            "files_touched": 6,
            "loc_delta": 400,
            "outcome": "fail",
        },
    ]
    out = summarize_outcomes(runs)
    assert "test" in out
    assert "refactor" in out
    assert "2 passed" in out or "pass: 2" in out.lower()
    assert "fail" in out.lower()
