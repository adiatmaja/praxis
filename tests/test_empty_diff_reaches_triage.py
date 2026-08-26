"""A leaf that fails by producing NOTHING must reach adaptive triage.

Measured live on 2026-08-26. A one-leaf ``execute_plan`` run on a real
repository: the worker produced no changes, the declared-edit-locations
discriminator correctly refused to close it as a false ``no_changes`` and
failed it, it was re-dispatched, produced nothing again, and again. Final
state: ``attempt=3``, ``tasks.triage_decision`` NULL the whole way, plan
FAILED. Triage was never called once. The leaf was never split, never
escalated, never handed to a human with a reason.

The cause was structural, not conditional. The triage gate lived only on the
review-verdict path at the end of ``review_task``; the empty-diff path decides
its own outcome and called ``_fail_and_maybe_retry`` directly, so the most
common repeated-failure shape on the flagship path could not be triaged at all.

Both directions of the fix fail SILENTLY, which is why both are pinned here:

- A worker-attributable failure that skips triage looks exactly like the old
  retry loop right up to the point the plan dies untriaged. There is no error
  and no log line saying a decision was skipped.
- An infrastructure failure that REACHES triage looks exactly like a healthy
  system, right up to the point one brain call reasons about evidence that says
  nothing about the leaf and answers ``human`` -- which is terminal, and which
  no clock undoes.

So the empty-diff DECLINE is not one fact. ``no_change_outcome`` declines for
several unrelated reasons and only two of them are about the worker: a declared
edit location the branch does not carry, and the leaf's OWN declared
verification refuting the no-op on that branch. Everything else -- the branch
could not be resolved, the gate raised, a configured gate could not reach the
repository, and the PROJECT verify command going red -- is the same class as a
reviewer that could not run, and is excluded for the same reason.

The project command joined that list on 2026-08-26, and it had been the whole
of the attributable set. On THIS path the branch verified is the tree the worker
was handed unchanged, so a red verdict is red identically before and after the
attempt -- the exact shape ``review_task`` calls ``_GATE_UNATTRIBUTED`` and
refuses to charge (cd0c127). It never discriminated what it was read as
discriminating either: on a healthy repository the identical worker behaviour is
CLOSED as a no-op, so the only empty diffs it ever charged were the ones sitting
on a red repository, and repository health was being written into the column the
capability loop reads as worker capability.
"""
# ruff: noqa: S101

from __future__ import annotations

from collections.abc import Sequence
from typing import Any
from unittest.mock import AsyncMock

import pytest

from orchestrator.core.llm_router import ProviderAuthError
from orchestrator.core.orchestrator_review import (
    _SKIP_NO_TOKEN,
    REVIEW_ERROR_ATTEMPT_CAP,
    _DeclaredPathCheck,
    _LeafVerifyRun,
    _PlanVerifyResult,
)
from orchestrator.models.schemas import TaskStatus, TriageDecision


#: The leaf's OWN declared verification, run on the branch it was cut from and
#: REFUTING the no-op. This -- not a red project command -- is what makes an
#: empty diff worker-attributable, and it is the same positive signal cd0c127
#: chose for the same question on the review path.
_LEAF_REFUTED = _LeafVerifyRun('python -c "import a"', passed=False, output="boom")


def _gate(
    orch: Any,
    status: str,
    reason: str = "",
    paths: _DeclaredPathCheck | None = None,
    leaf: _LeafVerifyRun | None = None,
) -> None:
    """Replace the base-branch verify gate with a stub of a stated verdict.

    ``reason``, ``paths`` and ``leaf`` are all load-bearing rather than
    decoration. ``_verify_plan_branch`` returns ``skipped`` for four different
    reasons and only two of them close a leaf; ``paths`` and ``leaf`` are the
    second and third independent questions the same checkout answers, and they
    are the only two that are about the LEAF rather than the repository. A stub
    that flattened any of them could only ever exercise a verdict the real gate
    never produces.
    """

    async def _stub(
        repo_url: str,
        branch: str,
        verify_cmd: str | None,
        disabled_reason: str | None = None,
        require_paths: Sequence[str] = (),
        leaf_verify_cmd: str | None = None,
    ) -> _PlanVerifyResult:
        return _PlanVerifyResult(status, reason=reason, paths=paths, leaf=leaf)

    orch._verify_plan_branch = _stub


