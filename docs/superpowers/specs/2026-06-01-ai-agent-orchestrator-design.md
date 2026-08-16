# Praxis — Design Spec

**Date:** 2026-06-01
**Status:** Draft
**Author:** Johannes (brainstormed with Claude Opus)

---

## Why "Praxis"

**Praxis** (Greek: *πρᾶξις*) — the process of turning theory into practice.

In Aristotelian philosophy, praxis is distinguished from *theoria* (pure knowledge) and
*poiesis* (making/producing). Praxis is the bridge — where ideas become action through
a cycle of reflection and doing. Later, in critical theory (Marx, Freire), praxis became
the iterative loop of **reflection → action → reflection** — you act on your understanding,
observe the result, reflect, and act again.

This maps directly to the system's core loop:

```
  Spec (theory) → Agents implement (action) → Opus reviews (reflection) → Iterate
```

The name captures what makes this project different from a simple task runner: it doesn't
just execute — it reflects, judges, and improves. The autonomous improvement cycle is
praxis in its purest form: the system examines its own output and decides whether further
action is warranted.

---

## Overview

A Docker-based AI agent orchestration system that uses Claude Opus (via Claude Code CLI)
as the planning and review brain, and local LLM models (via LM Studio + Aider) as
implementation workers. The system manages the full lifecycle: user submits a spec,
Opus breaks it into tasks, agents implement on separate branches, Opus reviews PRs,
and optionally proposes autonomous improvement cycles.

## Goals

- Offload bulk implementation to local LLM to conserve Claude subscription usage
- Support parallel agents working on separate branches simultaneously
- Provide both web dashboard and CLI for dispatch and monitoring
- Be hostable with a custom domain for remote access
- Handle Opus rate limits gracefully with auto-resume
- Support supervised and autonomous operation modes

## Non-Goals (v1)

- Multi-user with full RBAC (data model is user-aware, but only token auth for v1)
- Multiple LM Studio instances / model routing
- Custom agent types beyond Aider
- Mobile app

---

## Architecture

### System Overview

Single FastAPI monolith with 4 core components:

```
┌─────────────────────────────────────────────────────────────────────┐
│  CLIENTS                                                            │
│  ┌──────────┐   ┌──────────────┐   ┌────────────────────────────┐  │
│  │ Web UI   │   │  Typer CLI   │   │  Claude Code (Opus)        │  │
│  │ (Browser)│   │  (Terminal)  │   │  via `claude -p "..."`     │  │
│  └────┬─────┘   └──────┬───────┘   └─────────────┬──────────────┘  │
└───────┼────────────────┼──────────────────────────┼─────────────────┘
        └────────────────┼──────────────────────────┘
                    REST API + SSE
                         │
┌────────────────────────▼────────────────────────────────────────────┐
│  ORCHESTRATOR  (FastAPI)                                            │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐  ┌──────────┐  │
│  │  API Router │  │ Task Queue  │  │   Agent     │  │  Opus    │  │
│  │  REST + SSE │  │  (SQLite)   │  │  Manager    │  │  Bridge  │  │
│  └─────────────┘  └─────────────┘  └──────┬──────┘  └────┬─────┘  │
└────────────────────────────────────────────┼──────────────┼─────────┘
                                             │              │
                              ┌──────────────┼──────┐       │
                              ▼              ▼      ▼       │
                        aider-agent    aider-agent  ...     │
                        (Docker)       (Docker)          claude -p
                              │              │
                              └──────┬───────┘
                                     ▼
                                LM Studio :1234
```

### Core Components

| Component | Responsibility |
|-----------|---------------|
| **API Router** | REST endpoints + SSE streaming. Serves Web UI and CLI. |
| **Task Queue** | SQLite-backed task persistence. Tracks plan -> task -> agent mapping. |
| **Agent Manager** | Spawns/monitors/stops Aider Docker containers via Docker SDK. |
| **Opus Bridge** | Shells out to `claude -p` for planning, review, improvement analysis. Handles rate limit detection + auto-resume. |

---

## Workflow

