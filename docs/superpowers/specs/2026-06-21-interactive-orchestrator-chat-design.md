# Interactive Orchestrator Chat ("Claude Code panel")

**Date:** 2026-06-21
**Status:** Design — approved for planning

## Problem

Native Claude Code subagents (the `Task` tool) are **model-locked** — they can only run
Anthropic models. There is no native way to make Claude dispatch a coding task to an
incompatible local/OSS model (Aider, Ollama, etc.). This is confirmed by the open,
unresolved Anthropic feature request
[claude-code#38698](https://github.com/anthropics/claude-code/issues/38698).

Praxis already works around this out-of-band: Opus plans/reviews, local models implement
via harness containers. But that flow is form-driven (submit a spec → plan → tasks). There
is no **interactive** surface where a user converses with Opus, has it read the live repo,
and has it dispatch implementation subagents on demand.

This spec adds that surface: an in-dashboard, interactive Opus session that **reads** the
project (never writes code) and **dispatches** Praxis subagents to do the writing.

## Goals

- Interactive, multi-turn Opus chat per project, with streamed output.
- Left pane: read-only file tree + file viewer of the project repo.
- Opus reads code with its own native tools; dispatches implementation work via an MCP tool
  that reuses the existing task pipeline (branch → review → merge).
- Dispatch honors the existing approval-gate toggle (hybrid autonomy).
- Persistent conversation per project (resumable across reloads/restarts).

## Non-Goals (YAGNI)

- No in-browser code editing. The tree/viewer is read-only.
- No terminal emulator (xterm.js/PTY). Opus runs headless; the UI is chat, not a shell.
- No multiple named threads per project — **one thread per project** for v1.
- Opus does not write code. Only harness subagents touch files.

## Roles

| Actor | Responsibility |
|-------|----------------|
| Opus (interactive `claude -p` session) | Read repo, converse, decide on tasks, call `dispatch_agent` |
| Praxis MCP server | Expose `dispatch_agent` / `check_task_status` / `list_active_tasks` to Opus |
| Harness subagent (Aider/OpenCode/OpenHands) | Actually implement and commit code on an `agent/` branch |
| Existing review loop | Opus review → squash merge on pass, re-dispatch on fail (unchanged) |

## Architecture

```
 Browser (web/index.html)
   ├── File tree pane ──GET /api/projects/{id}/tree
   │                   ──GET /api/projects/{id}/file?path=...   (read-only)
   └── Chat pane      ──POST /api/projects/{id}/chat            (user message)
                       └─SSE  /api/events                       (streamed deltas + tool calls)
            │
 FastAPI  ─┤  ChatManager  (new: core/chat_manager.py)
            │     • ensures a clone at data/chat-workdirs/{project_id}/ (reuse if present)
            │     • per user message, spawns:
            │         claude -p <msg> --resume <claude_session_id>
            │                --output-format stream-json
            │                --mcp-config <praxis-mcp>   (cwd = clone)
            │     • parses NDJSON stream → republishes via EventBus → SSE
            │     • persists messages to chat_messages; captures claude_session_id on first run
            │
 Praxis MCP server (new: core/mcp_server.py, stdio transport)
       tools:
         dispatch_agent(task_title, description)  -> task_id | "awaiting approval"
         check_task_status(task_id)               -> status + summary
         list_active_tasks()                      -> [ {task_id, title, status} ]
         └── delegates to existing AgentManager / task_queue / git_ops
```

Because the `claude` process runs with **cwd = the repo clone**, Opus's native `Read`,
`Glob`, and `Grep` operate directly on the code. We therefore expose **no custom read
tools** — the MCP server only provides what Opus cannot do natively: dispatch + status.

## Dispatch & the approval gate (hybrid)

`dispatch_agent` reuses the existing task pipeline and respects the approval-gate toggle:

- **Gate off** → create the task and spawn the harness container immediately. The tool
  returns the `task_id`. The task flows through the normal `agent/` branch → Opus review →
  merge path.
- **Gate on** → create the task in `PENDING`, emit an event so the UI shows an **Approve**
  button, and return `"awaiting approval"` so Opus knows not to wait on a result yet. The
  user clicks Approve → the existing approval path spawns the container.

Dispatched tasks are the **same `tasks` rows** used elsewhere, so they appear in the
existing task list and review flow. No parallel task system is introduced.

## Streaming model

Multi-turn continuity uses `claude --resume <session_id>` rather than a long-lived process:

1. First message: spawn `claude -p <msg> --output-format stream-json` (no `--resume`).
   Capture the `session_id` emitted in the stream; persist it on the `chat_sessions` row.
2. Subsequent messages: spawn `claude -p <msg> --resume <session_id> --output-format
   stream-json`.
3. Each spawn's NDJSON events (assistant text deltas, `tool_use`, `tool_result`,
   final result) are parsed and republished through the existing `EventBus`, delivered to
   the browser over the existing `/api/events` SSE stream, scoped by `project_id`.
