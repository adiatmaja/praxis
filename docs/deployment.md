# Deployment

## Docker Images

### Orchestrator (`docker/orchestrator/Dockerfile`)

Python 3.11-slim with git, gh CLI, and uv. Installs the project as an editable
package, serves FastAPI on port 8080.

```bash
docker build -t orchestrator:latest -f docker/orchestrator/Dockerfile .
```

### Coding Agent (`docker/opencode-agent/`, `docker/agy-agent/`)

Each harness has its own single-use worker image that clones, implements, pushes, and
creates a PR, running as non-root `agent` user. **OpenCode is the default harness** (its
agentic loop reads files in bounded chunks and auto-compacts, so it survives large tasks);
agy/Antigravity is the experimental Gemini-backed alternative. Build the one(s) you use:

```bash
# Default harness (OpenCode)
docker build -t opencode-agent:latest -f docker/opencode-agent/Dockerfile docker/opencode-agent/

# Experimental Gemini harness (agy)
docker build -t agy-agent:latest -f docker/agy-agent/Dockerfile docker/agy-agent/
```

> All harness images honor the same entrypoint contract. AgentManager selects the image by
> the project's `harness` column. All are standalone (not in docker-compose); build directly.

#### agy (Antigravity / Gemini) harness — one-time credential setup

`agy` is an **experimental** first-party Google harness that implements tasks with Gemini.
Unlike the other harnesses it does not talk to LM Studio; it authenticates to Google with
OAuth. It has **no API-key auth** (upstream ignores `GEMINI_API_KEY`), so you must seed its
credentials once with an interactive login. These commands are **identical on Windows, macOS,
and Linux** — the credentials live in a Docker volume, not a host path, so nothing is
OS-specific.

```bash
# 1. Create the credentials volume and give the non-root agent user ownership.
#    (A fresh Docker volume mounts root-owned; agy runs as uid 1000 and needs to write.)
docker run --rm --user root \
  -v praxis-gemini-creds:/home/agent/.gemini \
  --entrypoint bash agy-agent:latest \
  -c 'chown -R agent:agent /home/agent/.gemini'

# 2. Log in once (interactive). agy prints an OAuth URL; open it in a browser,
#    approve, and the Linux-native credentials are written into the volume.
docker run --rm -it \
  -v praxis-gemini-creds:/home/agent/.gemini \
  --entrypoint bash agy-agent:latest \
  -c 'agy login'

# 3. (Optional) verify a fresh process can authenticate with the persisted creds:
docker run --rm \
  -v praxis-gemini-creds:/home/agent/.gemini \
  --entrypoint bash agy-agent:latest \
  -c 'agy --dangerously-skip-permissions --mode accept-edits \
        --model "Gemini 3.5 Flash (High)" -p "print PONG"'
```

The orchestrator mounts this volume **read-write** at `/home/agent/.gemini` in every agy
container (read-write so it can persist the ~1-hourly refreshed access token). The volume
name is configurable with `GEMINI_CREDS_VOLUME` (default `praxis-gemini-creds`). Tokens are
long-lived once seeded; re-run step 2 only if login is revoked. No host `~/.gemini` path is
ever mounted — that approach does not work across operating systems.

**Agent container environment variables** (harness-agnostic contract, set by AgentManager):

