# Praxis Orchestration Guide (MCP Resource) Design

**Date:** 2026-06-29
**Status:** Approved (brainstorming)
**Theme:** Theme 2 of the 2026-06-29 epic (see `capability-and-continuity-epic` memory).
**Relates to:** `2026-06-29-capability-aware-execution-design.md` (Spec 2) — this guide
documents the `execute_plan` tool that Spec 2 introduces. Ship this guide after or
alongside Spec 2; the guide file can be authored now with the `execute_plan` section
included.

---

## Problem

Praxis exposes a control surface over MCP (`src/mcp_server/`): `dispatch_task`,
`poll_task`, `list_providers`, `get_task_logs`, `cancel_task`, and (per Spec 2)
`execute_plan`. The intended consumer is an **orchestrating agent** — e.g. Claude Code
running in another project that connects to Praxis as an MCP server and delegates
implementation work to the local-LLM worker.

Today that agent only sees one-line tool docstrings. It has no narrative of the Praxis
workflow: when to delegate to Praxis at all, which tool to reach for, what context to
brief the worker with, how often to poll an async task, how to interpret each task
status, or how to triage a wedged run. Without this, the calling agent under-uses
Praxis, passes the wrong context, polls badly (tight loops or never), and
misinterprets `blocked` / escalated results.

A README would not reach the agent automatically (a human would have to paste it).
Tool docstrings have no room for a cross-tool workflow narrative. The right surface is
an **MCP resource served by the running Praxis server**, so any connected client
auto-discovers it and it is always in sync with the server's actual tools.

## Goal

Serve a single static markdown guide as an MCP resource that teaches an orchestrating
agent both **when** to delegate to Praxis and **how** to drive its tools end to end.

## Non-goals / Out of scope

- **Embedding live server state** (current providers, worker models, configured
  projects) in the resource. The guide stays static and cacheable; for live data it
  directs the agent to call `list_providers`. (Considered and rejected: coupling the
  doc to runtime state, re-rendering every read, and duplicating an existing tool as a
  resource.)
- **A second "state" resource.** `list_providers` already covers live state.
- **Changing tool behavior or adding engine logic.** This is documentation surface
  only: one resource handler + one content file + tests.
- **Rewriting tool docstrings into full workflow prose.** Docstrings stay the short
  "what"; the resource owns the long "when/how". No narrative duplication.

---

## Design

### Surface

A FastMCP resource registered in `src/mcp_server/server.py`:

```
@mcp.resource("praxis://guide/orchestration")
def orchestration_guide() -> str: ...
```

The handler reads and returns the contents of a markdown file shipped alongside the
server package: `src/mcp_server/resources/orchestration_guide.md`. Keeping the content
in a packaged file (not an inline Python string) means it is editable as a normal
markdown doc and reviewable in diffs, while still being installed with the package so it
loads from any working directory.

**Packaging:** the `.md` lives under the `mcp_server` package and is resolved relative to
the module (e.g. via `importlib.resources` / `Path(__file__).parent`), so it loads
regardless of CWD. The build must include non-Python package data (add the
`mcp_server/resources/*.md` glob to `[tool.setuptools.package-data]` /
`MANIFEST.in`-equivalent in `pyproject.toml`) so an installed wheel still carries it.

### Resource content (sections inside `orchestration_guide.md`)

1. **What Praxis is, and when to delegate to it.** The brain/hands split: the calling
   agent plans and reasons; Praxis runs a local-LLM worker (in a one-shot Docker
   container per task) that clones, implements, commits, and opens a PR; Praxis's *own*
   brain then reviews and merges on pass (re-dispatches on fail). Delegate bulk,
   parallelizable, or lower-novelty implementation to conserve the caller's own
   subscription budget; keep high-novelty / architectural / ambiguous work in the
   caller's own session. Stress the model: **asynchronous and one-shot** — you receive a
   `task_id` and poll; the MCP side is blind between calls.

