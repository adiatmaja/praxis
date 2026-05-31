# Plan 1: Project Skeleton, Config & Database

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Set up the project structure, configuration system, SQLite database with all tables, and Pydantic models for the AI Agent Orchestrator.

**Architecture:** Single Python package (`orchestrator`) under `src/`. Config via environment variables with pydantic-settings. SQLite via aiosqlite with raw SQL migrations (no ORM). Pydantic models for request/response validation.

**Tech Stack:** Python 3.11, FastAPI, aiosqlite, pydantic, pydantic-settings, ruff, mypy, pytest

---

## Full Project Context

This is **Plan 1 of 5** for the AI Agent Orchestrator — a Docker-based system that uses Claude Opus (via `claude -p` CLI) as a planning/review brain and local LLM (via LM Studio + Aider) as implementation workers.

**What this plan builds:** The foundation — project scaffolding, configuration, database schema, and data models. No business logic yet.

**What comes after:**
- Plan 2: Core engine (Agent Manager, Opus Bridge, Task Queue)
- Plan 3: REST API + CLI client
- Plan 4: Docker (Aider agent image, orchestrator Dockerfile, compose)
- Plan 5: Web dashboard, SSE streaming, autonomous improvement loop

**Project root:** `C:\working-space\praxis\`

**Design spec:** `docs/superpowers/specs/2026-06-01-praxis-design.md`

---

## File Map

| File | Action | Responsibility |
|------|--------|----------------|
| `pyproject.toml` | Create | Project metadata, dependencies, tool config |
| `.python-version` | Create | Pin Python 3.11 |
| `.env.example` | Create | Template for required env vars |
| `.gitignore` | Create | Python + project-specific ignores |
| `src/orchestrator/__init__.py` | Create | Package init |
| `src/orchestrator/config.py` | Create | Settings class (env vars → typed config) |
| `src/orchestrator/database.py` | Create | SQLite connection pool + migration runner |
| `src/orchestrator/models/__init__.py` | Create | Models package init |
| `src/orchestrator/models/schemas.py` | Create | Pydantic request/response models |
| `tests/__init__.py` | Create | Tests package |
| `tests/conftest.py` | Create | Shared fixtures (temp DB, test config) |
| `tests/test_config.py` | Create | Config loading tests |
| `tests/test_database.py` | Create | Database migration + CRUD tests |
| `tests/test_schemas.py` | Create | Pydantic model validation tests |

---

### Task 1: Project Scaffolding

**Files:**
- Create: `pyproject.toml`
- Create: `.python-version`
- Create: `.gitignore`
- Create: `.env.example`
- Create: `src/orchestrator/__init__.py`
- Create: `src/orchestrator/models/__init__.py`
- Create: `tests/__init__.py`

**Depends on:** None

- [ ] **Step 1: Initialize git repository**

```bash
cd C:\working-space\praxis
git init
```

- [ ] **Step 2: Create `.python-version`**

Create file `.python-version`:
```
3.11
```

- [ ] **Step 3: Create `.gitignore`**

Create file `.gitignore`:
```gitignore
# Python
__pycache__/
*.py[cod]
*$py.class
*.egg-info/
dist/
build/
*.egg

# Virtual environment
.venv/
venv/

# Environment
.env

# IDE
.vscode/
.idea/

# Testing
htmlcov/
.coverage
.pytest_cache/