### Full Lifecycle

1. User submits spec via Web/CLI, selects target repository
2. Orchestrator sends spec to Opus via `claude -p`
3. Opus breaks spec into tasks with branch names
4. Orchestrator creates plan branch from main
5. For each task: spawn Aider container on its own branch (forked from plan branch)
6. Agent implements, auto-commits, pushes, creates PR targeting plan branch
7. Agent calls back to orchestrator when done
8. Orchestrator sends PR diff to Opus for review
9. Opus returns pass/fail with feedback
10. **Pass:** squash merge PR into plan branch, delete agent branch
11. **Fail:** post feedback as PR comment, re-dispatch agent on same branch (max 3 retries)
12. All tasks merged into plan branch -> create integration PR to main
13. Opus generates PR summary from all completed tasks
14. If autonomous improvement enabled: Opus analyzes codebase for improvements
15. If confidence >= threshold: propose improvement plan (with approval gate if enabled)

### Autonomous Improvement Loop

After all tasks in a plan are completed:

- Opus analyzes the project for improvements (refactoring, missing tests, better error handling, new features)
- Returns a confidence score (0.0-1.0) and reasoning
- If `confidence >= confidence_threshold` (default 0.7):
  - If `approval_gate = ON`: show proposal + confidence to user, wait for approval
  - If `approval_gate = OFF`: auto-dispatch new plan
- Loop continues until confidence drops below threshold or `max_improvement_cycles` (default 5) reached
- Hard safety cap of `max_improvement_cycles` always applies, even in autonomous mode

### Opus Rate Limit Handling

When `claude -p` hits the 5-hour subscription limit:

- Orchestrator detects rate limit error
- Records timestamp, calculates resume time (5h + 1min buffer)
- Status transitions to `RATE_LIMITED`
- Running agents continue working (they use LM Studio, not Opus)
- Opus-dependent actions (planning, review, improvement) queued in SQLite
- Scheduler auto-resumes at calculated time
- Processes queued actions in order

**Opus States:** `AVAILABLE` | `RATE_LIMITED` | `RESUMING`

---

## Task State Machine

```
                ┌──────────┐
                │ PENDING  │
                └────┬─────┘
                     │  agent spawned
                     ▼
              ┌──────────────┐
              │ IN_PROGRESS  │◄─────────────────┐
              └──────┬───────┘                   │
                     │  agent finished           │
                     ▼                           │
              ┌──────────────┐                   │
              │  REVIEWING   │                   │
              └──────┬───────┘                   │
                     │                           │
                ┌────┴────┐                      │
                ▼         ▼                      │
        ┌──────────┐ ┌──────────┐                │
        │  PASSED  │ │  FAILED  │── re-dispatch ─┘
        └────┬─────┘ └──────────┘   (max 3 retries)
             │
             ▼
        ┌──────────┐
        │  MERGED  │
        └──────────┘
```

---

## Data Model (SQLite)

### users
| Column | Type | Notes |
|--------|------|-------|
| id | TEXT PK | UUID |
| name | TEXT | |
| token_hash | TEXT | bcrypt hash of API token |
| created_at | TIMESTAMP | |

### projects
| Column | Type | Notes |
|--------|------|-------|
| id | TEXT PK | UUID |
| user_id | TEXT FK | references users.id |
| name | TEXT | display name |
| repo_url | TEXT | GitHub repo URL |
| default_branch | TEXT | usually "main" |
| approval_gate | BOOLEAN | default TRUE |
| confidence_threshold | REAL | default 0.7 |
| max_retries | INTEGER | default 3 |
| max_improvement_cycles | INTEGER | default 5 |
| lm_studio_url | TEXT | default http://host.docker.internal:1234 |
| model_name | TEXT | model identifier for Aider |
| created_at | TIMESTAMP | |

