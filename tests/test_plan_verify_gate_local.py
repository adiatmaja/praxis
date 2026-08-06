"""The whole-plan verify gate must actually run for a local-mode project.

``ReviewMixin._verify_plan_branch`` resolved a GitHub token before it did
anything else and returned ``skipped`` when there was none.  A local bare repo
has no credential BY DESIGN: ``core.preflight._preflight_local`` requires none
and ``agent_manager`` bind-mounts the repo instead of cloning it over the
network.  So for every local project both gates that call this method -- the
per-wave cross-leaf gate in ``DispatchMixin._wave_verify_gate`` and the
whole-plan backstop in ``ReviewMixin.on_plan_completed`` -- were dead.

Neither caller can tell ``skipped`` from ``passed`` by control flow, so nothing
anywhere reported that the gate had not run.  That is why this hid.

It matters beyond correctness: the benchmark's condition C is the
gate-disabled arm.  Measuring condition B with the gate silently off makes B
and C the same experiment, and every number comparing them meaningless.

Every test here drives a REAL bare repo on disk.  The verify command inspects a
file whose content differs between ``main`` and the plan branch, so a gate that
clones but never checks the plan branch out fails these tests rather than
passing them by accident.
"""

from __future__ import annotations

import sys
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from orchestrator.core.event_bus import EventBus
from orchestrator.core.git_ops import GitOps
from orchestrator.core.github_credentials import PatCredentialProvider
from orchestrator.core.orchestrator import Orchestrator
from orchestrator.core.orchestrator_review import (
    _SKIP_NO_CREDENTIAL_PROVIDER,
    _SKIP_NO_TOKEN,
    _SKIP_NO_VERIFY_CMD,
)
from orchestrator.core.task_queue import TaskQueue
from orchestrator.database import Database

# The bare-repo builder from the local-mode e2e suite: ``main`` holds
# ``return 1`` and the pushed worker branch holds ``return 2``.  Reused rather
# than rebuilt so there is one definition of "a real local repo" in the suite.
from tests.test_local_mode_e2e import seeded as _seeded_bare_repo


# Re-exported under a local name so pytest registers the fixture in this
# module too.  Importing it as ``seeded`` and naming the test parameters
# ``seeded`` would shadow the import in every signature (ruff F811).
bare_repo = _seeded_bare_repo


# ``seeded`` names its pushed branch ``agent/fix``.  The gate keys on the
# branch STRING it is handed and never on the name's shape, so this stands in
# for the accumulated plan branch without a second fixture.
_PLAN_BRANCH = "agent/fix"


def _verify_that_finds(needle: str) -> str:
    """A verify command that passes only when ``app.py`` contains ``needle``.

    ``main`` holds ``return 1`` and the plan branch holds ``return 2``, so the
    command's verdict reports WHICH tree the gate actually ran in, not merely
    that it ran something.
    """
    return (
        f'"{sys.executable}" -c "'
        f"import sys; src = open('app.py').read(); print(src); "
        f"sys.exit(0 if '{needle}' in src else 1)\""
    )


# Passes ONLY on the plan branch: ``return 2`` does not exist on ``main``, so a
# gate that clones the repo but never checks the plan branch out fails this.
_VERIFY_SEES_PLAN_BRANCH = _verify_that_finds("return 2")

# The mirror image: passes only on ``main``, the branch a fresh clone lands on.
_VERIFY_SEES_MAIN = _verify_that_finds("return 1")

# Fails outright, standing in for a cross-leaf regression on the plan branch.
_VERIFY_FAILS = (
    f'"{sys.executable}" -c "import sys; print(\'CROSS-LEAF REGRESSION\'); sys.exit(1)"'
)


def _orchestrator(
    db: Database, bus: EventBus | None = None, git: Any = None
) -> Orchestrator:
    """An Orchestrator wired exactly as a credential-less deployment is.

    ``main.lifespan`` falls back to ``PatCredentialProvider("")`` when no
    GitHub credential is configured, which is the normal state for local mode.
    Using the real provider (not a mock) is the point: an ``AsyncMock`` hands
    back a truthy token and would hide the very branch under test.
    """
    return Orchestrator(
        task_queue=TaskQueue(db),
        agent_manager=MagicMock(),
        opus_bridge=AsyncMock(),
        git_ops=GitOps(PatCredentialProvider("")) if git is None else git,
        event_bus=bus or EventBus(),
        context_sync=None,
    )


