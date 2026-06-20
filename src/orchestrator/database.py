"""Async SQLite database wrapper and migrations."""

from __future__ import annotations

import contextlib
import logging
from typing import Any

import aiosqlite


logger = logging.getLogger(__name__)

SQLITE_URL_PREFIX = "sqlite+aiosqlite:///"

MIGRATIONS: tuple[str, ...] = (
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
        confidence_threshold REAL NOT NULL DEFAULT 0.7,
        max_retries INTEGER NOT NULL DEFAULT 3,
        max_improvement_cycles INTEGER NOT NULL DEFAULT 5,
        lm_studio_url TEXT NOT NULL DEFAULT 'http://host.docker.internal:1234',
        model_name TEXT NOT NULL DEFAULT '',
        harness TEXT NOT NULL DEFAULT 'aider',
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
        spec TEXT NOT NULL,
        opus_plan TEXT,
        plan_branch_name TEXT,
        source TEXT NOT NULL DEFAULT 'user',
        confidence REAL,
        confidence_reason TEXT,
        status TEXT NOT NULL DEFAULT 'pending',
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
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

    async def initialize(self) -> None:
        if self._connection is None:
            logger.info("Connecting to SQLite database at %s", self._db_path)
            self._connection = await aiosqlite.connect(self._db_path)
            self._connection.row_factory = aiosqlite.Row

        connection = self._require_connection()
        await connection.execute("PRAGMA journal_mode=WAL;")
        await connection.execute("PRAGMA foreign_keys=ON;")

        for migration in MIGRATIONS:
            await connection.execute(migration)

        for _column, ddl in (
            ("agent_model", "agent_model TEXT"),
            ("agent_model_effort", "agent_model_effort TEXT"),
            ("harness", "harness TEXT NOT NULL DEFAULT 'aider'"),
        ):
            with contextlib.suppress(Exception):
                await connection.execute(
                    f"ALTER TABLE projects ADD COLUMN {ddl}"  # noqa: S608
                )

        await connection.execute(
            """
            INSERT OR IGNORE INTO opus_state (id, status, queued_actions)
            VALUES (1, 'available', '[]')
            """
        )
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
