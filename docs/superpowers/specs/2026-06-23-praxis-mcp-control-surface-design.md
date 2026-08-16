---
title: Praxis MCP-First Control Surface
date: 2026-06-23
status: design
---

# Praxis MCP-First Control Surface — Design

## Summary

Expose Praxis as an **MCP server** so that an MCP client (e.g. Claude Code) can act as
the *brain* and dispatch implementation work to a **non-Anthropic** model running inside
Praxis. Claude Code's native subagent/Task tool is **model-locked to Claude**, so it
cannot dispatch work to other providers. An MCP tool is just a function the client calls
and gets a result from — it doesn't care what runs inside. A Praxis `dispatch_task` tool
therefore lets a Claude Code session drive implementation on a different model (today: a
local model served by **LM Studio** on the PC server, via the existing Aider harness).

This is **request/response delegation**, NOT a true in-loop subagent: the worker runs
inside Praxis, not Claude Code's agent runtime — no shared context, no live streaming back
into the CC loop, coarse-grained (dispatch task → poll → review PR).

## Headline / Positioning

- **One-liner:** *"Praxis is an agent-orchestration engine with an MCP-first control
  surface; the dashboard is the human window into it."*
- **The pitch (truthful with v1):** Claude Code is model-locked to Claude for its own
  subagents. Praxis lets that Claude brain dispatch implementation to a non-Anthropic
  model (whatever is loaded in LM Studio) — something CC cannot do natively.
- **What "MCP-first" changes:** nothing in the engine, everything in the story. The
  dashboard (`web/index.html`) already holds **no logic** — it is a pure REST client. MCP
  joins it as a co-equal client of the same `core/` engine. This moves the product out of
  the crowded "another AI dashboard" category into "infrastructure that gives any MCP
  brain a non-Claude workforce." The dashboard is demoted to the **human observability
  window** — specifically the deep-link target for async-failure escalation.

## Architecture

A standalone **`mcp_server.py`** (Python MCP / FastMCP SDK), launched by the MCP client as
a **stdio subprocess**. Every tool is a thin forwarder to the **existing FastAPI REST
API** over HTTP, attaching a Bearer token. **Zero engine changes** — MCP becomes a third
client of the `core/` engine alongside the dashboard and the Typer CLI.

```
  ┌─────────────┐   stdio    ┌──────────────┐   HTTP+Bearer   ┌──────────────────┐
  │ Claude Code │◄──────────►│ mcp_server.py│◄───────────────►│ Praxis REST API  │
  │  (the brain)│   MCP      │ (forwarder)  │                 │ (core/ engine)   │
  └─────────────┘            └──────────────┘                 └────────┬─────────┘
                                                                       │
                                                  ┌────────────────────┼────────────────────┐
                                                  ▼                    ▼                    ▼
                                            TaskQueue          AgentManager           git_ops
                                                              (Aider + LM Studio
                                                               model on PC server)
```

### Why stdio adapter → REST (not fastapi-mcp mount)

- **Zero engine changes**, ships and versions independently of the web server.
- Easy local install in the CC MCP config (subprocess + env vars).
- Decouples the MCP tool shape from REST route shape — MCP tools are intent-level
  (`dispatch_task`), not auto-generated-per-route (often too granular / leaky).
- Trade-off: Praxis server must be running; MCP is not remotely reachable on its own.
  A `fastapi-mcp` HTTP/SSE mount is a deliberate **later** option if a remote MCP client
  need arises — not v1.

## Planning Ownership

When driven via MCP, the CC session is already a capable planning brain. To avoid paying
for Opus planning twice and to avoid a double-brain, ownership is **selectable per call**,
with a deliberate default:

- **`dispatch_task` (flagship, low-level):** the CC brain has already planned. Praxis
  fires **no `claude -p` planning** — it enqueues a single-task `opus_plan` and runs the
  existing dispatch → (optional) review → merge path. This is the headline demo.
- **`submit_spec` (power-user, deferred):** the CC brain hands Praxis a spec; Praxis runs
  its **own** `plan_spec` and the full autonomous loop. This is the "Praxis is the brain"
  entry point — the *opposite* of the headline, so it is deferred to a later phase.

Both entry points feed the **same** `TaskQueue.activate_plan`; the only difference is
whether the `opus_plan` is a hand-made single task or a `plan_spec`-derived graph. Keeping
`submit_spec` in the design from day one ensures the shared entry path is designed in, not
retrofitted.

**Review ownership:** `dispatch_task` takes `review` (default `true`) — Praxis's
`review_diff` second opinion runs unless the brain sets it `false` to review the PR
itself. This answers "don't double up the brain" with a parameter, not a separate tool.

## Async Interaction Model

A real task is minutes long (spawn container → implement → push → PR → optional review).
MCP is request/response. The model is **handle + poll** — `dispatch_task` never blocks.

**Happy path:**
```
CC brain ──dispatch_task(repo, "add X", model=qwen3)──► mcp_server ──POST /tasks──► TaskQueue
   ◄── {task_id, dashboard_url, status:queued} ──────────────────────────────────────┘
        (returns immediately — never blocks)

   [engine, async]  spawn_agent(aider + LM Studio) → implement → push → PR → review?(optional)

CC brain ──poll_task(task_id)──► GET /api/tasks/{id}
   ◄── {status: in_progress | reviewing | passed, pr_url, review} ──┘   (CC loops at its own pace)
```