def _empty_diff(orch: Any) -> None:
    """Make the pull request carry no diff at all.

    Both backends fetch the diff through a checked command, so "" can only mean
    the command succeeded and printed nothing. That is a MEASUREMENT, and the
    tests below depend on it being one.
    """
    orch._git.get_pr_diff.return_value = ""


async def _second_attempt(orch: Any, task_id: str) -> None:
    """Move the task to its second attempt, parked in REVIEWING."""
    await orch._tq.retry_task(task_id)
    await orch._tq.update_task_status(task_id, TaskStatus.REVIEWING)


def _never_called() -> AsyncMock:
    """A triage stub for the tests that assert triage does NOT happen.

    It returns a real, benign ``retry`` decision rather than a bare mock, for
    the reason ``test_orchestrator_triage`` gives: a bare ``AsyncMock()`` return
    reaches the database and raises a binding error, so the test would go red on
    that instead of on ``assert_not_awaited`` and the mutation check would prove
    nothing about the bound under test.
    """
    return AsyncMock(return_value=TriageDecision(decision="retry", reason="n/a"))


# ---------------------------------------------------------------------------
# (i) A second worker-attributable empty diff DOES reach triage
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_a_second_empty_diff_failure_reaches_triage(
    orchestrator_fixture: tuple[Any, str, dict[str, Any]],
) -> None:
    """THE measured case. The worker produced nothing, twice.

    The leaf's OWN declared verification RAN on the branch it was cut from and
    refuted the no-op, so the absence of a change is evidence about this
    worker's output and nothing else. Before the fix this went straight to
    ``_fail_and_maybe_retry`` and the brain was never asked.
    """
    orch, task_id, project = orchestrator_fixture
    _empty_diff(orch)
    _gate(orch, "failed", leaf=_LEAF_REFUTED)
    await _second_attempt(orch, task_id)
    orch._triage_leaf = AsyncMock(
        return_value=TriageDecision(decision="retry", reason="one more")
    )

    await orch.review_task(task_id, project)

    orch._triage_leaf.assert_awaited_once()


@pytest.mark.unit
async def test_the_triage_decision_is_honoured_on_the_empty_diff_path(
    orchestrator_fixture: tuple[Any, str, dict[str, Any]],
) -> None:
    """Reaching the brain is worth nothing if the answer is then discarded.

    ``human`` is the one decision whose end state NO other path can produce
    here, and that is why it is the assertion. At attempt 2 of a max_retries=3
    project the plain path REQUEUES the task: it ends PENDING with attempt 3.
    Triage answering ``human`` ends it FAILED with retries still on the clock.
    A test that asserted only "the task is not REVIEWING", or that used a
    ``retry`` decision, would pass identically whether triage ran or not,
    because ``_fail_and_maybe_retry`` reaches the same PENDING row by itself.
    """
    orch, task_id, project = orchestrator_fixture
    _empty_diff(orch)
    _gate(orch, "failed", leaf=_LEAF_REFUTED)
    await _second_attempt(orch, task_id)
    orch._triage_leaf = AsyncMock(
        return_value=TriageDecision(decision="human", reason="the leaf is ambiguous")
    )

    await orch.review_task(task_id, project)

    task = await orch._tq.get_task(task_id)
    assert task is not None
    assert task["status"] == TaskStatus.FAILED, (
        "a triage decision of human is terminal; a PENDING row here means the "
        "plain retry path decided this task and the brain call was wasted"
    )
    assert int(task["attempt"]) < int(project["max_retries"]), (
        "the distinguishing fact: this leaf still had a retry left, so only "
        "triage could have made it terminal"
    )
    assert task["triage_decision"] == "human"
    assert "the leaf is ambiguous" in (task["review_feedback"] or "")