4. The full turn (user msg + assistant content + tool calls) is persisted to
   `chat_messages` so the thread re-renders on reload.

## Data model (SQLite, additive)

```sql
CREATE TABLE IF NOT EXISTS chat_sessions (
    id                TEXT PRIMARY KEY,
    project_id        TEXT NOT NULL,
    claude_session_id TEXT,                 -- captured from first claude run
    workdir           TEXT NOT NULL,        -- path to the repo clone
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    FOREIGN KEY (project_id) REFERENCES projects(id)
);

CREATE TABLE IF NOT EXISTS chat_messages (
    id              TEXT PRIMARY KEY,
    session_id      TEXT NOT NULL,
    role            TEXT NOT NULL,          -- 'user' | 'assistant'
    content         TEXT NOT NULL,
    tool_calls_json TEXT,                   -- JSON array of tool_use/tool_result, nullable
    created_at      TEXT NOT NULL,
    FOREIGN KEY (session_id) REFERENCES chat_sessions(id)
);
```

One `chat_sessions` row per project (created lazily on first open). Migrations follow the
existing inline `CREATE TABLE IF NOT EXISTS` convention in `database.py`.

## API endpoints

| Method | Path | Purpose |
|--------|------|---------|
| GET  | `/api/projects/{id}/tree` | Read-only file tree of the repo clone |
| GET  | `/api/projects/{id}/file?path=` | Raw file content (path-validated, within clone) |
| GET  | `/api/projects/{id}/chat` | Load persisted chat history for the project |
| POST | `/api/projects/{id}/chat` | Submit a user message; triggers a `claude` turn |

All require `verify_token` (Bearer), matching existing endpoints.

## Error handling

- **Rate limit:** reuse `OpusBridge`'s detection. A rate-limited turn surfaces the existing
  "resuming at…" state; the user's message is preserved and the turn can be retried.
- **Clone/git failure:** return `502` (as the Memory/Context view does), shown inline in the
  chat pane — not an opaque 500.
- **MCP `dispatch_agent` with Docker/AgentManager unavailable:** the tool returns a
  structured error string that Opus relays to the user; it must not crash the session.
- **Path traversal:** `file?path=` is validated to resolve within the clone directory;
  reject anything escaping it with `400`.

## Testing

Target 80%+ coverage, matching the repo.

- **Unit (`ChatManager`):** NDJSON stream parsing into events (mocked `claude` output);
  session create vs. resume (session_id captured then reused); message persistence.
- **Unit (MCP server):** `dispatch_agent` honors the gate — immediate spawn when off
  (mock AgentManager), `"awaiting approval"` + `PENDING` task when on; `check_task_status`
  / `list_active_tasks` shape.
- **Integration (API):** `POST /chat` persists messages and emits SSE events; `GET /chat`
  returns history; `tree`/`file` happy path + path-traversal rejection; auth required.

## Security & gotchas

- Reuses the non-root, `GH_TOKEN`-credential clone approach already used by Context Sync.
- The MCP server runs stdio, spawned per `claude` turn with the dispatch tools bound to the
  current `project_id` (no cross-project dispatch).
- `data/chat-workdirs/` is created under the existing `data/` dir (auto-created by lifespan);
  add to `.gitignore`.
- Clone reuse: `git fetch` + reset to default branch on session open so the tree reflects
  merged work, rather than re-cloning every message (cheaper than the Memory view's
  clone-per-open).
