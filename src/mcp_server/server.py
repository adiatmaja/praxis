"""Praxis MCP server: tool implementations + FastMCP registration.

Each ``*_impl`` function takes a PraxisClient and is independently testable.
The FastMCP tool wrappers (registered at module import) build a client from env
and delegate. Tools never raise to the MCP client; client errors are caught and
returned as ``{"error": code, "message": ...}`` so the brain can react.
"""

from __future__ import annotations

import json
from importlib import resources
from typing import Any, cast

from mcp.server.fastmcp import FastMCP

from mcp_server.client import PraxisClient, PraxisClientError
from orchestrator.core import status_vocab


def load_orchestration_guide() -> str:
    """Read the packaged orchestration-guide markdown, CWD-independent.

    Returns:
        The full markdown content of the orchestration guide.
    """
    return (
        resources.files("mcp_server.resources")
        .joinpath("orchestration_guide.md")
        .read_text(encoding="utf-8")
    )


# Claude Code warns above 10,000 tokens of MCP output and caps at 25,000 by
# default (roughly 100 KB of text). Container logs for a retried task exceed
# that easily, so the tool tails rather than dumps: for failure triage the
# useful end of a log is the bottom, not the top. Raising the client-side
# limit would not help; the head of a 5 MB log is noise either way.
LOG_TAIL_CHARS = 40_000


def _error(exc: PraxisClientError) -> dict[str, Any]:
    return {
        "summary": f"Praxis error: {exc.message}",
        "error": exc.code,
        "message": exc.message,
    }


def _handle_summary(result: dict[str, Any], id_key: str, label: str) -> str:
    """One human-readable line for a dispatch/execute-plan handle.

    Args:
        result: The raw API response dict (task_id/plan_id, status, ...).
        id_key: Which id field identifies this handle (``task_id`` or
            ``plan_id``).
        label: Leading verb phrase, e.g. ``"Dispatched"``.

    Returns:
        A short line such as ``"Dispatched: queued (t1)"``.
    """
    status = result.get("status") or "submitted"
    line = f"{label}: {status}"
    ident = result.get(id_key)
    if ident:
        line += f" ({ident})"
    return line


