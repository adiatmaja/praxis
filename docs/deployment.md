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

> Other harnesses (`docker/opencode-agent/`, `docker/openhands-agent/`) build the same
> way and honor the same entrypoint contract. AgentManager selects the image by the
> project's `harness` column. All are standalone (not in docker-compose); build directly.

**Agent container environment variables** (harness-agnostic contract, set by AgentManager):

| Variable | Description |
|----------|-------------|
| `HARNESS` | Selected harness (`aider` / `opencode` / `openhands`) |
| `REPO_URL` | GitHub repo clone URL |
| `BRANCH` | Agent branch name (`agent/{task-slug}`) |
| `BASE_BRANCH` | Plan branch to branch from and PR into |
| `TASK_PROMPT` | Full implementation instructions |
| `OPENAI_API_BASE` | LM Studio endpoint |
| `MODEL` | Raw model name; each entrypoint adds its own provider prefix |
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
| `POST` | `/api/plans/promote` | Derive tasks from a `plan.md` and create + activate a run |
| `POST` | `/api/plans/{id}/approve` | Approve an autonomous plan |
| `POST` | `/api/plans/{id}/reject` | Reject an autonomous plan |

### Lifecycle & Docs

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/projects/{id}/lifecycle` | One row per spec, joined to plan doc + DB run (Spec→Plan→Run) |
| `GET` | `/api/projects/{id}/doc-raw?path=` | Raw markdown of a target-repo spec/plan doc |
| `GET` | `/api/projects/{id}/context` | CLAUDE.md/MEMORY.md snapshot (Memory view) |
| `POST` | `/api/specs` | Create-Spec chat + generate plan |
| `GET` | `/api/docs`, `/api/docs/raw` | Orchestrator-local doc index / raw read |

### Settings

| Method | Path | Description |
|--------|------|-------------|
| `GET`/`PUT` | `/api/settings` | Global/project settings overrides |
| `GET`/`PUT` | `/api/settings/models` | Per-call-site model config (provider/model/effort) |
| `POST` | `/api/settings/models/reset` | Reset one call-site or all to defaults |
| `GET` | `/api/harnesses` | Harness catalog (image + About) |

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
| `GET` | `/api/status` | Orchestrator status (Opus state + agent counts; Planner `available` is gated on a real `claude --version` probe; returns `agent_model.cli_available`, effective `lm_studio_url`, and a `providers` list — per brain provider `{cli_available, authenticated, login_hint}`) |
| `GET` | `/api/lm-models` | List models loaded in LM Studio (`/v1/models` proxy) for the New-Project model dropdown |
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
| LM Studio | Implementer agents + local brain calls (`derive_tasks`) | Running on `localhost:1234` (or configured URL) |
| Claude Code CLI | Default brain call-sites (planning/review/classify) | `claude -p` must be available in PATH |
| GitHub CLI (`gh`) | PR operations | Authenticated via `GITHUB_TOKEN` |
| `codex` (optional) | Alt brain provider (GPT) | Only if routed in Settings → Models. Requires `codex login`; a dead session raises `ProviderAuthError` and shows a dashboard login banner. Verified working 2026-06-23. |
| `agy` (not usable) | Alt brain provider (Gemini) | Detected/launchable, but `--print` only renders to an interactive TTY → no capturable output non-interactively. Not usable as a brain until that's resolved. |

Global orchestrator settings load from `config/praxis.yaml` (overridable via `PRAXIS_*`
env vars); secrets (`AUTH_TOKEN`, `GITHUB_TOKEN`) stay in env / `.env`.

> **Callback URL ↔ `PORT`.** Agent containers POST completion to
> `http://host.docker.internal:{PORT}/api/internal/agent-done`, derived from `PORT` by
> `Settings.callback_url()`. If you run the orchestrator on a non-default port, set `PORT`
> accordingly (or set `AGENT_CALLBACK_URL` explicitly) — otherwise every agent callback
> 404s and tasks only finish via the reconcile backstop (and may be marked failed even
> when the agent succeeded).

---

## Verification Gate and Build Visibility

### Per-project `verify_cmd`

Each project can set a `verify_cmd` string (via the API or dashboard project settings).
When set, `review_task` runs this command against the cloned PR head **before** the brain
review step.

```
clone PR head -> run verify_cmd -> (non-zero exit = task FAILED) -> brain review
```

Example values:

```
npx tsc --noEmit && npm test
pytest tests/ -x -q
go build ./... && go test ./...
```

Key properties:

- **Deterministic, cheap gate.** A failing command hard-fails the task immediately with
  the command output as feedback. No brain tokens are spent on a broken build.
- **Orchestrator-side, harness-agnostic.** The command runs in the orchestrator process
  against the cloned checkout (`core/verify_gate.py`), so it applies equally to Aider,
  OpenCode, and OpenHands agents without any entrypoint changes.
- **Trusted operator config, never from a PR.** `verify_cmd` is stored in the projects
  table and set only by the operator via the API or dashboard. It is never read from PR
  content or branch files. Prefer running the orchestrator inside a container to further
  limit the blast radius of a misconfigured command.

### Build stamp on `/health` and `/api/status`

Both endpoints include a `build` object:

```json
{
  "build": {
    "commit": "a1b2c3d",
    "started_at": "2026-07-02T10:00:00Z"
  }
}
```

`commit` is derived from `git rev-parse --short HEAD` at startup, or from the
`PRAXIS_BUILD_SHA` environment variable if set (useful in CI/CD where the working tree
may not have git history). `started_at` is the server start time.