| Variable | Description |
|----------|-------------|
| `HARNESS` | Selected harness (`opencode` / `agy`) |
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
    -> run harness -> push -> create PR -> callback orchestrator
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
docker compose -f docker-compose.yml -f docker-compose.local.yml up --build -d
docker logs -f orchestrator   # tail logs while working
```

- Source code mounted (`src/`, `web/`) for live reload
- `--reload` flag on uvicorn
- Default dev tokens if env vars not set
- `restart: unless-stopped` inherited from the base file (see below)

### Default dev/run story: containerized, not bare uvicorn

**Run the orchestrator in its own container** (`restart: unless-stopped`) as the default
development and production story.  Do not rely on `uv run uvicorn ...` as the primary
run path.

**Why:** a bare uvicorn process is tied to the operator's terminal session.  When the
session ends (logout, SSH drop, Ctrl-C) the process dies.  At that point:

- Agent containers that were mid-run keep running and eventually POST their completion
  callback to an address with no listener.
- The reconciler that would mark timed-out or exited containers as failed is gone.
- Tasks wedge in `in_progress` until the orchestrator comes back and reconciles.

Agent containers and callback retries already tolerate a brief orchestrator restart
(containers keep running, callbacks retry with backoff, reconcile catches any that slip
through).  The control plane itself is the weak link when it is a bare host process.

The compose orchestrator service has `restart: unless-stopped` in `docker-compose.yml`.
`docker-compose.local.yml` (the dev override) does not repeat the key; it inherits the
base file's restart policy when both files are merged with `-f`.  The comment in
`docker-compose.local.yml` documents this explicitly.

**One-liner to start the dev orchestrator and leave it running:**

```bash
docker compose -f docker-compose.yml -f docker-compose.local.yml up --build -d
```

**Bare uvicorn** (`uv run uvicorn ...`) remains useful for rapid unit-level iteration
where no agents will actually be dispatched (e.g. running a single API test against a
live DB), but it should not be used for any session where real agent tasks may be
in-flight.

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
| An OpenAI-compatible model endpoint | Implementer agents + local brain calls (`derive_tasks`) | LM Studio (default) on `localhost:1234`, or any OpenAI-compatible endpoint (Ollama, hosted) via `LM_STUDIO_URL` |
| Claude Code CLI | Default brain call-sites (planning/review/classify) | `claude -p` must be available in PATH |
| GitHub CLI (`gh`) | PR operations | Authenticated via GitHub App installation token (recommended) or `GITHUB_TOKEN` PAT fallback |
| `codex` (optional) | Alt brain provider (GPT) | Only if routed in Settings → Models. Requires `codex login`; a dead session raises `ProviderAuthError` and shows a dashboard login banner. Verified working 2026-06-23. |
| `agy` (not usable) | Alt brain provider (Gemini) | Detected/launchable, but `--print` only renders to an interactive TTY → no capturable output non-interactively. Not usable as a brain until that's resolved. |

Global orchestrator settings load from `config/praxis.yaml` (overridable via `PRAXIS_*`
env vars); secrets (`AUTH_TOKEN`, GitHub App private key or `GITHUB_TOKEN`) stay in env / `.env`.

> **Callback URL ↔ `PORT`.** Agent containers POST completion to
> `http://host.docker.internal:{PORT}/api/internal/agent-done`, derived from `PORT` by
> `Settings.callback_url()`. If you run the orchestrator on a non-default port, set `PORT`
> accordingly (or set `AGENT_CALLBACK_URL` explicitly) — otherwise every agent callback
> 404s and tasks only finish via the reconcile backstop (and may be marked failed even
> when the agent succeeded).

## Configuration

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `AUTH_TOKEN` | Yes | n/a | Bearer token for API auth |

### Auth Token Rotation
If you are using raw tokens (default), updating `AUTH_TOKEN` in your `.env` file and restarting the orchestrator is sufficient to rotate the token for API access.

However, if you have enabled hashed tokens at rest by setting `AUTH_TOKEN_HASHED=true`:
1. Generate a new SHA-256 hash for your new secret token.
2. Update `AUTH_TOKEN` in your `.env` file with the new hash.
3. Because the hashed token is also stored in the SQLite `users` table for legacy integrations, you must manually update the database to rotate it completely:
   ```bash
   sqlite3 data/orchestrator.db "UPDATE users SET token_hash = 'YOUR_NEW_HASH' WHERE id = 'default';"
   ```
