"""Plan and task lifecycle management."""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from orchestrator.core.clarification_states import ASKED
from orchestrator.core.leaf_split import rewire_plan_for_split
from orchestrator.core.status_vocab import SATISFIED_STATUSES
from orchestrator.database import Database
from orchestrator.models.schemas import LeafTask, PlanStatus, TaskStatus


logger = logging.getLogger(__name__)


def _as_score(value: Any) -> float | None:
    """Return a difficulty score as a float, or None for anything unusable.

    ``tasks.difficulty_score`` is REAL, but SQLite type affinity is advisory:
    non-numeric text is stored verbatim rather than rejected. Dispatch then
    calls ``float()`` on it every loop tick, and the resulting ``ValueError``
    is one log line per interval forever instead of a visible failure. The
    graph is raw brain JSON on several activation paths, so the coercion
    belongs here, at the write boundary.

    Args:
        value: The raw ``difficulty_score`` from a plan-graph task dict.

    Returns:
        The score as a float, or None when absent or not a number.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        logger.warning("Discarding unusable difficulty_score %r", value)
        return None


#: Spellings a raw brain graph uses for a boolean it wrote as text.
_TRUE_TOKENS = frozenset({"true", "yes", "1"})
_FALSE_TOKENS = frozenset({"false", "no", "0", ""})


def _as_flag(value: Any) -> bool:
    """Return a graph flag as a bool, or False for anything unusable.

    Same write-boundary reasoning as :func:`_as_score`: ``tasks`` columns are
    declared INTEGER but SQLite type affinity is advisory, so the string
    ``"false"`` would be stored verbatim and read back TRUTHY by every consumer
    that does not re-parse it. The graph is raw brain JSON on several
    activation paths, so the coercion belongs here rather than at each read.

    Args:
        value: The raw flag from a plan-graph task dict.

    Returns:
        The flag as a bool. Anything that names no boolean is False, which is
        the column default and the conservative answer for a flag that only
        ever adds caution.
    """
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, int | float):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in _TRUE_TOKENS:
            return True
        if normalized in _FALSE_TOKENS:
            return False
    logger.warning("Discarding unusable needs_stronger_model %r", value)
    return False


def _graph_entries(opus_plan: Any, plan_id: str) -> list[Any]:
    """Return a plan graph's task entries in LIST ORDER, or [] if it has none.

    List order is the load-bearing part: ``activate_plan`` writes one row per
    entry in this order, ``insert_split_children`` only ever APPENDS to both,
    no path deletes a row, and ``get_tasks_for_plan`` orders by rowid, so entry
    *i* belongs to row *i*. Nothing is filtered here, for the same reason
    ``plan_graph.parse_graph_tasks`` filters nothing: dropping an entry would
    shift every index after it and break the alignment.

    A graph that is not an object, or that carries no ``tasks`` list, used to
    raise ``KeyError`` out of ``get_dispatchable_tasks``.
    ``Orchestrator.run_once`` has no per-plan try/except, so that aborted
    dispatch for EVERY runnable plan, every interval, until the row was
    hand-edited. One plan with an unusable graph is the smaller fact, so it is
    named in the log and skipped.

    Args:
        opus_plan: The parsed ``plans.opus_plan`` payload.
        plan_id: The owning plan, so the warning names something actionable.

    Returns:
        The raw graph entries, unfiltered and in order.
    """
    if not isinstance(opus_plan, dict):
        logger.warning(
            "Plan %s: task graph is %s, not an object; nothing to dispatch",
            plan_id,
            type(opus_plan).__name__,
        )
        return []
    entries = opus_plan.get("tasks")
    if not isinstance(entries, list):
        logger.warning(
            "Plan %s: task graph carries no 'tasks' list (%s); nothing to dispatch",
            plan_id,
            type(entries).__name__,
        )
        return []
    return entries


def _entry_slug(entry: Any, index: int, plan_id: str) -> str | None:
    """Return a graph entry's slug, or None when it does not carry a usable one.

    Returning None rather than raising keeps a single malformed entry from
    aborting the dispatch pass for every plan, and skipping it costs no
    alignment because the caller iterates POSITIONALLY: the entries after it
    keep their own indices.

    Args:
        entry: One raw graph entry.
        index: Its position in the graph, which is also its row's position.
        plan_id: The owning plan.

    Returns:
        The slug, or None.
    """
    if isinstance(entry, dict):
        slug = entry.get("slug")
        if isinstance(slug, str) and slug:
            return slug
    logger.warning(
        "Plan %s: graph entry %d carries no usable slug, so its task row can be "
        "neither dispatched nor depended on",
        plan_id,
        index,
    )
    return None


def _entry_dependencies(entry: Any, slug: str | None, plan_id: str) -> list[str]:
    """Return a graph entry's declared ``depends_on`` slugs, or [] if unusable.

    A ``depends_on`` that is not a list is DISCARDED with a warning rather than
    iterated. ``Orchestrator._validate_plan_shape`` checks that a planner's
    task carries the required KEYS, never their types, so a model answering
    ``"depends_on": "add-tests"`` activates cleanly; iterating that string then
    yields one CHARACTER per dependency, and the first character naming no slug
    raises out of a function ``run_once`` does not guard, stopping dispatch for
    every plan. The same discard is what
    ``plan_derive._sanitize_dependency_graph`` already does one layer up, for
    the paths that reach it.

    Args:
        entry: One raw graph entry.
        slug: The entry's own slug, so the warning names it; may be None.
        plan_id: The owning plan.

    Returns:
        The declared dependency slugs, as strings.
    """
    if not isinstance(entry, dict):
        return []
    raw = entry.get("depends_on")
    if raw is None:
        return []
    if not isinstance(raw, list):
        logger.warning(
            "Plan %s: task %r declares a depends_on that is not a list (%r); "
            "discarding the edge rather than reading it one character at a time",
            plan_id,
            slug,
            raw,
        )
        return []
    return [str(dep) for dep in raw]


def _dependency_satisfied(dep: str, slug_rows: dict[str, list[dict[str, Any]]]) -> bool:
    """True when every task row carrying ``dep`` has reached a satisfying status.

    A SUPERSEDED or NO_CHANGES dependency is satisfied, not outstanding:
    neither will ever reach MERGED (one was replaced by split children, the
    other found its work already present), so requiring MERGED alone deadlocks
    every dependent of it. The set is named once in ``status_vocab`` so this
    and ``all_tasks_done`` cannot drift apart.

    A dep carried by MORE than one row needs all of them. Nothing records which
    row a repeated slug meant, and satisfying the edge from whichever row
    happened to be last dispatches a leaf onto work that was never built.

    A dep with NO row is outstanding, never satisfied: its row has not been
    written yet.

    Args:
        dep: The dependency slug an entry declared.
        slug_rows: Slug -> every task row carrying it.

    Returns:
        Whether the dependency no longer blocks dispatch.
    """
    carrying = slug_rows.get(dep)
    if not carrying:
        return False
    return all(row["status"] in SATISFIED_STATUSES for row in carrying)


class TaskQueue:
    """Manage plan, task, and agent-run lifecycle with SQLite persistence."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def create_plan(
        self,
        project_id: str,
        summary: str | None = None,
        source: str = "user",
        confidence: float | None = None,
        confidence_reason: str | None = None,
        spec_path: str | None = None,
    ) -> str:
        plan_id = str(uuid.uuid4())
        await self._db.execute(
            """INSERT INTO plans
               (id, project_id, source, confidence, confidence_reason, spec_path)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (plan_id, project_id, source, confidence, confidence_reason, spec_path),
        )
        logger.info("Created plan %s for project %s", plan_id, project_id)
        return plan_id

    async def get_plan(self, plan_id: str) -> dict[str, Any] | None:
        return await self._db.fetch_one("SELECT * FROM plans WHERE id = ?", (plan_id,))

    async def get_plans_for_project(self, project_id: str) -> list[dict[str, Any]]:
        return await self._db.fetch_all(
            "SELECT * FROM plans WHERE project_id = ? ORDER BY created_at DESC, rowid",
            (project_id,),
        )

    async def get_runnable_plans(self) -> list[dict[str, Any]]:
        """Return pending and active plans for orchestration."""

        return await self._db.fetch_all(
            """SELECT * FROM plans
               WHERE status IN (?, ?)
               ORDER BY created_at, rowid""",
            (PlanStatus.PENDING, PlanStatus.ACTIVE),
        )

    async def get_project(self, project_id: str) -> dict[str, Any] | None:
        """Return a project by ID."""

        return await self._db.fetch_one(
            "SELECT * FROM projects WHERE id = ?",
            (project_id,),
        )

    async def activate_plan(
        self,
        plan_id: str,
        opus_plan: dict[str, Any],
        plan_branch_name: str,
    ) -> None:
        await self._db.execute(
            """UPDATE plans
               SET status = ?, opus_plan = ?, plan_branch_name = ?
               WHERE id = ?""",
            (PlanStatus.ACTIVE, json.dumps(opus_plan), plan_branch_name, plan_id),
        )
        for task_data in opus_plan["tasks"]:
            task_id = str(uuid.uuid4())
            await self._db.execute(
                """INSERT INTO tasks
                   (id, plan_id, title, description, branch_name,
                    difficulty_score, leaf_type, needs_stronger_model)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    task_id,
                    plan_id,
                    task_data["title"],
                    task_data["description"],
                    f"agent/{task_data['slug']}",
                    # The decomposer stamps these on the graph; without this
                    # write they never reach a task row, so dispatch flagging
                    # and triage evidence both read "not scored" forever. Both
                    # are nullable: a graph from any other caller (or from
                    # before this column existed) still inserts cleanly.
                    _as_score(task_data.get("difficulty_score")),
                    task_data.get("leaf_type"),
                    # Same class of bug, one field later. The brain sets this
                    # flag, F3 validates it against escalate_task_types, and
                    # TaskResponse publishes it to the CLI, the dashboard and
                    # MCP; while it was missing from this tuple every one of
                    # them reported False for every task on every install, and
                    # the column's own test only asserts that it EXISTS.
                    _as_flag(task_data.get("needs_stronger_model")),
                ),
            )
        logger.info("Activated plan %s with %d tasks", plan_id, len(opus_plan["tasks"]))

    async def create_pending_execute_plan(
        self, project_id: str, pending_input: str
    ) -> str:
        """Persist a PENDING execute-plan whose decomposition runs in the loop."""
        plan_id = str(uuid.uuid4())
        await self._db.execute(
            """INSERT INTO plans (id, project_id, source, status, pending_input)
               VALUES (?, ?, 'execute-plan', 'pending', ?)""",
            (plan_id, project_id, pending_input),
        )
        logger.info(
            "Created pending execute-plan %s for project %s", plan_id, project_id
        )
        return plan_id

    async def update_plan_status(self, plan_id: str, status: PlanStatus) -> None:
        await self._db.execute(
            "UPDATE plans SET status = ? WHERE id = ?",
            (status, plan_id),
        )

    async def set_plan_integration_pr(self, plan_id: str, pr_url: str) -> None:
        """Record the integration PR that carries a completed plan to its base.

        Written the moment the PR is opened (or an existing one is reused), so
        that every read-only surface can name it. Before this existed the URL
        lived only in an SSE event and one log line, and ``praxis pending``
        answered "Nothing awaiting approval" with the PR open.
        """
        await self._db.execute(
            "UPDATE plans SET integration_pr_url = ? WHERE id = ?",
            (pr_url, plan_id),
        )

    async def mark_plan_integrated(self, plan_id: str) -> None:
        """Stamp the plan as landed on its base branch.

        This is what takes the plan back OUT of the pending list. Without it
        an integration PR would be reported as awaiting approval forever,
        which is the same class of lie as never reporting it at all.
        """
        now = datetime.now(UTC).isoformat()
        await self._db.execute(
            "UPDATE plans SET integration_merged_at = ? WHERE id = ?",
            (now, plan_id),
        )

    async def set_plan_error(self, plan_id: str, error: str) -> None:
        """Persist the reason a plan went terminal (surfaced via the API + poll_plan)."""
        await self._db.execute(
            "UPDATE plans SET error = ? WHERE id = ?", (error, plan_id)
        )

    async def bump_plan_attempts(self, plan_id: str) -> int:
        """Count one more planning attempt against a plan.

        Args:
            plan_id: The plan whose planning was just attempted.

        Returns:
            The NEW count, which is what the caller compares against its
            maximum. Returning the pre-increment value would grant one extra
            attempt forever, in the direction nobody notices. Zero when the
            plan row has vanished, which cannot advance a bound that is only
            ever reached upward.
        """
        await self._db.execute(
            "UPDATE plans SET plan_attempts = plan_attempts + 1 WHERE id = ?",
            (plan_id,),
        )
        row = await self._db.fetch_one(
            "SELECT plan_attempts FROM plans WHERE id = ?", (plan_id,)
        )
        if row is None:
            logger.warning(
                "Plan %s vanished while counting a planning attempt", plan_id
            )
            return 0
        return int(row["plan_attempts"])

    async def reset_plan_attempts(self, plan_id: str) -> None:
        """Clear a plan's planning-attempt count after a successful activation.

        A plan that failed once and then planned cleanly must not carry the
        old count: leaving it means the NEXT transient failure lands on a
        budget that is already partly spent, and the plan goes terminal for a
        fault it has recovered from before.
        """
        await self._db.execute(
            "UPDATE plans SET plan_attempts = 0 WHERE id = ?", (plan_id,)
        )

    async def get_task(self, task_id: str) -> dict[str, Any] | None:
        return await self._db.fetch_one("SELECT * FROM tasks WHERE id = ?", (task_id,))

    async def get_tasks_for_plan(self, plan_id: str) -> list[dict[str, Any]]:
        return await self._db.fetch_all(
            "SELECT * FROM tasks WHERE plan_id = ? ORDER BY rowid",
            (plan_id,),
        )

    async def get_active_tasks_on_branch(
        self, project_id: str, branch: str
    ) -> list[dict[str, Any]]:
        """Return this project's tasks currently holding ``branch``.

        Active means IN_PROGRESS or REVIEWING: a worker is pushing to the
        branch, or a review is resolving a commit range on it. PASSED does not
        hold, its review having already happened.

        Scoped to the PROJECT and keyed on the branch rather than on a plan,
        because the resource being protected is the branch. Single-branch
        (auto-delegate) mode reaches Praxis through MCP ``dispatch_task``, and
        ``api/dispatch.py`` creates a NEW one-task plan on every call, so
        several plans share one caller-named work branch. A hold computed from
        one plan's own tasks can never fire there: with one task per plan there
        is never a second task in the plan to hold against.

        ``branch_name`` is the branch a task was ACTUALLY dispatched against,
        written after the container started, so an active task on this branch
        is recorded here whichever plan it belongs to.

        Args:
            project_id: The project whose tasks to search.
            branch: The branch name to match exactly.

        Returns:
            The matching task rows, empty when the branch is free.
        """
        return await self._db.fetch_all(
            "SELECT tasks.* FROM tasks "
            "JOIN plans ON tasks.plan_id = plans.id "
            "WHERE plans.project_id = ? AND tasks.branch_name = ? "
            "AND tasks.status IN (?, ?) ORDER BY tasks.rowid",
            (
                project_id,
                branch,
                TaskStatus.IN_PROGRESS.value,
                TaskStatus.REVIEWING.value,
            ),
        )

    async def update_task_status(self, task_id: str, status: TaskStatus | str) -> None:
        now = datetime.now(UTC).isoformat()
        await self._db.execute(
            "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
            (status, now, task_id),
        )

    async def mark_passed(self, task_id: str, feedback: str) -> None:
        """Park a reviewed-clean task awaiting human merge approval."""
        now = datetime.now(UTC).isoformat()
        await self._db.execute(
            """UPDATE tasks
               SET status = ?, review_feedback = ?, updated_at = ?
               WHERE id = ?""",
            (TaskStatus.PASSED, feedback, now, task_id),
        )

    async def mark_merged(self, task_id: str) -> None:
        """Mark a task merged and stamp the approval time.

        Clears the worker session handle in the same statement: the task is
        terminal, so the handle can only ever be stale from here.
        """
        now = datetime.now(UTC).isoformat()
        await self._db.execute(
            """UPDATE tasks
               SET status = ?, approved_at = ?, updated_at = ?,
                   worker_session_id = NULL, worker_session_harness = NULL
               WHERE id = ?""",
            (TaskStatus.MERGED, now, now, task_id),
        )

    async def fail_task(self, task_id: str, feedback: str) -> None:
        """Mark a task failed and drop any worker session handle."""
        now = datetime.now(UTC).isoformat()
        await self._db.execute(
            """UPDATE tasks
               SET status = ?, review_feedback = ?, updated_at = ?,
                   worker_session_id = NULL, worker_session_harness = NULL
               WHERE id = ?""",
            (TaskStatus.FAILED, feedback, now, task_id),
        )

    async def mark_needs_clarification(self, task_id: str, question: str) -> None:
        """Park a task that asked a question, WITHOUT consuming a retry attempt."""
        now = datetime.now(UTC).isoformat()
        await self._db.execute(
            """UPDATE tasks
               SET status = ?, clarification_question = ?,
                   clarification_state = ?, updated_at = ?
               WHERE id = ?""",
            (TaskStatus.NEEDS_CLARIFICATION, question, ASKED, now, task_id),
        )

    async def record_clarification_answer(
        self, task_id: str, answer: str, state: str
    ) -> None:
        """Store the answer, fold the Q&A into progress_note, requeue for dispatch."""
        task = await self.get_task(task_id)
        if task is None:
            message = f"Task {task_id} not found"
            raise ValueError(message)
        question = task.get("clarification_question") or "(question not recorded)"
        existing_note = task.get("progress_note") or ""
        qa_block = (
            f"ANSWER TO YOUR EARLIER QUESTION (act on this now):\n"
            f"Q: {question}\nA: {answer}"
        )
        merged_note = f"{existing_note}\n\n{qa_block}".strip()
        now = datetime.now(UTC).isoformat()
        await self._db.execute(
            """UPDATE tasks
               SET status = ?, clarification_answer = ?, clarification_state = ?,
                   progress_note = ?, attempt = ?, updated_at = ?
               WHERE id = ?""",
            (
                TaskStatus.PENDING,
                answer,
                state,
                merged_note,
                int(task["attempt"]) + 1,
                now,
                task_id,
            ),
        )

    async def retry_task(self, task_id: str) -> None:
        """Requeue a task for a plain retry and drop any worker session handle.

        A retry always rebuilds the branch from base, so a stored
        ``worker_session_id`` would describe a tree that no longer exists.
        Clearing the id here (not ``clarification_state``) is the
        load-bearing part: ``session_resume.resolve_resume_session`` refuses
        to resume without a stored id regardless of what
        ``clarification_state`` still says, so this alone closes the gate.
        ``clarification_state`` is left untouched deliberately -- resetting
        it is a broader behavior change to the clarification flow that is
        out of scope here, and is not needed to prevent the resume bug.
        """
        task = await self.get_task(task_id)
        if task is None:
            message = f"Task {task_id} not found"
            raise ValueError(message)
        now = datetime.now(UTC).isoformat()
        await self._db.execute(
            """UPDATE tasks
               SET status = ?, attempt = ?, updated_at = ?,
                   worker_session_id = NULL, worker_session_harness = NULL
               WHERE id = ?""",
            (TaskStatus.PENDING, int(task["attempt"]) + 1, now, task_id),
        )

    async def record_triage_decision(self, task_id: str, decision: str) -> None:
        """Stamp the triage decision so a leaf is never triaged twice.

        Presence of ``triage_decision`` is the durable enforcement of the
        "one triage brain call per leaf lifetime" bound.
        """
        now = datetime.now(UTC).isoformat()
        await self._db.execute(
            "UPDATE tasks SET triage_decision = ?, updated_at = ? WHERE id = ?",
            (decision, now, task_id),
        )

    async def supersede_task(self, task_id: str, decision: str, reason: str) -> None:
        """Retire a task that was replaced by split children.

        SUPERSEDED is terminal and counts as neither a success nor a failure in
        ``task_outcomes``; the split decision itself is the recorded event.  The
        worker session handle is dropped for the same reason it is on any other
        terminal transition: it can only ever be stale from here.
        """
        now = datetime.now(UTC).isoformat()
        await self._db.execute(
            """UPDATE tasks
               SET status = ?, triage_decision = ?, review_feedback = ?,
                   updated_at = ?,
                   worker_session_id = NULL, worker_session_harness = NULL
               WHERE id = ?""",
            (TaskStatus.SUPERSEDED, decision, reason, now, task_id),
        )

    async def mark_no_changes(self, task_id: str, reason: str) -> None:
        """Close a leaf whose work was already present in the repository.

        Terminal, and deliberately not FAILED. The worker ran, the harness
        exited clean, and the tree already satisfied the leaf, so there is
        nothing to commit, nothing to review, and nothing to merge. Calling
        that a failure re-dispatched the leaf up to three times to the same
        correct answer and then failed the whole plan, with the repository
        already in the state the spec asked for.

        The worker session handle is dropped for the same reason it is on any
        other terminal transition: it can only ever be stale from here.
        """
        now = datetime.now(UTC).isoformat()
        await self._db.execute(
            """UPDATE tasks
               SET status = ?, review_feedback = ?, updated_at = ?,
                   worker_session_id = NULL, worker_session_harness = NULL
               WHERE id = ?""",
            (TaskStatus.NO_CHANGES, reason, now, task_id),
        )

    async def insert_split_children(
        self,
        plan_id: str,
        parent_task_id: str,
        parent_slug: str,
        children: list[LeafTask],
        difficulty_scores: dict[str, float] | None = None,
    ) -> list[str]:
        """Rewire the plan graph and append one task row per split child.

        Children are APPENDED to both ``plans.opus_plan`` and the ``tasks``
        table, in the same order, and the parent row is left in place.
        ``get_dispatchable_tasks`` maps the two lists positionally, so any other
        ordering silently mis-associates every task after the parent.

        Children start at ``attempt = 2``, so with the default ``max_retries``
        of 3 they get two tries rather than a fresh three (spec bound: children
        inherit the remaining retry budget, they do not reset it).

        THIS METHOD IS NOT ATOMIC, and the write order is the mitigation.
        ``Database.execute`` commits every statement on its own and ``Database``
        exposes no transaction API, so the child INSERTs and the graph UPDATE
        cannot be made one unit.  The rows are therefore written FIRST and the
        graph LAST, which is deliberately the reverse of ``activate_plan``:

        - Rows first (chosen): a crash between the two leaves rows that the
          graph never names.  ``get_dispatchable_tasks`` builds its slug map by
          enumerating the GRAPH under ``if index < len(tasks)``, so the surplus
          rows are simply never mapped.  Nothing raises, the plan merely fails
          to complete, and the state is diagnosable and repairable.
        - Graph first (rejected): a crash between the two leaves the parent's
          dependents pointing at child slugs that have no row, and the
          dangling-dependency check in ``get_dispatchable_tasks`` then raises
          ``ValueError`` on every orchestration tick for that plan.  That is a
          hard wedge with no recovery path.

        ``activate_plan`` writes graph-then-rows, but it runs before any work
        exists on the plan, so recovery there is to discard and recreate.  A
        split lands mid-flight on a plan that may already have merged leaves,
        where recreation is not available.

        Args:
            plan_id: The plan owning the parent.
            parent_task_id: DB id of the leaf being split.
            parent_slug: Graph slug of the leaf being split.
            children: ``LeafTask`` children from triage, already graded by
                ``leaf_validator.validate_split_children``.  This method does
                NOT re-grade them: it is the persistence step, and a caller
                that skipped the gate would get an invalid child stored just
                as readily.  The one caller is the split branch of
                ``orchestrator_review._run_leaf_triage``.
            difficulty_scores: Optional ``{child.id: p_success}``.  Omitted
                means "not scored", which dispatch reads as "not flagged"; it
                must never be read as "safe".

        Returns:
            The new task row ids, in append order.

        Raises:
            ValueError: If the plan has no task graph, or if ``parent_slug`` has
                already been split.  The second case is propagated from
                ``rewire_plan_for_split``: duplicate child slugs would collapse
                the positional map, so it fails closed rather than appending.
            KeyError: If ``parent_slug`` is not present in the plan graph.
        """
        plan = await self.get_plan(plan_id)
        if plan is None or not plan.get("opus_plan"):
            message = f"plan {plan_id} has no task graph to split"
            raise ValueError(message)

        opus_plan = json.loads(plan["opus_plan"])
        # Rewires the in-memory graph only, and raises every rejection before
        # its first mutation, so a refusal here writes nothing at all.
        appended = rewire_plan_for_split(opus_plan, parent_slug, children)

        # The score goes on the GRAPH entry as well as the row. Every other
        # activation path writes both, and ``get_dispatchable_tasks`` aligns
        # the two positionally, so a row carrying a number its graph entry does
        # not is the shape that reads as corruption to anyone who checks later.
        # ``rewire_plan_for_split`` appends in ``children`` order, which is what
        # makes this zip the right pairing rather than a coincidence.
        scores = difficulty_scores or {}
        for child, child_data in zip(children, appended, strict=True):
            score = _as_score(scores.get(child.id))
            if score is not None:
                child_data["difficulty_score"] = score

        new_ids: list[str] = []
        for child_data in appended:
            child_id = str(uuid.uuid4())
            await self._db.execute(
                """INSERT INTO tasks
                   (id, plan_id, title, description, branch_name,
                    parent_task_id, leaf_type, difficulty_score, attempt)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 2)""",
                (
                    child_id,
                    plan_id,
                    child_data["title"],
                    child_data["description"],
                    f"agent/{child_data['slug']}",
                    parent_task_id,
                    child_data.get("leaf_type"),
                    child_data.get("difficulty_score"),
                ),
            )
            new_ids.append(child_id)

        # Last, and only once every child row exists: publishing the graph is
        # what makes the new child slugs resolvable to a row.
        await self._db.execute(
            "UPDATE plans SET opus_plan = ? WHERE id = ?",
            (json.dumps(opus_plan), plan_id),
        )
        logger.info(
            "Split task %s into %d children on plan %s",
            parent_task_id,
            len(new_ids),
            plan_id,
        )
        return new_ids

    async def set_task_implementer(
        self, task_id: str, harness: str, model: str, index: int
    ) -> None:
        """Pin the implementer for this task's next dispatch (escalation).

        Outcome attribution reads these columns, so an escalated success is
        never credited to the original worker.
        """
        now = datetime.now(UTC).isoformat()
        await self._db.execute(
            """UPDATE tasks
               SET implement_harness = ?, implement_model = ?,
                   escalation_index = ?, updated_at = ?
               WHERE id = ?""",
            (harness, model, index, now, task_id),
        )

    async def append_progress_note(self, task_id: str, note: str) -> None:
        """Append a block to the task's progress note (folded into the Bible)."""
        task = await self.get_task(task_id)
        if task is None:
            message = f"Task {task_id} not found"
            raise ValueError(message)
        existing = task.get("progress_note") or ""
        merged = f"{existing}\n\n{note}".strip()
        now = datetime.now(UTC).isoformat()
        await self._db.execute(
            "UPDATE tasks SET progress_note = ?, updated_at = ? WHERE id = ?",
            (merged, now, task_id),
        )

    async def set_task_pr_url(self, task_id: str, pr_url: str) -> None:
        now = datetime.now(UTC).isoformat()
        await self._db.execute(
            "UPDATE tasks SET pr_url = ?, updated_at = ? WHERE id = ?",
            (pr_url, now, task_id),
        )

    async def record_worker_session(
        self, task_id: str, session_id: str, harness: str
    ) -> None:
        """Store the worker's harness-native session handle for later resume.

        The harness is stored with the id because replay must refuse a handle
        minted by a different harness.
        """
        now = datetime.now(UTC).isoformat()
        await self._db.execute(
            "UPDATE tasks SET worker_session_id = ?, worker_session_harness = ?, "
            "updated_at = ? WHERE id = ?",
            (session_id, harness, now, task_id),
        )

    async def clear_worker_session(self, task_id: str) -> None:
        """Drop the session handle so a terminal task never replays a stale id."""
        now = datetime.now(UTC).isoformat()
        await self._db.execute(
            "UPDATE tasks SET worker_session_id = NULL, "
            "worker_session_harness = NULL, updated_at = ? WHERE id = ?",
            (now, task_id),
        )

    async def get_dispatchable_tasks(self, plan_id: str) -> list[dict[str, Any]]:
        """Return pending tasks whose declared dependencies are satisfied.

        The graph and the rows are paired POSITIONALLY, entry *i* to row *i*,
        and the whole return value is built from that pairing. It used to be
        flattened into a slug-keyed dict the instant it was built, which made
        the map non-injective as soon as two entries shared a slug: the earlier
        row became unreachable (PENDING forever, so ``all_tasks_done`` never
        turned true and ``plan_stalled`` never fired either, leaving the plan
        ACTIVE and indistinguishable from healthy) while the later row was
        returned once per duplicate, aiming two workers at one branch and
        widening both ends of per-task ``review_base_sha`` scoping. Neither the
        derive path nor the planner-JSON path guarantees unique slugs, so this
        must not assume them. Iterating positions rather than slugs is what
        makes each row appear exactly once, whatever the graph is called.

        Three facts are kept distinct instead of collapsing into one exception:

        * a ``depends_on`` slug that NO graph entry declares was invented by a
          producer and still raises, which is the contract ``plan_derive`` and
          ``leaf_split`` both go out of their way to defend;
        * a slug that IS a graph entry but has no row YET is not written yet,
          not dangling. That state is reachable after a crash inside
          ``activate_plan``, which writes the graph before the rows, and
          raising on it wedged dispatch for every plan on every tick. Its
          dependents simply wait, and the log names the slugs;
        * an entry with no usable slug is skipped without shifting the entries
          after it, because the loop is over positions.

        Args:
            plan_id: The plan to inspect.

        Returns:
            The task rows that may be dispatched now, in graph order.

        Raises:
            ValueError: On a ``depends_on`` slug that no graph entry declares.
        """
        plan = await self.get_plan(plan_id)
        if plan is None or plan["opus_plan"] is None:
            return []

        graph = _graph_entries(json.loads(plan["opus_plan"]), plan_id)
        rows = await self.get_tasks_for_plan(plan_id)
        slugs = [
            _entry_slug(entry, index, plan_id) for index, entry in enumerate(graph)
        ]
        declared = {slug for slug in slugs if slug is not None}
        # Read once, by position, so the dangling check below and the dispatch
        # decision further down can never be looking at different edges.
        edges = [
            _entry_dependencies(entry, slugs[index], plan_id)
            for index, entry in enumerate(graph)
        ]

        # Slug -> EVERY row carrying it. A list, never one row: overwriting is
        # exactly how the earlier row used to be orphaned.
        slug_rows: dict[str, list[dict[str, Any]]] = {}
        for index, slug in enumerate(slugs):
            if slug is not None and index < len(rows):
                slug_rows.setdefault(slug, []).append(rows[index])
        for slug, carrying in slug_rows.items():
            if len(carrying) > 1:
                logger.warning(
                    "Plan %s: %d task rows share the graph slug %r, so every "
                    "dependency naming it now waits for all of them",
                    plan_id,
                    len(carrying),
                    slug,
                )

        unwritten: set[str] = set()
        for index, dependencies in enumerate(edges):
            for dep in dependencies:
                if dep in slug_rows:
                    continue
                if dep in declared:
                    unwritten.add(dep)
                    continue
                msg = (
                    f"dangling dependency: task "
                    f"{slugs[index]!r} depends on unknown slug {dep!r}"
                )
                raise ValueError(msg)
        if unwritten:
            logger.warning(
                "Plan %s: %s named as a dependency but has no task row yet; "
                "holding the dependents until the rows are written",
                plan_id,
                ", ".join(sorted(unwritten)),
            )

        dispatchable: list[dict[str, Any]] = []
        for index, dependencies in enumerate(edges):
            if slugs[index] is None or index >= len(rows):
                continue
            row = rows[index]
            if row["status"] != TaskStatus.PENDING:
                continue
            if all(_dependency_satisfied(dep, slug_rows) for dep in dependencies):
                dispatchable.append(row)
        return dispatchable

    async def all_tasks_done(self, plan_id: str) -> bool:
        """True when every task reached a status that satisfies the plan.

        A SUPERSEDED parent was replaced by its split children and a
        NO_CHANGES leaf found its work already present; neither will ever
        reach MERGED, so treating either as outstanding would stop the plan
        from ever completing.
        """
        tasks = await self.get_tasks_for_plan(plan_id)
        return bool(tasks) and all(
            task["status"] in SATISFIED_STATUSES for task in tasks
        )

    async def create_agent_run(self, task_id: str, container_id: str) -> str:
        run_id = str(uuid.uuid4())
        await self._db.execute(
            "INSERT INTO agent_runs (id, task_id, container_id) VALUES (?, ?, ?)",
            (run_id, task_id, container_id),
        )
        return run_id

    async def get_agent_run(self, run_id: str) -> dict[str, Any] | None:
        return await self._db.fetch_one(
            "SELECT * FROM agent_runs WHERE id = ?",
            (run_id,),
        )

    async def get_runs_for_task(self, task_id: str) -> list[dict[str, Any]]:
        return await self._db.fetch_all(
            "SELECT * FROM agent_runs WHERE task_id = ? ORDER BY rowid",
            (task_id,),
        )

    async def get_running_runs(self) -> list[dict[str, Any]]:
        """Return every agent run still marked as running.

        Used by reconciliation to find orphaned runs whose containers no
        longer report completion (e.g. after an orchestrator restart).
        """
        return await self._db.fetch_all(
            "SELECT * FROM agent_runs WHERE status = 'running' ORDER BY rowid"
        )

    async def update_agent_run_logs(self, run_id: str, logs: str) -> None:
        """Persist in-progress logs for a running agent run.

        Unlike ``complete_agent_run`` this leaves status and finished_at
        untouched so live log streaming can checkpoint output incrementally.
        """
        await self._db.execute(
            "UPDATE agent_runs SET logs = ? WHERE id = ?",
            (logs, run_id),
        )

    async def complete_agent_run(self, run_id: str, status: str, logs: str) -> None:
        now = datetime.now(UTC).isoformat()
        await self._db.execute(
            """UPDATE agent_runs
               SET status = ?, logs = ?, finished_at = ?
               WHERE id = ?""",
            (status, logs, now, run_id),
        )

    async def record_run_tokens(
        self, run_id: str, tokens_used: int | None, source: str
    ) -> None:
        """Persist token telemetry for an agent run.

        Args:
            run_id: The agent_runs row to update.
            tokens_used: Total tokens reported by the harness, or None when the
                harness cannot report them.
            source: "harness" or "unavailable".
        """
        await self._db.execute(
            "UPDATE agent_runs SET tokens_used = ?, tokens_source = ? WHERE id = ?",
            (tokens_used, source, run_id),
        )
