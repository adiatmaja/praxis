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


# ---------------------------------------------------------------------------
# fetch_recent_outcomes query tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_scopes_by_model_and_project(db):
    """Rows for a different model are excluded; project-scoped query works."""
    from orchestrator.core.capability_history import fetch_recent_outcomes

    await db.execute(
        """INSERT INTO task_outcomes
           (id, project_id, model_name, outcome, failure_class, source, task_type,
            harness, files_touched, loc_delta, context_tokens_est,
            attempt, split_depth)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            "r1",
            "proj-A",
            "model-m",
            "pass",
            None,
            "run",
            "feature",
            "opencode",
            2,
            50,
            1000,
            1,
            0,
        ),
    )
    await db.execute(
        """INSERT INTO task_outcomes
           (id, project_id, model_name, outcome, failure_class, source, task_type,
            harness, files_touched, loc_delta, context_tokens_est,
            attempt, split_depth)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            "r2",
            "proj-A",
            "other-model",
            "pass",
            None,
            "run",
            "feature",
            "opencode",
            3,
            80,
            1200,
            1,
            0,
        ),
    )
    results = await fetch_recent_outcomes(db, model_name="model-m", project_id="proj-A")
    assert len(results) == 1
    assert results[0]["model_name"] == "model-m"
    assert results[0]["project_id"] == "proj-A"


@pytest.mark.asyncio
async def test_fetch_falls_back_to_model_wide_when_project_empty(db):
    """When no rows match (model, project), fall back to (model, *) and return them."""
    from orchestrator.core.capability_history import fetch_recent_outcomes

    await db.execute(
        """INSERT INTO task_outcomes
           (id, project_id, model_name, outcome, failure_class, source, task_type,
            harness, files_touched, loc_delta, context_tokens_est,
            attempt, split_depth)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            "r1",
            "proj-B",
            "model-m",
            "pass",
            None,
            "run",
            "feature",
            "opencode",
            2,
            50,
            1000,
            1,
            0,
        ),
    )
    results = await fetch_recent_outcomes(db, model_name="model-m", project_id="proj-A")
    assert len(results) == 1
    assert results[0]["project_id"] == "proj-B"


@pytest.mark.asyncio
async def test_fetch_excludes_provider_error_includes_verify_fail(db):
    """Non-attributable failures (provider_error) dropped; attributable (verify_fail) kept."""
    from orchestrator.core.capability_history import fetch_recent_outcomes

    await db.execute(
        """INSERT INTO task_outcomes
           (id, project_id, model_name, outcome, failure_class, source, task_type,
            harness, files_touched, loc_delta, context_tokens_est,
            attempt, split_depth)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            "r1",
            "proj-A",
            "model-m",
            "fail",
            "provider_error",
            "run",
            "feature",
            "opencode",
            0,
            0,
            0,
            1,
            0,
        ),
    )
    await db.execute(
        """INSERT INTO task_outcomes
           (id, project_id, model_name, outcome, failure_class, source, task_type,
            harness, files_touched, loc_delta, context_tokens_est,
            attempt, split_depth)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            "r2",
            "proj-A",
            "model-m",
            "fail",
            "verify_fail",
            "run",
            "feature",
            "opencode",
            1,
            10,
            500,
            1,
            0,
        ),
    )
    results = await fetch_recent_outcomes(db, model_name="model-m", project_id="proj-A")
    assert len(results) == 1
    assert results[0]["failure_class"] == "verify_fail"


@pytest.mark.asyncio
async def test_fetch_respects_limit(db):
    """The limit parameter caps the number of returned rows."""
    from orchestrator.core.capability_history import fetch_recent_outcomes

    for i in range(5):
        await db.execute(
            """INSERT INTO task_outcomes
               (id, project_id, model_name, outcome, failure_class, source, task_type,
                harness, files_touched, loc_delta, context_tokens_est,
                attempt, split_depth)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                f"r{i}",
                "proj-A",
                "model-m",
                "pass",
                None,
                "run",
                "feature",
                "opencode",
                1,
                10,
                500,
                1,
                0,
            ),
        )
    results = await fetch_recent_outcomes(
        db, model_name="model-m", project_id="proj-A", limit=3
    )
    assert len(results) == 3


@pytest.mark.asyncio
async def test_fetch_returns_empty_when_no_matching_rows(db):
    """Empty result when no rows match the model."""
    from orchestrator.core.capability_history import fetch_recent_outcomes

    results = await fetch_recent_outcomes(db, model_name="model-m", project_id="proj-A")
    assert results == []