async def dispatch_task_impl(
    client: Any,
    repo_url: str,
    instructions: str,
    model: str,
    harness: str | None = None,
    branch: str | None = None,
    context: str | None = None,
    local_context: str | None = None,
    expected_base_sha: str | None = None,
    files: list[str] | None = None,
    verification: str | None = None,
    neighbor_contracts: str | None = None,
    micro_edit: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Dispatch a single implementation task to a non-Anthropic worker model."""
    payload: dict[str, Any] = {
        "repo_url": repo_url,
        "instructions": instructions,
        "model": model,
    }
    if harness is not None:
        payload["harness"] = harness
    if branch is not None:
        payload["branch"] = branch
    if context is not None:
        payload["context"] = context
    if local_context is not None:
        payload["local_context"] = local_context
    if expected_base_sha is not None:
        payload["expected_base_sha"] = expected_base_sha
    if files is not None:
        payload["files"] = files
    if verification is not None:
        payload["verification"] = verification
    if neighbor_contracts is not None:
        payload["neighbor_contracts"] = neighbor_contracts
    if micro_edit is not None:
        payload["micro_edit"] = micro_edit
    try:
        result = cast(dict[str, Any], await client.post("/api/dispatch", payload))
    except PraxisClientError as exc:
        return _error(exc)
    # The API derives dashboard_url from its own bind port, which is not the
    # externally reachable URL. The MCP client's base_url is authoritative, so
    # override it here (consistent with poll_task/poll_plan). Only override when
    # the client exposes a base_url; otherwise keep the API's value.
    dash = _dashboard_url(client)
    if dash and isinstance(result, dict) and "dashboard_url" in result:
        result["dashboard_url"] = dash
    if isinstance(result, dict):
        result = {"summary": _handle_summary(result, "task_id", "Dispatched"), **result}
    return result


async def execute_plan_impl(
    client: Any,
    repo_url: str,
    plan: str,
    model: str,
    harness: str | None = None,
    branch: str | None = None,
    context: str | None = None,
    local_context: str | None = None,
    expected_base_sha: str | None = None,
) -> dict[str, Any]:
    """Submit a full, externally-authored plan for capability-aware execution."""
    payload: dict[str, Any] = {
        "repo_url": repo_url,
        "plan": plan,
        "model": model,
    }
    if harness is not None:
        payload["harness"] = harness
    if branch is not None:
        payload["branch"] = branch
    if context is not None:
        payload["context"] = context
    if local_context is not None:
        payload["local_context"] = local_context
    if expected_base_sha is not None:
        payload["expected_base_sha"] = expected_base_sha
    try:
        result = cast(dict[str, Any], await client.post("/api/execute-plan", payload))
    except PraxisClientError as exc:
        return _error(exc)
    # See dispatch_task_impl: the API's dashboard_url uses its bind port, not the
    # externally reachable URL. Override with the MCP client's authoritative base
    # when known; otherwise keep the API's value.
    dash = _dashboard_url(client)
    if dash and isinstance(result, dict) and "dashboard_url" in result:
        result["dashboard_url"] = dash
    if isinstance(result, dict):
        result = {
            "summary": _handle_summary(result, "plan_id", "Plan submitted"),
            **result,
        }
    return result


async def _approvals_digest_line(client: Any) -> str:
    """Return the approvals digest line, or "" on any failure.

    The digest is an add-on to poll_task/poll_plan; a broken lookup here must
    never break the poll itself, so every failure mode (network, malformed
    response) is swallowed and reported as "nothing to show" rather than
    propagated.
    """
    from orchestrator.core.approvals import digest_line

    try:
        pending = await client.get("/api/approvals/pending")
    except PraxisClientError:
        return ""
    except Exception:  # noqa: BLE001 - the digest must never break the poll
        return ""
    if not isinstance(pending, dict):
        return ""
    try:
        return digest_line(pending)
    except Exception:  # noqa: BLE001 - the digest must never break the poll
        return ""


async def pending_approvals_impl(client: Any) -> dict[str, Any]:
    """Return the summary of every task parked at the human merge gate."""
    from orchestrator.core.approvals import digest_line

    try:
        result = await client.get("/api/approvals/pending")
    except PraxisClientError as exc:
        return _error(exc)
    if not isinstance(result, dict):
        # A positive assertion of emptiness must come from a readable answer,
        # never from an unusable one. Claiming the queue is clear because the
        # response could not be parsed is the worst version of this defect:
        # the caller stops looking.
        return {
            "summary": "Praxis error: the approvals endpoint returned an "
            "unreadable response, so what is waiting is unknown.",
            "error": "bad_response",
            "message": f"expected a JSON object, got {type(result).__name__}",
        }
    summary = digest_line(result) or "Nothing is waiting on a human."
    return {"summary": summary, **result}


def _task_summary(task: dict[str, Any]) -> str:
    """One human-readable line for a task, as the first key of the payload."""
    title = task.get("title") or task.get("id") or "task"
    status = _TASK_STATUS_MAP.get(task.get("status", ""), task.get("status"))
    parts = [f"{title}: {status}"]
    if task.get("pr_url"):
        parts.append(str(task["pr_url"]))
    attempt = task.get("attempt")
    if isinstance(attempt, int) and attempt > 1:
        parts.append(f"attempt {attempt}")
    return ", ".join(parts)


async def poll_task_impl(client: Any, task_id: str) -> dict[str, Any]:
    """Return the current status, PR URL, and review of a dispatched task.

    When a task has been reviewed and its PR approved, the status is returned
    as ``awaiting_merge`` (mapped from the internal ``passed`` state) and
    ``verdict`` is set to ``"pass"``.  The caller should surface the ``pr_url``
    to a human for final merge approval.

    Note: this ``awaiting_merge`` alias is an MCP-surface convenience. The raw
    REST endpoint ``GET /api/tasks/{id}`` reports the underlying DB status
    ``passed`` for the same state, so a caller polling REST directly must match
    on ``passed`` (not ``awaiting_merge``). Prefer the MCP tools for consistency.

    The response also carries an ``approvals`` line summarizing ANY work
    parked at the merge gate across the whole project (not just this task) so
    a caller polling one task still learns about a growing review queue.  A
    failure fetching that digest never breaks this poll; ``approvals`` is
    simply "" in that case.
    """
    try:
        data = await client.get(f"/api/tasks/{task_id}")
    except PraxisClientError as exc:
        return _error(exc)
    task = data.get("task", {})
    raw_status = task.get("status")
    awaiting = raw_status == "passed"
    approvals = await _approvals_digest_line(client)
    summary = _task_summary(task)
    if raw_status == "needs_clarification":
        return {
            "summary": summary,
            "task_id": task_id,
            "status": "awaiting_clarification",
            "question": task.get("clarification_question") or "",
            "pr_url": task.get("pr_url"),
            "branch": task.get("branch_name"),
            "dashboard_url": _dashboard_url(client),
            "approvals": approvals,
        }
    return {
        "summary": summary,
        "task_id": task_id,
        "status": "awaiting_merge" if awaiting else raw_status,
        "pr_url": task.get("pr_url"),
        "review": task.get("review_feedback"),
        "branch": task.get("branch_name"),
        "verdict": "pass" if awaiting else None,
        # The docstring has promised this since the first version of this
        # tool; the field itself never shipped. `_task_summary` folds it into
        # the prose summary but only when > 1, which left a caller reading the
        # STRUCTURED result with no way to tell attempt 1 from attempt 3.
        "attempt": task.get("attempt"),
        "dashboard_url": _dashboard_url(client),
        "approvals": approvals,
    }


_TASK_STATUS_MAP = status_vocab.MCP_STATUS_ALIASES

# Statuses that are gated (passed review but not yet merged into the base branch).
_GATED_STATUSES = status_vocab.GATED_STATUSES

# Terminal statuses that cannot progress further without intervention.
_TERMINAL_STATUSES = status_vocab.TERMINAL_STATUSES


def is_terminal_status(raw_status: str) -> bool:
    """Check if a task status is terminal."""
    return raw_status in _TERMINAL_STATUSES


#: The only task status a dependent can never recover from on its own. A
#: dependency is satisfied by ``status_vocab.SATISFIED_STATUSES``; everything
#: outside that set is merely OUTSTANDING (a worker is running, a human owes an
#: answer, a merge is one approval away) except ``failed``, which the engine
#: will never revisit -- ``get_dispatchable_tasks`` cannot return a leaf behind
#: one, and no tick changes that. Named here rather than inlined so the reason
#: the set has exactly one member is written down: adding a status to it claims
#: the engine has given up on that status too.
_UNRECOVERABLE_STATUS = "failed"

#: Statuses the engine will still move by itself, without anybody doing
#: anything. Deliberately expressed as the LIVE set: a status added to the
#: vocabulary later is absent from this set and so reads as "not moving", which
#: at worst reports a healthy plan as stalled. The opposite polarity would let
#: a new status silently keep a dead plan looking alive, which is the failure
#: this whole module exists to stop.
_ENGINE_WILL_ADVANCE = frozenset({"in_progress", "reviewing", "needs_clarification"})


def _graph_pairs(
    opus_plan_json: str | None,
    tasks: list[dict[str, Any]],
) -> tuple[
    list[tuple[str, list[str], dict[str, Any]]], dict[str, list[dict[str, Any]]]
]:
    """Pair ``opus_plan`` graph entries to task rows the way dispatch does.

    ``TaskQueue.get_dispatchable_tasks`` is the authority on this pairing and
    the rules it enforces are reproduced here rather than invented:

    * entry *i* belongs to row *i*, POSITIONALLY. ``activate_plan`` writes one
      row per entry in graph order, ``insert_split_children`` only appends, and
      ``get_tasks_for_plan`` orders by rowid. Re-keying the pairs into a
      slug-keyed dict is what made the map non-injective the moment two entries
      shared a slug, orphaning the earlier row.
    * a slug maps to EVERY row carrying it, never to one row. A repeated slug
      is possible on both producer paths, and collapsing it hides a row.
    * an entry with no usable slug is skipped WITHOUT shifting the entries
      after it, because the loop is over positions.

    An unusable graph yields empty structures, and every caller reads that as
    "cannot establish anything", never as "nothing is blocked".

    Args:
        opus_plan_json: The ``plans.opus_plan`` column value, or None.
        tasks: Task rows in rowid order.

    Returns:
        ``(pairs, slug_rows)`` where each pair is ``(slug, depends_on, row)``.
    """
    if not opus_plan_json or not tasks:
        return [], {}
    try:
        opus_plan = json.loads(opus_plan_json)
    except (ValueError, TypeError):
        return [], {}
    if not isinstance(opus_plan, dict):
        return [], {}
    entries = opus_plan.get("tasks")
    if not isinstance(entries, list):
        return [], {}

    pairs: list[tuple[str, list[str], dict[str, Any]]] = []
    slug_rows: dict[str, list[dict[str, Any]]] = {}
    for index, entry in enumerate(entries):
        if index >= len(tasks) or not isinstance(entry, dict):
            continue
        slug = entry.get("slug")
        if not isinstance(slug, str) or not slug:
            continue
        raw_deps = entry.get("depends_on")
        # A depends_on that is not a list is DISCARDED, never iterated: a
        # planner answering `"depends_on": "add-tests"` would otherwise yield
        # one CHARACTER per dependency. Same discard as `_entry_dependencies`.
        deps = [str(dep) for dep in raw_deps] if isinstance(raw_deps, list) else []
        row = tasks[index]
        pairs.append((slug, deps, row))
        slug_rows.setdefault(slug, []).append(row)
    return pairs, slug_rows


def derive_stalled_by_failure_state(
    opus_plan_json: str | None,
    tasks: list[dict[str, Any]],
) -> dict[str, Any]:
    """Name the pending leaves that can never be dispatched, and why.

    Observed live: a two-leaf plan whose first leaf exhausted its retries and
    whose second leaf declared it as its only dependency. ``failed`` is not in
    ``SATISFIED_STATUSES``, so dispatch can never return the second leaf, yet
    every field of ``poll_plan`` read exactly like a plan mid-flight and a
    caller polls it forever. ``merge_gate`` cannot cover this: it fires only
    when the unmet dependency is GATED, i.e. when a human approving a merge
    would release it.

    The plan is NOT dead, which is why this is its own signal rather than a
    plan status: ``POST /api/tasks/{id}/retry`` puts a failed task back to
    PENDING, so the recovery is a verb a human can run. Writing the plan FAILED
    instead would hand its branch to the stale-branch sweeper's terminal-failed
    set, where a real ``git push --delete`` runs over every leaf that already
    merged onto it.

    Unreachability is TRANSITIVE and computed to a fixpoint: a leaf behind a
    leaf that is itself behind a failure will never run either, and reporting
    only the direct dependents is the same false "still making progress" one
    hop further out.

    Everything that cannot be ESTABLISHED counts as reachable. An unreadable
    graph, a dependency slug with no row written yet, a slug no entry declares:
    all leave the leaf alone. A false "reachable" costs a caller one more poll;
    a false "unreachable" tells a human to abandon a live plan.

    Args:
        opus_plan_json: The ``plans.opus_plan`` column value (JSON string), or
            None while the plan is still being decomposed.
        tasks: Task rows from ``GET /api/plans/{id}/tasks``, in rowid order.

    Returns:
        A dict with ``blocked_by_failure`` (one entry per unreachable pending
        leaf, naming the rows that will never satisfy its edges),
        ``action_required`` (``"retry_failed_task"`` or None) and ``hint``.
    """
    pairs, slug_rows = _graph_pairs(opus_plan_json, tasks)

    # A slug is unsatisfiable when ANY row carrying it is, because
    # `_dependency_satisfied` requires EVERY row carrying it to be satisfied.
    dead_rows: dict[int, dict[str, Any]] = {
        id(row): row for row in tasks if row.get("status") == _UNRECOVERABLE_STATUS
    }
    blocked_by: dict[int, list[dict[str, Any]]] = {}

    changed = True
    while changed:
        changed = False
        dead_slugs: dict[str, list[dict[str, Any]]] = {}
        for slug, carrying in slug_rows.items():
            offenders = [row for row in carrying if id(row) in dead_rows]
            if offenders:
                dead_slugs[slug] = offenders
        for _slug, deps, row in pairs:
            if row.get("status") != "pending" or id(row) in dead_rows:
                continue
            offenders = [
                offender for dep in deps for offender in dead_slugs.get(dep, [])
            ]
            if not offenders:
                continue
            dead_rows[id(row)] = row
            blocked_by[id(row)] = offenders
            changed = True

    blocked_by_failure: list[dict[str, Any]] = [
        {
            "task_id": row.get("id"),
            "title": row.get("title"),
            "blocked_by_task_ids": [o.get("id") for o in blocked_by[id(row)]],
            "blocked_by_titles": [o.get("title") for o in blocked_by[id(row)]],
        }
        for _slug, _deps, row in pairs
        if id(row) in blocked_by
    ]

    action_required: str | None = None
    hint: str | None = None
    if blocked_by_failure:
        action_required = "retry_failed_task"
        stuck = ", ".join(
            str(b["task_id"]) for b in blocked_by_failure if b.get("task_id")
        )
        blockers = ", ".join(
            sorted(
                {
                    str(task_id)
                    for b in blocked_by_failure
                    for task_id in b["blocked_by_task_ids"]
                    if task_id
                }
            )
        )
        hint = (
            f"{len(blocked_by_failure)} task(s) can never be dispatched: "
            f"{stuck}. Their dependencies ({blockers}) will never satisfy the "
            "edge, so no further tick will move this plan. Nothing here is "
            "waiting on the orchestrator. Either retry a failed task -- MCP "
            "tool retry_task(task_id), 'praxis retry <task-id>', or "
            "POST /api/tasks/{task_id}/retry, each of which resets it to "
            "pending and lets the wave run again -- or abandon the plan; the "
            "plan is left active on purpose, because its branch still carries "
            "whatever did merge."
        )

    return {
        "blocked_by_failure": blocked_by_failure,
        "action_required": action_required,
        "hint": hint,
    }


def derive_plan_blocked_state(
    opus_plan_json: str | None,
    tasks: list[dict[str, Any]],
) -> dict[str, Any]:
    """Derive a machine-readable summary of any merge-gate stall in a plan.

    Cross-references the ``depends_on`` graph from the serialized ``opus_plan``
    against the live task statuses to find tasks that are pending solely because
    their dependencies are gated at ``passed``/``awaiting_merge`` (i.e. passed
    review but awaiting a human ``approve-merge`` call).

    Two rules this function used to re-type are now taken from where they are
    already decided, so the module carries one copy of each:

    * a dependency is met when its rows are in ``SATISFIED_STATUSES``, not when
      they are literally ``"merged"``. `no_changes` and `superseded` unblock a
      dependent exactly as a merge does, and `no_changes` occurs in eight of
      eight observed plans. Reading one as unmet made the "every unmet dep is
      gated" test fail, so NO flag was raised for a plan whose only obstacle
      was an unapproved merge.
    * the graph is paired to rows by ``_graph_pairs``, i.e. positionally, with
      a slug mapping to EVERY row carrying it. The slug -> one-row dict this
      used to build was last-wins, and with two entries sharing a slug it hid
      a row from ``gated_task_ids`` entirely and judged the edge from whichever
      row happened to come last -- reaching the opposite verdict in both
      directions.

    Args:
        opus_plan_json: The ``plans.opus_plan`` column value (JSON string), or
            ``None`` when the plan is still being decomposed.
        tasks: Ordered list of task rows from ``GET /api/plans/{id}/tasks``.

    Returns:
        A dict with:

        - ``gated_task_ids``: list of task ids sitting at ``passed``/
          ``awaiting_merge``.
        - ``blocked_by_gate``: list of ``{task_id, title, blocked_by}`` for
          pending tasks whose every unmet dependency is gated (not merely
          absent).
        - ``action_required``: ``"approve_merge"`` when at least one pending
          task is blocked purely by gated deps, else ``None``.
        - ``hint``: human-readable explanation string, or ``None``.
    """
    pairs, slug_rows = _graph_pairs(opus_plan_json, tasks)

    # Identify gated tasks: passed review but not yet merged. Iterating the
    # PAIRS rather than a slug-keyed map is what keeps a repeated slug's other
    # row visible here; a row appears at exactly one position, so there are no
    # duplicates to guard against.
    gated_task_ids: list[str] = [
        str(row["id"])
        for _slug, _deps, row in pairs
        if row.get("status") in _GATED_STATUSES and row.get("id")
    ]

    # Find pending tasks blocked exclusively by gated deps (not failed/missing deps).
    blocked_by_gate: list[dict[str, Any]] = []
    for _slug, deps, row in pairs:
        if row.get("status") != "pending" or not deps:
            continue
        blocking_rows: list[dict[str, Any]] = []
        unmet = False
        all_gated = True
        for dep in deps:
            carrying = slug_rows.get(dep)
            if not carrying:
                # A dependency slug no graph entry declares, or one whose row
                # is not written yet. It is unmet, and it is not something a
                # human could approve, so the gate claim is withdrawn rather
                # than made about a row that does not exist.
                unmet = True
                all_gated = False
                continue
            # EVERY row carrying the slug has to be satisfied, the same rule
            # `get_dispatchable_tasks` applies, so one outstanding row makes
            # the edge unmet however many siblings already landed.
            outstanding = [
                dep_row
                for dep_row in carrying
                if str(dep_row.get("status")) not in status_vocab.SATISFIED_STATUSES
            ]
            if not outstanding:
                continue
            unmet = True
            if all(dep_row.get("status") in _GATED_STATUSES for dep_row in outstanding):
                blocking_rows.extend(outstanding)
            else:
                all_gated = False
        if not unmet or not all_gated:
            continue
        blocked_by_gate.append(
            {
                "task_id": row.get("id"),
                "title": row.get("title"),
                "blocked_by_task_ids": [
                    dep_row["id"] for dep_row in blocking_rows if dep_row.get("id")
                ],
                "blocked_by_pr_urls": [
                    dep_row["pr_url"]
                    for dep_row in blocking_rows
                    if dep_row.get("pr_url")
                ],
            }
        )

    action_required: str | None = None
    hint: str | None = None
    if blocked_by_gate:
        action_required = "approve_merge"
        ids_str = ", ".join(b["task_id"] for b in blocked_by_gate if b.get("task_id"))
        gate_ids_str = ", ".join(gated_task_ids)
        hint = (
            f"{len(blocked_by_gate)} task(s) are pending because their dependencies "
            f"({gate_ids_str}) passed review but have not been merged yet. "
            f"Blocked tasks: {ids_str}. "
            f"Call approve-merge on the gated tasks (or POST /api/plans/{{plan_id}}/approve-merges) "
            f"to unblock the next wave."
        )

    return {
        "gated_task_ids": gated_task_ids,
        "blocked_by_gate": blocked_by_gate,
        "action_required": action_required,
        "hint": hint,
    }


def derive_terminal_incomplete_state(
    plan_status: str | None,
    tasks: list[dict[str, Any]],
    opus_plan_json: str | None = None,
) -> dict[str, Any]:
    """Detect when nothing will advance this plan again, and whether work landed.

    "Terminal" here means the ORCHESTRATOR is done with it: no tick will change
    anything. It does not mean irrecoverable -- a human can still merge, answer
    a question, or retry a failed task -- which is why the plan's own status is
    left alone and only the report changes.

    Two readings used to suppress this flag on exactly the shapes that needed
    it, and both had to go:

    * ``merged_count > 0`` excluded the total-failure case, the worst one. The
      clause looks like it was there because the hint talks about merging
      partial progress, but a hint that does not apply is not a reason to
      suppress the FLAG. The hint now branches instead.
    * every PENDING leaf counted as progress. The correction that put it there
      was real (a plan with one merged, one failed, one gated and one pending
      leaf reported ``terminal_incomplete`` AND
      ``merge_gate.action_required="approve_merge"`` in one payload: two
      contradictory instructions, one abandoning work a single approval away
      from running) but it over-shot. A pending leaf counts as progress only
      while it is REACHABLE; one wedged behind a terminally failed dependency
      is not going anywhere, and that was the observed defect.

    A GATED leaf counts as progress too. It is one human approval from merging,
    so a plan holding one has not stopped moving -- and without this, dropping
    the ``merged_count`` clause would newly report a failed-plus-gated plan as
    a plan to abandon.

    Args:
        plan_status: Current plan status string (e.g. ``"active"``, ``"failed"``).
        tasks: Task rows for the plan.
        opus_plan_json: The ``plans.opus_plan`` graph, so pending leaves can be
            judged reachable. Omitted or unreadable means reachability cannot
            be established, and every pending leaf is then treated as
            reachable: that is the polarity that never abandons a live plan.

    Returns:
        A dict with:

        - ``terminal_incomplete``: ``True`` when some task failed and nothing
          the engine owns can still move.
        - ``failed_count``: number of tasks with ``status="failed"``.
        - ``merged_count``: number of tasks with ``status="merged"``.
        - ``hint``: human-readable explanation, or ``None``. It differs by
          whether anything landed: partial progress is worth integrating and
          an all-failed plan has no integration PR to go looking for.
    """
    if not tasks:
        return {
            "terminal_incomplete": False,
            "failed_count": 0,
            "merged_count": 0,
            "hint": None,
        }

    failed_count = sum(1 for t in tasks if t.get("status") == "failed")
    merged_count = sum(1 for t in tasks if t.get("status") == "merged")

    unreachable = {
        entry["task_id"]
        for entry in derive_stalled_by_failure_state(opus_plan_json, tasks)[
            "blocked_by_failure"
        ]
    }
    advanceable = 0
    for t in tasks:
        raw_status = t.get("status")
        # Three ways a leaf is still moving: the engine owns it, a human
        # approval releases it, or it dispatches as soon as its dependencies
        # land. The fourth shape -- pending behind a terminal failure -- is the
        # one that used to be counted here and is the whole defect.
        if (
            raw_status in _ENGINE_WILL_ADVANCE
            or raw_status in _GATED_STATUSES
            or (raw_status == "pending" and t.get("id") not in unreachable)
        ):
            advanceable += 1

    terminal_incomplete = failed_count > 0 and advanceable == 0

    hint: str | None = None
    if terminal_incomplete and merged_count:
        hint = (
            f"{failed_count} task(s) failed; {merged_count} task(s) merged. "
            "The orchestrator may have opened an integration PR for the merged tasks. "
            "Check the dashboard_url for the integration PR and consider merging partial "
            "progress, then re-plan the failed tasks."
        )
    elif terminal_incomplete:
        # Deliberately NOT the partial-progress wording. There is no
        # integration PR for an all-failed plan (the orchestrator refuses to
        # open one when a task exhausted its retries), so sending a caller to
        # look for one has them find nothing and read that as a second bug.
        hint = (
            f"{failed_count} task(s) failed and no task merged, so nothing "
            "landed and there is no integration PR to merge. Nothing is "
            "waiting on the orchestrator. Re-plan the failed tasks, or retry "
            "them individually with POST /api/tasks/{task_id}/retry."
        )

    return {
        "terminal_incomplete": terminal_incomplete,
        "failed_count": failed_count,
        "merged_count": merged_count,
        "hint": hint,
    }


def _plan_summary(tasks: list[dict[str, Any]]) -> str:
    """One human-readable line for a plan's leaf states.

    Counts SATISFIED leaves, not merged ones. ``SATISFIED_STATUSES`` is the set
    that unblocks dependents and lets a plan complete: ``merged`` did the work
    itself, ``superseded`` handed it to split children, ``no_changes`` found it
    already present. Counting only ``merged`` made a COMPLETED plan whose leaves
    ended ``no_changes`` report "0 of 3 leaves merged", and an assistant reading
    that line reports a finished plan as a failure. A leaf writing the next
    leaf's file has happened in eight of eight observed plans, so ``no_changes``
    is the common case rather than an edge one.

    The non-merged kinds are named rather than folded in silently, because
    "3 of 3 satisfied" alone would hide that nothing was actually committed.
    """
    total = len(tasks)
    satisfied = [
        t for t in tasks if str(t.get("status")) in status_vocab.SATISFIED_STATUSES
    ]
    gated = sum(1 for t in tasks if str(t.get("status")) in _GATED_STATUSES)
    failed = sum(1 for t in tasks if t.get("status") == "failed")

    kinds: dict[str, int] = {}
    for t in satisfied:
        key = str(t.get("status"))
        kinds[key] = kinds.get(key, 0) + 1
    detail = ", ".join(f"{n} {name}" for name, n in sorted(kinds.items()))
    head = f"{len(satisfied)} of {total} leaves satisfied"
    if detail and set(kinds) != {"merged"}:
        head += f" ({detail})"

    parts = [head]
    if gated:
        parts.append(f"{gated} awaiting approval")
    if failed:
        parts.append(f"{failed} failed")
    return ", ".join(parts)


async def poll_plan_impl(client: Any, plan_id: str) -> dict[str, Any]:
    """Return the plan status plus a one-line summary of every task in the plan.

    The response is enriched with four diagnostic fields.

    ``merge_gate``, ``stalled`` and ``terminal_incomplete`` are ALWAYS PRESENT,
    and their "nothing to report" value is a populated dict, not an empty one.
    So ``if result["merge_gate"]:`` is true on every poll of every healthy
    plan. Test the inner field instead: ``merge_gate["action_required"]``,
    ``stalled["action_required"]`` and
    ``terminal_incomplete["terminal_incomplete"]``.

    - ``merge_gate``: ``{gated_task_ids, blocked_by_gate, action_required,
      hint}``. ``action_required`` is ``"approve_merge"`` when pending tasks
      are stalled because their dependencies passed review but have not been
      merged, else ``None``.

    - ``stalled``: ``{blocked_by_failure, action_required, hint}``.
      ``action_required`` is ``"retry_failed_task"`` when a pending task can
      never be dispatched because a task it depends on failed terminally.
      Nothing else in this payload says so: the plan stays ``active``,
      ``error`` stays null, and ``merge_gate`` covers only the case a merge
      approval would release. Stop polling when this is set; no tick will
      change it.

    - ``terminal_incomplete``: ``{terminal_incomplete, failed_count,
      merged_count, hint}``. The boolean is True when some task failed and
      nothing the orchestrator owns can still move it -- including a plan
      where EVERY task failed, and a plan whose only pending tasks are the
      unreachable ones ``stalled`` names. The hint differs by whether anything
      merged: partial progress is worth going to integrate, an all-failed plan
      has no integration PR to look for.

    - ``approvals``: a one-line digest of ANY work parked at any gate across
      the whole deployment (not just this plan). A failure fetching that digest
      never breaks this poll; ``approvals`` is simply "" in that case, which is
      also what it is when nothing is parked.

    ``plan_attempts`` is how many times planning has been tried and failed
    (migration 11). A plan mid-retry and a healthy decomposition both present
    as ``active``/``pending`` with no tasks yet; this count is the only field
    that tells them apart.

    ``integration_pr_url`` and ``integration_merged_at`` are the last step of a
    plan: a COMPLETED plan's work sits on the plan branch, and the integration
    PR is the only thing between it and the base branch. They were dropped from
    this payload, so "completed" read as "landed" with no way to tell the two
    apart.
    """
    try:
        plan_data = await client.get(f"/api/plans/{plan_id}")
    except PraxisClientError as exc:
        return _error(exc)
    try:
        tasks_data = await client.get(f"/api/plans/{plan_id}/tasks")
    except PraxisClientError as exc:
        return _error(exc)
    tasks: list[dict[str, Any]] = tasks_data if isinstance(tasks_data, list) else []
    opus_plan_json: str | None = plan_data.get("opus_plan")
    merge_gate = derive_plan_blocked_state(opus_plan_json, tasks)
    stalled = derive_stalled_by_failure_state(opus_plan_json, tasks)
    term = derive_terminal_incomplete_state(
        plan_data.get("status"), tasks, opus_plan_json
    )
    approvals = await _approvals_digest_line(client)
    return {
        "summary": _plan_summary(tasks),
        "plan_id": plan_id,
        "status": plan_data.get("status"),
        "error": plan_data.get("error"),
        "task_count": len(tasks),
        "tasks": [
            {
                "task_id": t.get("id"),
                "title": t.get("title"),
                "status": _TASK_STATUS_MAP.get(t.get("status", ""), t.get("status")),
                "pr_url": t.get("pr_url"),
            }
            for t in tasks
        ],
        "merge_gate": merge_gate,
        # The third gate, and the one nothing reported. `merge_gate` fires only
        # when a human APPROVING A MERGE would release the pending leaf; a leaf
        # wedged behind a terminally FAILED one is released by nothing the
        # orchestrator does, and every other field of this payload reads as a
        # plan mid-flight. Kept separate from `terminal_incomplete` because the
        # ACTION differs: that one says whether to go collect what landed, this
        # one names the leaf to retry.
        "stalled": stalled,
        "terminal_incomplete": term,
        # The plan's own last gate. Without these a caller cannot distinguish
        # "completed and landed on the base branch" from "completed, and the
        # work is still sitting on the plan branch behind an unapproved PR".
        "integration_pr_url": plan_data.get("integration_pr_url"),
        "integration_merged_at": plan_data.get("integration_merged_at"),
        # How many times planning has been tried and failed (migration 11). A
        # plan mid-retry and a healthy decomposition both present as
        # active/pending with no tasks; this count is the only thing that
        # tells them apart, and `PlanResponse.plan_attempts` already returns
        # it over REST. Dropping it here left the primary surface (MCP is the
        # primary surface by directive) unable to say which one this is.
        "plan_attempts": plan_data.get("plan_attempts"),
        # The cap the count is counting TOWARDS. Without it the count is an
        # unanswerable question: "planning has failed twice" does not say
        # whether the next tick is a retry or the end of the plan, and that is
        # the only thing a caller polling for a terminal status needs to know.
        # Served rather than mirrored, for the reason `PlanResponse` carries
        # it: this MCP server routinely talks to a container built from an
        # older tree, so a locally-held constant would be this process's
        # belief about the server's cap. Absent from an older server, in
        # which case a caller shows the count alone rather than inventing one.
        "max_planning_attempts": plan_data.get("max_planning_attempts"),
        "dashboard_url": _dashboard_url(client),
        "approvals": approvals,
    }


async def list_providers_impl(client: Any) -> dict[str, Any]:
    """List brain providers and the worker models available to dispatch to."""
    try:
        status_data = await client.get("/api/status")
        models_data = await client.get("/api/lm-models")
    except PraxisClientError as exc:
        return _error(exc)
    return {
        "brain_providers": status_data.get("providers", []),
        "worker_models": models_data.get("models", []),
        "lm_studio_url": status_data.get("lm_studio_url"),
        "lm_studio_connected": models_data.get("connected", False),
    }


async def get_project_impl(client: Any, repo_url: str) -> dict[str, Any]:
    """Return the project config for a given repo_url (or null if unknown)."""
    try:
        projects = await client.get("/api/projects")
    except PraxisClientError as exc:
        return _error(exc)
    rows = projects if isinstance(projects, list) else []
    for row in rows:
        if row.get("repo_url") == repo_url:
            # Defensive .get(): MCP tools must never raise on a malformed row
            # (only PraxisClientError is caught), so a missing key returns null.
            config = {
                "project_id": row.get("id"),
                "name": row.get("name"),
                "model": row.get("model_name"),
                "harness": row.get("harness"),
                "default_branch": row.get("default_branch"),
                "verify_cmd": row.get("verify_cmd"),
                # `auto_merge` is the field that decides whether Praxis merges
                # without a human, and it was not returned at all.
                # `approval_gate`, which WAS returned, gates something else
                # entirely: whether an autonomous improvement plan starts
                # running unapproved. A caller reading `approval_gate: false`
                # as "merges are automatic here" gets the opposite of the
                # truth, so both are present and the key names say which.
                "auto_merge": row.get("auto_merge"),
                "improvement_plan_approval_gate": row.get("approval_gate"),
            }
            # The `project` key is present on BOTH branches. It was absent on
            # this one, so the guide's documented `result["project"] is None`
            # test for "unknown repo" was True for a repo Praxis knew perfectly
            # well.
            return {"project": config, **config}
    return {"project": None}


async def list_projects_impl(client: Any) -> dict[str, Any]:
    """Return a slim list of all configured projects."""
    try:
        projects = await client.get("/api/projects")
    except PraxisClientError as exc:
        return _error(exc)
    rows = projects if isinstance(projects, list) else []
    count = len(rows)
    noun = "project" if count == 1 else "projects"
    return {
        "summary": f"{count} {noun} configured.",
        "projects": [
            # Defensive .get(): a malformed row must not raise out of an MCP tool.
            {
                "id": row.get("id"),
                "name": row.get("name"),
                "repo_url": row.get("repo_url"),
                "model": row.get("model_name"),
                "harness": row.get("harness"),
            }
            for row in rows
        ],
    }


async def get_task_logs_impl(client: Any, task_id: str) -> dict[str, Any]:
    """Return the TAIL of a task's agent-run logs (inline failure triage).

    Logs are concatenated across every run for the task, then clipped to the
    last ``LOG_TAIL_CHARS`` characters. Truncation is announced in the payload
    and inline in the text: a silently clipped log invites the reader to trust
    an incomplete picture, which is worse than a short one.
    """
    try:
        data = await client.get(f"/api/tasks/{task_id}")
    except PraxisClientError as exc:
        return _error(exc)
    runs = data.get("runs", [])
    logs = "".join(str(run.get("logs") or "") for run in runs)
    total = len(logs)
    truncated = total > LOG_TAIL_CHARS
    if truncated:
        logs = (
            f"[truncated: showing the last {LOG_TAIL_CHARS} of {total} "
            "characters; the tail is what matters for triage]\n"
            + logs[-LOG_TAIL_CHARS:]
        )
    return {
        "task_id": task_id,
        "logs": logs,
        "truncated": truncated,
        "total_chars": total,
        # A task with zero runs also gives `logs == ""`. "the worker never
        # started" and "the worker ran and said nothing" are different
        # diagnoses and the payload could not tell them apart.
        "run_count": len(runs),
    }


async def cancel_task_impl(client: Any, task_id: str) -> dict[str, Any]:
    """Stop a running task's agent containers and mark it failed."""
    try:
        data = await client.post(f"/api/tasks/{task_id}/stop")
    except PraxisClientError as exc:
        return _error(exc)
    stopped = data.get("stopped", 0)
    return {
        "status": "cancelled",
        "stopped": stopped,
        # `stopped` counts run ROWS closed, not containers killed. Forwarding
        # only that told an assistant on a Docker-less host that N containers
        # had been stopped when nothing was contacted.
        "containers_stopped": data.get("containers_stopped", stopped),
        "docker_available": data.get("docker_available", True),
    }


async def retry_task_impl(client: Any, task_id: str) -> dict[str, Any]:
    """Reset a FAILED task to PENDING so the engine dispatches it again.

    The counterpart to ``derive_stalled_by_failure_state``: that function tells
    a caller the plan will never move again and names ``retry_failed_task`` as
    the action, and until this existed there was no tool with which to take it.

    A response that is not the updated task row settles NOTHING, so it is
    reported as an error rather than read through: ``{}.get("status")`` is
    None, and a payload carrying ``status: null`` beside no error reads to a
    brain as "it worked, poll it" for a task that was never requeued.
    """
    try:
        data = await client.post(f"/api/tasks/{task_id}/retry")
    except PraxisClientError as exc:
        return _error(exc)
    if not isinstance(data, dict):
        return {
            "summary": f"Praxis error: retry of {task_id} returned no task row",
            "error": "bad_response",
            "message": (
                f"expected the updated task row, got {type(data).__name__}; "
                "the retry may or may not have been applied, so poll_task "
                "before acting on this"
            ),
        }
    status = data.get("status")
    attempt = data.get("attempt")
    return {
        "summary": f"Retried {task_id}: now {status}, attempt {attempt}",
        "task_id": data.get("id") or task_id,
        "status": status,
        "attempt": attempt,
        "plan_id": data.get("plan_id"),
    }


async def get_mode_impl(client: Any) -> dict[str, Any]:
    """Return auto-delegate mode state {enabled, worker:{harness,model}}."""
    try:
        data = await client.get_mode()
    except PraxisClientError as exc:
        return _error(exc)
    if not isinstance(data, dict):
        # `{}` here reads as `enabled: None` to a caller doing
        # `.get("enabled")`, i.e. auto-delegate is OFF, which is a verdict
        # derived from an unreadable response rather than from the server.
        return {
            "error": "bad_response",
            "message": f"expected a JSON object, got {type(data).__name__}",
        }
    return data


async def _with_client(impl: Any, **kwargs: Any) -> dict[str, Any]:
    """Run ``impl`` against a freshly built client, guarding construction too.

    ``PraxisClient.from_env`` raises ``PraxisClientError("config_error", ...)``
    when ``PRAXIS_AUTH_TOKEN`` is unset. Every wrapper used to call it as an
    ARGUMENT, outside the impl's own try, so that exception propagated out of
    the tool: the module contract promises tools never raise, and the guide
    lists ``config_error`` among the codes a caller can expect, and it was the
    one code that could never be returned. It is also the most likely failure
    on a first run.
    """
    try:
        client = PraxisClient.from_env()
    except PraxisClientError as exc:
        return _error(exc)
    return cast(dict[str, Any], await impl(client, **kwargs))


def _dashboard_url(client: Any) -> str:
    base = getattr(client, "base_url", "").rstrip("/")
    return f"{base}/" if base else ""


# --- FastMCP registration -------------------------------------------------

mcp = FastMCP(
    "praxis",
    instructions=(
        "Praxis connects this session to other coding harnesses: it dispatches "
        "implementation to a configured worker (harness + model) and hands back "
        "a reviewed pull request. Read the praxis://guide/orchestration "
        "resource before your first dispatch. Typical loop: get_project to read a "
        "repo's configured worker, dispatch_task or execute_plan to delegate, "
        "then poll_task or poll_plan until the task reaches a TERMINAL status. "
        "Do not poll for awaiting_merge specifically: a task can end at "
        "no_changes (the work was already there, a success with no PR), "
        "superseded, or failed without ever passing through it, and a task at "
        "awaiting_clarification is waiting on a person and will never advance "
        "on its own. On awaiting_merge, relay the PR URL to the human for "
        "approval: Praxis does not merge without them unless the project has "
        "opted into auto_merge, which get_project reports."
    ),
)


@mcp.tool()
async def dispatch_task(
    repo_url: str,
    instructions: str,
    model: str,
    harness: str | None = None,
    branch: str | None = None,
    context: str | None = None,
    local_context: str | None = None,
    expected_base_sha: str | None = None,
    files: list[str] | None = None,
    verification: str | None = None,
    neighbor_contracts: str | None = None,
    micro_edit: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Dispatch one implementation task to a configured worker inside Praxis.

    Returns a handle: {task_id, plan_id, project_id, status, warnings,
    dashboard_url}. Poll with poll_task. Praxis always runs its own review
    before merge, and never merges without a human.

    ``status`` in the handle is the literal string ``"queued"``: an
    acknowledgement that the submission was accepted, NOT a task status. The
    task row is written ``pending``, which is what poll_task reports a second
    later.

    ``warnings`` lists pre-flight checks that were SKIPPED rather than passed
    (most commonly "GitHub credential not configured; remote checks skipped",
    which disables the expected_base_sha compare below). An empty list means
    every check ran.

    SIDE EFFECTS on the project row, which is why they are stated here: an
    unknown ``repo_url`` CREATES a project, and a known one has its stored
    ``model_name`` OVERWRITTEN with the value passed here. A one-off model
    argument therefore re-points the repo's configured worker for every future
    dispatch, including what get_project will report. ``harness`` is NOT
    overwritten when omitted: it is passed through as None so an unstated
    harness can never downgrade an existing project.

    repo_url: ``https://github.com/owner/repo`` or ``git@github.com:owner/repo``.
    GitHub only; other https hosts and ssh://, git://, ext:: are rejected. A
    local path works only where the server enables ``allow_local_repo_paths``.

    context: Optional curated context to brief the worker: task-relevant project
    memory, conventions, and architecture notes that help implement THIS task.
    Pass a focused slice, not your whole memory tree. Do NOT include secrets,
    tokens, or .env values - they are redacted server-side, but keep them out
    anyway.

    local_context: Optional NON-committed context the worker cannot see from a
    git clone (gitignored config shapes, user-scope conventions). Self-contained
    inline text, never a "read file X" pointer. Prefer env var NAMES/shapes over
    live values: the worker writes code, it does not run it.

    expected_base_sha: origin base sha you validated locally. Compared against
    ``origin/main`` REGARDLESS of the ``branch`` argument (execute_plan compares
    against ``branch`` instead), and skipped entirely, with a ``warnings`` entry
    and no error, when no GitHub credential is configured.

    files: Optional list of repo-relative paths the worker should edit (the
    same edit-locations slot a decomposed plan leaf gets). Improves worker
    focus when you already know which files are in scope.

    verification: Optional acceptance check for the worker to run before
    finishing (e.g. a pytest command). Falls back to the project's configured
    verify_cmd when omitted, and is DEMOTED in favour of that command when
    the value supplied here is not something a shell can run.

    neighbor_contracts: Optional signatures of directly-adjacent functions or
    endpoints the worker must not break while implementing this task.

    micro_edit: Optional {path, content, commit_message} for the MICRO-EDIT
    LANE. Pass it and NO worker is spawned: Praxis commits that one file to
    ``branch`` itself, then governs it exactly as it governs a worker's change
    (verify gate, review, human merge gate, outcome row). The lane skips the
    worker, never the governance. ``content`` is the file's FULL new content,
    not a patch. ``instructions`` is still required and is what the review
    judges the change against.

    Use it only when ALL of these hold: a single file, a handful of lines, and
    NO logic change (a typo, a comment or docstring, prose in docs, a config
    value, a version string, a renamed doc reference). Everything else
    dispatches as normal. When in doubt, delegate: an ordinary dispatch of a
    small task wastes a few minutes, while a micro edit of a logic-bearing
    change skips the isolation that change needed.

    Requires ``branch`` and requires auto-delegate mode; both are rejected with
    a 422 rather than silently downgraded. A micro edit whose verify gate fails
    is failed TERMINALLY and never retried or escalated to a worker, so a
    mis-sized estimate stays visible and the decision to dispatch it properly
    stays yours.
    """
    return await _with_client(
        dispatch_task_impl,
        repo_url=repo_url,
        instructions=instructions,
        model=model,
        harness=harness,
        branch=branch,
        context=context,
        local_context=local_context,
        expected_base_sha=expected_base_sha,
        files=files,
        verification=verification,
        neighbor_contracts=neighbor_contracts,
        micro_edit=micro_edit,
    )


@mcp.tool()
async def execute_plan(
    repo_url: str,
    plan: str,
    model: str,
    harness: str | None = None,
    branch: str | None = None,
    context: str | None = None,
    local_context: str | None = None,
    expected_base_sha: str | None = None,
) -> dict[str, Any]:
    """Execute a full, externally-authored implementation plan on a repo.

    Praxis accepts the plan and returns immediately with {plan_id, project_id,
    dashboard_url, status="pending"}: the status the plan row actually holds,
    and one poll_plan will report back. Decomposition (a multi-minute brain
    call) then runs asynchronously in the orchestration loop; the task graph
    and per-task PRs appear shortly after. Watch the dashboard_url, or poll the
    plan's tasks as they are created. Pass the FULL plan text. Use this (not
    dispatch_task) when you already have a multi-step plan.

    context: Optional curated, secret-scrubbed reference text for the worker.

    local_context: Optional NON-committed context the worker cannot see from a
    git clone (gitignored config shapes, user-scope conventions). Self-contained
    inline text, never a "read file X" pointer. Prefer env var NAMES/shapes over
    live values: the worker writes code, it does not run it.

    expected_base_sha: origin base sha you validated locally, compared against
    ``branch`` (or ``main`` when no branch is given). Skipped, with a warning
    and no error, when no GitHub credential is configured.

    SIDE EFFECTS on the project row: an unknown ``repo_url`` CREATES a project,
    and a known one has its stored ``model_name`` and ``harness`` OVERWRITTEN
    with the values passed here.

    repo_url: ``https://github.com/owner/repo`` or ``git@github.com:owner/repo``.
    GitHub only.
    """
    return await _with_client(
        execute_plan_impl,
        repo_url=repo_url,
        plan=plan,
        model=model,
        harness=harness,
        branch=branch,
        context=context,
        local_context=local_context,
        expected_base_sha=expected_base_sha,
    )


@mcp.tool()
async def poll_task(task_id: str) -> dict[str, Any]:
    """Get the status, PR URL, and review of a dispatched task.

    Normal payload: {summary, task_id, status, verdict, pr_url, branch,
    review, attempt, dashboard_url, approvals}.

    ``status="awaiting_merge"`` (with ``verdict="pass"``) means the PR passed
    review and is parked for human approval. Relay ``pr_url`` to the user; only
    they can merge it.

    ``status="awaiting_clarification"`` is a DIFFERENT SHAPE: it carries
    ``question`` and has NO ``verdict`` and NO ``review``. The worker stopped
    and asked something, and the task sits there until a person answers, so do
    not poll through it. No MCP tool can answer it: relay the question to the
    user, who replies with ``praxis clarify <task-id> "..."`` (or
    POST /api/tasks/{id}/clarify).

    Terminal statuses a poll can end on: ``merged``, ``failed``, ``no_changes``
    (the work was already present, a success with no PR to relay) and
    ``superseded``. Polling "until awaiting_merge" can therefore wait forever.
    """
    return await _with_client(poll_task_impl, task_id=task_id)


@mcp.tool()
async def poll_plan(plan_id: str) -> dict[str, Any]:
    """Get the status of a plan and a one-line summary of each of its tasks.

    Returns {summary, plan_id, status, error, task_count, tasks, merge_gate,
    stalled, terminal_incomplete, integration_pr_url, integration_merged_at,
    plan_attempts, dashboard_url, approvals}.

    ``plan_attempts`` is how many times planning itself has been tried and
    failed. A plan stuck retrying decomposition and a plan decomposing
    normally for the first time both read as ``active``/``pending`` with no
    tasks; this count is the only thing that tells them apart.

    ``merge_gate``, ``stalled`` and ``terminal_incomplete`` are ALWAYS present
    and are always non-empty dicts, so truth-testing them is always true. Read
    ``merge_gate["action_required"]``, ``stalled["action_required"]`` and
    ``terminal_incomplete["terminal_incomplete"]``.

    STOP POLLING when ``stalled["action_required"]`` is set. It means a pending
    task depends on one that failed terminally, so no tick will ever dispatch
    it: ``status`` stays ``active`` and ``error`` stays null forever. The plan
    is not lost -- ``stalled["hint"]`` names the failed task and the retry
    endpoint that resets it to pending -- but nothing happens until a person
    acts. Report it rather than continuing to poll.

    Tasks with status ``awaiting_merge`` have passed review and are parked for
    human PR approval; relay the pr_url. Tasks with ``awaiting_clarification``
    are blocked on a question only a human can answer (see poll_task).

    ``status="completed"`` does NOT mean the work reached the base branch. A
    completed plan's leaves are merged into the PLAN branch, and
    ``integration_pr_url`` is the pull request that takes it to the base
    branch; it has landed only once ``integration_merged_at`` is set. Report
    the integration PR to the user rather than announcing the plan as done.
    """
    return await _with_client(poll_plan_impl, plan_id=plan_id)


@mcp.tool()
async def pending_approvals() -> dict[str, Any]:
    """List everything waiting on a human, across all projects and ALL THREE gates.

    Returns {summary, count, task_count, plan_count, proposal_count,
    clarification_count, oldest_hours, tasks, plans, proposals, clarifications}.

    - ``tasks``: reviewed-clean task PRs parked at the merge gate.
    - ``plans``: completed plans whose integration PR is open.
    - ``proposals``: autonomous improvement plans nobody has approved to RUN.
    - ``clarifications``: tasks blocked on a question, with the question text.

    ``count`` is the number of DISTINCT PULL REQUESTS across ``tasks`` and
    ``plans``, which is what it is rendered as on every surface that shows it.
    In single-branch mode N tasks push to ONE shared work branch and so share
    ONE pull request: ``count`` is then SMALLER than
    ``task_count + plan_count``, and deliberately so. Nine parked tasks on
    four pull requests are four decisions, not nine.

    Proposals and clarifications have no PR at all and are excluded, so
    ``count`` is NOT the answer to "is anything waiting on a human": for that
    read ``summary``, or add ``count``, ``proposal_count`` and
    ``clarification_count``. Reporting only ``tasks`` tells the user their
    queue is clear while three other kinds of work sit in the same payload.
    """
    return await _with_client(pending_approvals_impl)


@mcp.tool()
async def list_providers() -> dict[str, Any]:
    """List brain providers, and the LOCALLY LOADED worker models.

    ``worker_models`` enumerates what LM Studio currently has loaded, which is
    the opencode/local arm only. A project on the agy harness runs a Gemini
    model string that can never appear in that list, so its absence is not
    evidence the model is wrong. Use get_project for what a repo is actually
    configured to run.
    """
    return await _with_client(list_providers_impl)


@mcp.tool()
async def get_task_logs(task_id: str) -> dict[str, Any]:
    """Return the agent-run logs for a task (for diagnosing a wedged run).

    Returns {task_id, logs, truncated, total_chars, run_count}. Only the LAST
    40,000 characters are returned, because the useful end of a failure log is
    the bottom; ``truncated`` says whether anything was cut.

    ``run_count == 0`` means no worker ever started, which is a different
    diagnosis from a run that produced no output. Both give ``logs == ""``.
    """
    return await _with_client(get_task_logs_impl, task_id=task_id)


@mcp.tool()
async def cancel_task(task_id: str) -> dict[str, Any]:
    """Mark a task failed, and stop its containers if any are running.

    Returns {status, stopped, containers_stopped, docker_available}.
    ``"cancelled"`` describes THIS CALL and is not a task status: the task row
    is set to ``failed``, which is what poll_task reports afterwards.

    ``stopped`` counts run ROWS closed; ``containers_stopped`` counts
    containers actually signalled. They differ whenever Docker is unreachable
    (``docker_available: false``), where every row closes and nothing is
    stopped, so report the container count, not ``stopped``.

    There is no precondition. Calling this on a task that already passed review
    or merged flips that good row to ``failed`` and still answers
    ``{"status": "cancelled", "stopped": 0}``, which reads like a clean no-op.
    Check the task's status first.
    """
    return await _with_client(cancel_task_impl, task_id=task_id)


@mcp.tool()
async def retry_task(task_id: str) -> dict[str, Any]:
    """Requeue a FAILED task: reset it to pending for one more attempt.

    Returns {summary, task_id, status, attempt, plan_id}. ``status`` comes back
    ``pending`` and ``attempt`` is one higher than before; together those are
    the only evidence the retry took.

    The ONLY status this accepts is ``failed``. Everything else answers 409,
    surfaced as ``{"error": "request_error"}`` -- including the three OTHER
    terminal statuses, which are not failures and have nothing to re-run:
    ``merged`` (landed), ``no_changes`` (the work was already present, a
    success) and ``superseded`` (split into children that carry the work).
    ``awaiting_merge`` is a human's decision and ``awaiting_clarification``
    needs an answer, not a retry.

    Call it when poll_plan reports ``stalled["action_required"] ==
    "retry_failed_task"``. A pending leaf whose dependency failed terminally
    can never be dispatched, so the plan sits ``active`` with nothing moving.
    Retrying the failed leaf re-runs it, and once it reaches a satisfied
    status its dependents become dispatchable again on the next tick -- that
    is the whole point of the call, not a side effect.

    The re-run starts CLEAN: the branch is rebuilt from base and the worker's
    stored session is dropped, so the worker does not continue where it left
    off. Nothing here is capped -- the automatic retry bound belongs to the
    review path -- so a repeated failure will repeat. Read get_task_logs first
    and change something (a clearer task, a stronger worker) rather than
    calling this in a loop.
    """
    return await _with_client(retry_task_impl, task_id=task_id)


@mcp.tool()
async def get_project(repo_url: str) -> dict[str, Any]:
    """Read a repo's configured worker and gate settings.

    Always returns a ``project`` key: the config dict, or ``None`` when Praxis
    does not know this repo.

    ``auto_merge`` is the field that decides whether Praxis merges without a
    human. ``improvement_plan_approval_gate`` does NOT: it gates whether an
    autonomous improvement PLAN starts running without approval. It is
    deliberately not called ``approval_gate`` here, because reading that name
    as "the merge gate" gets the truth backwards.
    """
    return await _with_client(get_project_impl, repo_url=repo_url)


@mcp.tool()
async def list_projects() -> dict[str, Any]:
    """List all repos Praxis knows, each with its configured model + harness."""
    return await _with_client(list_projects_impl)


@mcp.tool()
async def get_mode() -> dict[str, Any]:
    """Return auto-delegate mode state {enabled, worker:{harness,model}}."""
    return await _with_client(get_mode_impl)


@mcp.resource("praxis://guide/orchestration")
def orchestration_guide() -> str:
    """Workflow guide for an agent orchestrating Praxis over MCP.

    Covers when to delegate to Praxis and how to drive its tools: tool
    selection, what context to pass, polling cadence, task statuses, and
    troubleshooting. For live provider/model state, call list_providers.
    """
    return load_orchestration_guide()
