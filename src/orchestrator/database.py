"""Async SQLite database wrapper and migrations."""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import aiosqlite


logger = logging.getLogger(__name__)

SQLITE_URL_PREFIX = "sqlite+aiosqlite:///"

CREATE_TABLE_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS users (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        token_hash TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS projects (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        name TEXT NOT NULL,
        repo_url TEXT NOT NULL,
        default_branch TEXT NOT NULL DEFAULT 'main',
        approval_gate INTEGER NOT NULL DEFAULT 1,
        auto_merge INTEGER NOT NULL DEFAULT 0,
        verify_cmd TEXT,
        confidence_threshold REAL NOT NULL DEFAULT 0.7,
        max_retries INTEGER NOT NULL DEFAULT 3,
        max_improvement_cycles INTEGER NOT NULL DEFAULT 5,
        lm_studio_url TEXT NOT NULL DEFAULT 'http://host.docker.internal:1234',
        model_name TEXT NOT NULL DEFAULT '',
        harness TEXT NOT NULL DEFAULT 'opencode',
        agent_model TEXT,
        agent_model_effort TEXT,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (user_id) REFERENCES users (id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS plans (
        id TEXT PRIMARY KEY,
        project_id TEXT NOT NULL,
        opus_plan TEXT,
        plan_branch_name TEXT,
        source TEXT NOT NULL DEFAULT 'user',
        confidence REAL,
        confidence_reason TEXT,
        status TEXT NOT NULL DEFAULT 'pending',
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        spec_path TEXT,
        plan_path TEXT,
        FOREIGN KEY (project_id) REFERENCES projects (id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS tasks (
        id TEXT PRIMARY KEY,
        plan_id TEXT NOT NULL,
        title TEXT NOT NULL,
        description TEXT NOT NULL,
        branch_name TEXT NOT NULL,
        pr_url TEXT,
        status TEXT NOT NULL DEFAULT 'pending',
        attempt INTEGER NOT NULL DEFAULT 1,
        review_feedback TEXT,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (plan_id) REFERENCES plans (id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS agent_runs (
        id TEXT PRIMARY KEY,
        task_id TEXT NOT NULL,
        container_id TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'running',
        logs TEXT NOT NULL DEFAULT '',
        started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        finished_at TEXT,
        FOREIGN KEY (task_id) REFERENCES tasks (id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS opus_state (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        status TEXT NOT NULL DEFAULT 'available',
        rate_limited_at TEXT,
        resume_at TEXT,
        queued_actions TEXT NOT NULL DEFAULT '[]'
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS doc_index (
        path TEXT PRIMARY KEY,
        category TEXT NOT NULL,
        title TEXT,
        content_hash TEXT NOT NULL,
        branch TEXT,
        done_count INTEGER NOT NULL DEFAULT 0,
        total_count INTEGER NOT NULL DEFAULT 0,
        classified_by TEXT NOT NULL DEFAULT 'marker',
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS settings_overrides (
        key TEXT PRIMARY KEY,
        value TEXT,
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,
)


@dataclass(frozen=True)
class Migration:
    """One ordered, idempotent schema step.

    ``apply`` receives the open connection. Steps MUST be safe to re-run
    (guard with PRAGMA table_info / IF NOT EXISTS), because a crash between
    apply and the version bump replays the step on next startup.
    """

    version: int
    description: str
    apply: Callable[[aiosqlite.Connection], Awaitable[None]]


async def _migration_0001_baseline(connection: aiosqlite.Connection) -> None:
    """Baseline marker for databases created before versioning existed.

    All tables are created by the idempotent CREATE TABLE IF NOT EXISTS block
    in ``initialize`` and the legacy conditional rebuilds; this step only
    exists so pre-framework databases converge on version 1.
    """


async def _migration_0002_pending_input(connection: aiosqlite.Connection) -> None:
    """Add plans.pending_input: raw decomposition inputs for async execute-plan."""
    cursor = await connection.execute("PRAGMA table_info(plans)")
    cols = {row[1] for row in await cursor.fetchall()}
    if "pending_input" not in cols:
        await connection.execute("ALTER TABLE plans ADD COLUMN pending_input TEXT")


async def _migration_0003_plan_error(connection: aiosqlite.Connection) -> None:
    """Add plans.error: the reason a plan went terminal (e.g. decomposition failure)."""
    cursor = await connection.execute("PRAGMA table_info(plans)")
    cols = {row[1] for row in await cursor.fetchall()}
    if "error" not in cols:
        await connection.execute("ALTER TABLE plans ADD COLUMN error TEXT")


async def _migration_0004_capability_events(connection: aiosqlite.Connection) -> None:
    """Add capability_events: decision-record table for capability calibration."""
    await connection.execute(
        """
        CREATE TABLE IF NOT EXISTS capability_events (
            id TEXT PRIMARY KEY,
            schema_version INTEGER NOT NULL,
            event_type TEXT NOT NULL,
            plan_id TEXT,
            task_id TEXT,
            payload TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )


async def _migration_0005_task_outcomes(connection: aiosqlite.Connection) -> None:
    """Add task_outcomes: calibration table for capability-aware task outcomes."""
    await connection.execute(
        """
        CREATE TABLE IF NOT EXISTS task_outcomes (
            id TEXT PRIMARY KEY,
            task_id TEXT,
            project_id TEXT,
            model_name TEXT,
            harness TEXT,
            task_type TEXT,
            files_touched INTEGER,
            loc_delta INTEGER,
            context_tokens_est INTEGER,
            attempt INTEGER,
            outcome TEXT NOT NULL,
            failure_class TEXT,
            split_depth INTEGER NOT NULL DEFAULT 0,
            source TEXT NOT NULL DEFAULT 'run',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    await connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_task_outcomes_model_project
        ON task_outcomes (model_name, project_id, created_at)
        """
    )


async def _migration_0006_worker_session(connection: aiosqlite.Connection) -> None:
    """Add tasks.worker_session_id / worker_session_harness for session resume.

    The harness is stored alongside the id because a project's harness can
    change between dispatches, and an agy conversation id is meaningless to
    OpenCode. Replay checks both.
    """
    cursor = await connection.execute("PRAGMA table_info(tasks)")
    cols = {row[1] for row in await cursor.fetchall()}
    if "worker_session_id" not in cols:
        await connection.execute("ALTER TABLE tasks ADD COLUMN worker_session_id TEXT")
    if "worker_session_harness" not in cols:
        await connection.execute(
            "ALTER TABLE tasks ADD COLUMN worker_session_harness TEXT"
        )


async def _migration_0007_leaf_triage(connection: aiosqlite.Connection) -> None:
    """Add the columns adaptive split-on-failure and escalation need.

    The umbrella spec's DDL names three columns (``parent_task_id``,
    ``difficulty_score``, ``leaf_type``).  Four more are required to enforce the
    spec's own hard bounds durably rather than in memory:

    - ``triage_decision``: presence enforces "one triage call per leaf lifetime".
    - ``escalation_index``: how many ``implement_escalation`` entries this leaf
      has already burned, so escalation stops at the list length.
    - ``implement_harness`` / ``implement_model``: the model that ACTUALLY
      implemented this attempt.  Outcome attribution must never credit the
      original worker with an escalated success, or the calibration loop learns
      lies.

    ``parent_task_id`` doubles as the one-split-generation guard: a task with a
    parent may never be split again.
    """
    cursor = await connection.execute("PRAGMA table_info(tasks)")
    cols = {row[1] for row in await cursor.fetchall()}
    additions = (
        ("parent_task_id", "TEXT"),
        ("difficulty_score", "REAL"),
        ("leaf_type", "TEXT"),
        ("triage_decision", "TEXT"),
        ("escalation_index", "INTEGER NOT NULL DEFAULT 0"),
        ("implement_harness", "TEXT"),
        ("implement_model", "TEXT"),
    )
    for name, decl in additions:
        if name not in cols:
            # `name` and `decl` come from the frozen `additions` tuple above,
            # never from a caller, so the f-string interpolates literals only.
            await connection.execute(f"ALTER TABLE tasks ADD COLUMN {name} {decl}")
    await connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_tasks_parent ON tasks (parent_task_id)"
    )


async def _migration_0008_run_tokens(connection: aiosqlite.Connection) -> None:
    """Add token telemetry to agent_runs so harnesses are comparable.

    ``tokens_source`` distinguishes "the harness reported 0" from "this harness
    cannot report", which is the whole point: an unexplained NULL is the same
    invisible gap the column exists to close.
    """
    cursor = await connection.execute("PRAGMA table_info(agent_runs)")
    cols = {row[1] for row in await cursor.fetchall()}
    if "tokens_used" not in cols:
        await connection.execute(
            "ALTER TABLE agent_runs ADD COLUMN tokens_used INTEGER"
        )
    if "tokens_source" not in cols:
        await connection.execute("ALTER TABLE agent_runs ADD COLUMN tokens_source TEXT")


async def _migration_0009_plan_integration(connection: aiosqlite.Connection) -> None:
    """Add plans.integration_pr_url / integration_merged_at.

    The integration PR is the last link in the loop and it used to exist in
    exactly two places, neither of which a user can reach: an SSE event that
    has already fired by the time anybody looks, and one INFO line in the
    orchestrator's own log. ``praxis pending`` reported "Nothing awaiting
    approval" with integration PRs open, because the URL was never written
    down. Persisting it is what lets every read-only surface answer the
    question "where did my work go".

    ``integration_merged_at`` is what stops the answer from becoming a
    different lie once the PR is merged: without it the plan would sit in
    ``pending`` forever. It is set when Praxis merges the PR, and by the
    reconcile sweep when someone merged it on GitHub instead.
    """
    cursor = await connection.execute("PRAGMA table_info(plans)")
    cols = {row[1] for row in await cursor.fetchall()}
    if "integration_pr_url" not in cols:
        await connection.execute("ALTER TABLE plans ADD COLUMN integration_pr_url TEXT")
    if "integration_merged_at" not in cols:
        await connection.execute(
            "ALTER TABLE plans ADD COLUMN integration_merged_at TEXT"
        )


async def _migration_0010_review_base_sha(connection: aiosqlite.Connection) -> None:
    """Add tasks.review_base_sha: where this task's own work starts on a branch.

    In single-branch (auto-delegate) mode every task pushes to one shared work
    branch, so the pull request it carries accumulates every task's commits and
    the per-task reviewer was shown all of them. Every task after the first
    failed review for touching files outside its scope, and
    ``core/outcome_recorder`` wrote that FAIL against a worker that had done its
    task correctly, poisoning the calibration signal.

    A branch name cannot say where one task's commits begin on a branch several
    tasks share. A SHA can, so the review can be bounded to
    ``review_base_sha..head``.

    Nullable, and that is load-bearing: every row that predates this column has
    no such SHA and must keep taking the whole-pull-request path unchanged, as
    must two-tier rows whose lookup failed.
    """
    cursor = await connection.execute("PRAGMA table_info(tasks)")
    cols = {row[1] for row in await cursor.fetchall()}
    if "review_base_sha" not in cols:
        await connection.execute("ALTER TABLE tasks ADD COLUMN review_base_sha TEXT")


async def _migration_0011_plan_attempts(connection: aiosqlite.Connection) -> None:
    """Add plans.plan_attempts: how many times planning has been tried.

    A planner failure used to be unbounded. The exception escaped
    ``plan_and_activate``, ``run_once``'s per-plan guard logged it, and the plan
    stayed PENDING to be retried on the next tick, forever. Nothing external
    could tell that apart from a healthy decomposition in progress, because a
    plan mid-decomposition is also ACTIVE with no tasks.

    A count on the row is what makes the retry bounded AND visible: the loop
    stops at ``_MAX_PLANNING_ATTEMPTS`` and every read-only surface can say how
    many tries a plan has burned.

    NOT NULL DEFAULT 0 rather than nullable: the bound is a comparison on the
    new count, and a NULL there is a TypeError on exactly the tick that is
    supposed to stop the loop. Every pre-existing plan has made zero attempts
    under this counter, which is the honest value as well as the safe one.
    """
    cursor = await connection.execute("PRAGMA table_info(plans)")
    cols = {row[1] for row in await cursor.fetchall()}
    if "plan_attempts" not in cols:
        await connection.execute(
            "ALTER TABLE plans ADD COLUMN plan_attempts INTEGER NOT NULL DEFAULT 0"
        )


async def _migration_0012_project_context_window(
    connection: aiosqlite.Connection,
) -> None:
    """Add projects.context_window: an operator's declared worker window.

    The pre-dispatch budget gate sizes a task's context pack against the
    worker's context window. It used to establish that window by asking LM
    Studio and, when LM Studio did not know the model, by using a hardcoded
    8192. Every cloud-harness project (agy/Gemini today; claude and codex the
    moment they arrive) hit that path on every dispatch, and a 14 KB
    instructions body was refused against a model with a million-token window.

    This column is the escape hatch that needs no code change and no restart:
    an operator who knows their model's window states it here and it beats
    every other source, including a window declared in the settings file.

    NULLABLE, and the NULL is load-bearing: it means "I have not declared one",
    which falls through to the declared/probed/unknown chain in
    ``core/context_window``. A zero or negative value is treated the same way
    rather than as a real window, because it cannot be one.
    """
    cursor = await connection.execute("PRAGMA table_info(projects)")
    cols = {row[1] for row in await cursor.fetchall()}
    if "context_window" not in cols:
        await connection.execute(
            "ALTER TABLE projects ADD COLUMN context_window INTEGER"
        )


MIGRATIONS: list[Migration] = [
    Migration(1, "baseline: schema as of 2026-07-02", _migration_0001_baseline),
    Migration(
        2,
        "add plans.pending_input for async execute-plan",
        _migration_0002_pending_input,
    ),
    Migration(
        3,
        "add plans.error for terminal-failure reasons",
        _migration_0003_plan_error,
    ),
    Migration(
        4,
        "add capability_events decision-record table",
        _migration_0004_capability_events,
    ),
    Migration(
        5,
        "add task_outcomes calibration table",
        _migration_0005_task_outcomes,
    ),
    Migration(
        6,
        "add tasks.worker_session_id/worker_session_harness for session resume",
        _migration_0006_worker_session,
    ),
    Migration(
        7,
        "add tasks triage/split/escalation columns for decomposition standard v2",
        _migration_0007_leaf_triage,
    ),
    Migration(
        8,
        "add agent_runs token telemetry (tokens_used, tokens_source)",
        _migration_0008_run_tokens,
    ),
    Migration(
        9,
        "add plans integration PR columns (integration_pr_url, integration_merged_at)",
        _migration_0009_plan_integration,
    ),
    Migration(
        10,
        "add tasks.review_base_sha so a review can be scoped to one task",
        _migration_0010_review_base_sha,
    ),
    Migration(
        11,
        "add plans.plan_attempts so a failing planner cannot retry forever",
        _migration_0011_plan_attempts,
    ),
    Migration(
        12,
        "add projects.context_window so an operator can declare the worker window",
        _migration_0012_project_context_window,
    ),
]

CURRENT_SCHEMA_VERSION = MIGRATIONS[-1].version


class Database:
    """Simple asynchronous SQLite wrapper for orchestrator storage."""

    def __init__(self, database_url: str) -> None:
        self._database_url = database_url
        self._db_path = self._extract_sqlite_path(database_url)
        self._connection: aiosqlite.Connection | None = None

    @property
    def db_path(self) -> str:
        """Resolved SQLite database filesystem path."""

        return self._db_path

    @staticmethod
    def _extract_sqlite_path(database_url: str) -> str:
        if not database_url.startswith(SQLITE_URL_PREFIX):
            message = f"Unsupported database URL: {database_url}"
            raise ValueError(message)
        return database_url.removeprefix(SQLITE_URL_PREFIX)

    async def initialize(
        self,
        before_drop: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        if self._connection is None:
            logger.info("Connecting to SQLite database at %s", self._db_path)
            self._connection = await aiosqlite.connect(self._db_path)
            self._connection.row_factory = aiosqlite.Row

        connection = self._require_connection()
        await connection.execute("PRAGMA journal_mode=WAL;")
        await connection.execute("PRAGMA foreign_keys=ON;")

        for statement in CREATE_TABLE_STATEMENTS:
            await connection.execute(statement)

        for table, column_ddls in (
            (
                "projects",
                (
                    "agent_model TEXT",
                    "agent_model_effort TEXT",
                    "harness TEXT NOT NULL DEFAULT 'opencode'",
                    "auto_merge INTEGER NOT NULL DEFAULT 0",
                    "verify_cmd TEXT",
                ),
            ),
            ("plans", ("spec_path TEXT", "plan_path TEXT")),
            (
                "tasks",
                (
                    "needs_stronger_model INTEGER DEFAULT 0",
                    "escalation_state TEXT",
                    "escalated_to TEXT",
                    "checklist TEXT",
                    "progress_note TEXT",
                    "approved_at TEXT",
                    "clarification_question TEXT",
                    "clarification_answer TEXT",
                    "clarification_state TEXT",
                ),
            ),
        ):
            for ddl in column_ddls:
                with contextlib.suppress(Exception):
                    await connection.execute(
                        f"ALTER TABLE {table} ADD COLUMN {ddl}"  # noqa: S608
                    )

        # Drop redundant plans.spec column for pre-existing databases.
        # SQLite has no DROP COLUMN, so rebuild the table without it.
        plan_cols = {
            row["name"]
            for row in await (
                await connection.execute("PRAGMA table_info(plans)")
            ).fetchall()
        }
        if before_drop is not None:
            await before_drop()

        if "spec" in plan_cols:
            await connection.executescript(
                """
                PRAGMA foreign_keys=OFF;
                DROP TABLE IF EXISTS plans_new;
                CREATE TABLE plans_new (
                    id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    opus_plan TEXT,
                    plan_branch_name TEXT,
                    source TEXT NOT NULL DEFAULT 'user',
                    confidence REAL,
                    confidence_reason TEXT,
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    spec_path TEXT,
                    plan_path TEXT,
                    FOREIGN KEY (project_id) REFERENCES projects (id)
                );
                INSERT INTO plans_new
                    (id, project_id, opus_plan, plan_branch_name, source,
                     confidence, confidence_reason, status, created_at,
                     spec_path, plan_path)
                SELECT id, project_id, opus_plan, plan_branch_name, source,
                       confidence, confidence_reason, status, created_at,
                       spec_path, plan_path
                FROM plans;
                DROP TABLE plans;
                ALTER TABLE plans_new RENAME TO plans;
                PRAGMA foreign_keys=ON;
                """
            )

        await connection.execute(
            """
            INSERT OR IGNORE INTO opus_state (id, status, queued_actions)
            VALUES (1, 'available', '[]')
            """
        )
        await connection.commit()

        cursor = await connection.execute("PRAGMA user_version")
        row = await cursor.fetchone()
        current = int(row[0]) if row else 0
        for migration in MIGRATIONS:
            if migration.version <= current:
                continue
            logger.info(
                "Applying schema migration %d: %s",
                migration.version,
                migration.description,
            )
            await migration.apply(connection)
            await connection.execute(f"PRAGMA user_version = {migration.version}")
            await connection.commit()

        logger.info("Database initialized successfully")

    async def close(self) -> None:
        if self._connection is None:
            return

        await self._connection.close()
        self._connection = None
        logger.info("Database connection closed")

    async def execute(
        self, query: str, params: tuple[Any, ...] = ()
    ) -> aiosqlite.Cursor:
        connection = self._require_connection()
        cursor = await connection.execute(query, params)
        await connection.commit()
        return cursor

    async def fetch_one(
        self, query: str, params: tuple[Any, ...] = ()
    ) -> dict[str, Any] | None:
        connection = self._require_connection()
        async with connection.execute(query, params) as cursor:
            row = await cursor.fetchone()
        return None if row is None else dict(row)

    async def fetch_all(
        self, query: str, params: tuple[Any, ...] = ()
    ) -> list[dict[str, Any]]:
        connection = self._require_connection()
        async with connection.execute(query, params) as cursor:
            rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    def _require_connection(self) -> aiosqlite.Connection:
        if self._connection is None:
            message = "Database connection is not initialized"
            raise RuntimeError(message)
        return self._connection
