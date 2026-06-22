# Contributing to Praxis

Thanks for your interest in contributing! This guide covers local setup, the project
layout, and the conventions we follow.

## Development Setup

```bash
git clone https://github.com/adiatmaja/praxis.git
cd praxis

uv venv && uv sync --extra dev
cp .env.example .env
# Set AUTH_TOKEN to any secret. For local work without git ops,
# GITHUB_TOKEN can be a placeholder.

uv run uvicorn orchestrator.main:app --port 8080 --reload
```

- Dashboard: http://localhost:8080
- API docs: http://localhost:8080/docs

## Project Layout

```
praxis/
├── src/
│   ├── orchestrator/         # FastAPI backend
│   │   ├── main.py           #   App entrypoint + lifespan
│   │   ├── config.py         #   Settings (pydantic-settings)
│   │   ├── database.py       #   SQLite async wrapper
│   │   ├── api/              #   REST + SSE endpoints
│   │   ├── core/             #   Business logic (orchestration, LLM router, agents)
│   │   └── models/           #   Pydantic schemas
│   └── cli/main.py           # Typer CLI client
├── web/index.html            # Single-file dashboard
├── docker/                   # Dockerfiles + Caddyfile
├── tests/                    # pytest suite
├── docs/                     # Architecture, workflow, deployment docs
├── docker-compose.yml
└── pyproject.toml
```

More detail lives in [docs/architecture.md](docs/architecture.md),
[docs/workflow.md](docs/workflow.md), and [docs/deployment.md](docs/deployment.md).

## Checks

Please run these before opening a pull request:

```bash
# Tests (aim to keep coverage at 80%+)
uv run pytest --cov=orchestrator --cov-report=term-missing -v

# Lint + format
uv run ruff format src/ tests/
uv run ruff check --fix src/ tests/

# Type check
uv run mypy src/orchestrator/ --ignore-missing-imports
```

## Coding Standards

- Python 3.11+, PEP 8, type annotations on all function signatures
- Line length 88 (ruff default); use `X | Y` unions and built-in generics (`list[str]`)
- Use the `logging` module, never `print()` in production code
- Google-style docstrings
- Catch specific exceptions; use `raise ... from` for chaining

## Pull Requests

1. Fork the repo and create a branch off `main`.
2. Make your change with tests, and keep the suite green.
3. Use [Conventional Commits](https://www.conventionalcommits.org/) for commit messages
   (`feat:`, `fix:`, `docs:`, `refactor:`, `test:`, `chore:`).
4. Open a PR describing **what** changed and **why**. Link any related issue.

## Reporting Issues

Found a bug or have a feature idea? Open an issue with steps to reproduce (for bugs)
or a clear description of the use case (for features).

By contributing, you agree that your contributions are licensed under the
[Apache License 2.0](LICENSE).
