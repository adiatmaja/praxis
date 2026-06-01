# Deployment

## Docker Images

### Orchestrator (`docker/orchestrator/Dockerfile`)

Python 3.11-slim with git, gh CLI, and uv. Installs the project as an editable
package, serves FastAPI on port 8080.

```bash
docker build -t orchestrator:latest -f docker/orchestrator/Dockerfile .
```

### Aider Agent (`docker/aider-agent/Dockerfile`)

Python 3.11-slim with git, gh CLI, and `aider-chat`. Runs as non-root `agent` user.
Each container is a single-use worker that clones, implements, pushes, and creates a PR.

```bash
docker build -t aider-agent:latest -f docker/aider-agent/Dockerfile docker/aider-agent/
```

**Agent container environment variables** (all required, set by AgentManager):

| Variable | Description |
|----------|-------------|
| `REPO_URL` | GitHub repo clone URL |
| `BRANCH` | Agent branch name (`agent/{task-slug}`) |
| `BASE_BRANCH` | Plan branch to branch from and PR into |
| `TASK_PROMPT` | Full implementation instructions |
| `OPENAI_API_BASE` | LM Studio endpoint for Aider |
| `AIDER_MODEL` | Model name for Aider (`--model` flag) |
| `GH_TOKEN` | GitHub token for push and PR creation |
| `CALLBACK_URL` | Orchestrator callback (`/api/internal/agent-done`) |
| `TASK_ID` | Task ID for the callback payload |
| `RUN_ID` | Agent run ID for the callback payload |

**Agent entrypoint pipeline** (`entrypoint.sh`):

```
clone repo -> checkout base branch -> create agent branch
    -> run aider -> push -> create PR -> callback orchestrator
```

On failure, the trap sends a `"failed"` callback so the orchestrator can retry.

## Compose Profiles

### Local (default)

```bash
docker compose up --build
```

- Orchestrator on port 8080 (configurable via `PORT` env)
- Docker socket mounted for spawning agent containers
- SSH keys mounted read-only for git operations
- `data/` volume for SQLite persistence
- `host.docker.internal` mapped for LM Studio access

### Local Dev (hot reload)

```bash
docker compose -f docker-compose.yml -f docker-compose.local.yml up --build
```

- Source code mounted (`src/`, `web/`) for live reload
- `--reload` flag on uvicorn
- Default dev tokens if env vars not set

### Hosted (with Caddy)

```bash
DOMAIN=praxis.example.com docker compose --profile hosted up --build
```

- Caddy reverse proxy with automatic HTTPS via Let's Encrypt
- Security headers: `X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy`
- Caddy data/config volumes for certificate persistence

## Compose Services

```yaml
# docker-compose.yml
services:
  orchestrator:       # FastAPI server (always runs)
  caddy:              # Reverse proxy (profile: hosted)
```

### Volumes

| Volume | Mount | Purpose |
|--------|-------|---------|
| `./data` | `/app/data` | SQLite database persistence |
| `/var/run/docker.sock` | `/var/run/docker.sock` | Docker SDK for spawning agents |
| `${SSH_KEY_PATH:-~/.ssh}` | `/root/.ssh:ro` | Git SSH keys (read-only) |
| `caddy_data` | `/data` | Caddy certificates (hosted only) |
| `caddy_config` | `/config` | Caddy config (hosted only) |

## API Reference

All `/api/*` endpoints require Bearer token auth (`Authorization: Bearer <AUTH_TOKEN>`)
except `/health` and `/api/internal/*`.

Interactive docs available at `/docs` (Swagger UI) when the server is running.

### Projects

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/projects` | Create a project |
| `GET` | `/api/projects` | List all projects |
| `GET` | `/api/projects/{id}` | Get project by ID |
| `PATCH` | `/api/projects/{id}` | Update project settings |

### Plans

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/projects/{id}/plans` | Submit a spec for planning |
| `GET` | `/api/projects/{id}/plans` | List plans for a project |
| `GET` | `/api/plans/{id}` | Get plan details |
| `POST` | `/api/plans/{id}/approve` | Approve an autonomous plan |
| `POST` | `/api/plans/{id}/reject` | Reject an autonomous plan |

### Tasks

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/plans/{id}/tasks` | List tasks in a plan |
| `GET` | `/api/tasks/{id}` | Get task with agent run history |
| `POST` | `/api/tasks/{id}/stop` | Stop a running agent |
| `GET` | `/api/tasks/{id}/logs` | Stream agent logs |

### System

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/status` | Orchestrator status (Opus state + agent counts) |
| `GET` | `/api/opus/state` | Opus availability and queue |
| `GET` | `/api/events` | SSE event stream (long-lived) |
| `GET` | `/health` | Health check (no auth) |

### Internal

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/internal/agent-done` | Agent completion callback (no auth) |

## Prerequisites

| Dependency | Required For | Notes |
|------------|-------------|-------|
| Python 3.11+ | Local dev | Via uv |
| Docker | Agent spawning | Docker socket must be accessible |
| LM Studio | Aider agents | Running on `localhost:1234` (or configured URL) |
| Claude Code CLI | Planning/review | `claude -p` must be available in PATH |
| GitHub CLI (`gh`) | PR operations | Authenticated via `GITHUB_TOKEN` |
