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


MIGRATIONS: list[Migration] = [
    Migration(1, "baseline: schema as of 2026-07-02", _migration_0001_baseline),
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
