"""The review path states what it found out, and nothing more.

``review_task`` decides PASS or FAIL and the merge gate acts on that verdict.
Every other surface in Praxis reports a fact somebody else established; this one
ESTABLISHES it. So a wrong statement here is not a misleading line, it is the
loop believing something false about work that already happened, and in several
of these cases the false statement is fed back to the worker or to the triage
brain and acted on.

Each test below pins one such statement against the evidence the code actually
had at that point.
"""
# ruff: noqa: S101

from __future__ import annotations

from collections.abc import Sequence
from typing import Any
from unittest.mock import AsyncMock

import pytest

from orchestrator.core.orchestrator_review import (
    _SKIP_BENCH_MODE_DISABLED,
    _SKIP_CHECKOUT_UNAVAILABLE,
    _PlanVerifyResult,
)
from orchestrator.models.schemas import TaskStatus


def _gate(
    orch: Any, status: str, *, reason: str | None = None, output: str = ""
) -> Any:
    """Pin the plan-branch verify gate to one verdict, recording its arguments."""
    seen: list[dict[str, Any]] = []

    async def _stub(
        repo_url: str,
        branch: str,
        verify_cmd: str | None,
        disabled_reason: str | None = None,
        require_paths: Sequence[str] = (),
        leaf_verify_cmd: str | None = None,
    ) -> _PlanVerifyResult:
        seen.append(
            {
                "branch": branch,
                "disabled_reason": disabled_reason,
                # The gate now also answers whether the leaf's declared edit
                # locations exist on that branch. Recorded rather than
                # swallowed: a stub that accepted and ignored the argument
                # would let the whole declaration be dropped with every test
                # here still green.
                "require_paths": tuple(require_paths),
            }
        )
        return _PlanVerifyResult(status, output=output, reason=reason)

    orch._verify_plan_branch = _stub  # type: ignore[method-assign]
    return seen


def _empty_diff_backend() -> Any:
    backend = AsyncMock()
    backend.name = "github"
    backend.get_diff.return_value = ""
    backend.checkout.side_effect = RuntimeError("no checkout in this test")
    return backend


async def _review(orch: Any, task_id: str, project: dict[str, Any]) -> None:
    await orch._tq.update_task_status(task_id, TaskStatus.REVIEWING)
    await orch.review_task(task_id, project)


# --------------------------------------------------------------------------
# The empty-diff decision declines for four unrelated facts, and says which.
# --------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    ("status", "reason", "expected", "forbidden"),
    [
        # A red PROJECT command with no leaf check of its own. Since 2026-08-26
        # this no longer says "did not verify clean, so the work is genuinely
        # missing": on this path the command runs on the very tree the worker was
        # handed, so it is red identically before and after the attempt and says
        # nothing about the work. The sentence a worker reads must not send it
        # chasing a failure it did not cause and cannot fix from here.
        pytest.param(
            "failed",
            None,
            "says nothing about the work it was asked to do",
            "genuinely missing",
        ),
        pytest.param(
            "error", None, "could not be verified at all", "did not verify clean"
        ),
        pytest.param(
            "skipped",
            _SKIP_CHECKOUT_UNAVAILABLE,
            "was skipped",
            "did not verify clean",
        ),
    ],
    ids=["gate-failed", "gate-errored", "gate-skipped"],
)
async def test_the_empty_diff_failure_names_the_fact_that_produced_it(
    orchestrator_fixture, status, reason, expected, forbidden
):
    """Three ways to decline, one sentence each, and it reaches the worker.

    ``fail_task`` stores this on the row, the ``task_failed`` event carries it,
    and ``core/worker_bible`` injects it verbatim into the next worker's prompt
    under "PREVIOUS ATTEMPT FEEDBACK (fix these before anything else)". Telling a
    worker a verification failed when the gate never ran costs it a retry
    chasing a check nobody performed.
    """
    orch, task_id, project = orchestrator_fixture
    orch._resolve_backend = lambda _repo_url: _empty_diff_backend()  # type: ignore[method-assign]
    _gate(orch, status, reason=reason)

    await _review(orch, task_id, project)

    updated = await orch._tq.get_task(task_id)
    assert updated is not None
    stored = updated["review_feedback"] or ""
    assert expected in stored
    assert forbidden not in stored


