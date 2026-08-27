"""The whole-plan backstop must ask the base branch before crying regression.

``on_plan_completed`` runs the project's verify command against the accumulated
plan branch and, when it goes red, publishes ``plan_verify_failed`` describing a
cross-task regression. Until this file existed it made that claim with NO base
comparison at all, so on a repository whose default branch is red by design -
which this project's own live rig (``adiatmaja/playground``) is - the alarm fired
on every completed plan and meant nothing.

This is the fourth and last seat of the class the other three were fixed at on
2026-08-26: a fact about the REPOSITORY or the BASE BRANCH recorded as a fact
about THIS plan. The rule is the same one ``attribute_wave_verify_failure``
states one layer up, and only TWO of its three parts apply here: the project
command settles REGRESSION, the base branch settles ATTRIBUTION, and there is no
third "positive signal" step because a plan branch carries several leaves and no
single leaf's declared check can speak for the whole tree.

Deliberately NOT escalated into a gate. The integration PR is opened either way,
before and after, because an operator who has learned this event is advisory must
not one day find it blocking integration.
"""

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from orchestrator.core.event_bus import EventBus
from orchestrator.core.orchestrator import Orchestrator
from orchestrator.core.orchestrator_review import (
    _PLAN_VERIFY_UNATTRIBUTED,
    _PlanVerifyResult,
)
from orchestrator.database import Database
from orchestrator.models.schemas import TaskStatus
from tests.test_orchestrator import _setup


_REVIEW_LOGGER = "orchestrator.core.orchestrator_review"

# ``_setup`` activates the plan onto this branch and leaves ``default_branch``
# at its column default.
_PLAN_BRANCH = "plan/2026-06-01-auth"
_BASE_BRANCH = "main"

_VERIFY_CMD = "uv run pytest -q"


class _NotAskedError(AssertionError):
    """Raised when the base branch is asked on a path that must not ask it."""


async def _seed(db: Database) -> tuple[Any, str]:
    """A project with a verify command and one merged task, ready to complete."""
    task_queue, plan_id, task_id = await _setup(db)
    await task_queue.update_task_status(task_id, TaskStatus.MERGED)
    await db.execute(
        "UPDATE projects SET verify_cmd = ? WHERE id = 'p1'", (_VERIFY_CMD,)
    )
    return task_queue, plan_id


def _orchestrator(task_queue: Any, bus: EventBus) -> Orchestrator:
    """An orchestrator whose integration PR always opens, so the gate is isolated."""
    git = AsyncMock()
    git.open_integration_pr = AsyncMock(return_value="https://github.com/u/a/pull/9")
    git.repo_slug = MagicMock(return_value="u/a")
    # ``_existing_integration_pr`` must not find one, or the PR block short
    # circuits before the branch under test.
    git._token_for_repo = AsyncMock(return_value="tok")
    git._run_command = AsyncMock(return_value=(0, "[]", ""))
    return Orchestrator(
        task_queue=task_queue,
        agent_manager=MagicMock(),
        opus_bridge=AsyncMock(),
        git_ops=git,
        event_bus=bus,
        context_sync=None,
    )


def _gate(
    orch: Orchestrator,
    *,
    head: _PlanVerifyResult,
    base: _PlanVerifyResult | None,
) -> AsyncMock:
    """Answer the backstop's gate calls BY BRANCH.

    ``_verify_plan_branch`` is the seam both the head run and the base run go
    through, and they differ only in the branch argument, so the double has to
    key on it. A double keyed on call ORDER would pass just as well for an
    implementation that ran the base first, or twice, or against the wrong
    branch, which are the mistakes worth catching here.

    Args:
        orch: The orchestrator whose gate is replaced.
        head: What the gate says about the plan branch.
        base: What it says about ``main``, or None to assert it is never asked.

    Returns:
        The mock, so a test can count the calls it received.
    """

    async def _answer(
        _repo_url: str, branch: str, _verify_cmd: str | None, **_kwargs: Any
    ) -> _PlanVerifyResult:
        if branch == _PLAN_BRANCH:
            return head
        if branch != _BASE_BRANCH:
            wrong = f"the gate ran against an unexpected branch: {branch!r}"
            raise _NotAskedError(wrong)
        if base is None:
            unwanted = (
                "the base branch was asked on a path that has no verdict to "
                "attribute; that costs a second full clone and test run for "
                "nothing"
            )
            raise _NotAskedError(unwanted)
        return base

    mock = AsyncMock(side_effect=_answer)
    orch._verify_plan_branch = mock  # type: ignore[method-assign]
    return mock