### plans
| Column | Type | Notes |
|--------|------|-------|
| id | TEXT PK | UUID |
| project_id | TEXT FK | references projects.id |
| spec | TEXT | user-submitted specification |
| opus_plan | TEXT | JSON — Opus task breakdown (see Opus Plan Format) |
| plan_branch_name | TEXT | e.g. plan/2026-06-01-user-auth |
| source | TEXT | "user" or "autonomous" |
| confidence | REAL | null for user plans |
| confidence_reason | TEXT | null for user plans |
| status | TEXT | pending / active / completed / rejected |
| created_at | TIMESTAMP | |

### tasks
| Column | Type | Notes |
|--------|------|-------|
| id | TEXT PK | UUID |
| plan_id | TEXT FK | references plans.id |
| title | TEXT | task title |
| description | TEXT | full task description for Aider |
| branch_name | TEXT | e.g. agent/user-auth-login |
| pr_url | TEXT | nullable, set after PR creation |
| status | TEXT | pending / in_progress / reviewing / passed / failed / merged |
| attempt | INTEGER | current attempt number (1-based) |
| review_feedback | TEXT | nullable, Opus feedback on failure |
| created_at | TIMESTAMP | |
| updated_at | TIMESTAMP | |

### agent_runs
| Column | Type | Notes |
|--------|------|-------|
| id | TEXT PK | UUID |
| task_id | TEXT FK | references tasks.id |
| container_id | TEXT | Docker container ID |
| status | TEXT | running / completed / failed / stopped |
| logs | TEXT | captured container logs |
| started_at | TIMESTAMP | |
| finished_at | TIMESTAMP | nullable |

### opus_state
| Column | Type | Notes |
|--------|------|-------|
| id | INTEGER PK | singleton (always 1) |
| status | TEXT | available / rate_limited / resuming |
| rate_limited_at | TIMESTAMP | nullable |
| resume_at | TIMESTAMP | nullable |
| queued_actions | TEXT | JSON array of pending Opus calls |

---

## API Endpoints

### Projects
- `POST /api/projects` — register a GitHub repo
- `GET /api/projects` — list all projects
- `GET /api/projects/{id}` — project details + settings
- `PATCH /api/projects/{id}` — update settings

### Plans
- `POST /api/projects/{id}/plans` — submit a spec (triggers Opus planning)
- `GET /api/projects/{id}/plans` — list plans for a project
- `GET /api/plans/{id}` — plan details + task breakdown
- `POST /api/plans/{id}/approve` — approve autonomous improvement plan
- `POST /api/plans/{id}/reject` — reject autonomous improvement plan

### Tasks & Agents
- `GET /api/plans/{id}/tasks` — list tasks in a plan
- `GET /api/tasks/{id}` — task details + agent run history
- `POST /api/tasks/{id}/stop` — stop a running agent
- `GET /api/tasks/{id}/logs` — SSE stream of agent logs

### System
- `GET /api/status` — orchestrator health + Opus state + active agents
- `GET /api/opus/state` — Opus availability, rate limit info, queue depth
- `GET /api/events` — SSE stream of all system events (global feed)

### Authentication
All endpoints require `Authorization: Bearer <token>` header.
v1 uses a single static token. Data model supports multi-user for future.

---

## Git Workflow

### Branch Strategy

Two-tier branching:

```
main
 ├── plan/{date}-{slug}              ← groups all tasks from one plan
 │    ├── agent/{task-slug}          ← individual task branch
 │    ├── agent/{task-slug}
 │    └── agent/{task-slug}
 │
 ├── plan/{date}-{slug}              ← autonomous improvement plan
      ├── improve/{task-slug}
      └── improve/{task-slug}
```

### Operations by Phase

| Phase | Git Operations |
|-------|---------------|
| Plan received | `checkout main && pull`, create `plan/{date}-{slug}`, push |
| Agent dispatched | Clone repo, checkout plan branch, create `agent/{slug}` |
| Agent done | Push agent branch, `gh pr create --base plan/{slug}` |
| Review pass | `gh pr merge --squash --delete-branch` |
| Review fail | `gh pr comment` with feedback, re-dispatch on same branch/PR |
| All tasks done | `gh pr create --base main --head plan/{slug}` (integration PR) |
| Conflict detected | Opus instructs agent to `git pull --rebase` and resolve |