@pytest.mark.unit
async def test_an_unresolvable_base_branch_does_not_claim_a_verify_result(
    orchestrator_fixture,
):
    """Nothing was checked, so nothing may be reported about a check."""
    orch, task_id, project = orchestrator_fixture
    blind = dict(project)
    blind["repo_url"] = ""
    orch._resolve_backend = lambda _repo_url: _empty_diff_backend()  # type: ignore[method-assign]

    await _review(orch, task_id, blind)

    updated = await orch._tq.get_task(task_id)
    assert updated is not None
    stored = updated["review_feedback"] or ""
    assert "could not be resolved" in stored
    assert "verify" not in stored.replace("verified", "")


@pytest.mark.unit
async def test_bench_mode_is_not_reported_as_an_unconfigured_verify_command(
    orchestrator_fixture, monkeypatch
):
    """Bench condition C suppresses the gate; the project still has a command.

    The reason lands in ``tasks.review_feedback`` and on the ``task_no_changes``
    event, so reporting the operator's configuration wrongly is a claim about
    the project, not a debug detail. Bench mode is what writes the published
    numbers, which makes its own records the worst place to be wrong.
    """
    orch, task_id, project = orchestrator_fixture
    configured = dict(project)
    configured["verify_cmd"] = "uv run pytest -q"
    monkeypatch.setattr(
        "orchestrator.core.orchestrator_review.verify_gate_disabled", lambda: True
    )
    seen = _gate(orch, "skipped", reason=_SKIP_BENCH_MODE_DISABLED)

    closed, why = await orch.no_change_outcome(task_id, configured, None)

    assert closed is True
    assert seen[0]["disabled_reason"] == _SKIP_BENCH_MODE_DISABLED
    assert _SKIP_BENCH_MODE_DISABLED in why
    assert "no verify_cmd configured" not in why
    # This task has no plan row, so it can declare no edit locations and the
    # gate must be asked for no path check. The reason then has to SAY the
    # check did not run rather than leaving the stronger claim standing.
    assert seen[0]["require_paths"] == ()
    assert "declared no edit locations" in why


# --------------------------------------------------------------------------
# A gate failure establishes nothing about the size of the change.
# --------------------------------------------------------------------------


@pytest.mark.unit
async def test_a_verify_gate_failure_records_unknown_diff_stats_not_zero(
    orchestrator_fixture, monkeypatch
):
    """Zero files touched is the signature of a worker that did nothing.

    On the gate-failure path the diff is never fetched, so 0 is not a
    measurement, it is an invention. The columns are nullable and
    ``context_tokens_est=None`` is passed on the next line for exactly this
    case, which is what makes 0 a positive claim rather than an absence.
    """
    orch, task_id, project = orchestrator_fixture
    gated = dict(project)
    gated["verify_cmd"] = "pytest -q"
    backend = AsyncMock()
    backend.name = "github"
    backend.checkout.return_value = "/tmp/checkout"  # noqa: S108 - never touched
    orch._resolve_backend = lambda _repo_url: backend  # type: ignore[method-assign]
    monkeypatch.setattr(
        "orchestrator.core.orchestrator_review.run_verify",
        AsyncMock(return_value=(False, "1 failed")),
    )

    await _review(orch, task_id, gated)

    rows = await orch._tq._db.fetch_all(
        "SELECT * FROM task_outcomes WHERE task_id = ?", (task_id,)
    )
    assert len(rows) == 1
    assert rows[0]["outcome"] == "fail"
    assert rows[0]["files_touched"] is None
    assert rows[0]["loc_delta"] is None


@pytest.mark.unit
def test_the_triage_prompt_says_unknown_rather_than_a_number_it_does_not_have():
    """The brain is entitled to tell "nothing changed" from "nobody looked"."""
    from orchestrator.core.leaf_triage import _render_attempt

    rendered = _render_attempt(
        {
            "attempt": 2,
            "files_touched": None,
            "loc_delta": None,
            "verify_exit_code": None,
            "diff": "",
            "verify_tail": "",
            "review_reason": "reviewer said no",
        }
    )

    assert "files touched: unknown (not measured)" in rendered
    assert "LOC delta: unknown (not measured)" in rendered
    assert "verify exit code: unknown (not measured)" in rendered
    assert "files touched: 0" not in rendered
    assert "verify exit code: 1" not in rendered


# --------------------------------------------------------------------------
# A configured gate that did not run must not read as a clean pass.
# --------------------------------------------------------------------------


