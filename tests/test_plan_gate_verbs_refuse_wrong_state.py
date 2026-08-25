"""``approve`` and ``reject`` are the improvement-proposal gate, not a setter.

Both verbs read the plan and then called ``update_plan_status`` with no regard
for what state the plan was in, so the dashboard was the only thing holding the
contract: it gates both buttons on ``status === "pending" && source ===
"autonomous"``. The API, MCP and ``curl`` had no such gate, and three of the
reachable states are destructive:

* approving a PENDING **execute-plan** (one still awaiting decomposition) sets
  it ACTIVE with ``opus_plan`` NULL. ``process_plan_once`` then routes an ACTIVE
  plan with no graph to ``plan_and_activate``, which finds no ``spec_path``
  (execute-plan rows never have one) and fails the plan. ONE call destroys it,
  and the work it was carrying lives only in ``pending_input``;
* approving a COMPLETED plan re-activates it. The next tick re-completes it,
  re-runs ``on_plan_completed`` and ``check_improvements``, and produces a fresh
  improvement proposal nobody asked for;
* rejecting a COMPLETED or FAILED plan writes REJECTED over a verdict that was
  already reached, taking the diagnosis with it.

What must keep working is the mid-flight cancel. ``_still_activatable`` exists
because a reject can legitimately land while the planner is running, so ACTIVE
stays rejectable; the positive controls at the bottom pin that.
"""

# ruff: noqa: S101

from __future__ import annotations

import json
from typing import Any

import pytest
from httpx import AsyncClient

from orchestrator.database import Database


_GRAPH: dict[str, Any] = {
    "plan_summary": "Auth",
    "plan_slug": "auth",
    "tasks": [{"title": "Login", "slug": "login", "description": "Build login"}],
}


async def _seed_project(db: Database) -> str:
    await db.execute(
        "INSERT INTO users (id, name, token_hash) VALUES (?, ?, ?)",
        ("u1", "User", "hash"),
    )
    await db.execute(
        """INSERT INTO projects (id, user_id, name, repo_url, model_name,
                                 default_branch)
           VALUES (?, ?, ?, ?, ?, ?)""",
        ("p1", "u1", "App", "https://github.com/u/a", "deepseek", "main"),
    )
    return "p1"


async def _seed_plan(
    db: Database,
    plan_id: str,
    *,
    status: str,
    source: str = "user",
    opus_plan: dict[str, Any] | None = None,
    spec_path: str | None = None,
    pending_input: str | None = None,
    error: str | None = None,
) -> None:
    await db.execute(
        """INSERT INTO plans
           (id, project_id, source, status, opus_plan, spec_path,
            pending_input, error)
           VALUES (?, 'p1', ?, ?, ?, ?, ?, ?)""",
        (
            plan_id,
            source,
            status,
            json.dumps(opus_plan) if opus_plan else None,
            spec_path,
            pending_input,
            error,
        ),
    )


async def _row(db: Database, plan_id: str) -> dict[str, Any]:
    plan = await db.fetch_one("SELECT * FROM plans WHERE id = ?", (plan_id,))
    assert plan is not None
    return plan


@pytest.mark.integration
async def test_approving_a_pending_execute_plan_is_refused_and_names_the_state(
    client: AsyncClient, db: Database, auth_headers: dict[str, str]
) -> None:
    """The one that destroys a plan outright, in one call.

    An execute-plan needs no approval: the loop decomposes it on its own. What
    approval DID was force it ACTIVE with a NULL graph, which is the one shape
    ``process_plan_once`` hands to the spec planner, and it has no spec.
    """
    await _seed_project(db)
    await _seed_plan(
        db,
        "ep-1",
        status="pending",
        source="execute-plan",
        pending_input=json.dumps({"plan": "Add auth", "model": "m", "branch": "b"}),
    )

    resp = await client.post("/api/plans/ep-1/approve", headers=auth_headers)

    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert "execute-plan" in detail or "no task graph" in detail, detail
    plan = await _row(db, "ep-1")
    assert plan["status"] == "pending", "the row was mutated by a refused call"
    assert plan["pending_input"], "the plan's only copy of its work must survive"


@pytest.mark.integration
async def test_approving_a_completed_plan_is_refused(
    client: AsyncClient, db: Database, auth_headers: dict[str, str]
) -> None:
    """Re-activating a landed plan re-runs the improvement loop behind it."""
    await _seed_project(db)
    await _seed_plan(db, "done-1", status="completed", opus_plan=_GRAPH)

    resp = await client.post("/api/plans/done-1/approve", headers=auth_headers)

    assert resp.status_code == 409, resp.text
    assert "completed" in resp.json()["detail"], resp.text
    assert (await _row(db, "done-1"))["status"] == "completed"