### Conflict Prevention

Before dispatching agents in parallel, orchestrator checks if tasks touch overlapping
files using `git diff --name-only`. Overlapping tasks run sequentially instead of parallel.

---

## Docker Architecture

### Compose Services (always running)

**orchestrator** — Python 3.11 + FastAPI + Uvicorn
- Port: 8080
- Volumes: `./data` (SQLite), Docker socket, SSH keys (read-only)
- Env: `AUTH_TOKEN`, `GITHUB_TOKEN`, `LM_STUDIO_URL`

### Dynamically Spawned Containers

**aider-agent:{task_id}** — custom image based on `python:3.11-slim`
- Installs: `aider-chat`, `git`, `gh`, `curl`
- Entrypoint: clone → branch → aider → push → PR → callback
- Environment: `REPO_URL`, `BRANCH`, `TASK_PROMPT`, `OPENAI_API_BASE`, `AIDER_MODEL`, `CALLBACK_URL`
- Auto-removed after completion
- Runs as non-root user
- SSH keys and tokens mounted read-only

### Deployment Modes

**Local mode:**
- Orchestrator + LM Studio on same machine
- LM Studio at `host.docker.internal:1234`
- Dashboard at `localhost:8080`

**Hosted mode:**
- Orchestrator on VPS
- Caddy reverse proxy for auto-HTTPS + custom domain
- LM Studio on server (GPU) or tunneled from local machine
- Claude Code can run on VPS or locally (hits API remotely)

---

## Error Handling

| Error | Detection | Recovery |
|-------|-----------|----------|
| Agent container crash | Docker SDK exit code != 0 | Capture logs, mark failed, auto-retry under max_retries |
| Opus rate limit | `claude -p` error output parsing | Record timestamp, queue calls, auto-resume at 5h+1min |
| LM Studio unreachable | Health check before dispatch | Hold tasks as pending, retry connectivity every 30s, SSE notification |
| Orchestrator restart | N/A | SQLite state persists, scan for orphaned containers, re-attach logs, re-schedule queued calls |
| Git push rejected | Non-zero exit from git push | Opus reviews error, decides rebase+retry or flag for user |
| GitHub API failure | HTTP error from `gh` CLI | Exponential backoff, 3 attempts, then mark failed + SSE notification |

---

## Project Structure

```
praxis/
├── src/
│   └── orchestrator/
│       ├── __init__.py
│       ├── main.py                  # FastAPI app entrypoint
│       ├── config.py                # Settings (env vars + defaults)
│       ├── database.py              # SQLite connection + migrations
│       ├── api/
│       │   ├── projects.py          # /api/projects endpoints
│       │   ├── plans.py             # /api/plans endpoints
│       │   ├── tasks.py             # /api/tasks endpoints
│       │   ├── system.py            # /api/status, /api/opus/state
│       │   ├── events.py            # /api/events SSE stream
│       │   └── auth.py              # Token validation middleware
│       ├── core/
│       │   ├── agent_manager.py     # Docker SDK — spawn/monitor/stop
│       │   ├── opus_bridge.py       # claude -p invocation + rate limit
│       │   ├── task_queue.py        # Task state machine + scheduling
│       │   ├── git_ops.py           # Branch, PR, merge, conflict ops
│       │   └── improvement.py       # Autonomous improvement loop
│       └── models/
│           └── schemas.py           # Pydantic request/response models
├── docker/
│   ├── orchestrator/Dockerfile
│   ├── aider-agent/
│   │   ├── Dockerfile
│   │   └── entrypoint.sh
│   └── caddy/Caddyfile
├── web/
│   └── index.html                   # Single-file dashboard
├── cli/
│   └── main.py                      # Typer CLI client
├── tests/
│   ├── conftest.py
│   ├── test_agent_manager.py
│   ├── test_opus_bridge.py
│   ├── test_task_queue.py
│   ├── test_git_ops.py
│   ├── test_api_projects.py
│   ├── test_api_plans.py
│   └── test_api_tasks.py
├── docker-compose.yml
├── docker-compose.local.yml
├── pyproject.toml
├── .env.example
├── .python-version
└── README.md
```

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Backend | Python 3.11, FastAPI, Uvicorn |
| Database | SQLite (aiosqlite for async) |
| CLI | Typer |
| Web UI | Single-file HTML/CSS/JS |
| Containerization | Docker, Docker SDK for Python |
| Agent | Aider (via custom Docker image) |
| LLM (planning/review) | Claude Opus via `claude -p` (subscription) |
| LLM (implementation) | Code-focused model via LM Studio (OpenAI-compatible API) |
| Git/GitHub | git CLI, gh CLI |
| Reverse Proxy | Caddy (auto-HTTPS) |
| Linting | ruff fmt + ruff check |
| Type Checking | Pyright (IDE) + mypy (CI) |
| Testing | pytest, 80%+ coverage |