@pytest.mark.unit
async def test_a_gate_that_could_not_run_is_carried_to_the_merge_gate(
    orchestrator_fixture, caplog
):
    """The human approving the merge sees that the mechanical gate never ran.

    ``_verify_plan_branch`` already logs the same class of fault at WARNING for
    the plan gate, on the stated ground that "logging that at INFO is how a skip
    comes to read like a pass". Here the PR head could not be cloned, so a
    project that CONFIGURED a gate did not get one, and the PASS parked at the
    merge gate said nothing about it.
    """
    orch, task_id, project = orchestrator_fixture
    gated = dict(project)
    gated["verify_cmd"] = "pytest -q"
    backend = AsyncMock()
    backend.name = "github"
    backend.get_diff.return_value = "diff --git a/x b/x\n+one\n"
    backend.checkout.side_effect = RuntimeError("clone failed")
    orch._resolve_backend = lambda _repo_url: backend  # type: ignore[method-assign]
    orch._opus.review_diff.return_value = {"verdict": "pass", "feedback": "ok"}
    events: list[dict[str, Any]] = []
    orch._bus.publish = events.append  # type: ignore[method-assign]

    with caplog.at_level("WARNING", logger="orchestrator.core.orchestrator_review"):
        await _review(orch, task_id, gated)

    parked = [e for e in events if e.get("type") == "task_awaiting_merge"]
    assert len(parked) == 1
    assert parked[0]["verify_gate_skipped"] == _SKIP_CHECKOUT_UNAVAILABLE
    # The SKIP line specifically, not merely the reason appearing somewhere in
    # the log: the parked-at-merge-gate warning below also names it, so an
    # assertion on the reason alone is satisfied by the other guard and this one
    # goes inert.
    warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert any("verify gate skipped" in m and "ONLY evidence" in m for m in warnings)


@pytest.mark.unit
async def test_an_ordinary_pass_carries_no_skip(orchestrator_fixture):
    """The sibling branch: a project with no gate configured is not a warning."""
    orch, task_id, project = orchestrator_fixture
    backend = AsyncMock()
    backend.name = "github"
    backend.get_diff.return_value = "diff --git a/x b/x\n+one\n"
    backend.checkout.side_effect = RuntimeError("clone failed")
    orch._resolve_backend = lambda _repo_url: backend  # type: ignore[method-assign]
    orch._opus.review_diff.return_value = {"verdict": "pass", "feedback": "ok"}
    events: list[dict[str, Any]] = []
    orch._bus.publish = events.append  # type: ignore[method-assign]

    await _review(orch, task_id, project)

    parked = [e for e in events if e.get("type") == "task_awaiting_merge"]
    assert len(parked) == 1
    assert parked[0]["verify_gate_skipped"] is None


# --------------------------------------------------------------------------
# Small ones, each a statement the code could not support.
# --------------------------------------------------------------------------


@pytest.mark.unit
async def test_a_verdict_with_stray_whitespace_is_still_a_pass(orchestrator_fixture):
    """ "pass " is a model agreeing, not a model failing the task.

    Anything that is not exactly "pass" falls through to the failure path: a PR
    comment, a retry, and a ``fail`` row that ``counts_against_worker``. That row
    is indistinguishable from a real review failure in the calibration data.
    """
    orch, task_id, project = orchestrator_fixture
    backend = AsyncMock()
    backend.name = "github"
    backend.get_diff.return_value = "diff --git a/x b/x\n+one\n"
    backend.checkout.side_effect = RuntimeError("no checkout in this test")
    orch._resolve_backend = lambda _repo_url: backend  # type: ignore[method-assign]
    orch._opus.review_diff.return_value = {"verdict": " Pass\n", "feedback": "ok"}

    await _review(orch, task_id, project)

    updated = await orch._tq.get_task(task_id)
    assert updated is not None
    assert updated["status"] == TaskStatus.PASSED
    rows = await orch._tq._db.fetch_all(
        "SELECT outcome FROM task_outcomes WHERE task_id = ?", (task_id,)
    )
    assert [r["outcome"] for r in rows] == ["pass"]


@pytest.mark.unit
async def test_a_resolved_clarification_with_no_answer_is_parked_for_a_human(
    orchestrator_fixture,
):
    """ "Resolved" with an empty answer resolves nothing.

    ``record_clarification_answer`` writes the answer into the worker's progress
    note under "ANSWER TO YOUR EARLIER QUESTION (act on this now)". An empty one
    redispatches a blocked worker having told it that its question was answered.
    """
    from orchestrator.core.clarification_states import ASKED

    orch, task_id, project = orchestrator_fixture
    await orch._tq.update_task_status(task_id, TaskStatus.NEEDS_CLARIFICATION)
    await orch._tq._db.execute(
        "UPDATE tasks SET clarification_state = ?, clarification_question = ? "
        "WHERE id = ?",
        (ASKED, "which module?", task_id),
    )
    orch._opus.is_available.return_value = True
    orch._opus.answer_clarification.return_value = {
        "resolved": True,
        "confidence": 0.95,
    }

    await orch.handle_clarification(task_id, project)

    updated = await orch._tq.get_task(task_id)
    assert updated is not None
    assert updated["status"] == TaskStatus.NEEDS_CLARIFICATION
    assert updated["clarification_state"] != "answered_by_brain"