@pytest.mark.unit
async def test_the_empty_diff_evidence_pack_carries_the_measured_zero(
    orchestrator_fixture: tuple[Any, str, dict[str, Any]],
) -> None:
    """Zero files touched is a MEASUREMENT here, and must be stated as one.

    ``leaf_triage._unknown`` renders ``None`` as "unknown (not measured)" and
    documents why: "zero files touched is the signature of a worker that did
    nothing, which pushes the triage decision toward escalate or human. The
    brain is entitled to know the difference between 'nothing changed' and
    'nobody looked'."

    On this path the diff WAS fetched and WAS empty, so "nothing changed" is
    the true statement and the one that carries the signal. Passing ``None``
    would tell the brain nobody looked, which is false and strictly weaker
    evidence -- it would suppress exactly the push this path needs.
    """
    orch, task_id, project = orchestrator_fixture
    _empty_diff(orch)
    _gate(orch, "failed", leaf=_LEAF_REFUTED)
    await _second_attempt(orch, task_id)
    orch._triage_leaf = AsyncMock(
        return_value=TriageDecision(decision="retry", reason="one more")
    )

    await orch.review_task(task_id, project)

    evidence = orch._triage_leaf.await_args.args[0]
    attempt = evidence.attempts[0]
    assert attempt["files_touched"] == 0, (
        "the diff was fetched and measured empty; reporting that as unknown "
        "hides the signature of a worker that did nothing"
    )
    assert attempt["loc_delta"] == 0
    assert attempt["diff"] == ""
    # The pack must describe THIS failure, not a review verdict that never
    # happened: the brain is never shown a reviewer's opinion of an empty diff.
    assert "carries no diff" in attempt["review_reason"]
    # And it is still the right leaf.
    assert evidence.task_slug == "a"


@pytest.mark.unit
async def test_the_first_empty_diff_failure_still_takes_the_cheap_retry_path(
    orchestrator_fixture: tuple[Any, str, dict[str, Any]],
) -> None:
    """ADaPT: decompose only when the executor actually fails, twice.

    One empty diff is not yet evidence about the leaf's SIZE, and the retry is
    far cheaper than the brain call. The empty-diff path must inherit that
    bound, not just the gate.
    """
    orch, task_id, project = orchestrator_fixture
    _empty_diff(orch)
    _gate(orch, "failed", leaf=_LEAF_REFUTED)
    orch._triage_leaf = _never_called()

    await orch.review_task(task_id, project)

    orch._triage_leaf.assert_not_awaited()
    task = await orch._tq.get_task(task_id)
    assert task is not None
    assert task["status"] == TaskStatus.PENDING
    assert task["attempt"] == 2


@pytest.mark.unit
async def test_an_empty_diff_triage_runs_at_most_once_per_leaf(
    orchestrator_fixture: tuple[Any, str, dict[str, Any]],
) -> None:
    """One triage call per leaf lifetime, whichever path reaches it first.

    ``tasks.triage_decision`` is the bound, and it is shared: a leaf triaged
    from the review-verdict path must not buy a second call by failing with an
    empty diff next time.
    """
    orch, task_id, project = orchestrator_fixture
    _empty_diff(orch)
    _gate(orch, "failed", leaf=_LEAF_REFUTED)
    await orch._tq.retry_task(task_id)
    await orch._tq.record_triage_decision(task_id, "retry")
    await orch._tq.update_task_status(task_id, TaskStatus.REVIEWING)
    orch._triage_leaf = _never_called()

    await orch.review_task(task_id, project)

    orch._triage_leaf.assert_not_awaited()


# ---------------------------------------------------------------------------
# (ii) Failures that are NOT about the worker never reach triage
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_an_empty_diff_the_gate_could_not_verify_never_reaches_triage(
    orchestrator_fixture: tuple[Any, str, dict[str, Any]],
) -> None:
    """The clone, the checkout or the command RAISED. That is infrastructure.

    Nothing here says anything about the leaf, so triaging it would spend a
    brain call to reason about evidence that is not about the worker and could
    permanently ``human``-gate a leaf over a broken deployment. Without this
    guard a fix that simply triages every empty-diff decline passes every test
    above.
    """
    orch, task_id, project = orchestrator_fixture
    _empty_diff(orch)
    _gate(orch, "error")
    await _second_attempt(orch, task_id)
    orch._triage_leaf = _never_called()

    await orch.review_task(task_id, project)

    orch._triage_leaf.assert_not_awaited()
    task = await orch._tq.get_task(task_id)
    assert task is not None
    assert task["triage_decision"] is None
    assert task["status"] == TaskStatus.PENDING


@pytest.mark.unit
async def test_an_empty_diff_whose_gate_could_not_reach_the_repo_never_triages(
    orchestrator_fixture: tuple[Any, str, dict[str, Any]],
) -> None:
    """A CONFIGURED gate that could not reach the repository is the same class.

    ``_no_op_evidence`` already refuses to close a leaf on this skip, in its own
    words because "a verify command IS configured and the gate could not reach
    the repository". The same sentence is the reason it must not triage either:
    the skip is a broken deployment, not a fact about the worker.
    """
    orch, task_id, project = orchestrator_fixture
    _empty_diff(orch)
    _gate(orch, "skipped", reason=_SKIP_NO_TOKEN)
    await _second_attempt(orch, task_id)
    orch._triage_leaf = _never_called()

    await orch.review_task(task_id, project)

    orch._triage_leaf.assert_not_awaited()
    task = await orch._tq.get_task(task_id)
    assert task is not None
    assert task["triage_decision"] is None