## Per-Project Settings

| Setting | Type | Default | Description |
|---------|------|---------|-------------|
| approval_gate | bool | true | Require user approval for autonomous improvement plans |
| confidence_threshold | float | 0.7 | Minimum confidence for Opus to propose improvements |
| max_retries | int | 3 | Max re-dispatches per failed review |
| max_improvement_cycles | int | 5 | Hard cap on autonomous improvement loops |
| lm_studio_url | string | http://host.docker.internal:1234 | LM Studio endpoint |
| model_name | string | (required) | Model identifier for Aider |

## MVP Scope

The minimum viable product covers:

1. Register a GitHub repo as a project
2. Submit a spec → Opus breaks into tasks
3. Dispatch Aider agents on separate branches
4. Stream agent logs via SSE
5. Opus reviews completed work
6. Re-dispatch on failure with feedback
7. Squash merge on pass, integration PR to main
8. Web dashboard + CLI for dispatch and monitoring
9. Rate limit detection and auto-resume
10. Single-user token auth
11. Autonomous improvement loop (confidence scoring, approval gate toggle)

**Post-MVP:** Multi-user auth, multiple LM Studio instances, agent type plugins.

---

## Opus Plan Format

When Opus receives a spec via `claude -p`, it must return a structured JSON that the
orchestrator can parse. The prompt instructs Opus to respond with this format:

```json
{
  "plan_summary": "Implement user authentication system",
  "plan_slug": "user-auth",
  "tasks": [
    {
      "title": "Login page",
      "slug": "user-auth-login-page",
      "description": "Create a login page with email/password fields...",
      "depends_on": []
    },
    {
      "title": "Signup flow",
      "slug": "user-auth-signup-flow",
      "description": "Create a signup page with validation...",
      "depends_on": []
    },
    {
      "title": "Password reset",
      "slug": "user-auth-password-reset",
      "description": "Implement forgot password flow...",
      "depends_on": ["user-auth-login-page"]
    }
  ]
}
```

**Fields:**
- `plan_summary` — one-line description of the plan
- `plan_slug` — URL-safe slug for branch naming (plan/{date}-{slug})
- `tasks[].title` — human-readable task name
- `tasks[].slug` — URL-safe slug for branch naming (agent/{slug})
- `tasks[].description` — detailed implementation instructions for Aider
- `tasks[].depends_on` — list of task slugs that must complete first (for sequential ordering)

**Opus review response format:**

```json
{
  "verdict": "pass",
  "feedback": "Code looks good. Well-structured components with proper error handling.",
  "issues": []
}
```

or

```json
{
  "verdict": "fail",
  "feedback": "Login form is missing CSRF protection and input validation.",
  "issues": [
    "No CSRF token in the form submission",
    "Email field accepts any string without validation",
    "Missing rate limiting on login attempts"
  ]
}
```

**Opus improvement analysis response format:**

```json
{
  "confidence": 0.82,
  "reason": "The authentication system lacks session management and remember-me functionality, which are standard features users expect.",
  "proposed_tasks": [
    {
      "title": "Add session management",
      "slug": "improve-session-management",
      "description": "Implement session tokens with configurable expiry..."
    }
  ]
}
```
