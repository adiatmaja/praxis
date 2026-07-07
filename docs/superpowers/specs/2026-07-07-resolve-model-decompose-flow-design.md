# Resolve-model-first decompose flow (portable, MCP-delivered)

**Date:** 2026-07-07
**Status:** Design — approved for planning

## Problem

An orchestrating client (Claude Code / Codex / Antigravity) that drives Praxis over
MCP must already know the worker `model` to pass to `execute_plan` / `dispatch_task`.
There is no way to read what a project is *already configured* to use, so the
often-stated goal "if it is configured in Praxis, use those" is impossible today. The
orchestrator either hardcodes a model or re-asks the human on every call.

The engine-side decomposition is already capability-keyed on that model
(`decompose_plan()` -> `capability_profile(model)` -> `build_review_prompt`). The gap
is purely **read-back + guidance**, and it must be delivered through **MCP** so that
all three target clients benefit equally. Skills (`SKILL.md`) are opt-in per harness
and per developer; the MCP server is the one surface every Praxis-as-MCP user is
guaranteed to have.

## Scope

**In scope**

1. A read-back MCP tool pair (`get_project`, `list_projects`) so any client can
   resolve a repo's configured worker model + harness.
2. An orchestration-guide update teaching the resolve-model -> execute -> poll flow.
3. An audit (spec section) of other functions needing read-back / better guidance.

**Out of scope**

- The optional client `decompose` skill (deferred; see
  `docs/superpowers/notes/decompose-skill-optional.md`).
- Any change to engine/decompose logic. It is untouched.

## Design

### 1. Read-back MCP tools

Two thin tools wrapping the existing `/api/projects` REST surface, following the
established `*_impl` + FastMCP-wrapper pattern in `src/mcp_server/server.py` and the
`PraxisClient` HTTP wrapper.

`get_project(repo_url)` ->
```
{project_id, name, model, harness, default_branch, approval_gate}
```
or `{"project": null}` when **no project exists yet for that repo**. This is a normal
state, NOT an error: `execute_plan` / `dispatch_task` create projects lazily, so a repo
Praxis has never seen simply has no configured model. The orchestrator branches on
`null` to fall back to asking the human. Client/transport failures still return the
structured `{"error": code, "message": ...}` shape like every other tool.

`list_projects()` ->
```
{projects: [{id, name, repo_url, model, harness}, ...]}
```
For discovery: an orchestrator that just connected can enumerate the repos Praxis
already knows instead of guessing a `repo_url`. Chosen over a `get_project`-only design
because the intended usage is multi-repo and multi-developer, where guessing the exact
`repo_url` is fragile.

Both tools map `repo_url` matching to the existing project lookup (`SELECT ... FROM
projects WHERE repo_url = ?`, the same match `execute_plan` uses).

### 2. Orchestration-guide update

Add a "Resolve the worker model before dispatching" section to
`src/mcp_server/resources/orchestration_guide.md` codifying the flow:

1. `get_project(repo_url)`.
2. If `model` is set, use it.
3. If `null` / unset, call `list_providers` for candidate worker models and ask the
   human which to use.
4. Then `execute_plan` (or `dispatch_task`) with the resolved model.
5. `poll_plan` until terminal.

This is the portable home for the decompose-workflow guidance, reachable by every MCP
client via `praxis://guide/orchestration`.

### 3. Function audit (read-back / guidance gaps)

| Function | Gap | Action |
|----------|-----|--------|
| `execute_plan` | needs resolved `model` | covered by guide (step above) |
| `dispatch_task` | same resolve-model need | covered by guide (same flow applies) |
| `poll_task` / `poll_plan` | none | no change |
| `list_providers` | none (already read-back) | no change |
| `get_task_logs` / `cancel_task` | none | no change |
| project config (model/harness) | not readable over MCP | **fixed by `get_project`/`list_projects`** |

No implementation beyond Sections 1-2. Anything larger surfaced by the audit becomes
its own future item.

## Testing

- Unit tests for `get_project_impl` / `list_projects_impl`: found, missing -> `null`,
  and client-error paths, mirroring existing MCP tool tests.
- A content assertion that the orchestration guide contains the resolve-model section.
- No engine/decompose tests: that path is unchanged.

## Delivery notes

- Follows existing MCP conventions: `*_impl(client, ...)` independently testable, thin
  `@mcp.tool()` wrapper building `PraxisClient.from_env()`.
- Reuses `GET /api/projects` (already present) — no new REST endpoint expected unless a
  by-repo lookup endpoint is cleaner than filtering `list` client-side; decide in the plan.
```
