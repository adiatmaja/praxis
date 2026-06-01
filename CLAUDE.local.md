# CLAUDE.local.md — Testing & Debugging

## Setup

```bash
uv venv && uv sync --extra dev
cp .env.example .env
# Set AUTH_TOKEN to any string (e.g. "local-dev-token-praxis")
# Set GITHUB_TOKEN to "placeholder" for local testing without git ops
```

## Running Tests

```bash
# Full suite with coverage
uv run pytest --cov=orchestrator --cov-report=term-missing -v

# Single test file
uv run pytest tests/test_orchestrator.py -v

# Single test
uv run pytest tests/test_api_projects.py::test_create_project -v

# By marker
uv run pytest -m unit -v
uv run pytest -m integration -v
```

## Running the Server

```bash
# Start (creates data/orchestrator.db and seeds default user automatically)
uv run uvicorn orchestrator.main:app --host 127.0.0.1 --port 8080

# With auto-reload for development
uv run uvicorn orchestrator.main:app --host 127.0.0.1 --port 8080 --reload
```

- Dashboard: http://127.0.0.1:8080
- Swagger UI: http://127.0.0.1:8080/docs
- Health check: http://127.0.0.1:8080/health

## Testing API with curl

```bash
TOKEN="local-dev-token-praxis"  # must match AUTH_TOKEN in .env

# Health (no auth)
curl http://127.0.0.1:8080/health

# Create project
curl -X POST http://127.0.0.1:8080/api/projects \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"name":"my-repo","repo_url":"https://github.com/user/repo","model_name":"qwen3-32b"}'

# List projects
curl http://127.0.0.1:8080/api/projects -H "Authorization: Bearer $TOKEN"

# Submit a plan
curl -X POST http://127.0.0.1:8080/api/projects/<project-id>/plans \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"spec":"Add input validation to the user registration endpoint"}'

# System status
curl http://127.0.0.1:8080/api/status -H "Authorization: Bearer $TOKEN"
```

## Linting & Type Checking

```bash
# Format (check only)
uv run ruff format --check src/ tests/

# Format (apply)
uv run ruff format src/ tests/

# Lint (check only)
uv run ruff check src/ tests/

# Lint (auto-fix)
uv run ruff check --fix src/ tests/

# Type check
uv run mypy src/orchestrator/ --ignore-missing-imports
```

## Resetting State

```bash
# Delete the database to start fresh
rm -f data/orchestrator.db
# Restart the server — it will recreate tables and seed the default user
```

## Debugging Common Issues

### Port already in use (Windows)

```bash
# Find what's using port 8080
netstat -ano | grep ":8080.*LISTENING"
# Kill it (use the PID from the output)
taskkill //PID <pid> //F
```

### "No user found" on project creation

The default user is auto-seeded on startup. If you see this error, the database
was created by an older version of main.py before the seeding code was added.
Fix: delete `data/orchestrator.db` and restart.

### Config test fails with "DID NOT RAISE"

The `.env` file provides fallback values via pydantic-settings. Tests that assert
missing env vars must pass `_env_file=None`:

```python
with pytest.raises(ValidationError):
    Settings(_env_file=None)
```

### Orchestration loop errors on startup

The orchestration loop runs automatically on startup. If there are pending plans
in the database but no `claude` CLI or Docker available, errors will appear in logs.
These are non-fatal — the loop catches exceptions and retries on the next interval.

### Agent Manager unavailable

On machines without Docker, `AgentManager` initialization is caught and set to `None`.
The warning `"Agent manager unavailable during startup"` is expected. The API still
works — only container spawn operations will fail.

## Test Architecture

- **conftest.py** — shared fixtures: in-memory SQLite, test client, auth headers,
  seeded user/project/plan/task data
- **test_api_*.py** — integration tests using FastAPI TestClient
- **test_*.py** (core modules) — unit tests with mocked subprocess/Docker SDK
- All tests use `asyncio_mode = "auto"` — async test functions run automatically
