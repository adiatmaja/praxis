"""The per-wave cross-leaf gate must PROVE a regression before it parks a wave.

``DispatchMixin._wave_verify_gate`` ran the project ``verify_cmd`` against the
accumulated plan branch and parked the next wave on a non-zero exit, calling
that "a cross-leaf regression".  That word is a CLAIM, and it is only true if
the same command was GREEN on the branch the plan was cut from.

Measured live on 2026-08-26, on a two-leaf dependent plan whose project command
is red on ``main`` because the repository's acceptance file imports the symbol
LEAF 2 is contracted to write:

    Wave verify gate FAILED for plan c03b3ff6-... after 1 merged leaves;
    parking the next wave.

Nothing could ever clear that park.  ``merged_count`` advances only when
something merges, nothing can merge while the wave is parked, and the memo
``state[plan_id] = (merged_count, False)`` makes the verdict permanent for that
count.  No leaf is FAILED, so ``plan_reachability`` sees a healthy graph, and
the plan reads ACTIVE with a null ``error`` on every read-only surface.

This is the SAME un-compared inference commit cd0c127 removed from the review
path one layer down, and fixing that layer is what made this one reachable:
before it, leaf 1 of a dependent chain failed review and nothing ever merged, so
this gate never fired at all.

Every arm that can be driven on disk is driven on disk.  The bare repo holds
``return 1`` on ``main`` and ``return 2`` on the plan branch, and the verify
commands are imported from ``test_plan_verify_gate_local`` rather than rebuilt
so that "green on the base, red on the head" is a fact the fixture CREATES,
never a pair of mock return values that agree by construction.
"""

# ruff: noqa: S101

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from orchestrator.core.event_bus import EventBus
from orchestrator.core.git_backend import PullRequestRef
from orchestrator.core.git_ops import GitOps
from orchestrator.core.github_credentials import PatCredentialProvider
from orchestrator.core.orchestrator import Orchestrator
from orchestrator.core.orchestrator_review import _PlanVerifyResult
from orchestrator.core.task_queue import TaskQueue
from orchestrator.database import Database

# ONE definition of "a real local repo" and ONE definition of each verify
# command in the suite.  ``_VERIFY_SEES_MAIN`` passes only on ``main`` and so is
# the only true cross-leaf regression available; ``_VERIFY_FAILS`` fails on
# every branch and so is the live defect's shape; ``_VERIFY_SEES_PLAN_BRANCH``
# passes only on the plan branch.
from tests.test_local_mode_e2e import seeded as _seeded_bare_repo
from tests.test_plan_verify_gate_local import (
    _VERIFY_FAILS,
    _VERIFY_SEES_MAIN,
    _VERIFY_SEES_PLAN_BRANCH,
)


bare_repo = _seeded_bare_repo

_DISPATCH_LOGGER = "orchestrator.core.orchestrator_dispatch"

#: What ``seeded`` pushes.  The gate keys on the branch STRING it is handed, so
#: this stands in for the accumulated plan branch without a second fixture.
_PLAN_BRANCH = "agent/fix"
_BASE_BRANCH = "main"


def _orchestrator(db: Database, bus: EventBus | None = None) -> Orchestrator:
    """An Orchestrator wired as a credential-less local deployment is.

    The real ``PatCredentialProvider("")`` rather than a mock: an ``AsyncMock``
    hands back a truthy token and would route the local repo down the GitHub
    path, hiding every branch under test.
    """
    return Orchestrator(
        task_queue=TaskQueue(db),
        agent_manager=MagicMock(),
        opus_bridge=AsyncMock(),
        git_ops=GitOps(PatCredentialProvider("")),
        event_bus=bus or EventBus(),
        context_sync=None,
    )