async def _seed_plan(db: Database, repo_url: str, verify_cmd: str) -> str:
    """Create a user, a local-mode project, and one activated plan.

    Args:
        db: The test database.
        repo_url: The project's repo_url (a bare-repo path, for local mode).
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
        ("p1", "u1", "App", repo_url, "qwen3.6-27b", 3, "main", verify_cmd),
    )
    task_queue = TaskQueue(db)
    plan_id = await task_queue.create_plan("p1", "Build auth")
    await task_queue.activate_plan(
        plan_id,
        {
            "plan_summary": "Auth",
            "plan_slug": "auth",
            "tasks": [
                {
                    "title": "Login",
                    "slug": "login",
                    "description": "Build login",
                    "depends_on": [],
                }
            ],
        },
        _PLAN_BRANCH,
    )
    return plan_id


def _drain(bus: EventBus) -> Any:
    """Subscribe-and-drain helper returning a getter for published events."""
    queue = bus.subscribe()

    def _events() -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        while not queue.empty():
            out.append(queue.get_nowait())
        return out

    return _events


@pytest.mark.integration
async def test_a_local_plan_branch_is_really_verified(
    db: Database, bare_repo: Any
) -> None:
    """The load-bearing one: a local project gets a real verdict, not a skip."""
    orch = _orchestrator(db)

    result = await orch._verify_plan_branch(
        str(bare_repo), _PLAN_BRANCH, _VERIFY_SEES_PLAN_BRANCH
    )

    assert result.status == "passed", f"gate did not run: {result!r}"
    assert "return 2" in result.output


@pytest.mark.integration
async def test_a_failing_local_verify_is_reported_as_failed(
    db: Database, bare_repo: Any
) -> None:
    """The gate must be able to FAIL.

    A gate that always reports ``passed`` is the same bug wearing a different
    hat: both callers advance on ``passed`` exactly as they advanced on the
    old silent ``skipped``.
    """
    orch = _orchestrator(db)

    result = await orch._verify_plan_branch(str(bare_repo), _PLAN_BRANCH, _VERIFY_FAILS)

    assert result.status == "failed", f"a non-zero exit must fail: {result!r}"
    assert "CROSS-LEAF REGRESSION" in result.output


@pytest.mark.integration
async def test_the_local_gate_runs_against_the_plan_branch_not_the_clone_default(
    db: Database, bare_repo: Any
) -> None:
    """Cloning is not checking out.

    A fresh clone of the bare repo lands on ``main``.  ``return 1`` exists only
    there, so a gate that skipped the checkout would report ``passed`` here and
    green a plan branch it never looked at.
    """
    orch = _orchestrator(db)

    result = await orch._verify_plan_branch(
        str(bare_repo), _PLAN_BRANCH, _VERIFY_SEES_MAIN
    )

    assert result.status == "failed", f"gate ran in the wrong tree: {result!r}"


@pytest.mark.integration
async def test_a_local_plan_branch_that_cannot_be_checked_out_errors(
    db: Database, bare_repo: Any
) -> None:
    """An unusable branch is an ``error``, never a ``skipped``.

    Both callers fail closed on ``error`` (park the wave, publish
    ``plan_verify_failed``) and neither reacts to ``skipped`` at all, so
    returning ``skipped`` here would re-open the hole for a different reason.
    """
    orch = _orchestrator(db)

    result = await orch._verify_plan_branch(
        str(bare_repo), "plan/never-pushed", _VERIFY_SEES_PLAN_BRANCH
    )

    assert result.status == "error", f"a broken checkout must fail closed: {result!r}"


@pytest.mark.integration
async def test_no_verify_cmd_is_the_only_local_skip(
    db: Database, bare_repo: Any
) -> None:
    """``skipped`` keeps exactly one meaning: there is nothing to run."""
    orch = _orchestrator(db)

    result = await orch._verify_plan_branch(str(bare_repo), _PLAN_BRANCH, None)

    assert result.status == "skipped"
    assert result.reason == _SKIP_NO_VERIFY_CMD


@pytest.mark.integration
async def test_a_github_project_without_a_token_still_skips(db: Database) -> None:
    """The GitHub path is untouched, including its credential-less skip.

    A GitHub repo with no credential cannot be fetched at all.  That carve-out
    predates this fix and stays: it is what lets someone run the orchestrator
    against local paths without a GitHub credential.  Pinned here so routing
    local mode away from it cannot quietly take GitHub with it.
    """
    orch = _orchestrator(db)

    result = await orch._verify_plan_branch(
        "https://github.com/u/a", "plan/x", _VERIFY_SEES_PLAN_BRANCH
    )

    assert result.status == "skipped"
    assert result.reason == _SKIP_NO_TOKEN


@pytest.mark.integration
async def test_a_github_project_without_a_credential_provider_skips(
    db: Database,
) -> None:
    """The other GitHub skip: no provider at all (``git_ops=None``)."""
    orch = _orchestrator(db, git=None)
    orch._git = None

    result = await orch._verify_plan_branch(
        "https://github.com/u/a", "plan/x", _VERIFY_SEES_PLAN_BRANCH
    )

    assert result.status == "skipped"
    assert result.reason == _SKIP_NO_CREDENTIAL_PROVIDER


@pytest.mark.integration
async def test_the_wave_gate_parks_a_local_plan_on_a_real_regression(
    db: Database, bare_repo: Any
) -> None:
    """Caller 1: the per-wave cross-leaf gate.

    Before the fix this returned True for every local plan, dispatching the
    next wave on top of a branch nobody had verified.
    """
    plan_id = await _seed_plan(db, str(bare_repo), _VERIFY_FAILS)
    bus = EventBus()
    events = _drain(bus)
    orch = _orchestrator(db, bus=bus)
    plan = dict(await orch._tq.get_plan(plan_id))  # type: ignore[arg-type]
    project = dict(await orch._tq.get_project("p1"))  # type: ignore[arg-type]

    proceed = await orch._wave_verify_gate(plan_id, plan, project, merged_count=1)

    assert proceed is False
    published = events()
    failed = [e for e in published if e["type"] == "plan_wave_verify_failed"]
    assert len(failed) == 1
    assert "CROSS-LEAF REGRESSION" in failed[0]["output"]


@pytest.mark.integration
async def test_the_wave_gate_lets_a_clean_local_plan_through(
    db: Database, bare_repo: Any
) -> None:
    """The fix must not park every local wave: a real pass still dispatches."""
    plan_id = await _seed_plan(db, str(bare_repo), _VERIFY_SEES_PLAN_BRANCH)
    bus = EventBus()
    events = _drain(bus)
    orch = _orchestrator(db, bus=bus)
    plan = dict(await orch._tq.get_plan(plan_id))  # type: ignore[arg-type]
    project = dict(await orch._tq.get_project("p1"))  # type: ignore[arg-type]

    proceed = await orch._wave_verify_gate(plan_id, plan, project, merged_count=1)

    assert proceed is True
    assert [e for e in events() if e["type"] == "plan_wave_verify_failed"] == []


@pytest.mark.integration
async def test_on_plan_completed_publishes_a_real_local_verdict(
    db: Database, bare_repo: Any
) -> None:
    """Caller 2: the whole-plan backstop.

    Before the fix ``verify_status`` was ``skipped`` on every local plan and
    ``plan_verify_failed`` never fired, so a cross-leaf break reached the
    integration step unnoticed.
    """
    plan_id = await _seed_plan(db, str(bare_repo), _VERIFY_FAILS)
    bus = EventBus()
    events = _drain(bus)
    # An AsyncMock git keeps ``open_integration_pr`` from shelling out to gh.
    # Its ``_provider`` mirrors production so a regression that routed local
    # mode back through the GitHub path would skip, and be caught here.
    git = AsyncMock()
    git._provider = PatCredentialProvider("")
    git.open_integration_pr = AsyncMock(return_value=None)
    orch = _orchestrator(db, bus=bus, git=git)

    await orch.on_plan_completed(plan_id)

    published = events()
    ready = next(e for e in published if e["type"] == "plan_integration_ready")
    assert ready["verify_status"] == "failed"
    failed = next(e for e in published if e["type"] == "plan_verify_failed")
    assert "CROSS-LEAF REGRESSION" in failed["output"]
