# Context-fidelity manifest (client-gathered non-committed context)

**Date:** 2026-07-07
**Status:** Design — approved for planning
**Related:** `2026-06-29-worker-context-continuity-design.md` (defines the Static Bible +
`repo_memory` slot this fills), `2026-07-07-resolve-model-decompose-flow-design.md` (the
MCP read-back + guide pattern this reuses).

## Problem

When Praxis delegates a task to the local worker, the worker only sees what `git clone`
gives it. Gitignored files (`.env`, local configs, data samples) and harness user-scope
memory (`~/.claude/CLAUDE.md`, `MEMORY.md`) never travel. Proof:
`orchestrator_dispatch.py:183` sets `repo_memory=None  # repo files folded in by
entrypoint --read`, and the entrypoint `--read` only folds in **committed** files
(`CLAUDE.md`, `MEMORY.md`, `AGENTS.md`). The `repo_memory` slot in `BibleSources` was
designed for exactly this content but is always empty. This is the known
"handoff-fidelity gap": the implementer starts cold, missing the non-committed context
the human/orchestrator takes for granted.

## Design decisions (locked)

1. **Client-gathers, not engine-infers.** The gitignored files and user-scope memory
   physically exist in exactly one place: the orchestrating MCP client's working
   directory (the Claude Code / Codex / Antigravity session driving Praxis, which sits
   in the real repo checkout). The Praxis brain (`claude -p`) only ever sees a fresh
   `git clone`, so it can at best *guess* from committed templates — which is precisely
   the information that already travels. The client is the only source of ground truth.
   This also matches the standing architecture: Praxis is MCP-driven, engine untouched,
   dashboard for visualization only.

2. **New dedicated field, not folded into `context`.** A separate optional
   `local_context` param threads into the currently-empty, **droppable** `repo_memory`
   Bible slot (priority 9). The existing `context`/`context_text` is a **floor** slot
   (`caller_context`, never dropped) carrying task *intent*; environment *reference*
   material is semantically distinct and correctly droppable under budget pressure.

3. **Self-contained, minimum-blocking, never "read file X".** The manifest inlines the
   information the worker needs, but only the **essential, blocking** subset — the
   minimum without which the implementation cannot be written correctly. It must NOT
   contain pointers like "read `.env`" or "see `~/.claude/CLAUDE.md`", because the
   worker's clone does not contain those files; a "go read X" instruction is a dead end.
   The worker can only use what physically arrives in the Bible.

4. **Privacy by default: names/shapes over values.** A Praxis worker almost always just
   *writes* code, it does not *run* it, so it rarely needs live secret values at all — it
   needs variable **names**, config **structure**, data **shapes**, and **conventions**.
   The default is therefore to include the shape (e.g. the env var name and its purpose,
   a field list, a masked example) and to omit the actual value. A real value is included
   only in the rare case where the code cannot be written correctly without it, and even
   then only that one value. When in doubt, include the name and leave the value out.

5. **Engine decompose is untouched.** The brain does not need the manifest to
   capability-gate a plan. `decompose_plan` / `build_review_prompt` are not modified.

## Data flow

```
  Client session (real working dir + user-scope memory)
        │  gathers a minimum-blocking, self-contained, scrubbed manifest
        ▼
  execute_plan / dispatch_task(local_context=...)      ← new optional field
        │
        ▼  threaded per-leaf as task["repo_memory"]
  orchestrator_dispatch._build_worker_bible
        │  BibleSources(repo_memory=<manifest>)         ← was always None
        ▼  build_bible scrubs every section + budgets
  Worker container: "# REPO MEMORY" section (droppable, priority 9)
```

## Manifest contents (what the guide prescribes)

The test for inclusion is: *would the absence of this block writing correct code?*
Because the worker writes code but does not run it, the answer is usually satisfied by
the shape, not the value.

Included, minimum-blocking and inline:

- **Environment shape** — the env var **names** the code references and their purpose
  (e.g. `REDIS_URL` = cache connection), so the worker wires them correctly. The literal
  value is included only when the code cannot be written without it.
- **Local config structure** — the relevant keys/shape of gitignored config files, not
  their secret contents.
- **Data-sample shapes** — the field list / structure of the records the task operates
  on; a masked example row rather than real data where possible.
- **User-scope conventions** — the relevant portions of `~/.claude/CLAUDE.md` /
  `MEMORY.md` the worker must honor (coding standards, project gotchas), copied inline.

Excluded:

- **Live secret values not required to write the code** — tokens, passwords, keys. The
  client omits or masks these by default; `build_bible` re-scrubs server-side as
  defense-in-depth.
- **Anything non-blocking** — if its absence would not break the implementation, leave
  it out. Smaller manifest, less leak surface, less budget pressure.
- **"Read file" pointers** — never reference a path the worker's clone does not contain.

## Components changed

- **MCP tools** (`src/mcp_server/server.py`, `src/mcp_server/client.py`):
  `execute_plan` and `dispatch_task` gain optional `local_context: str | None`,
  forwarded to the REST layer.
- **REST** (`api/execute_plan.py`, `api/dispatch.py`, `models/schemas.py`): accept the
  field, scrub it (`scrub_context`), and thread it onto each leaf task as `repo_memory`,
  mirroring the existing `context_text` pattern (`execute_plan_decompose.py:114`,
  `dispatch.py:265`).
- **Dispatch** (`orchestrator_dispatch.py:87,183`): read `plan_task.get("repo_memory")`
  and pass it into `BibleSources.repo_memory`, replacing the hardcoded `None`.
- **Orchestration guide** (`src/mcp_server/resources/orchestration_guide.md`): new
  "Gather local context before dispatching" section codifying the manifest contents
  (minimum-blocking, names/shapes over values, secret-omitting, no read-file pointers)
  and where it fits in the
  resolve-model -> execute -> poll flow.
- **Docs**: update the `orchestrator_dispatch.py:183` comment and add a CLAUDE.md gotcha.

## Safety & budgeting

- **Double scrub.** Client scrubs by guide instruction; `build_bible` re-scrubs every
  section server-side (`worker_bible.py:67`). No new scrub code.
- **Correct droppability.** The manifest lands in the priority-9 `repo_memory` section,
  dropped before goal/handover/plan under a tight window — right, since it is reference,
  not intent.
- **GitHub-only stance preserved.** Nothing is mounted into the worker; only scrubbed
  prose travels, exactly like `context_text` today. This does not reopen the rejected
  "mount local/gitignored files" path — the client, not Praxis, reads the files, and
  only their needed content (as prose) crosses the boundary.

## Testing

- `local_context` threads onto every leaf as `repo_memory` (execute_plan + dispatch).
- Dispatch passes `repo_memory` into `BibleSources` (replacing `None`); the assembled
  Bible contains a `# REPO MEMORY` section when provided.
- Under a tight context window, the manifest section is dropped before floor sections.
- The field is scrubbed server-side (credential-shaped content stripped).
- Guide content-assertion for the new "Gather local context" section, including the
  minimum-blocking / names-over-values privacy principle.
- No engine/decompose tests: `decompose_plan` is unchanged.

## Out of scope

- Engine-side inference of the manifest (rejected above).
- Mounting gitignored/local files into the worker (standing rejection, unchanged).
- Any change to capability gating or the decompose review prompt.