2. **Picking the tool (decision tree).**
   - `dispatch_task` — one self-contained change the caller has already sized small
     ("I know this is one task"). No capability gating.
   - `execute_plan` — a whole externally-authored plan; Praxis capability-gates it
     against the local model and decomposes it into do-able leaves, flagging any leaf
     too hard as `needs_stronger_model` (Spec 2). Use when handing over a multi-task
     plan rather than a single edit.
   - `list_providers` — call first to see available worker models and brain/provider
     auth status before dispatching.
   - `poll_task`, `get_task_logs`, `cancel_task` — lifecycle/triage (below).

3. **What context to pass.** A focused, task-relevant slice: conventions, architecture
   notes, the relevant plan slice. Not the caller's whole memory tree. **Never** secrets,
   tokens, or `.env` values — they are redacted server-side, but keep them out anyway.
   (Mirrors the existing `dispatch_task` docstring guidance, expanded.)

4. **Polling cadence.** It is async and MCP is blind between calls. Poll `poll_task` at a
   reasonable interval (not a tight loop); expect work to take minutes. The
   `dashboard_url` returned by the tools is the rich human view for live logs.

5. **Reading statuses.** Map the task state machine for the caller:
   `pending → in_progress → reviewing → passed → merged`; `failed` (Praxis auto
   re-dispatches up to the project's max_retries before going terminal); and Spec 2's
   `blocked` / escalated states (`needs_stronger_model`) — what each means and what the
   caller should do (wait, inspect logs, revise the task, or accept the escalation
   outcome).

6. **Troubleshooting.** `get_task_logs` to inspect a wedged or failed run; `cancel_task`
   to stop a running task; and the client error codes the tools surface
   (`connection_error`, `wrong_service`, `auth_error`, `validation_error`,
   `config_error`) with the fix for each (server down, wrong `PRAXIS_BASE_URL`/port,
   missing/incorrect `PRAXIS_AUTH_TOKEN`, malformed request).

### Drift guard

Tests assert the guide names every registered tool, so adding/renaming a tool without
updating the guide fails CI. This keeps the static doc honest against the live surface
without making it dynamic.

---

## Code changes (overview)

| File | Change |
|------|--------|
| `src/mcp_server/resources/orchestration_guide.md` (new) | The guide content (6 sections above) |
| `src/mcp_server/server.py` | Register `@mcp.resource("praxis://guide/orchestration")`; loader reads the packaged `.md` |
| `pyproject.toml` | Include `mcp_server/resources/*.md` as package data so installed wheels carry it |
| `tests/test_mcp_resources.py` (new) | Resource registered + readable; non-empty markdown; names every tool (drift guard); loads from packaged path |

No data model changes. No new REST endpoints. No engine logic.

## Testing

- Resource is registered under `praxis://guide/orchestration` and returns non-empty
  markdown.
- Returned content references every MCP tool name (`dispatch_task`, `poll_task`,
  `list_providers`, `get_task_logs`, `cancel_task`, `execute_plan`) — drift guard.
- The content file loads via the package-relative resolver regardless of CWD (simulate
  by resolving from the module path, not a hardcoded relative path).
- ≥80% coverage on the new resource handler/loader; mark unit.

## Risks / trade-offs

- **Static content can drift from tool behavior.** Mitigated by the tool-name drift
  guard test; semantic drift (e.g. a status rename) is a normal doc-maintenance cost,
  accepted for the simplicity of a static, cacheable resource.
- **`execute_plan` section documents a tool that lands in Spec 2.** Accepted: the guide
  is authored with the section now and ships after/alongside Spec 2, so the two land
  consistent. If this guide ships first, the `execute_plan` section reads as
  forthcoming until Spec 2 merges.
- **MCP resource discovery varies by client.** Some clients surface resources
  prominently, others require an explicit fetch. The guide is still the canonical,
  in-sync source; clients that ignore resources fall back to tool docstrings (the short
  "what"), which remain self-sufficient for basic use.