4. Restart the orchestrator to apply changes.
| `GITHUB_APP_ID` | Recommended | n/a | GitHub App id. With `GITHUB_APP_PRIVATE_KEY`, Praxis mints short-lived, repo-scoped installation tokens (preferred over `GITHUB_TOKEN`) |
| `GITHUB_APP_PRIVATE_KEY` | Recommended | n/a | GitHub App private key: PEM contents or a path to the PEM file |
| `GITHUB_APP_INSTALLATION_ID` | No | n/a | GitHub App installation id; auto-resolved per repo when unset |
| `GITHUB_TOKEN` | Fallback | n/a | GitHub PAT (`repo` scope). Used only when no GitHub App is configured |
| `DATABASE_URL` | No | `sqlite+aiosqlite:///data/orchestrator.db` | SQLite path |
| `LM_STUDIO_URL` | No | `http://host.docker.internal:1234` | LM Studio endpoint (implementer / local brain calls) |
| `AGENT_MODEL` | No | `claude-opus-4-8` | Default planner model (per-call-site overrides in **Settings → Models**) |
| `HOST` | No | `0.0.0.0` | Bind address |
| `PORT` | No | `12323` | Host port (uncommon by design to avoid 8080 collisions; MCP `PRAXIS_BASE_URL` and agent callbacks must match it) |
| `GEMINI_CREDS_VOLUME` | No | `praxis-gemini-creds` | Docker volume holding agy OAuth creds; only used by the `agy` harness (see [agy setup](#agy-antigravity--gemini-harness--one-time-credential-setup)) |

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
  against the cloned checkout (`core/verify_gate.py`), so it applies equally to OpenCode
  and agy agents without any entrypoint changes.
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

## GitHub authentication

Praxis needs GitHub credentials to clone repos, push agent branches, and manage
PRs. Configure one of the two options below. When both are present, the GitHub
App takes precedence over `GITHUB_TOKEN`.

### Option A: GitHub App (recommended)

Praxis mints short-lived (<=1 hour), repo-scoped installation tokens per
operation. The App private key stays on the orchestrator and is never placed in
a worker container, so a leaked worker token is narrow and expires within the hour.

1. Create a GitHub App (Settings > Developer settings > GitHub Apps). Grant
   repository permissions: Contents (Read and write) and Pull requests (Read and
   write).
2. Generate a private key (PEM) and store it as an orchestrator secret.
3. Install the App on the repositories Praxis will operate on.
4. Configure the orchestrator:
   - `GITHUB_APP_ID` = the App's numeric id
   - `GITHUB_APP_PRIVATE_KEY` = the PEM contents or a path to the PEM file
   - `GITHUB_APP_INSTALLATION_ID` = optional; auto-resolved per repo when unset

### Option B: Personal Access Token (fallback)

Set `GITHUB_TOKEN` to a PAT with `repo` scope. Simple, but the token is
long-lived and broadly scoped, and it is injected into every agent container.
Used only when no GitHub App is configured.

**Known limitation:** installation tokens expire after 1 hour. An agent run that
exceeds an hour may fail its final push on an expired token. A future refresh
endpoint will let a worker renew its token mid-run.

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
> the entrypoint's `send_callback`. A stale image (built before this logic)
> sends an empty header, so every callback 401s and tasks stall at `in_progress` until the
> reconciler fails them — implement→review→merge never completes. Rebuild with
> `docker build -t opencode-agent:latest -f docker/opencode-agent/Dockerfile docker/opencode-agent/`
> (or the equivalent for `agy-agent`).

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

**Defense in depth.**  Prefer a GitHub App, whose short-lived, repo-scoped installation
tokens (`contents:write` + `pull_requests:write`) limit the blast radius of a leaked
worker token. If you use the `GITHUB_TOKEN` PAT fallback, scope it least-privilege
(`contents:write` + `pull_requests:write` only; no admin or branch-protection-bypass).
Either way, enable GitHub branch protection on your default branch, so the merge gate is
enforced by GitHub even if orchestrator logic is bypassed.

### Prompt injection via PR diffs

When reviewing a PR, the orchestrator feeds the full diff to the reviewing LLM.
A malicious contributor could embed adversarial instructions in a code comment or
commit message, attempting to influence the LLM verdict (prompt-injection).

Mitigation: keep `approval_gate` enabled so a human sees the diff before any merge
occurs.  Do not point Praxis at repositories with untrusted external contributors
unless you review diffs independently of the LLM verdict.

---

## Troubleshooting

| Symptom | Likely cause and fix |
|---------|---------------------|
| Task stuck at "implementing", then marked failed by reconcile | Stale agent image sending a bad callback. Rebuild the harness image (see [Docker Images](#docker-images)). |
| Every task fails with no commits, agent log shows the model chatting code | Worker model too small for the edit format. Use a mid-size coding model (see [open-weight-models-complete.md](open-weight-models-complete.md)). |
| Agent callbacks 404, tasks only finish via reconcile | Orchestrator running on a non-default port without `AGENT_CALLBACK_URL` set. Keep `PORT`, `PRAXIS_BASE_URL`, and callbacks in sync. |
| MCP tools error or hang | Praxis server not running, or `PRAXIS_BASE_URL` points at the wrong port. Ask your assistant to run `list_providers` to test connectivity. |
| Planner shows unavailable | `claude` CLI not installed or not logged in on the orchestrator host (`claude --version`, then log in). |
| Dispatch fails with a Docker image error | The harness image for the project isn't built. Build it per [Docker Images](#docker-images). |
| No models in the New Project dropdown | LM Studio isn't running or isn't reachable at `LM_STUDIO_URL`. |
