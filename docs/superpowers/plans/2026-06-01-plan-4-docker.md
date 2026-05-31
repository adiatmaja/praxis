# Plan 4: Docker — Aider Agent Image, Orchestrator Dockerfile, Compose

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Create the Aider agent Docker image (with entrypoint script), the orchestrator Dockerfile, docker-compose files for both local and hosted modes, and a Caddy reverse proxy config.

**Architecture:** Two Docker images — orchestrator (FastAPI server) and aider-agent (Aider + git + gh CLI). The orchestrator runs via docker-compose and spawns aider-agent containers dynamically via Docker SDK. Caddy provides reverse proxy + auto-HTTPS for hosted mode.

**Tech Stack:** Docker, Docker Compose, Caddy, Python 3.11, Aider, git, gh CLI

---

## Full Project Context

This is **Plan 4 of 5** for the AI Agent Orchestrator.

**What Plans 1-3 built:**
- `src/orchestrator/` — Full FastAPI app with:
  - `config.py` — Settings class (env vars: `AUTH_TOKEN`, `GITHUB_TOKEN`, `DATABASE_URL`, `LM_STUDIO_URL`, `HOST`, `PORT`)
  - `database.py` — SQLite async wrapper with migrations
  - `models/schemas.py` — All Pydantic models
  - `core/task_queue.py` — Plan/task state machine
  - `core/git_ops.py` — Branch/PR management
  - `core/opus_bridge.py` — `claude -p` invocation + rate limits
  - `core/agent_manager.py` — Docker container lifecycle (uses image `aider-agent:latest`, container name `aider-agent-{task_id[:8]}`)
  - `api/` — REST endpoints: projects, plans, tasks, system, internal callback
  - `main.py` — FastAPI app with lifespan, registers all routers
- `cli/main.py` — Typer CLI client (reads `ORCHESTRATOR_URL`, `ORCHESTRATOR_TOKEN`)
- `tests/` — Full test suite with 80%+ coverage
- `pyproject.toml` — Project config and dependencies

**Agent container environment variables (set by AgentManager.spawn_agent):**
- `REPO_URL` — GitHub repo URL
- `BRANCH` — Target branch name (e.g., `agent/login`)
- `BASE_BRANCH` — Plan branch to fork from (e.g., `plan/2026-06-01-auth`)
- `TASK_PROMPT` — Implementation instructions
- `OPENAI_API_BASE` — LM Studio endpoint (e.g., `http://host.docker.internal:1234/v1`)
- `AIDER_MODEL` — Model name for Aider (e.g., `openai/deepseek-coder-v2`)
- `GH_TOKEN` — GitHub token for PR creation
- `CALLBACK_URL` — Orchestrator callback (e.g., `http://host.docker.internal:8080/api/internal/agent-done`)
- `TASK_ID` — Task UUID for callback identification

**Agent container lifecycle:** Spawned with `detach=True`, `auto_remove=False`, `network_mode="host"`. Orchestrator polls status and cleans up after callback.

---

## File Map

| File | Action | Responsibility |
|------|--------|----------------|
| `docker/aider-agent/Dockerfile` | Create | Aider agent image with git, gh, curl |
| `docker/aider-agent/entrypoint.sh` | Create | Clone → branch → aider → push → PR → callback |
| `docker/orchestrator/Dockerfile` | Create | Orchestrator image with FastAPI |
| `docker/caddy/Caddyfile` | Create | Reverse proxy config for hosted mode |
| `docker-compose.yml` | Create | Production compose (orchestrator + caddy) |
| `docker-compose.local.yml` | Create | Local dev overrides |
| `.dockerignore` | Create | Exclude unnecessary files from builds |

---

### Task 1: Aider Agent Docker Image

**Files:**
- Create: `docker/aider-agent/Dockerfile`
- Create: `docker/aider-agent/entrypoint.sh`

**Depends on:** None

- [ ] **Step 1: Create the entrypoint script**