@pytest.mark.unit
async def test_a_reviewer_that_could_not_run_never_reaches_triage(
    orchestrator_fixture: tuple[Any, str, dict[str, Any]],
) -> None:
    """The reviewer failed, not the change. The gate's own comment says so.

    ``_handle_review_error`` words its feedback for the floor model that reads
    it next: "The change itself was never judged, so nothing here says it is
    wrong." Triaging that would hand the brain a reviewer's stack trace as
    evidence about a leaf, and ``human`` would gate the leaf permanently over a
    gateway blip. This path shares ``_fail_and_maybe_retry``, never the gate.
    """
    orch, task_id, project = orchestrator_fixture
    orch._opus.review_diff.side_effect = ProviderAuthError("claude", "claude login")
    await _second_attempt(orch, task_id)
    orch._triage_leaf = _never_called()

    for _ in range(REVIEW_ERROR_ATTEMPT_CAP):
        await orch.review_task(task_id, project)

    orch._triage_leaf.assert_not_awaited()
    task = await orch._tq.get_task(task_id)
    assert task is not None
    assert task["triage_decision"] is None
    # It really did reach the failure path, so the assertion above is about the
    # gate and not about a task that quietly stayed in REVIEWING forever.
    assert task["status"] == TaskStatus.PENDING
    assert "reviewer failed" in (task["review_feedback"] or "")


# ---------------------------------------------------------------------------
# Which declines are worker-attributable, decided where the fact is known
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_a_missing_declared_path_is_worker_attributable(
    orchestrator_fixture: tuple[Any, str, dict[str, Any]],
) -> None:
    """The strongest evidence there is: the branch does not carry the file.

    It outranks every verdict the command can give, and it is the only decline
    that names something the next worker can act on.
    """
    orch, task_id, project = orchestrator_fixture
    _gate(
        orch,
        "passed",
        paths=_DeclaredPathCheck(missing=("src/a.py",)),
    )
    plan = await orch._tq.get_plan((await orch._tq.get_task(task_id))["plan_id"])

    decision = await orch.no_change_outcome(task_id, project, plan)

    assert decision.closed is False
    assert decision.worker_attributable is True


@pytest.mark.unit
async def test_a_red_project_command_alone_is_not_worker_attributable(
    orchestrator_fixture: tuple[Any, str, dict[str, Any]],
) -> None:
    """This test asserted the OPPOSITE until 2026-08-26, and it was wrong.

    It read: "the gate produced a real answer about the repository and it
    refutes the no-op ... the same class of evidence the review-verdict path
    already triages on". Two things in that were false. The evidence is not the
    same class: on the review path the command runs on the PR HEAD, which
    carries the worker's change, and cd0c127 then compares it against the base
    before charging anyone. Here it runs on the branch the leaf was cut FROM,
    and the worker changed nothing, so head and base are one tree -- the
    comparison cd0c127 makes is satisfied automatically and its answer is always
    "not this task's". And the gate answering something about the REPOSITORY is
    not the same as answering something about the LEAF, which is the only
    question attribution asks.

    A test that pins a defect outlives it. This one is rewritten rather than
    deleted, because the shape it exercises is still the one a real run produces
    most often and something has to hold the corrected answer.
    """
    orch, task_id, project = orchestrator_fixture
    _gate(orch, "failed")
    plan = await orch._tq.get_plan((await orch._tq.get_task(task_id))["plan_id"])

    decision = await orch.no_change_outcome(task_id, project, plan)

    assert decision.closed is False, (
        "no positive evidence closed the leaf, so it must still fail closed"
    )
    assert decision.worker_attributable is False


@pytest.mark.unit
async def test_a_leaf_verification_that_ran_and_failed_is_worker_attributable(
    orchestrator_fixture: tuple[Any, str, dict[str, Any]],
) -> None:
    """The other half, and the one that keeps the correction from being a mute.

    The leaf's OWN declared command ran on that same tree and refuted the no-op.
    That IS about this leaf, so it may buy the triage call the test above must
    not. Asserted beside its opposite, on the same fixture and the same gate
    verdict, so the only thing that differs between them is the fact under test.
    """
    orch, task_id, project = orchestrator_fixture
    _gate(orch, "failed", leaf=_LEAF_REFUTED)
    plan = await orch._tq.get_plan((await orch._tq.get_task(task_id))["plan_id"])

    decision = await orch.no_change_outcome(task_id, project, plan)

    assert decision.closed is False
    assert decision.worker_attributable is True