@pytest.mark.unit
async def test_a_missing_integration_pr_does_not_blame_the_wrong_cause(
    orchestrator_fixture,
):
    """A completed plan with a NULL url may still have a real open PR.

    ``on_plan_completed`` opens the PR and records it in a separate step whose
    failure is deliberately non-fatal, so "the PR could not be opened" invites
    an operator to open a second one.
    """
    orch, task_id, project = orchestrator_fixture
    task = await orch._tq.get_task(task_id)
    assert task is not None
    plan_id = task["plan_id"]
    await orch._tq._db.execute(
        "UPDATE plans SET status = 'completed' WHERE id = ?", (plan_id,)
    )

    with pytest.raises(ValueError, match="check the remote for an open PR"):
        await orch.approve_plan_integration(plan_id, project)

    await orch._tq._db.execute(
        "UPDATE plans SET status = 'active' WHERE id = ?", (plan_id,)
    )
    with pytest.raises(ValueError, match="not completed"):
        await orch.approve_plan_integration(plan_id, project)


@pytest.mark.unit
async def test_a_silent_verify_failure_is_not_reported_as_an_infra_error(
    db, monkeypatch
):
    """A command may print nothing and exit non-zero, and that is a real failure.

    ``test -f dist/bundle.js`` is the shape. Keying the message on the emptiness
    of the output rather than on the STATUS told the reader the gate raised
    during clone or checkout, which is a different fault with a different
    remedy, for a genuine cross-task regression.
    """
    from unittest.mock import MagicMock

    from orchestrator.core import orchestrator_review as mod
    from orchestrator.core.event_bus import EventBus
    from orchestrator.core.github_credentials import PatCredentialProvider
    from orchestrator.core.orchestrator import Orchestrator
    from tests.test_orchestrator import _setup

    task_queue, plan_id, task_id = await _setup(db)
    await task_queue.update_task_status(task_id, TaskStatus.MERGED)
    await db.execute(
        "UPDATE projects SET verify_cmd = ? WHERE id = 'p1'", ("test -f dist/x",)
    )

    published: list[dict[str, Any]] = []
    bus = EventBus()
    bus.publish = published.append  # type: ignore[method-assign]

    mock_git = AsyncMock()
    mock_git.open_integration_pr = AsyncMock(
        return_value="https://github.com/u/a/pull/9"
    )
    mock_git.repo_slug = MagicMock(return_value="u/a")
    mock_git._provider = PatCredentialProvider("test-token-xyz")
    monkeypatch.setattr(mod, "clone_with_token", MagicMock(), raising=True)
    monkeypatch.setattr(mod, "checkout_branch", MagicMock(), raising=True)
    # Exit status is the verdict; the output is incidental and may be empty.
    # Two calls since 2026-08-27: the plan branch, then the base branch the
    # backstop compares it against. Green on the base, so the silent failure is
    # attributed to this plan and still reaches the event under test -- a single
    # ``return_value`` would make it red on both and correctly publish nothing.
    monkeypatch.setattr(
        mod, "run_verify", AsyncMock(side_effect=[(False, ""), (True, "")])
    )

    orch = Orchestrator(
        task_queue=task_queue,
        agent_manager=MagicMock(),
        opus_bridge=AsyncMock(),
        git_ops=mock_git,
        event_bus=bus,
        context_sync=None,
    )
    await orch.on_plan_completed(plan_id=plan_id)

    failed = next(e for e in published if e["type"] == "plan_verify_failed")
    assert failed["status"] == "failed"
    assert "errored" not in failed["output"]
    assert "FAILED" in failed["output"]