def _drain(bus: EventBus) -> Any:
    """Subscribe-and-drain helper returning a getter for published events."""
    queue = bus.subscribe()

    def _events() -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        while not queue.empty():
            out.append(queue.get_nowait())
        return out

    return _events


async def _complete(
    db: Database, *, head: _PlanVerifyResult, base: _PlanVerifyResult | None
) -> tuple[list[dict[str, Any]], AsyncMock]:
    """Drive one plan through ``on_plan_completed`` with the gate doubled."""
    task_queue, plan_id = await _seed(db)
    bus = EventBus()
    events = _drain(bus)
    orch = _orchestrator(task_queue, bus)
    gate = _gate(orch, head=head, base=base)

    await orch.on_plan_completed(plan_id)

    return events(), gate


@pytest.mark.unit
async def test_a_base_branch_red_identically_publishes_no_alarm(
    db: Database, caplog: pytest.LogCaptureFixture
) -> None:
    """The defect, in one test.

    The plan branch is red and ``main`` is red on the SAME command, so nothing
    this plan merged is what broke it. Publishing ``plan_verify_failed`` here
    calls a pre-existing repository failure a cross-task regression, and on a
    repository whose default branch is red by design it does so for every plan
    that ever completes.

    Both runs carry exit code 1 because this test's NAME is "identically", and
    since 2026-08-27 that word is a comparison rather than an assumption. A
    fixture supplying no codes at all would land in ``INCOMPARABLE`` and assert
    an identity it never created.
    """
    with caplog.at_level(logging.WARNING, logger=_REVIEW_LOGGER):
        published, gate = await _complete(
            db,
            head=_PlanVerifyResult(
                "failed", "E   ImportError: no module named a", returncode=1
            ),
            base=_PlanVerifyResult(
                "failed", "E   ImportError: no module named a", returncode=1
            ),
        )

    assert [e["type"] for e in published].count("plan_verify_failed") == 0, (
        "a failure that pre-dates the plan was published as its regression"
    )
    assert gate.await_count == 2, "the base branch was never asked"
    ready = next(e for e in published if e["type"] == "plan_integration_ready")
    # NOT "passed": the gate ran and went RED. Not "failed" either, which is
    # paired with the alarm at every reader this event has ever had.
    assert ready["verify_status"] == _PLAN_VERIFY_UNATTRIBUTED
    assert f"fails identically on {_BASE_BRANCH}" in caplog.text, (
        "a red plan branch left no trace anywhere"
    )


@pytest.mark.unit
async def test_a_base_branch_red_a_different_way_does_publish_the_alarm(
    db: Database,
) -> None:
    """A red base is not one answer, and status equality never said which.

    Both branches are red, and they are red for different reasons: the plan
    branch RAN the suite and an assertion failed (exit 1), the base never
    collected a test (exit 2). Something this plan merged changed the failure
    mode, which is precisely the claim ``plan_verify_failed`` has always made.
    Before 2026-08-27 the whole comparison was ``base.status == "failed"``, so
    this was silently filed with the case above and the alarm was suppressed.

    This seat alarms where the wave gate deliberately does not park: the
    integration PR is opened on every arm here, so the cost is an operator's
    attention rather than a plan wedged forever.
    """
    published, gate = await _complete(
        db,
        head=_PlanVerifyResult("failed", "E   AssertionError: 3 failed", returncode=1),
        base=_PlanVerifyResult(
            "failed", "E   ImportError while importing test module", returncode=2
        ),
    )

    assert gate.await_count == 2, "the base branch was never asked"
    assert [e["type"] for e in published].count("plan_verify_failed") == 1
    ready = next(e for e in published if e["type"] == "plan_integration_ready")
    assert ready["verify_status"] == "failed"


@pytest.mark.unit
async def test_a_green_base_branch_still_publishes_the_alarm(db: Database) -> None:
    """The arm that must keep working, and the reason the fix is not a mute.

    Green on the branch the plan was cut from and red on the plan branch is a
    real cross-task regression, and it is the only shape that ever was one.
    """
    published, gate = await _complete(
        db,
        head=_PlanVerifyResult("failed", "E   AssertionError: 1 failed test"),
        base=_PlanVerifyResult("passed", "all green"),
    )

    assert gate.await_count == 2
    failed = next(e for e in published if e["type"] == "plan_verify_failed")
    assert failed["status"] == "failed"
    assert "CROSS-LEAF REGRESSION" in failed["output"]
    assert f"PASSES on {_BASE_BRANCH}" in failed["output"]
    # The head command's own output still rides along: the sentence says WHOSE
    # failure it is, the output says WHAT failed, and an operator needs both.
    assert "1 failed test" in failed["output"]
    ready = next(e for e in published if e["type"] == "plan_integration_ready")
    assert ready["verify_status"] == "failed"