**Failure path (the async-blindness answer):** when a run wedges (lost callback, dead
container, rate limit), the engine's `reconcile_runs` flips it to `failed`/retry.
`poll_task` surfaces that state plus a `dashboard_url`; `get_task_logs` gives inline
detail. The deep-link is the human-escalation hatch — MCP stays blind on *streaming*, but
never *silently* blind. This failure class is exactly what the dashboard's SSE live log +
`reconcile_runs` + monitor-at-dispatch already exist to surface.

## Tool Surface (v1)

Five tools, built in priority order. All forward to existing REST endpoints; the MCP layer
adds nothing to how workers run.

| Tool | Forwards to | Returns | Notes |
|------|-------------|---------|-------|
| `dispatch_task` | `POST /api/.../tasks` (single-task `opus_plan`) | `{task_id, dashboard_url, status: "queued"}` | **Flagship.** Args: `repo_url`, `instructions`, `model` (LM Studio model), `harness?` (default `aider`), `branch?`, `review?` (default `true`). No `claude -p` planning. |
| `poll_task` | `GET /api/tasks/{id}` | `{status, pr_url?, review?, error?, dashboard_url}` | Surfaces wedged-task states (failed / reconciled / rate_limited). |
| `list_providers` | `GET /api/status` (providers) + `GET /api/lm-models` | `[{provider, model, authenticated}]` | Discovery — proves "pick a non-Claude worker." Built **alongside** the flagship, not after. |
| `get_task_logs` | `GET /api/tasks/{id}/logs` (or persisted `logs`) | `{logs}` | Inline failure diagnosis without leaving the chat. |
| `cancel_task` | task cancel/abort endpoint | `{status: "cancelled"}` | Cleanup; lowest priority. |

### Build order

1. `dispatch_task` + `poll_task` + `list_providers` — first shippable unit (the demo).
2. `get_task_logs` — the safety net for the failure that *will* happen live.
3. `cancel_task` — cleanup, lowest urgency.

### Deferred to a later phase

- `submit_spec` + `poll_plan` — full autonomous loop. Shares `TaskQueue.activate_plan`
  with `dispatch_task` (see Planning Ownership).

## Worker Provider Scope (v1)

The implementation worker is the existing Docker **harness** (Aider) consuming a `MODEL`
env var routed through **LM Studio on the PC server** (OpenAI-compatible). "Any provider"
in v1 means **any model loaded in LM Studio** — the non-Anthropic story is that the worker
model isn't Claude. **`AgentManager.spawn_agent` is unchanged.** `list_providers` returns
whatever LM Studio has loaded (via `/api/lm-models`), so the demo visibly shows CC picking
a non-Claude worker.

**Staged future phases (out of scope for v1, recorded so the claim is honest):**

- **Phase 2:** point the harness at any OpenAI-compatible `base_url` + model (OpenRouter,
  hosted endpoints). Small extension to the `spawn_agent` env contract + entrypoints.
- **Phase 3:** the worker *is* a brain-provider CLI (codex / agy) running as the in-
  container coder. Biggest build — new harness images/entrypoints per CLI, in-container
  auth. The brain-provider CLIs already exist for planning/review, so this is the natural
  eventual convergence.

## Auth

CC's MCP config passes `PRAXIS_BASE_URL` + `PRAXIS_AUTH_TOKEN` as subprocess **env vars**
(standard MCP pattern). The adapter reads them at startup and attaches
`Authorization: Bearer <token>` on every REST call. No secrets in tool args or transcripts;
no per-call auth parameter. Same token the dashboard and CLI already use (`AUTH_TOKEN`).

## Error Handling

The adapter translates HTTP failures into **structured MCP tool errors** the brain can act
on — never opaque stack traces.

| Condition | MCP result |
|-----------|-----------|
| 401/403 from REST | `auth_error` — "check PRAXIS_AUTH_TOKEN" |
| Praxis server unreachable | `connection_error` — "is Praxis running at PRAXIS_BASE_URL?" |
| Unknown `model` / provider not in `list_providers` | `validation_error` listing available models |
| `poll_task` on unknown `task_id` | `not_found` |
| Task `failed` after retries | `poll_task` returns `status: failed` + error + `dashboard_url` (a normal terminal state, not an exception) |
| Provider auth dead (codex / agy) | reflect existing `ProviderAuthError` as `auth_error` with login hint |

## Testing

- **Unit** — each tool's REST-forward + response mapping against a mocked REST layer
  (httpx mock). Assert Bearer is attached, env-var resolution works, and error translation
  is correct.
- **Integration** — `mcp_server.py` against a live TestClient-backed Praxis (in-memory
  SQLite, mocked `AgentManager` / `spawn_agent`): `dispatch_task` enqueues a task →
  `poll_task` reflects state transitions → `list_providers` returns seeded models.
- **No new engine tests** — engine behavior is covered by the existing test suite; MCP adds
  only adapter-layer coverage. Target the project's 80%+ on the new module.

## Out of Scope (v1)

- `fastapi-mcp` HTTP/SSE mount (remote MCP clients).
- `submit_spec` / `poll_plan` full-loop trigger.
- Non-LM-Studio worker endpoints (Phase 2) and CLI-as-worker (Phase 3).
- Streaming agent output back into the MCP client loop (architecturally not possible with
  request/response delegation).