@pytest.mark.integration
async def test_rejecting_a_completed_plan_is_refused(
    client: AsyncClient, db: Database, auth_headers: dict[str, str]
) -> None:
    """REJECTED written over a plan that landed is a lie about what happened."""
    await _seed_project(db)
    await _seed_plan(db, "done-2", status="completed", opus_plan=_GRAPH)

    resp = await client.post("/api/plans/done-2/reject", headers=auth_headers)

    assert resp.status_code == 409, resp.text
    assert "completed" in resp.json()["detail"], resp.text
    assert (await _row(db, "done-2"))["status"] == "completed"


@pytest.mark.integration
async def test_rejecting_a_failed_plan_keeps_the_diagnosis(
    client: AsyncClient, db: Database, auth_headers: dict[str, str]
) -> None:
    """A terminal verdict and its reason are evidence, not a draft."""
    await _seed_project(db)
    await _seed_plan(
        db,
        "dead-1",
        status="failed",
        opus_plan=_GRAPH,
        error="the planner answered in prose",
    )

    resp = await client.post("/api/plans/dead-1/reject", headers=auth_headers)

    assert resp.status_code == 409, resp.text
    assert "failed" in resp.json()["detail"], resp.text
    plan = await _row(db, "dead-1")
    assert plan["status"] == "failed"
    assert plan["error"] == "the planner answered in prose"


@pytest.mark.integration
async def test_a_missing_plan_still_answers_404_not_409(
    client: AsyncClient, db: Database, auth_headers: dict[str, str]
) -> None:
    """The state check must not swallow the identity check.

    A 409 naming a state for a plan that does not exist would send an operator
    looking for a row that was never there.
    """
    await _seed_project(db)

    approve = await client.post("/api/plans/nope/approve", headers=auth_headers)
    reject = await client.post("/api/plans/nope/reject", headers=auth_headers)

    assert approve.status_code == 404
    assert reject.status_code == 404


# ---------------------------------------------------------------------------
# Positive controls, LAST. Every assertion above is a refusal, and a gate that
# refused EVERYTHING would satisfy all of them while breaking the only two
# things these verbs exist to do.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_the_proposal_gate_still_opens_and_still_closes(
    client: AsyncClient, db: Database, auth_headers: dict[str, str]
) -> None:
    """The autonomous improvement proposal: approve dispatches, reject closes."""
    await _seed_project(db)
    await _seed_plan(
        db, "prop-1", status="pending", source="autonomous", opus_plan=_GRAPH
    )
    await _seed_plan(
        db, "prop-2", status="pending", source="autonomous", opus_plan=_GRAPH
    )

    approved = await client.post("/api/plans/prop-1/approve", headers=auth_headers)
    rejected = await client.post("/api/plans/prop-2/reject", headers=auth_headers)

    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "active"
    assert rejected.status_code == 200, rejected.text
    assert rejected.json()["status"] == "rejected"


@pytest.mark.integration
async def test_an_active_plan_can_still_be_cancelled(
    client: AsyncClient, db: Database, auth_headers: dict[str, str]
) -> None:
    """The mid-flight cancel ``_still_activatable`` was written for.

    A reject landing while the planner runs is a supported operation: the
    orchestrator re-reads the status after the brain call precisely so it does
    not write ACTIVE back over the rejecter's answer. Refusing ACTIVE here
    would take that away.
    """
    await _seed_project(db)
    await _seed_plan(db, "live-1", status="active", opus_plan=_GRAPH)

    resp = await client.post("/api/plans/live-1/reject", headers=auth_headers)

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "rejected"


@pytest.mark.integration
async def test_a_pending_spec_plan_can_still_be_approved(
    client: AsyncClient, db: Database, auth_headers: dict[str, str]
) -> None:
    """A submitted spec has something to plan FROM, so approving it is honest.

    This is the case the execute-plan refusal must not catch by accident: the
    distinction is whether the row carries anything the activation path can
    read, not which endpoint created it.
    """
    await _seed_project(db)
    await _seed_plan(
        db,
        "spec-1",
        status="pending",
        source="user",
        spec_path="docs/superpowers/specs/auth.md",
    )

    resp = await client.post("/api/plans/spec-1/approve", headers=auth_headers)

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "active"