@pytest.mark.unit
async def test_the_checkbox_sync_does_not_claim_a_push_git_refused(
    orchestrator_fixture, monkeypatch, caplog
):
    """``commit_and_push`` returns False when the index was already clean.

    Discarding that and logging "Flipped checkbox ... (target repo ...)" reports
    a push that did not happen. git_ops documents that the caller must be able
    to tell the two apart.
    """
    from pathlib import Path
    from unittest.mock import MagicMock

    from orchestrator.core import orchestrator_review as mod

    orch, task_id, project = orchestrator_fixture
    task = await orch._tq.get_task(task_id)
    assert task is not None
    await orch._tq._db.execute(
        "INSERT INTO doc_index (path, category, title, content_hash) "
        "VALUES ('plan.md', 'plan', 'P', 'h')"
    )
    orch._doc_indexer = AsyncMock()

    def _fake_clone(repo_url: str, ws: str, token: str) -> None:
        Path(ws, "plan.md").write_text("- [ ] A\n", encoding="utf-8")

    monkeypatch.setattr(mod, "clone_with_token", MagicMock(side_effect=_fake_clone))
    monkeypatch.setattr(mod, "commit_and_push", MagicMock(return_value=False))

    with caplog.at_level("INFO", logger="orchestrator.core.orchestrator_review"):
        await orch._sync_plan_checkbox(task)

    messages = [r.getMessage() for r in caplog.records]
    assert not any(m.startswith("Flipped checkbox") for m in messages)
    assert any("nothing was pushed" in m for m in messages)


@pytest.mark.unit
async def test_the_checkbox_sync_says_when_it_searched_nothing(
    orchestrator_fixture, monkeypatch, caplog
):
    """No plan file in the clone is an absent search, not a negative result."""
    from unittest.mock import MagicMock

    from orchestrator.core import orchestrator_review as mod

    orch, task_id, project = orchestrator_fixture
    task = await orch._tq.get_task(task_id)
    assert task is not None
    await orch._tq._db.execute(
        "INSERT INTO doc_index (path, category, title, content_hash) "
        "VALUES ('plan.md', 'plan', 'P', 'h')"
    )
    orch._doc_indexer = AsyncMock()
    monkeypatch.setattr(mod, "clone_with_token", MagicMock())

    with caplog.at_level("DEBUG", logger="orchestrator.core.orchestrator_review"):
        await orch._sync_plan_checkbox(task)

    messages = [r.getMessage() for r in caplog.records]
    assert any("no checkbox was searched" in m for m in messages)
    assert not any("no unchecked item" in m for m in messages)


@pytest.mark.unit
async def test_the_gate_reports_the_reason_its_caller_gave_it(orchestrator_fixture):
    """``_verify_plan_branch`` itself, not a stub of it.

    The caller can suppress the command for a reason of its own (bench
    condition C), and only the caller knows which. Without honouring that, the
    gate states the one reason it can think of, which is a claim about the
    operator's configuration.
    """
    orch, _task_id, _project = orchestrator_fixture

    stated = await orch._verify_plan_branch(
        "https://github.com/o/r",
        "plan/x",
        None,
        disabled_reason=_SKIP_BENCH_MODE_DISABLED,
    )
    default = await orch._verify_plan_branch("https://github.com/o/r", "plan/x", None)

    assert stated.status == "skipped"
    assert stated.reason == _SKIP_BENCH_MODE_DISABLED
    assert default.reason != _SKIP_BENCH_MODE_DISABLED


@pytest.mark.unit
async def test_the_triage_evidence_carries_no_verify_exit_code_it_did_not_see(
    orchestrator_fixture, monkeypatch
):
    """The evidence pack the triage brain acts on, built by the review path.

    A verify command that exits non-zero gives ``run_verify`` a bool, so even
    there the code is unknown; on a plain reviewer failure no verify command ran
    at all. Stating 1 told the brain a verification had failed, and that
    decision is acted on irreversibly with one triage call per leaf.
    """
    from orchestrator.models.schemas import TriageDecision

    orch, task_id, project = orchestrator_fixture
    gated = dict(project)
    gated["verify_cmd"] = "pytest -q"
    backend = AsyncMock()
    backend.name = "github"
    backend.checkout.return_value = "/tmp/checkout"  # noqa: S108 - never touched
    orch._resolve_backend = lambda _repo_url: backend  # type: ignore[method-assign]
    monkeypatch.setattr(
        "orchestrator.core.orchestrator_review.run_verify",
        AsyncMock(return_value=(False, "1 failed")),
    )
    await orch._tq.retry_task(task_id)
    await orch._tq.update_task_status(task_id, TaskStatus.REVIEWING)
    orch._triage_leaf = AsyncMock(  # type: ignore[method-assign]
        return_value=TriageDecision(decision="retry", reason="one more")
    )

    await orch.review_task(task_id, gated)

    evidence = orch._triage_leaf.await_args.args[0]
    attempt = evidence.attempts[0]
    assert attempt["verify_exit_code"] is None
    assert attempt["files_touched"] is None
    assert attempt["loc_delta"] is None