@pytest.mark.unit
async def test_a_leaf_verification_that_passed_closes_the_leaf_as_a_no_op(
    orchestrator_fixture: tuple[Any, str, dict[str, Any]],
) -> None:
    """The third answer, and the one that stops the dependent chain dying.

    The project command is red for a sibling's contract and the leaf's own check
    passes on the same tree: the leaf really is satisfied. Without this the leaf
    is retried to the identical correct answer until its attempts are spent, and
    every leaf behind it becomes unreachable.
    """
    orch, task_id, project = orchestrator_fixture
    _gate(
        orch,
        "failed",
        leaf=_LeafVerifyRun('python -c "import a"', passed=True, output="ok"),
    )
    plan = await orch._tq.get_plan((await orch._tq.get_task(task_id))["plan_id"])

    decision = await orch.no_change_outcome(task_id, project, plan)

    assert decision.closed is True
    assert decision.worker_attributable is False


@pytest.mark.unit
async def test_a_gate_that_raised_is_not_worker_attributable(
    orchestrator_fixture: tuple[Any, str, dict[str, Any]],
) -> None:
    """No answer was produced, so there is nothing about the worker to weigh."""
    orch, task_id, project = orchestrator_fixture
    _gate(orch, "error")
    plan = await orch._tq.get_plan((await orch._tq.get_task(task_id))["plan_id"])

    decision = await orch.no_change_outcome(task_id, project, plan)

    assert decision.closed is False
    assert decision.worker_attributable is False


@pytest.mark.unit
async def test_a_gate_that_could_not_reach_the_repository_is_not_attributable(
    orchestrator_fixture: tuple[Any, str, dict[str, Any]],
) -> None:
    """A configured gate that never ran establishes nothing either way."""
    orch, task_id, project = orchestrator_fixture
    _gate(orch, "skipped", reason=_SKIP_NO_TOKEN)
    plan = await orch._tq.get_plan((await orch._tq.get_task(task_id))["plan_id"])

    decision = await orch.no_change_outcome(task_id, project, plan)

    assert decision.closed is False
    assert decision.worker_attributable is False


@pytest.mark.unit
async def test_an_unresolvable_base_branch_is_not_worker_attributable(
    orchestrator_fixture: tuple[Any, str, dict[str, Any]],
) -> None:
    """Nothing was checked at all; the project row is what is wrong."""
    orch, task_id, project = orchestrator_fixture
    blind = dict(project)
    blind["repo_url"] = ""
    blind["default_branch"] = ""

    decision = await orch.no_change_outcome(task_id, blind, None)

    assert decision.closed is False
    assert decision.worker_attributable is False


@pytest.mark.unit
async def test_a_leaf_closed_as_a_no_op_is_not_a_worker_attributable_failure(
    orchestrator_fixture: tuple[Any, str, dict[str, Any]],
) -> None:
    """A no-op is a SUCCESS. There is no failure here to attribute to anyone."""
    orch, task_id, project = orchestrator_fixture
    _gate(orch, "passed")
    plan = await orch._tq.get_plan((await orch._tq.get_task(task_id))["plan_id"])

    decision = await orch.no_change_outcome(task_id, project, plan)

    assert decision.closed is True
    assert decision.worker_attributable is False


@pytest.mark.unit
async def test_the_decision_still_unpacks_as_closed_and_why(
    orchestrator_fixture: tuple[Any, str, dict[str, Any]],
) -> None:
    """Two call sites outside this module unpack the result as a 2-tuple.

    ``api/internal.py`` (the worker callback) and
    ``orchestrator_dispatch.py`` (the micro-edit lane) both do
    ``closed, why = await ...no_change_outcome(...)``. Widening the seam must
    not silently break either of them, and neither is typed strictly enough for
    mypy to notice if it did.
    """
    orch, task_id, project = orchestrator_fixture
    _gate(orch, "failed", leaf=_LEAF_REFUTED)
    plan = await orch._tq.get_plan((await orch._tq.get_task(task_id))["plan_id"])

    closed, why = await orch.no_change_outcome(task_id, project, plan)

    assert closed is False
    assert isinstance(why, str)
    assert "own declared verification" in why