Use this to confirm the live server is running the code you just deployed. If `commit`
does not match your expected SHA after a deploy, the old process is still running:
**restart the orchestrator after every deploy.**

---

## Security / Trust Model

Praxis is designed to run as a **trusted single-operator tool**, not a multi-tenant
service.  Several architectural choices reflect this; understand them before exposing
Praxis to a broader network.

### Internal callback authentication

Agent containers POST `POST /api/internal/agent-done` when they finish.  This endpoint
is protected by a shared secret sent in the `X-Praxis-Callback-Token` header.

By default the secret is derived from `AUTH_TOKEN` so existing deployments work without
configuration.  To use a dedicated secret set `INTERNAL_CALLBACK_SECRET` in `.env`:

```
INTERNAL_CALLBACK_SECRET=<random-string>
```

The orchestrator passes this secret to agent containers as `CALLBACK_TOKEN` via the
Docker environment.  The comparison uses `secrets.compare_digest` to prevent timing
attacks.

> **Rebuild the agent image after editing `entrypoint.sh`.** The token header is sent by
> the entrypoint's `send_callback`. A stale `aider-agent:latest` (built before this logic)
> sends an empty header, so every callback 401s and tasks stall at `in_progress` until the
> reconciler fails them — implement→review→merge never completes. Rebuild with
> `docker build -t aider-agent:latest -f docker/aider-agent/Dockerfile docker/aider-agent/`.

### Docker socket exposure

`docker-compose.yml` mounts `/var/run/docker.sock` into the orchestrator container.
This is functionally equivalent to giving the container **root access to the host**,
because it can spawn arbitrary containers with any volume or network configuration.
Acceptable for a single-operator workstation; not for a multi-tenant or
internet-facing deployment without additional isolation (e.g. Docker-in-Docker
with resource limits, or socket proxies like `tecnativa/docker-socket-proxy`).

### Mounted host credentials

Two host credential directories are mounted into the orchestrator container:

| Mount | Purpose | Privilege level |
|-------|---------|----------------|
| `~/.ssh` (`:ro`) | git clone over SSH | All host SSH private keys |
| `~/.claude` (`:rw`) | `claude -p` subscription auth | Claude Code credentials |

`~/.ssh` is read-only; `~/.claude` is read-write because `claude config set` writes
during first-run setup.  If claude is already configured you may add `:ro` to the
`~/.claude` mount.

To reduce exposure, override `SSH_KEY_PATH` to point at a single deploy key instead
of the full `~/.ssh` directory:

```
SSH_KEY_PATH=~/.ssh/id_praxis_deploy
```

### Agent container networking

Agent containers run on Docker's default **bridge** network with
`extra_hosts={"host.docker.internal": "host-gateway"}`. They reach LM Studio and the
orchestrator callback via `host.docker.internal`; a `localhost`/`127.0.0.1` LM Studio URL
is rewritten to `host.docker.internal` for the container env (`_container_host_url`), while
the host-side context-limit probe keeps the original URL. On native Linux the
`host-gateway` mapping is what makes `host.docker.internal` resolve (Docker Desktop maps it
automatically). This removes the blanket host-network access the old `network_mode: host`
gave each container, but the worker can still reach host-gateway services, so it reduces
rather than fully isolates network exposure.

### Auto-merge and approval gate

When `approval_gate` is **enabled** (the default), every Opus-reviewed PR requires a
human approval before merging.  Disabling the approval gate (`approval_gate: false` per
project) allows the orchestrator to merge LLM-written code autonomously.

**This is a deliberate supply-chain trust decision.**  Only disable it for repositories
where you are comfortable with fully automated merges of AI-generated code.

### Merge approval & security

Praxis **parks a reviewed PR for explicit human approval by default** instead of
auto-merging.  On an Opus review PASS the task lands in the `PASSED` status (the PR is
left open) and a `task_awaiting_merge` event is emitted; nothing is merged until a human
approves.

Approve or reject a parked merge via:

| Endpoint | Effect |
|----------|--------|
| `POST /api/tasks/{id}/approve-merge` | Squash-merge one review-passed task's PR. |
| `POST /api/tasks/{id}/reject-merge` | Comment on the PR, fail the task, and re-dispatch if retry attempts remain (optional `{"feedback": "..."}` body). |
| `POST /api/plans/{id}/approve-merges` | Batch-approve every `PASSED` task in a plan; returns `{approved, errors}`. |

MCP `poll_task` surfaces a parked task as `status: awaiting_merge` (with `pr_url`,
`review`, `branch`, `verdict`) so a main brain can relay the PR for approval.

**Opt-in auto-merge.**  A per-project `auto_merge` flag (default off) restores the old
merge-on-PASS behavior.  Even with `auto_merge=True`, Praxis **never** auto-merges into a
protected branch (the project default branch, or any `main` / `master` / `release*`
branch): the rule lives in `core/merge_policy.py` and an unknown base branch is treated as
protected (fail safe).

**Defense in depth.**  Scope `GITHUB_TOKEN` least-privilege (`contents:write` +
`pull_requests:write` only; no admin or branch-protection-bypass) and enable GitHub
branch protection on your default branch, so the merge gate is enforced by GitHub even if
orchestrator logic is bypassed.

### Prompt injection via PR diffs

When reviewing a PR, the orchestrator feeds the full diff to the reviewing LLM.
A malicious contributor could embed adversarial instructions in a code comment or
commit message, attempting to influence the LLM verdict (prompt-injection).

Mitigation: keep `approval_gate` enabled so a human sees the diff before any merge
occurs.  Do not point Praxis at repositories with untrusted external contributors
unless you review diffs independently of the LLM verdict.