Create file `docker/aider-agent/entrypoint.sh`:
```bash
#!/bin/bash
set -euo pipefail

# Required environment variables
: "${REPO_URL:?REPO_URL is required}"
: "${BRANCH:?BRANCH is required}"
: "${BASE_BRANCH:?BASE_BRANCH is required}"
: "${TASK_PROMPT:?TASK_PROMPT is required}"
: "${OPENAI_API_BASE:?OPENAI_API_BASE is required}"
: "${AIDER_MODEL:?AIDER_MODEL is required}"
: "${GH_TOKEN:?GH_TOKEN is required}"
: "${CALLBACK_URL:?CALLBACK_URL is required}"
: "${TASK_ID:?TASK_ID is required}"

WORKSPACE="/workspace"
STATUS="completed"
PR_URL=""

cleanup() {
    local exit_status=$?
    if [ $exit_status -ne 0 ]; then
        STATUS="failed"
    fi
    # Send callback to orchestrator
    curl -s -X POST "${CALLBACK_URL}" \
        -H "Content-Type: application/json" \
        -d "{\"task_id\": \"${TASK_ID}\", \"run_id\": \"${RUN_ID:-}\", \"status\": \"${STATUS}\", \"pr_url\": ${PR_URL:+\"$PR_URL\"}${PR_URL:-null}}" \
        || echo "WARNING: Failed to send callback"
    exit $exit_status
}
trap cleanup EXIT

echo "=== Agent starting ==="
echo "Repo: ${REPO_URL}"
echo "Branch: ${BRANCH}"
echo "Base: ${BASE_BRANCH}"
echo "Model: ${AIDER_MODEL}"

# Step 1: Clone repository
echo "--- Cloning repository ---"
git clone "${REPO_URL}" "${WORKSPACE}"
cd "${WORKSPACE}"

# Step 2: Configure git
git config user.email "agent@orchestrator.local"
git config user.name "AI Agent"

# Step 3: Checkout base branch and create task branch
echo "--- Creating branch ${BRANCH} from ${BASE_BRANCH} ---"
git checkout "${BASE_BRANCH}" 2>/dev/null || git checkout -b "${BASE_BRANCH}" "origin/${BASE_BRANCH}"
git checkout -b "${BRANCH}"

# Step 4: Run Aider
echo "--- Running Aider ---"
aider \
    --message "${TASK_PROMPT}" \
    --model "${AIDER_MODEL}" \
    --auto-commits \
    --yes-always \
    --no-auto-lint \
    --no-suggest-shell-commands \
    --no-show-model-warnings

# Step 5: Push branch
echo "--- Pushing branch ---"
git push -u origin "${BRANCH}"

# Step 6: Create PR
echo "--- Creating PR ---"
PR_URL=$(gh pr create \
    --title "agent: ${BRANCH}" \
    --body "Automated implementation by AI Agent.

Task: ${TASK_PROMPT:0:500}

---
Generated by AI Agent Orchestrator" \
    --base "${BASE_BRANCH}" \
    --head "${BRANCH}" \
    2>&1)

echo "PR created: ${PR_URL}"
echo "=== Agent completed ==="
```

- [ ] **Step 2: Create Dockerfile**

Create file `docker/aider-agent/Dockerfile`:
```dockerfile
FROM python:3.11-slim

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    curl \
    openssh-client \
    && rm -rf /var/lib/apt/lists/*

# Install GitHub CLI
RUN curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
    -o /usr/share/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
    > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends gh \
    && rm -rf /var/lib/apt/lists/*

# Install aider
RUN pip install --no-cache-dir aider-chat

# Create non-root user
RUN useradd -m -s /bin/bash agent
USER agent
WORKDIR /home/agent

# Copy entrypoint
COPY --chown=agent:agent entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
```

- [ ] **Step 3: Test the image builds**

```bash
cd C:\working-space\praxis
docker build -t aider-agent:latest -f docker/aider-agent/Dockerfile docker/aider-agent/
```

Expected: Build succeeds

- [ ] **Step 4: Commit**

```bash
git add docker/aider-agent/
git commit -m "feat: add Aider agent Docker image with entrypoint script"
```

---

### Task 2: Orchestrator Docker Image

**Files:**
- Create: `docker/orchestrator/Dockerfile`
- Create: `.dockerignore`

**Depends on:** None

- [ ] **Step 1: Create .dockerignore**

Create file `.dockerignore`:
```
.git
.venv
venv
__pycache__
*.pyc
.pytest_cache
htmlcov
.coverage
.env
.superpowers
data/*.db
docker/aider-agent
node_modules
```

- [ ] **Step 2: Create orchestrator Dockerfile**

Create file `docker/orchestrator/Dockerfile`:
```dockerfile
FROM python:3.11-slim

# Install system dependencies (git and gh for git_ops)
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    curl \
    openssh-client \
    && rm -rf /var/lib/apt/lists/*

# Install GitHub CLI
RUN curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
    -o /usr/share/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
    > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends gh \
    && rm -rf /var/lib/apt/lists/*

# Install uv for fast dependency management
RUN pip install --no-cache-dir uv

WORKDIR /app

# Copy dependency files first (layer caching)
COPY pyproject.toml .
RUN uv pip install --system -e ".[dev]" --no-cache

# Copy application code
COPY src/ src/
COPY cli/ cli/
COPY web/ web/

# Create data directory
RUN mkdir -p /app/data

EXPOSE 8080

CMD ["uvicorn", "orchestrator.main:app", "--host", "0.0.0.0", "--port", "8080"]
```

- [ ] **Step 3: Test the image builds**

```bash
cd C:\working-space\praxis
docker build -t orchestrator:latest -f docker/orchestrator/Dockerfile .
```

Expected: Build succeeds

- [ ] **Step 4: Commit**

```bash
git add docker/orchestrator/Dockerfile .dockerignore
git commit -m "feat: add orchestrator Docker image"
```

---

