"""Drive bench instances through Praxis via the REST API.

The runner owns no engine logic.  It registers a project pointed at a prepared
bare repo, submits the issue either monolithically (condition A) or as a plan to
decompose (conditions B, C, D), polls to a terminal state, and writes one
``AttemptRecord``.  Grading happens separately, in ``bench/grade.py``, with the
official harness.

Two of its rules exist because breaking them is INVISIBLE in the published
numbers rather than loud:

- a verify-gated condition (B, D) with no resolved ``verify_cmd`` is refused
  before anything is dispatched.  Passing ``None`` makes the plan-branch verify
  gate return ``"skipped"``, and ``"skipped"`` is not distinguishable from
  ``"passed"`` by either of its callers, so condition B would silently run as
  condition C while its record still claimed a gate.  The ablation B versus C
  would then compare an arm to itself and report a null result nobody could
  trace;
- the poll's terminal set is imported from the frozen status vocabulary rather
  than retyped.  ``superseded`` is terminal here (a split parent ends there), so
  a retyped ``("merged", "failed", "passed")`` tuple would leave a finished
  attempt polling until the one-hour ceiling and record it as a timeout.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from bench.config import CONDITIONS, SEEDS, WORKERS, Condition, Worker
from bench.metrics import AttemptRecord, append_record
from orchestrator.core.status_vocab import GATED_STATUSES, TERMINAL_STATUSES
from orchestrator.models.schemas import PlanStatus, TaskStatus


logger = logging.getLogger(__name__)

# Poll cadence and ceiling for one attempt. An instance that has not reached a
# terminal state by the ceiling is recorded as an error, never dropped.
_POLL_INTERVAL_S = 15
_ATTEMPT_TIMEOUT_S = 60 * 60

# The states that end a task poll. TERMINAL_STATUSES is the frozen vocabulary
# ({failed, merged, superseded}); ``passed`` is added DELIBERATELY on top of it.
# ``passed`` means review succeeded and the change is parked at the human merge
# gate. The bench registers projects with auto_merge=True and
# approval_gate=False so that should not happen, but a protected base or a
# supply-chain diff guard forces the gate regardless of the review verdict. From
# the runner's point of view that attempt IS over: nothing further will move it
# without a human, so polling on would only burn the ceiling and mislabel a
# finished attempt as a timeout. It is counted as a human gate touch instead.
TERMINAL_TASK_STATUSES: frozenset[str] = TERMINAL_STATUSES | {TaskStatus.PASSED.value}

# The states that end a plan poll, from the PlanStatus enum for the same reason.
TERMINAL_PLAN_STATUSES: frozenset[str] = frozenset(
    {
        PlanStatus.COMPLETED.value,
        PlanStatus.FAILED.value,
        PlanStatus.REJECTED.value,
    }
)


class ConfoundedDesignError(Exception):
    """Raised when a requested condition set cannot support its comparisons."""


class MissingVerifyCommandError(Exception):
    """Raised when a verify-gated condition has no verify command to run."""


class MissingProblemStatementError(Exception):
    """Raised when a sample entry carries no issue text to hand the worker."""


def assert_matched_pair(condition_keys: list[str]) -> None:
    """Refuse a condition set that would produce a confounded comparison.

    Condition C is condition B with the verify gate disabled.  Its only valid
    reading is "how much of B's effect is verification", which requires the
    no-gate baseline A in the same run.  Running C without A yields a number
    with no interpretation, so the runner refuses rather than producing one.

    Args:
        condition_keys: The condition keys requested for this run.

    Raises:
        ConfoundedDesignError: On an unknown key, or on C without A.
    """
    known = {c.key for c in CONDITIONS}
    unknown = set(condition_keys) - known
    if unknown:
        message = f"unknown conditions: {sorted(unknown)}; known are {sorted(known)}"
        raise ConfoundedDesignError(message)
    if "C" in condition_keys and "A" not in condition_keys:
        message = (
            "condition C (decomposition without the verify gate) requires "
            "condition A (monolithic without a verify gate) in the same run: "
            "they are a matched no-gate pair and the ablation is confounded "
            "without both"
        )
        raise ConfoundedDesignError(message)


@dataclass(frozen=True)
class Attempt:
    """One cell of the run matrix."""

    instance: dict[str, Any]
    condition: Condition
    worker: Worker
    seed: int


def plan_attempts(
    instances: list[dict[str, Any]],
    condition_keys: list[str],
    worker_keys: list[str],
    seeds: list[int],
) -> list[Attempt]:
    """Return the full, deterministically ordered cross product."""
    assert_matched_pair(condition_keys)
    by_condition = {c.key: c for c in CONDITIONS}
    by_worker = {w.key: w for w in WORKERS}
    attempts: list[Attempt] = []
    for instance in sorted(instances, key=lambda i: i["instance_id"]):
        for condition_key in sorted(condition_keys):
            for worker_key in sorted(worker_keys):
                for seed in sorted(seeds):
                    attempts.append(
                        Attempt(
                            instance=instance,
                            condition=by_condition[condition_key],
                            worker=by_worker[worker_key],
                            seed=seed,
                        )
                    )
    return attempts


def resolve_verify_cmd(
    instance: dict[str, Any], condition: Condition, default: str | None
) -> str | None:
    """Return the verify command this cell must run under, or refuse.

    Precedence is per-instance over run-wide: a repo with its own fast check
    should use it, and the ``--verify-cmd`` default covers the rest.

    An ungated condition (A, C) always resolves to ``None``, even when a
    command is available: A and C are the matched no-gate pair, and quietly
    gating either of them is the same confound in the other direction.

    A gated condition (B, D) that resolves nothing is REFUSED rather than run
    with ``None``.  ``None`` produces a project with no ``verify_cmd``, which
    makes the plan-branch verify gate return ``"skipped"``, which neither
    ``DispatchMixin._wave_verify_gate`` (parks only on failed/error) nor
    ``ReviewMixin.on_plan_completed`` (opens the integration PR either way)
    can tell apart from ``"passed"``.  Condition B would then be condition C
    wearing B's label, and no number in the report would show it.

    Args:
        instance: One sample entry, which may carry a ``verify_cmd`` key.
        condition: The arm this cell runs under.
        default: The run-wide ``--verify-cmd``, if one was given.

    Returns:
        The command for a gated condition, or ``None`` for an ungated one.

    Raises:
        MissingVerifyCommandError: If a gated condition resolves nothing.
    """
    if not condition.verify_gate:
        return None
    candidate = instance.get("verify_cmd") or default
    if candidate is None or not str(candidate).strip():
        message = (
            f"condition {condition.key} ({condition.label}) is verify-gated but "
            f"instance {instance.get('instance_id')!r} resolves no verify_cmd. "
            "Set a 'verify_cmd' on the sample entry or pass --verify-cmd. "
            "Running without one would make the plan-branch gate return "
            "'skipped', which the orchestrator treats as a pass, so this arm "
            "would silently become the ungated arm."
        )
        raise MissingVerifyCommandError(message)
    return str(candidate)


def _issue_prompt(instance: dict[str, Any]) -> str:
    """Render the instance's problem statement as a worker-facing task.

    Args:
        instance: One sample entry.

    Returns:
        The worker-facing task text.

    Raises:
        MissingProblemStatementError: If the entry carries no issue text.
    """
    statement = instance.get("problem_statement")
    if not statement or not str(statement).strip():
        message = (
            f"instance {instance.get('instance_id')!r} carries no "
            "'problem_statement'; the committed sample must be enriched with "
            "the upstream issue text before a run (see bench/README.md)"
        )
        raise MissingProblemStatementError(message)
    return (
        f"{statement}\n\n"
        "Fix this in the repository. Add or update tests only where the issue "
        "describes behavior that should be covered."
    )


def assert_runnable(attempts: list[Attempt], verify_cmd_default: str | None) -> None:
    """Check the WHOLE matrix before the first container is spawned.

    Both failures this catches are per-instance data problems that would
    otherwise be swallowed by ``run_attempt``'s broad handler and recorded as
    error rows, one per cell, for the entire matrix.  Finding out after the run
    costs the whole run.

    Args:
        attempts: The planned matrix.
        verify_cmd_default: The run-wide ``--verify-cmd``, if one was given.

    Raises:
        MissingVerifyCommandError: If a gated cell resolves no verify command.
        MissingProblemStatementError: If an instance carries no issue text.
    """
    for attempt in attempts:
        resolve_verify_cmd(attempt.instance, attempt.condition, verify_cmd_default)
        _issue_prompt(attempt.instance)


class BenchClient:
    """Thin REST client. Mirrors src/mcp_server/client.PraxisClient deliberately."""

    def __init__(self, base_url: str, token: str) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=120,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def register_project(
        self,
        name: str,
        repo_url: str,
        worker: Worker,
        verify_cmd: str | None,
        adaptive_split: bool,
    ) -> str:
        """Create the project row that carries this condition's switches.

        ``/api/dispatch`` and ``/api/execute-plan`` are keyed on ``repo_url``
        and reuse an existing project for it (``SELECT * FROM projects WHERE
        repo_url = ?``), so registering first is how per-condition settings
        (``verify_cmd``, ``max_retries``, ``auto_merge``) reach the run.
        """
        response = await self._client.post(
            "/api/projects",
            json={
                "name": name,
                "repo_url": repo_url,
                "model_name": worker.model,
                "harness": worker.harness,
                "default_branch": "main",
                "verify_cmd": verify_cmd,
                # Only condition D reaches the second worker-attributable
                # failure that triggers adaptive triage; the others cap at one
                # attempt so triage can never fire. See bench/README.md.
                "max_retries": 3 if adaptive_split else 1,
                # The bench measures the engine, not the human gate.
                "auto_merge": True,
                "approval_gate": False,
            },
        )
        response.raise_for_status()
        return str(response.json()["id"])

    async def dispatch(self, repo_url: str, instructions: str, worker: Worker) -> str:
        """Monolithic condition A. DispatchRequest is keyed on repo_url."""
        response = await self._client.post(
            "/api/dispatch",
            json={
                "repo_url": repo_url,
                "instructions": instructions,
                "model": worker.model,
                "harness": worker.harness,
                "branch": "main",
            },
        )
        response.raise_for_status()
        return str(response.json()["task_id"])

    async def execute_plan(self, repo_url: str, plan: str, worker: Worker) -> str:
        """Decomposed conditions B, C, D. ExecutePlanRequest is keyed on repo_url."""
        response = await self._client.post(
            "/api/execute-plan",
            json={
                "repo_url": repo_url,
                "plan": plan,
                "model": worker.model,
                "harness": worker.harness,
                "branch": "main",
            },
        )
        response.raise_for_status()
        return str(response.json()["plan_id"])

    async def poll_plan(self, plan_id: str) -> dict[str, Any]:
        response = await self._client.get(f"/api/plans/{plan_id}")
        response.raise_for_status()
        return dict(response.json())

    async def plan_tasks(self, plan_id: str) -> list[dict[str, Any]]:
        response = await self._client.get(f"/api/plans/{plan_id}/tasks")
        response.raise_for_status()
        return list(response.json())

    async def get_task(self, task_id: str) -> dict[str, Any]:
        """Return one task row. ``GET /api/tasks/{id}`` wraps it under 'task'."""
        response = await self._client.get(f"/api/tasks/{task_id}")
        response.raise_for_status()
        return dict(response.json()["task"])


async def run_attempt(
    attempt: Attempt,
    client: BenchClient,
    run_id: str,
    repo_root: Path,
    out_path: Path,
    verify_cmd_default: str | None = None,
) -> AttemptRecord:
    """Run one cell to a terminal state and record it.

    Every failure mode, including a timeout or an orchestrator error, produces a
    record with ``error`` set.  Nothing is dropped.

    The one exception is the verify-command refusal, which is raised BEFORE the
    handler and propagates out of this function.  A cell that cannot honor its
    own condition is a design error, not an observation: recording it as an
    error row would put a row labelled "condition B" in the file for a cell that
    never ran as B.

    Args:
        attempt: The cell to run.
        client: REST client for the running orchestrator.
        run_id: Identifier shared by every row of this run.
        repo_root: Directory holding the prepared bare repos.
        out_path: JSONL file rows are appended to.
        verify_cmd_default: Run-wide ``--verify-cmd``, if one was given.

    Returns:
        The recorded attempt.

    Raises:
        MissingVerifyCommandError: If a gated condition resolves no command.
        MissingProblemStatementError: If the instance carries no issue text.
    """
    instance = attempt.instance
    # Both resolutions run OUTSIDE the handler below, on purpose: they must
    # abort the run rather than become data.
    verify_cmd = resolve_verify_cmd(instance, attempt.condition, verify_cmd_default)
    prompt = _issue_prompt(instance)

    bare = repo_root / f"{instance['instance_id']}.git"
    started = time.monotonic()
    error: str | None = None
    leaf_count = 0
    leaf_retries = 0
    whole_task_retries = 0
    clarifications = 0
    human_gate_touches = 0

    try:
        await client.register_project(
            name=f"bench-{instance['instance_id']}-{attempt.condition.key}-{uuid.uuid4().hex[:6]}",
            repo_url=str(bare),
            worker=attempt.worker,
            # Condition C and the matched baseline A run WITHOUT a verify gate.
            verify_cmd=verify_cmd,
            adaptive_split=attempt.condition.adaptive_split,
        )

        if attempt.condition.decompose:
            plan_id = await client.execute_plan(str(bare), prompt, attempt.worker)
            deadline = time.monotonic() + _ATTEMPT_TIMEOUT_S
            while time.monotonic() < deadline:
                plan = await client.poll_plan(plan_id)
                if plan["status"] in TERMINAL_PLAN_STATUSES:
                    break
                await asyncio.sleep(_POLL_INTERVAL_S)
            else:
                error = "attempt timed out"
            tasks = await client.plan_tasks(plan_id)
            leaf_count = len(tasks)
            leaf_retries = sum(max(int(t.get("attempt", 1)) - 1, 0) for t in tasks)
            clarifications = sum(1 for t in tasks if t.get("clarification_question"))
            human_gate_touches = sum(
                1 for t in tasks if t.get("status") in GATED_STATUSES
            )
        else:
            task_id = await client.dispatch(str(bare), prompt, attempt.worker)
            deadline = time.monotonic() + _ATTEMPT_TIMEOUT_S
            leaf_count = 1
            while time.monotonic() < deadline:
                task = await client.get_task(task_id)
                if task["status"] in TERMINAL_TASK_STATUSES:
                    whole_task_retries = max(int(task.get("attempt", 1)) - 1, 0)
                    if task["status"] in GATED_STATUSES:
                        human_gate_touches = 1
                    break
                await asyncio.sleep(_POLL_INTERVAL_S)
            else:
                error = "attempt timed out"
    except Exception as exc:  # noqa: BLE001 - an errored cell is data, not a crash
        logger.exception("attempt failed: %s", instance["instance_id"])
        error = f"{type(exc).__name__}: {exc}"

    record = AttemptRecord(
        run_id=run_id,
        instance_id=instance["instance_id"],
        condition=attempt.condition.key,
        worker=attempt.worker.key,
        seed=attempt.seed,
        stratum_patch=instance["stratum_patch"],
        stratum_repo=instance["stratum_repo"],
        # Grading happens later, against the official harness.
        resolved=False,
        plausible=False,
        leaf_count=leaf_count,
        leaf_retries=leaf_retries,
        whole_task_retries=whole_task_retries,
        clarifications=clarifications,
        human_gate_touches=human_gate_touches,
        brain_tokens=0,
        worker_tokens=0,
        wall_clock_s=time.monotonic() - started,
        error=error,
    )
    append_record(out_path, record)
    return record


async def _main_async(args: argparse.Namespace) -> int:
    sample = json.loads(Path(args.sample).read_text(encoding="utf-8"))
    attempts = plan_attempts(
        sample["instances"],
        [k.strip() for k in args.conditions.split(",") if k.strip()],
        [k.strip() for k in args.worker.split(",") if k.strip()],
        [int(s) for s in args.seeds.split(",")] if args.seeds else list(SEEDS),
    )
    # Refuse the whole matrix up front rather than one error row per cell.
    assert_runnable(attempts, args.verify_cmd)

    run_id = args.run_id or f"run-{uuid.uuid4().hex[:8]}"
    out_path = Path(args.out or f"bench/.work/runs/{run_id}/attempts.jsonl")
    logger.info("run %s: %d attempts to %s", run_id, len(attempts), out_path)

    client = BenchClient(args.api, args.token)
    try:
        for index, attempt in enumerate(attempts, start=1):
            logger.info(
                "[%d/%d] %s condition %s worker %s seed %d",
                index,
                len(attempts),
                attempt.instance["instance_id"],
                attempt.condition.key,
                attempt.worker.key,
                attempt.seed,
            )
            await run_attempt(
                attempt,
                client,
                run_id,
                Path(args.repos),
                out_path,
                verify_cmd_default=args.verify_cmd,
            )
    finally:
        await client.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description="Run the Praxis bench matrix")
    parser.add_argument("--sample", required=True)
    parser.add_argument("--conditions", default="A,B")
    parser.add_argument("--worker", default="local-openweight")
    parser.add_argument("--seeds", default="1")
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--out", default=None)
    parser.add_argument("--repos", default="bench/.work/repos")
    parser.add_argument(
        "--verify-cmd",
        default=os.environ.get("PRAXIS_BENCH_VERIFY_CMD") or None,
        help=(
            "run-wide verify command for the gated conditions (B, D). A sample "
            "entry's own 'verify_cmd' wins over this. A gated condition with "
            "neither is refused before the run starts."
        ),
    )
    parser.add_argument(
        "--api", default=os.environ.get("PRAXIS_API", "http://127.0.0.1:12323")
    )
    parser.add_argument("--token", default=os.environ.get("AUTH_TOKEN", ""))
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    return asyncio.run(_main_async(args))


if __name__ == "__main__":
    sys.exit(main())