async def _seed_plan(db: Database, repo_url: str, verify_cmd: str | None) -> str:
    """Create a user, a local-mode project, and one activated plan.

    Args:
        db: The test database.
        repo_url: The project's ``repo_url`` (a bare-repo path, for local mode).
        verify_cmd: The project's configured verification command.

    Returns:
        The plan id, whose ``plan_branch_name`` is ``_PLAN_BRANCH``.
    """
    await db.execute(
        "INSERT INTO users (id, name, token_hash) VALUES (?, ?, ?)",
        ("u1", "User", "hash"),
    )
    await db.execute(
        """INSERT INTO projects
           (id, user_id, name, repo_url, model_name, max_retries,
            default_branch, verify_cmd)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        ("p1", "u1", "App", repo_url, "qwen3.8-27b", 3, _BASE_BRANCH, verify_cmd),
    )
    queue = TaskQueue(db)
    plan_id = await queue.create_plan("p1", "Build a type checker")
    await queue.activate_plan(
        plan_id,
        {
            "plan_summary": "Hindley-Milner",
            "plan_slug": "hm",
            "tasks": [
                {
                    "title": "Types, lexer, parser",
                    "slug": "hm-core",
                    "description": "Build the core",
                    "depends_on": [],
                },
                {
                    "title": "infer_type",
                    "slug": "hm-infer",
                    "description": "Add inference",
                    "depends_on": ["hm-core"],
                },
            ],
        },
        _PLAN_BRANCH,
    )
    return plan_id


def _drain(bus: EventBus) -> Any:
    """Return a callable that empties the subscription into a list."""
    queue = bus.subscribe()

    def _pull() -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        while not queue.empty():
            out.append(queue.get_nowait())
        return out

    return _pull


# ---------------------------------------------------------------------------
# On disk: the fixture CREATES the difference the assertions read.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_a_wave_is_not_parked_when_the_base_branch_is_red_identically(
    db: Database, bare_repo: Any
) -> None:
    """THE DEFECT.  Red on the plan branch AND red on ``main`` is not ours.

    ``_VERIFY_FAILS`` exits non-zero in every tree, which is exactly the live
    shape: a project whose acceptance file imports a symbol a later leaf is
    contracted to write is red on ``main`` before the plan starts.  Parking on
    it kills the plan permanently and silently.
    """
    plan_id = await _seed_plan(db, str(bare_repo), _VERIFY_FAILS)
    bus = EventBus()
    events = _drain(bus)
    orch = _orchestrator(db, bus=bus)
    plan = dict(await orch._tq.get_plan(plan_id))  # type: ignore[arg-type]
    project = dict(await orch._tq.get_project("p1"))  # type: ignore[arg-type]

    proceed = await orch._wave_verify_gate(plan_id, plan, project, merged_count=1)

    assert proceed is True
    assert [e for e in events() if e["type"] == "plan_wave_verify_failed"] == []
    # Nothing was established about this plan, so nothing is stored against it.
    after = await orch._tq.get_plan(plan_id)
    assert after is not None
    assert after["error"] is None


@pytest.mark.integration
async def test_a_wave_is_parked_when_the_command_is_green_on_the_base(
    db: Database, bare_repo: Any
) -> None:
    """The arm that must not change: green on ``main``, red on the plan branch.

    ``_VERIFY_SEES_MAIN`` passes only where ``app.py`` holds ``return 1``, so
    the base is GREEN and the plan branch is RED.  That is a real cross-leaf
    regression and it parks, memoized, exactly as before.
    """
    plan_id = await _seed_plan(db, str(bare_repo), _VERIFY_SEES_MAIN)
    bus = EventBus()
    events = _drain(bus)
    orch = _orchestrator(db, bus=bus)
    plan = dict(await orch._tq.get_plan(plan_id))  # type: ignore[arg-type]
    project = dict(await orch._tq.get_project("p1"))  # type: ignore[arg-type]

    proceed = await orch._wave_verify_gate(plan_id, plan, project, merged_count=1)

    assert proceed is False
    failed = [e for e in events() if e["type"] == "plan_wave_verify_failed"]
    assert len(failed) == 1
    # The gate ran in the PLAN BRANCH tree (the command prints the source it
    # read), and the event says the comparison was made.
    assert "return 2" in failed[0]["output"]
    assert _BASE_BRANCH in failed[0]["output"]
    assert orch._wave_verify_state[plan_id] == (1, False)


@pytest.mark.integration
async def test_a_parked_wave_records_why_on_the_plan(
    db: Database, bare_repo: Any
) -> None:
    """The park that can never clear must not be invisible.

    A memoized park cannot be lifted by anything the loop does: ``merged_count``
    only advances when something merges and nothing can merge while the wave is
    parked.  ``plan_reachability`` cannot see it either -- it is pure over
    ``(opus_plan, task rows)`` and every leaf here is a healthy PENDING.  So the
    reason lands on ``plans.error``, which ``PlanResponse``, MCP ``poll_plan``,
    ``praxis plans`` and the dashboard already render.
    """
    plan_id = await _seed_plan(db, str(bare_repo), _VERIFY_SEES_MAIN)
    orch = _orchestrator(db)
    plan = dict(await orch._tq.get_plan(plan_id))  # type: ignore[arg-type]
    project = dict(await orch._tq.get_project("p1"))  # type: ignore[arg-type]

    assert await orch._wave_verify_gate(plan_id, plan, project, 1) is False

    after = await orch._tq.get_plan(plan_id)
    assert after is not None
    stored = str(after["error"] or "")
    assert _PLAN_BRANCH in stored
    assert _BASE_BRANCH in stored
    # It has to say the park will not clear on its own, or a reader waits.
    assert "parked" in stored


@pytest.mark.integration
async def test_a_green_plan_branch_never_pays_for_a_base_run(
    db: Database, bare_repo: Any
) -> None:
    """A passing head asks the base nothing: one clone and one test run, as before.

    Counted rather than asserted through a mock, because the count IS the
    behaviour: a second full verify run per wave boundary on every healthy plan
    would be a real cost regression.
    """
    plan_id = await _seed_plan(db, str(bare_repo), _VERIFY_SEES_PLAN_BRANCH)
    orch = _orchestrator(db)
    plan = dict(await orch._tq.get_plan(plan_id))  # type: ignore[arg-type]
    project = dict(await orch._tq.get_project("p1"))  # type: ignore[arg-type]

    branches: list[str] = []
    real = orch._verify_plan_branch

    async def _counting(
        repo_url: str, plan_branch: str, *args: Any, **kwargs: Any
    ) -> _PlanVerifyResult:
        branches.append(plan_branch)
        return await real(repo_url, plan_branch, *args, **kwargs)

    orch._verify_plan_branch = _counting  # type: ignore[method-assign]

    assert await orch._wave_verify_gate(plan_id, plan, project, 1) is True
    assert branches == [_PLAN_BRANCH]
    assert orch._wave_verify_state[plan_id] == (1, True)


# ---------------------------------------------------------------------------
# The status vocabulary, stubbed: every answer the base branch can give.
# ---------------------------------------------------------------------------


def _stub_two_branch_verify(
    orch: Orchestrator, head: _PlanVerifyResult, base: _PlanVerifyResult
) -> list[str]:
    """Answer ``head`` for the plan branch and ``base`` for ``main``.

    Returns the list the stub appends each requested branch to, so a caller can
    assert WHICH branches were asked and how often.
    """
    asked: list[str] = []

    async def _fake(
        repo_url: str, plan_branch: str, *args: Any, **kwargs: Any
    ) -> _PlanVerifyResult:
        asked.append(plan_branch)
        return head if plan_branch == _PLAN_BRANCH else base

    orch._verify_plan_branch = _fake  # type: ignore[method-assign]
    return asked


async def _seed_stub_plan(db: Database) -> tuple[Orchestrator, str, dict, dict, Any]:
    """A plan/project pair for the stubbed arms, with a drained bus."""
    plan_id = await _seed_plan(db, "https://github.com/u/a", "pytest -q")
    bus = EventBus()
    events = _drain(bus)
    orch = _orchestrator(db, bus=bus)
    plan = dict(await orch._tq.get_plan(plan_id))  # type: ignore[arg-type]
    project = dict(await orch._tq.get_project("p1"))  # type: ignore[arg-type]
    return orch, plan_id, plan, project, events


@pytest.mark.parametrize(
    ("base_status", "base_reason"),
    [
        ("error", ""),
        ("skipped", "no GitHub token is configured"),
    ],
)
@pytest.mark.integration
async def test_a_base_branch_that_could_not_be_asked_fails_closed(
    db: Database, base_status: str, base_reason: str
) -> None:
    """An unanswered question must never buy a plan a pass.

    ``error`` and every skip mean the comparison was NOT made.  The wave parks
    exactly as it did before the comparison existed -- and the event says the
    comparison is missing rather than implying it was made.
    """
    orch, plan_id, plan, project, events = await _seed_stub_plan(db)
    _stub_two_branch_verify(
        orch,
        head=_PlanVerifyResult("failed", "E   AttributeError: leaf.slug"),
        base=_PlanVerifyResult(base_status, reason=base_reason),
    )

    assert await orch._wave_verify_gate(plan_id, plan, project, 1) is False

    failed = [e for e in events() if e["type"] == "plan_wave_verify_failed"]
    assert len(failed) == 1
    assert "could NOT be established" in failed[0]["output"]
    assert _BASE_BRANCH in failed[0]["output"]
    # NOT memoized: the next loop tick asks again, because a transient
    # clone/network fault on the base must not wedge the plan forever.
    assert plan_id not in orch._wave_verify_state
    # And nothing durable is claimed about a plan whose fault this may not be.
    after = await orch._tq.get_plan(plan_id)
    assert after is not None
    assert after["error"] is None


@pytest.mark.integration
async def test_a_head_error_never_spends_a_second_run_on_the_base(
    db: Database,
) -> None:
    """The gate itself did not run, so there is nothing to attribute.

    Unchanged behaviour, pinned because the obvious refactor is to route every
    non-passing status through the comparison: an ``error`` on the head has no
    verdict to compare, and asking the base would be a second full test run
    bought for nothing.
    """
    orch, plan_id, plan, project, events = await _seed_stub_plan(db)
    asked = _stub_two_branch_verify(
        orch,
        head=_PlanVerifyResult("error"),
        base=_PlanVerifyResult("passed"),
    )

    assert await orch._wave_verify_gate(plan_id, plan, project, 1) is False

    assert asked == [_PLAN_BRANCH]
    assert len([e for e in events() if e["type"] == "plan_wave_verify_failed"]) == 1
    assert plan_id not in orch._wave_verify_state


@pytest.mark.integration
async def test_an_unattributed_wave_is_memoized_so_it_is_not_re_run(
    db: Database,
) -> None:
    """ "Not ours" is a real answer from a real double run, so it is reused.

    Without the memo the gate re-clones and re-runs the suite TWICE on every
    loop tick for the whole life of a plan whose project command is red on the
    base branch -- which is precisely the plan shape this fix exists for.
    """
    orch, plan_id, plan, project, _events = await _seed_stub_plan(db)
    asked = _stub_two_branch_verify(
        orch,
        head=_PlanVerifyResult("failed", "E   ImportError: infer_type"),
        base=_PlanVerifyResult("failed", "E   ImportError: infer_type"),
    )

    assert await orch._wave_verify_gate(plan_id, plan, project, 1) is True
    assert asked == [_PLAN_BRANCH, _BASE_BRANCH]

    assert await orch._wave_verify_gate(plan_id, plan, project, 1) is True
    assert asked == [_PLAN_BRANCH, _BASE_BRANCH]


@pytest.mark.integration
async def test_the_stored_reason_is_not_rewritten_every_tick(db: Database) -> None:
    """The stored string IS the latch, exactly as ``_reconcile_parked_plan``'s is.

    The memo is per-process, so a restart re-runs the gate and would re-write an
    identical row and re-log it forever.
    """
    orch, plan_id, plan, project, _events = await _seed_stub_plan(db)
    _stub_two_branch_verify(
        orch,
        head=_PlanVerifyResult("failed", "E   AttributeError: leaf.slug"),
        base=_PlanVerifyResult("passed"),
    )

    assert await orch._wave_verify_gate(plan_id, plan, project, 1) is False
    stored = str((await orch._tq.get_plan(plan_id))["error"])  # type: ignore[index]
    assert stored

    # A fresh process: the memo is gone, the plan row is not.
    orch._wave_verify_state.clear()
    refreshed = dict(await orch._tq.get_plan(plan_id))  # type: ignore[arg-type]
    writes: list[tuple[str, str]] = []
    real_set = orch._tq.set_plan_error

    async def _counting_set(pid: str, error: str) -> None:
        writes.append((pid, error))
        await real_set(pid, error)

    orch._tq.set_plan_error = _counting_set  # type: ignore[method-assign]

    assert await orch._wave_verify_gate(plan_id, refreshed, project, 1) is False
    assert writes == []


# ---------------------------------------------------------------------------
# One rule, two seats.  A change to either side turns this red.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("base_status", "held_against_the_plan"),
    [
        ("passed", True),
        ("failed", False),
        ("error", True),
        ("skipped", True),
    ],
)
@pytest.mark.integration
async def test_both_seats_attribute_a_red_command_the_same_way(
    db: Database, base_status: str, held_against_the_plan: bool
) -> None:
    """The wave gate and the review gate must answer one question identically.

    ``orchestrator_review._attribute_head_verify_failure`` (per task, on a PR
    head) and ``orchestrator_dispatch._wave_verify_gate`` (per wave, on the
    accumulated plan branch) both ask "is this red command OURS".  They are in
    different modules and only this test holds them together: drift here is
    silent, and the whole reason this gate needed fixing is that the review
    path's rule was corrected while this one kept the old inference.

    Both seats are really invoked; neither answer is stubbed.
    """
    orch, plan_id, plan, project, _events = await _seed_stub_plan(db)
    _stub_two_branch_verify(
        orch,
        head=_PlanVerifyResult("failed", "E   ImportError: infer_type"),
        base=_PlanVerifyResult(base_status, reason="stubbed"),
    )

    # Seat 1: the wave gate.  Parking IS holding it against the plan.
    parked = not await orch._wave_verify_gate(plan_id, plan, project, 1)

    # Seat 2: the review gate.  A non-None ``review`` IS holding it against the
    # task.  ``leaf_verification=None`` keeps the leaf-check arm out of the way
    # so both seats are answering the base-branch question alone.
    attribution = await orch._attribute_head_verify_failure(
        task={"id": "t1"},
        project=project,
        plan=plan,
        ref=PullRequestRef(backend="local", branch=_PLAN_BRANCH, base=_BASE_BRANCH),
        checkout="/nonexistent",
        verify_cmd="pytest -q",
        gate_output="E   ImportError: infer_type",
        leaf_verification=None,
        log=logging.getLogger(_DISPATCH_LOGGER),
    )
    held_by_review = attribution.review is not None

    assert parked is held_against_the_plan
    assert held_by_review is held_against_the_plan