# Database
data/*.db

# Docker
.superpowers/

# OS
Thumbs.db
.DS_Store
```

- [ ] **Step 4: Create `pyproject.toml`**

Create file `pyproject.toml`:
```toml
[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.backends._legacy:_Backend"

[project]
name = "praxis"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = [
    "fastapi>=0.115",
    "uvicorn[standard]>=0.32",
    "aiosqlite>=0.20",
    "pydantic>=2.0",
    "pydantic-settings>=2.0",
    "httpx>=0.27",
    "docker>=7.0",
    "typer>=0.12",
    "rich>=13.0",
    "bcrypt>=4.0",
    "sse-starlette>=2.0",
]

[project.optional-dependencies]
dev = [
    "pytest>=8.0",
    "pytest-cov>=5.0",
    "pytest-asyncio>=0.24",
    "pytest-mock>=3.0",
    "ruff>=0.8",
    "mypy>=1.0",
]

[project.scripts]
orchestrator-cli = "cli.main:app"

[tool.setuptools.packages.find]
where = ["src"]

[tool.ruff]
line-length = 88
target-version = "py311"

[tool.ruff.lint]
select = ["E", "W", "F", "I", "N", "UP", "B", "C4", "DTZ", "EM", "ISC", "PIE", "PT", "Q", "RET", "SIM", "TID", "ARG"]
ignore = ["E501", "B008", "ARG001", "ARG002"]

[tool.ruff.lint.per-file-ignores]
"__init__.py" = ["F401"]

[tool.ruff.format]
quote-style = "double"

[tool.ruff.lint.isort]
lines-after-imports = 2

[tool.mypy]
python_version = "3.11"
disallow_untyped_defs = true
disallow_incomplete_defs = true
warn_return_any = true
warn_redundant_casts = true
warn_unused_ignores = true
strict_equality = true
no_implicit_optional = true

[tool.pytest.ini_options]
testpaths = ["tests"]
asyncio_mode = "auto"
markers = [
    "unit: fast, isolated unit tests",
    "integration: tests that touch the database",
]
addopts = "-v --tb=short"
```

- [ ] **Step 5: Create `.env.example`**

Create file `.env.example`:
```bash
# Required
AUTH_TOKEN=your-secret-token-here
GITHUB_TOKEN=ghp_your_github_token

# Optional (defaults shown)
DATABASE_URL=sqlite+aiosqlite:///data/orchestrator.db
LM_STUDIO_URL=http://host.docker.internal:1234
HOST=0.0.0.0
PORT=8080
```

- [ ] **Step 6: Create package init files**

Create file `src/orchestrator/__init__.py`:
```python
"""AI Agent Orchestrator — Docker-based AI agent management system."""
```

Create file `src/orchestrator/models/__init__.py`:
```python
"""Pydantic models for request/response validation."""
```

Create file `tests/__init__.py`:
```python
```

- [ ] **Step 7: Initialize virtual environment and install dependencies**

```bash
cd C:\working-space\praxis
uv venv
uv sync --extra dev
```

- [ ] **Step 8: Commit**

```bash
git add .
git commit -m "chore: scaffold project structure with pyproject.toml and dependencies"
```

---

### Task 2: Configuration System

**Files:**
- Create: `src/orchestrator/config.py`
- Create: `tests/test_config.py`

**Depends on:** Task 1

- [ ] **Step 1: Write failing tests for config**

Create file `tests/test_config.py`:
```python
import pytest

from orchestrator.config import Settings


@pytest.mark.unit
class TestSettings:
    def test_loads_required_fields(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AUTH_TOKEN", "test-token")
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_test")
        settings = Settings()
        assert settings.auth_token == "test-token"
        assert settings.github_token == "ghp_test"

    def test_default_values(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AUTH_TOKEN", "test-token")
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_test")
        settings = Settings()
        assert settings.database_url == "sqlite+aiosqlite:///data/orchestrator.db"
        assert settings.lm_studio_url == "http://host.docker.internal:1234"
        assert settings.host == "0.0.0.0"
        assert settings.port == 8080

    def test_missing_required_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("AUTH_TOKEN", raising=False)
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        with pytest.raises(Exception):
            Settings()

    def test_custom_values(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AUTH_TOKEN", "custom-token")
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_custom")
        monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///custom.db")
        monkeypatch.setenv("LM_STUDIO_URL", "http://localhost:9999")
        monkeypatch.setenv("PORT", "3000")
        settings = Settings()
        assert settings.database_url == "sqlite+aiosqlite:///custom.db"
        assert settings.lm_studio_url == "http://localhost:9999"
        assert settings.port == 3000
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd C:\working-space\praxis
uv run pytest tests/test_config.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'orchestrator.config'`

- [ ] **Step 3: Implement config module**

Create file `src/orchestrator/config.py`:
```python
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    auth_token: str
    github_token: str
    database_url: str = "sqlite+aiosqlite:///data/orchestrator.db"
    lm_studio_url: str = "http://host.docker.internal:1234"
    host: str = "0.0.0.0"
    port: int = 8080

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run pytest tests/test_config.py -v
```

Expected: All 4 tests PASS

- [ ] **Step 5: Commit**

```bash
git add src/orchestrator/config.py tests/test_config.py
git commit -m "feat: add configuration system with pydantic-settings"
```

---

### Task 3: Database Module

**Files:**
- Create: `src/orchestrator/database.py`
- Create: `tests/conftest.py`
- Create: `tests/test_database.py`

**Depends on:** Task 2

- [ ] **Step 1: Write shared test fixtures**

Create file `tests/conftest.py`:
```python
import asyncio
from pathlib import Path

import pytest
import pytest_asyncio
import aiosqlite

from orchestrator.config import Settings
from orchestrator.database import Database


@pytest.fixture
def test_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Settings:
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("AUTH_TOKEN", "test-token")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_test")
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")
    return Settings()


@pytest_asyncio.fixture
async def db(test_settings: Settings) -> Database:
    database = Database(test_settings.database_url)
    await database.initialize()
    yield database
    await database.close()
```

- [ ] **Step 2: Write failing tests for database**

Create file `tests/test_database.py`:
```python
import pytest

from orchestrator.database import Database


@pytest.mark.integration
class TestDatabaseInitialization:
    async def test_creates_all_tables(self, db: Database) -> None:
        tables = await db.fetch_all(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
        table_names = [row["name"] for row in tables]
        assert "users" in table_names
        assert "projects" in table_names
        assert "plans" in table_names
        assert "tasks" in table_names
        assert "agent_runs" in table_names
        assert "opus_state" in table_names

    async def test_opus_state_singleton_exists(self, db: Database) -> None:
        row = await db.fetch_one("SELECT * FROM opus_state WHERE id = 1")
        assert row is not None
        assert row["status"] == "available"

    async def test_initialize_is_idempotent(self, db: Database) -> None:
        await db.initialize()
        tables = await db.fetch_all(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
        assert len(tables) >= 6


@pytest.mark.integration
class TestDatabaseOperations:
    async def test_execute_and_fetch_one(self, db: Database) -> None:
        await db.execute(
            "INSERT INTO users (id, name, token_hash) VALUES (?, ?, ?)",
            ("u1", "TestUser", "hash123"),
        )
        row = await db.fetch_one("SELECT * FROM users WHERE id = ?", ("u1",))
        assert row is not None
        assert row["name"] == "TestUser"

    async def test_fetch_all(self, db: Database) -> None:
        await db.execute(
            "INSERT INTO users (id, name, token_hash) VALUES (?, ?, ?)",
            ("u1", "User1", "hash1"),
        )
        await db.execute(
            "INSERT INTO users (id, name, token_hash) VALUES (?, ?, ?)",
            ("u2", "User2", "hash2"),
        )
        rows = await db.fetch_all("SELECT * FROM users ORDER BY name")
        assert len(rows) == 2
        assert rows[0]["name"] == "User1"
        assert rows[1]["name"] == "User2"

    async def test_fetch_one_returns_none_for_missing(self, db: Database) -> None:
        row = await db.fetch_one("SELECT * FROM users WHERE id = ?", ("missing",))
        assert row is None
```

- [ ] **Step 3: Run tests to verify they fail**

```bash
uv run pytest tests/test_database.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'orchestrator.database'`

- [ ] **Step 4: Implement database module**

Create file `src/orchestrator/database.py`:
```python
import logging
from datetime import datetime, timezone

import aiosqlite


logger = logging.getLogger(__name__)

MIGRATIONS = [
    """
    CREATE TABLE IF NOT EXISTS users (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        token_hash TEXT NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS projects (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL REFERENCES users(id),
        name TEXT NOT NULL,
        repo_url TEXT NOT NULL,
        default_branch TEXT NOT NULL DEFAULT 'main',
        approval_gate BOOLEAN NOT NULL DEFAULT 1,
        confidence_threshold REAL NOT NULL DEFAULT 0.7,
        max_retries INTEGER NOT NULL DEFAULT 3,
        max_improvement_cycles INTEGER NOT NULL DEFAULT 5,
        lm_studio_url TEXT NOT NULL DEFAULT 'http://host.docker.internal:1234',
        model_name TEXT NOT NULL DEFAULT '',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS plans (
        id TEXT PRIMARY KEY,
        project_id TEXT NOT NULL REFERENCES projects(id),
        spec TEXT NOT NULL,
        opus_plan TEXT,
        plan_branch_name TEXT,
        source TEXT NOT NULL DEFAULT 'user',
        confidence REAL,
        confidence_reason TEXT,
        status TEXT NOT NULL DEFAULT 'pending',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS tasks (
        id TEXT PRIMARY KEY,
        plan_id TEXT NOT NULL REFERENCES plans(id),
        title TEXT NOT NULL,
        description TEXT NOT NULL,
        branch_name TEXT NOT NULL,
        pr_url TEXT,
        status TEXT NOT NULL DEFAULT 'pending',
        attempt INTEGER NOT NULL DEFAULT 1,
        review_feedback TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS agent_runs (
        id TEXT PRIMARY KEY,
        task_id TEXT NOT NULL REFERENCES tasks(id),
        container_id TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'running',
        logs TEXT DEFAULT '',
        started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        finished_at TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS opus_state (
        id INTEGER PRIMARY KEY,
        status TEXT NOT NULL DEFAULT 'available',
        rate_limited_at TIMESTAMP,
        resume_at TIMESTAMP,
        queued_actions TEXT DEFAULT '[]'
    )
    """,
]

SEED_OPUS_STATE = """
    INSERT OR IGNORE INTO opus_state (id, status, queued_actions)
    VALUES (1, 'available', '[]')
"""


class Database:
    """Async SQLite database wrapper with migration support."""

    def __init__(self, database_url: str) -> None:
        # Extract file path from URL: sqlite+aiosqlite:///path/to/db
        self._db_path = database_url.replace("sqlite+aiosqlite:///", "")
        self._conn: aiosqlite.Connection | None = None

    async def initialize(self) -> None:
        self._conn = await aiosqlite.connect(self._db_path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA foreign_keys=ON")
        for migration in MIGRATIONS:
            await self._conn.execute(migration)
        await self._conn.execute(SEED_OPUS_STATE)
        await self._conn.commit()
        logger.info("Database initialized at %s", self._db_path)

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()
            self._conn = None

    async def execute(
        self, query: str, params: tuple = ()
    ) -> aiosqlite.Cursor:
        assert self._conn is not None, "Database not initialized"
        cursor = await self._conn.execute(query, params)
        await self._conn.commit()
        return cursor

    async def fetch_one(
        self, query: str, params: tuple = ()
    ) -> dict | None:
        assert self._conn is not None, "Database not initialized"
        cursor = await self._conn.execute(query, params)
        row = await cursor.fetchone()
        if row is None:
            return None
        return dict(row)

    async def fetch_all(
        self, query: str, params: tuple = ()
    ) -> list[dict]:
        assert self._conn is not None, "Database not initialized"
        cursor = await self._conn.execute(query, params)
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
uv run pytest tests/test_database.py -v
```

Expected: All 6 tests PASS

- [ ] **Step 6: Commit**

```bash
git add src/orchestrator/database.py tests/conftest.py tests/test_database.py
git commit -m "feat: add SQLite database with migrations and async wrapper"
```

---

### Task 4: Pydantic Models

**Files:**
- Create: `src/orchestrator/models/schemas.py`
- Create: `tests/test_schemas.py`

**Depends on:** Task 1

- [ ] **Step 1: Write failing tests for schemas**

Create file `tests/test_schemas.py`:
```python
import pytest
from pydantic import ValidationError

from orchestrator.models.schemas import (
    ProjectCreate,
    ProjectResponse,
    ProjectUpdate,
    PlanCreate,
    PlanResponse,
    TaskResponse,
    AgentRunResponse,
    OpusStateResponse,
    OpusPlanPayload,
    OpusReviewPayload,
    OpusImprovementPayload,
    TaskStatus,
    PlanStatus,
    OpusStatus,
)


@pytest.mark.unit
class TestProjectSchemas:
    def test_project_create_valid(self) -> None:
        p = ProjectCreate(
            name="My Project",
            repo_url="https://github.com/user/repo",
            model_name="deepseek-coder-v2",
        )
        assert p.name == "My Project"
        assert p.default_branch == "main"

    def test_project_create_defaults(self) -> None:
        p = ProjectCreate(
            name="Test",
            repo_url="https://github.com/user/repo",
            model_name="qwen3-32b",
        )
        assert p.approval_gate is True
        assert p.confidence_threshold == 0.7
        assert p.max_retries == 3
        assert p.max_improvement_cycles == 5

    def test_project_update_partial(self) -> None:
        u = ProjectUpdate(approval_gate=False)
        assert u.approval_gate is False
        assert u.model_name is None

    def test_project_create_missing_required(self) -> None:
        with pytest.raises(ValidationError):
            ProjectCreate(name="Test")  # type: ignore[call-arg]


@pytest.mark.unit
class TestPlanSchemas:
    def test_plan_create_valid(self) -> None:
        p = PlanCreate(spec="Build a login page with email and password")
        assert p.spec == "Build a login page with email and password"

    def test_plan_create_empty_spec_rejected(self) -> None:
        with pytest.raises(ValidationError):
            PlanCreate(spec="")


@pytest.mark.unit
class TestOpusPayloads:
    def test_opus_plan_payload_valid(self) -> None:
        payload = OpusPlanPayload(
            plan_summary="Auth system",
            plan_slug="auth-system",
            tasks=[
                {
                    "title": "Login",
                    "slug": "auth-login",
                    "description": "Build login",
                    "depends_on": [],
                }
            ],
        )
        assert len(payload.tasks) == 1
        assert payload.tasks[0]["slug"] == "auth-login"

    def test_opus_review_pass(self) -> None:
        r = OpusReviewPayload(
            verdict="pass",
            feedback="Looks good",
            issues=[],
        )
        assert r.verdict == "pass"

    def test_opus_review_fail_with_issues(self) -> None:
        r = OpusReviewPayload(
            verdict="fail",
            feedback="Missing validation",
            issues=["No email check", "No CSRF"],
        )
        assert len(r.issues) == 2

    def test_opus_review_invalid_verdict(self) -> None:
        with pytest.raises(ValidationError):
            OpusReviewPayload(
                verdict="maybe",
                feedback="Unsure",
                issues=[],
            )

    def test_opus_improvement_payload(self) -> None:
        payload = OpusImprovementPayload(
            confidence=0.82,
            reason="Missing error handling",
            proposed_tasks=[
                {
                    "title": "Add error handling",
                    "slug": "improve-error-handling",
                    "description": "Add try/except blocks",
                }
            ],
        )
        assert payload.confidence == 0.82
        assert len(payload.proposed_tasks) == 1


@pytest.mark.unit
class TestEnums:
    def test_task_statuses(self) -> None:
        assert TaskStatus.PENDING == "pending"
        assert TaskStatus.IN_PROGRESS == "in_progress"
        assert TaskStatus.REVIEWING == "reviewing"
        assert TaskStatus.PASSED == "passed"
        assert TaskStatus.FAILED == "failed"
        assert TaskStatus.MERGED == "merged"

    def test_plan_statuses(self) -> None:
        assert PlanStatus.PENDING == "pending"
        assert PlanStatus.ACTIVE == "active"
        assert PlanStatus.COMPLETED == "completed"
        assert PlanStatus.REJECTED == "rejected"

    def test_opus_statuses(self) -> None:
        assert OpusStatus.AVAILABLE == "available"
        assert OpusStatus.RATE_LIMITED == "rate_limited"
        assert OpusStatus.RESUMING == "resuming"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest tests/test_schemas.py -v
```

Expected: FAIL with `ImportError`

- [ ] **Step 3: Implement schemas module**

Create file `src/orchestrator/models/schemas.py`:
```python
from datetime import datetime
from enum import StrEnum
from typing import TypedDict

from pydantic import BaseModel, Field, field_validator


class TaskStatus(StrEnum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    REVIEWING = "reviewing"
    PASSED = "passed"
    FAILED = "failed"
    MERGED = "merged"


class PlanStatus(StrEnum):
    PENDING = "pending"
    ACTIVE = "active"
    COMPLETED = "completed"
    REJECTED = "rejected"


class OpusStatus(StrEnum):
    AVAILABLE = "available"
    RATE_LIMITED = "rate_limited"
    RESUMING = "resuming"


class AgentRunStatus(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    STOPPED = "stopped"


# --- Request schemas ---


class ProjectCreate(BaseModel):
    name: str
    repo_url: str
    model_name: str
    default_branch: str = "main"
    approval_gate: bool = True
    confidence_threshold: float = Field(default=0.7, ge=0.0, le=1.0)
    max_retries: int = Field(default=3, ge=1, le=10)
    max_improvement_cycles: int = Field(default=5, ge=1, le=20)
    lm_studio_url: str = "http://host.docker.internal:1234"


class ProjectUpdate(BaseModel):
    name: str | None = None
    approval_gate: bool | None = None
    confidence_threshold: float | None = Field(default=None, ge=0.0, le=1.0)
    max_retries: int | None = Field(default=None, ge=1, le=10)
    max_improvement_cycles: int | None = Field(default=None, ge=1, le=20)
    lm_studio_url: str | None = None
    model_name: str | None = None


class PlanCreate(BaseModel):
    spec: str

    @field_validator("spec")
    @classmethod
    def spec_not_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("Spec must not be empty")
        return v


# --- Response schemas ---


class ProjectResponse(BaseModel):
    id: str
    user_id: str
    name: str
    repo_url: str
    default_branch: str
    approval_gate: bool
    confidence_threshold: float
    max_retries: int
    max_improvement_cycles: int
    lm_studio_url: str
    model_name: str
    created_at: str


class PlanResponse(BaseModel):
    id: str
    project_id: str
    spec: str
    opus_plan: str | None
    plan_branch_name: str | None
    source: str
    confidence: float | None
    confidence_reason: str | None
    status: str
    created_at: str


class TaskResponse(BaseModel):
    id: str
    plan_id: str
    title: str
    description: str
    branch_name: str
    pr_url: str | None
    status: str
    attempt: int
    review_feedback: str | None
    created_at: str
    updated_at: str


class AgentRunResponse(BaseModel):
    id: str
    task_id: str
    container_id: str
    status: str
    logs: str
    started_at: str
    finished_at: str | None


class OpusStateResponse(BaseModel):
    status: str
    rate_limited_at: str | None
    resume_at: str | None
    queued_count: int


# --- Opus JSON payloads ---


class OpusTaskItem(TypedDict):
    title: str
    slug: str
    description: str
    depends_on: list[str]


class OpusImprovementTaskItem(TypedDict):
    title: str
    slug: str
    description: str


class OpusPlanPayload(BaseModel):
    plan_summary: str
    plan_slug: str
    tasks: list[OpusTaskItem]


class OpusReviewPayload(BaseModel):
    verdict: str
    feedback: str
    issues: list[str]

    @field_validator("verdict")
    @classmethod
    def verdict_must_be_valid(cls, v: str) -> str:
        if v not in ("pass", "fail"):
            raise ValueError("Verdict must be 'pass' or 'fail'")
        return v


class OpusImprovementPayload(BaseModel):
    confidence: float = Field(ge=0.0, le=1.0)
    reason: str
    proposed_tasks: list[OpusImprovementTaskItem]
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run pytest tests/test_schemas.py -v
```

Expected: All 12 tests PASS

- [ ] **Step 5: Commit**

```bash
git add src/orchestrator/models/schemas.py tests/test_schemas.py
git commit -m "feat: add Pydantic models for all request/response schemas"
```

---

### Task 5: FastAPI App Skeleton

**Files:**
- Create: `src/orchestrator/main.py`
- Create: `src/orchestrator/api/__init__.py`
- Create: `src/orchestrator/api/auth.py`
- Create: `src/orchestrator/core/__init__.py`

**Depends on:** Task 2, Task 3

- [ ] **Step 1: Create package init files**

Create file `src/orchestrator/api/__init__.py`:
```python
"""API route handlers."""
```

Create file `src/orchestrator/core/__init__.py`:
```python
"""Core business logic modules."""
```

- [ ] **Step 2: Implement auth dependency**

Create file `src/orchestrator/api/auth.py`:
```python
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from orchestrator.config import Settings


security = HTTPBearer()


def get_settings() -> Settings:
    return Settings()


async def verify_token(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    settings: Settings = Depends(get_settings),
) -> str:
    if credentials.credentials != settings.auth_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication token",
        )
    return credentials.credentials
```

- [ ] **Step 3: Implement FastAPI app with lifespan**

Create file `src/orchestrator/main.py`:
```python
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncGenerator

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from orchestrator.config import Settings
from orchestrator.database import Database


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(name)s | %(levelname)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    settings = Settings()
    db_path = settings.database_url.replace("sqlite+aiosqlite:///", "")
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    db = Database(settings.database_url)
    await db.initialize()
    app.state.db = db
    app.state.settings = settings
    logger.info("Orchestrator started on %s:%d", settings.host, settings.port)
    yield
    await db.close()
    logger.info("Orchestrator stopped")


app = FastAPI(
    title="AI Agent Orchestrator",
    version="0.1.0",
    lifespan=lifespan,
)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}
```

- [ ] **Step 4: Verify app starts**

```bash
cd C:\working-space\praxis
AUTH_TOKEN=test GITHUB_TOKEN=test uv run uvicorn orchestrator.main:app --host 127.0.0.1 --port 8080
```

Expected: Server starts, visit `http://127.0.0.1:8080/health` returns `{"status": "ok"}`. Stop with Ctrl+C.

- [ ] **Step 5: Commit**

```bash
git add src/orchestrator/main.py src/orchestrator/api/__init__.py src/orchestrator/api/auth.py src/orchestrator/core/__init__.py
git commit -m "feat: add FastAPI app skeleton with lifespan and auth dependency"
```

---

### Task 6: Run Full Test Suite + Lint

**Files:** None (verification only)

**Depends on:** Task 2, Task 3, Task 4, Task 5

- [ ] **Step 1: Run all tests with coverage**

```bash
cd C:\working-space\praxis
uv run pytest --cov=orchestrator --cov-report=term-missing -v
```

Expected: All tests PASS, coverage > 80%

- [ ] **Step 2: Run ruff format and lint**

```bash
uv run ruff fmt src/ tests/
uv run ruff check --fix src/ tests/
```

Expected: No errors (or auto-fixed)

- [ ] **Step 3: Run mypy**

```bash
uv run mypy src/orchestrator/ --ignore-missing-imports
```

Expected: No errors

- [ ] **Step 4: Commit any formatting fixes**

```bash
git add -A
git diff --cached --quiet || git commit -m "style: apply ruff formatting and lint fixes"
```

---

## Parallel Execution Map

- **Wave 1:** Task 1 (scaffolding), Task 4 (schemas — only needs package init from Task 1, can run in parallel if Task 1 is done first)
- **Wave 2:** Task 2 (config — depends on Task 1)
- **Wave 3:** Task 3 (database — depends on Task 2)
- **Wave 4:** Task 5 (FastAPI skeleton — depends on Task 2, Task 3)
- **Wave 5:** Task 6 (verification — depends on all)

In practice, run Tasks 1 → 2 → 3/4 parallel → 5 → 6.