@pytest.mark.unit
@pytest.mark.parametrize(
    "base",
    [
        pytest.param(_PlanVerifyResult("error"), id="error"),
        pytest.param(
            _PlanVerifyResult("skipped", reason="no GitHub token for repo"),
            id="skipped-no-token",
        ),
    ],
)
async def test_a_base_comparison_that_could_not_be_made_fails_closed_and_says_so(
    db: Database, base: _PlanVerifyResult
) -> None:
    """An unanswered question must never buy a plan a silence.

    ``error`` and every skip mean the gate produced no ANSWER about the base
    branch. The alarm still fires - fail closed - and the reason NAMES the
    comparison that is missing rather than implying one was made, in the shared
    words ``core/verify_gate.base_comparison_unavailable`` gives the review seat.

    The clause is spelled out here rather than obtained by CALLING that
    function, and the difference is the whole guard. Deriving the expected value
    from the code under test makes the assertion tautological: a mutation of the
    shared function moves both sides together and the test stays green, which is
    measured -- the first draft of this file did exactly that and the mutation
    survived. Written out, an edit to that one function turns this file AND
    ``tests/test_review_verify_attribution.py`` red together, which is the only
    thing that makes "shared" mean anything.
    """
    published, gate = await _complete(
        db, head=_PlanVerifyResult("failed", "E   ImportError"), base=base
    )

    assert gate.await_count == 2
    failed = next(e for e in published if e["type"] == "plan_verify_failed")
    assert failed["status"] == "failed"
    assert "could NOT be established" in failed["output"]
    assert (
        f"the same command could not be run on {_BASE_BRANCH} "
        f"(status={base.status}, reason={base.reason or '-'})"
    ) in failed["output"]
    assert "E   ImportError" in failed["output"]
    ready = next(e for e in published if e["type"] == "plan_integration_ready")
    assert ready["verify_status"] == "failed"


@pytest.mark.unit
async def test_a_head_error_is_published_without_asking_the_base(
    db: Database,
) -> None:
    """A gate that did not RUN has no verdict to attribute.

    Asking the base here could only buy a second full clone and test run to
    compare against nothing, which is the same call the wave gate makes one
    layer up. ``_gate`` raises if the base is asked, so this cannot pass by the
    base merely agreeing.
    """
    published, gate = await _complete(db, head=_PlanVerifyResult("error"), base=None)

    assert gate.await_count == 1
    failed = next(e for e in published if e["type"] == "plan_verify_failed")
    assert failed["status"] == "error"
    assert "errored" in failed["output"]
    ready = next(e for e in published if e["type"] == "plan_integration_ready")
    assert ready["verify_status"] == "error"


@pytest.mark.unit
async def test_a_passing_gate_never_asks_the_base_and_never_alarms(
    db: Database,
) -> None:
    """The green path pays nothing for the comparison."""
    published, gate = await _complete(
        db, head=_PlanVerifyResult("passed", "all green"), base=None
    )

    assert gate.await_count == 1
    assert "plan_verify_failed" not in [e["type"] for e in published]
    ready = next(e for e in published if e["type"] == "plan_integration_ready")
    assert ready["verify_status"] == "passed"


@pytest.mark.unit
async def test_the_integration_pr_is_opened_on_every_arm(db: Database) -> None:
    """The event stays ADVISORY.

    An operator who has learned that ``plan_verify_failed`` does not block
    integration must not find that it suddenly does. Both new arms - the
    unattributed one that publishes nothing, and the uncompared one that
    publishes - still reach the integration PR.
    """
    for head, base in (
        (_PlanVerifyResult("failed", "red"), _PlanVerifyResult("failed", "red")),
        (_PlanVerifyResult("failed", "red"), _PlanVerifyResult("error")),
    ):
        published, _gate_mock = await _complete(db, head=head, base=base)
        ready = next(e for e in published if e["type"] == "plan_integration_ready")
        assert ready["pr_url"] == "https://github.com/u/a/pull/9"
        assert ready["integration_status"] == "opened"
        await db.execute("DELETE FROM tasks")
        await db.execute("DELETE FROM plans")
        await db.execute("DELETE FROM projects")
        await db.execute("DELETE FROM users")