### Task 3: Docker Compose Files

**Files:**
- Create: `docker-compose.yml`
- Create: `docker-compose.local.yml`

**Depends on:** Task 1, Task 2

- [ ] **Step 1: Create production docker-compose**

Create file `docker-compose.yml`:
```yaml
services:
  orchestrator:
    build:
      context: .
      dockerfile: docker/orchestrator/Dockerfile
    container_name: orchestrator
    ports:
      - "${PORT:-8080}:8080"
    volumes:
      - ./data:/app/data
      - /var/run/docker.sock:/var/run/docker.sock
      - ${SSH_KEY_PATH:-~/.ssh}:/root/.ssh:ro
    environment:
      - AUTH_TOKEN=${AUTH_TOKEN}
      - GITHUB_TOKEN=${GITHUB_TOKEN}
      - DATABASE_URL=sqlite+aiosqlite:///data/orchestrator.db
      - LM_STUDIO_URL=${LM_STUDIO_URL:-http://host.docker.internal:1234}
      - HOST=0.0.0.0
      - PORT=8080
    restart: unless-stopped
    extra_hosts:
      - "host.docker.internal:host-gateway"

  caddy:
    image: caddy:2-alpine
    container_name: caddy
    ports:
      - "80:80"
      - "443:443"
    volumes:
      - ./docker/caddy/Caddyfile:/etc/caddy/Caddyfile:ro
      - caddy_data:/data
      - caddy_config:/config
    depends_on:
      - orchestrator
    profiles:
      - hosted

volumes:
  caddy_data:
  caddy_config:
```

- [ ] **Step 2: Create local dev overrides**

Create file `docker-compose.local.yml`:
```yaml
# Usage: docker compose -f docker-compose.yml -f docker-compose.local.yml up
services:
  orchestrator:
    build:
      context: .
      dockerfile: docker/orchestrator/Dockerfile
    volumes:
      - ./src:/app/src
      - ./cli:/app/cli
      - ./web:/app/web
    command: uvicorn orchestrator.main:app --host 0.0.0.0 --port 8080 --reload
    environment:
      - AUTH_TOKEN=${AUTH_TOKEN:-dev-token}
      - GITHUB_TOKEN=${GITHUB_TOKEN:-ghp_dev}
      - DATABASE_URL=sqlite+aiosqlite:///data/orchestrator.db
      - LM_STUDIO_URL=${LM_STUDIO_URL:-http://host.docker.internal:1234}
```

- [ ] **Step 3: Commit**

```bash
git add docker-compose.yml docker-compose.local.yml
git commit -m "feat: add docker-compose for production and local dev"
```

---

### Task 4: Caddy Reverse Proxy

**Files:**
- Create: `docker/caddy/Caddyfile`

**Depends on:** None

- [ ] **Step 1: Create Caddyfile**

Create file `docker/caddy/Caddyfile`:
```
{$DOMAIN:localhost} {
    reverse_proxy orchestrator:8080

    # Security headers
    header {
        X-Content-Type-Options "nosniff"
        X-Frame-Options "DENY"
        Referrer-Policy "strict-origin-when-cross-origin"
    }

    # Rate limiting for API
    @api path /api/*
    handle @api {
        reverse_proxy orchestrator:8080
    }

    # Serve static files for web dashboard
    handle {
        reverse_proxy orchestrator:8080
    }

    log {
        output stdout
        format console
    }
}
```

- [ ] **Step 2: Commit**

```bash
git add docker/caddy/Caddyfile
git commit -m "feat: add Caddy reverse proxy config with auto-HTTPS"
```

---

### Task 5: Data Directory + Startup Verification

**Files:**
- Create: `data/.gitkeep`

**Depends on:** Task 3

- [ ] **Step 1: Create data directory**

```bash
mkdir -p C:\working-space\praxis\data
touch C:\working-space\praxis\data/.gitkeep
```

- [ ] **Step 2: Verify local compose starts**

```bash
cd C:\working-space\praxis
echo "AUTH_TOKEN=test-token" > .env
echo "GITHUB_TOKEN=ghp_test" >> .env
docker compose -f docker-compose.yml -f docker-compose.local.yml up --build -d
```

Expected: Orchestrator starts successfully

- [ ] **Step 3: Verify health endpoint**

```bash
curl http://localhost:8080/health
```

Expected: `{"status":"ok"}`

- [ ] **Step 4: Stop and clean up**

```bash
docker compose -f docker-compose.yml -f docker-compose.local.yml down
rm .env
```

- [ ] **Step 5: Commit**

```bash
git add data/.gitkeep
git commit -m "chore: add data directory and verify Docker setup"
```

---

## Parallel Execution Map

- **Wave 1:** Task 1 (Aider agent image), Task 2 (Orchestrator image), Task 4 (Caddy) — all independent
- **Wave 2:** Task 3 (Docker Compose — depends on Task 1, Task 2)
- **Wave 3:** Task 5 (Verification — depends on Task 3)
