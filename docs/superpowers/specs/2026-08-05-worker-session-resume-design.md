# Worker Session Resume — Design

- **Date:** 2026-08-05
- **Status:** Draft (approved in brainstorming, pending spec review)
- **Topic:** Give a re-dispatched worker its own conversation back. When a blocked worker
  is answered and re-dispatched, resume its harness-native session (agy `--conversation`,
  OpenCode `--session`) instead of starting cold, and checkpoint its work-in-progress so
  memory and working tree agree.

## Problem

Praxis already implements the industry-standard ask-back pattern. A worker that hits an
ambiguity emits `Status: BLOCKED` / `NEEDS_CONTEXT`, exits without a PR, the task moves to
`NEEDS_CLARIFICATION` (no retry burned), the brain answers via `handle_clarification` or a
human answers via `POST /api/tasks/{id}/clarify`, and the task is re-dispatched. This is a
hand-rolled equivalent of A2A's `input-required` task state.

What is missing is the other half of that pattern: **state continuity across the turn**.
Every re-dispatch is a cold container with a fresh `TASK_PROMPT`. Two things are lost:

1. **The worker's conversation.** All of its reasoning, its tool calls, its reading of the
   codebase, and its understanding of why it got stuck. It re-derives everything from the
   Static Bible and the git spine, which are a summary, not a transcript.
2. **The worker's edits.** A blocked worker exits before the commit block at
   `entrypoint.sh:241` (opencode) / `:261` (agy), so any files it changed die with the
   container. Work is discarded precisely at the moment the worker demonstrated it was
   engaged enough to ask a question.

The cost is paid on every clarification round: full context re-derivation, plus repeated
work. The mechanism to avoid it already exists in both harnesses and is unused.

## Prior art

Session continuation is universal across coding-agent CLIs:

| Harness | Continuation flags | Id surfaced via |
|---|---|---|
| agy (Antigravity) | `-c` / `--continue`, `--conversation <id>` | `conversation_id` in the `--output-format json` envelope |
| OpenCode | `-c` / `--continue`, `-s` / `--session <id>` | `opencode session list --format json` |

The broader ecosystem splits the same problem into three patterns: resume between turns,
ask-back mid-task (A2A `input-required`, LangGraph `interrupt()` / `Command(resume=...)`),
and mid-run steering (OpenClaw `/steer`, Ably AI Transport). Praxis has the second and is
missing the first. This spec adds only the first.

## Goals

- A re-dispatched worker resumes its own conversation when, and only when, doing so is safe.
- A blocked worker's work-in-progress survives to the next turn.
- Fail open: every failure path degrades to exactly today's cold-start behavior.
- Symmetric support for both shipped harnesses, with no new container lifecycle.

## Non-Goals

- **Mid-run ask-back.** No inbox endpoint, no `send_message` MCP tool, no worker that stays
  alive polling for an answer. Considered and deferred: it needs message ids, acks, idle
  timeouts, and orphan reaping in reconcile.
- **A live channel.** No long-lived container running `opencode serve`. agy headless is one
  prompt per invocation by design, so this would split the harnesses onto different
  execution models.
- **Resume on failure retries.** A review-FAILED retry rebuilds the branch from base and is
  a deliberate clean re-implementation. Restoring memory there would contradict the tree.
- **Cross-harness session portability.** An agy conversation id is meaningless to OpenCode.
- **Changing the clarification state machine.** States, retry accounting, and the human gate
  are untouched.

## Design

### 1. The worker session handle

Two nullable columns on `tasks`, added by a new `Migration(n, ...)` in `database.py`
(idempotent, re-run safe, per the existing migration convention):

| Column | Meaning |
|---|---|
| `worker_session_id` | Harness-native conversation / session id |
| `worker_session_harness` | Which harness minted it |

Both are required. A project's `harness` can change between dispatches, and replaying an
agy conversation id into OpenCode is nonsense, so the harness is stored alongside the id
and checked before any replay.

### 2. Capture (worker to orchestrator)

Deliberately asymmetric. Each harness takes its own safest path rather than forcing a
uniform mechanism onto both.

**OpenCode.** The `opencode run` invocation is left byte-for-byte unchanged. After it
returns, the entrypoint reads `opencode session list --format json`. A fresh container has
exactly one session, so "the only entry" is unambiguous. This deliberately avoids depending
on the `--format json` event schema and leaves the existing `Status:` grep at
`entrypoint.sh:227` untouched.

**agy.** There is no session-list equivalent, so the invocation switches to
`--output-format json` and the entrypoint reads `conversation_id` from the envelope, then
feeds the response text into the existing `Status:` parse. The field name carrying the
response body is **unverified**; the implementation plan must probe it once against real
agy v1.1.2 before wiring. If the JSON parse fails for any reason, the entrypoint falls back
to today's text-mode invocation and reports no session id.

Both extractors live in `docker/<harness>-agent/extract_session.py`, baked into the image
and invoked from the entrypoint, rather than as inline `python3 -c` one-liners. This makes
them unit-testable against fixture JSON, which an inline snippet is not.

**Transport.** One new optional `session_id` field on the `/api/internal/agent-done`
payload and its Pydantic model, persisted to the two new columns. The callback already
carries `task_id`, `run_id`, `status`, `pr_url`, and `question`; this is one more optional
field and one UPDATE. Both entrypoints build the payload in `send_callback`, so the shell
change is symmetric.

### 3. Checkpoint on BLOCKED

**`BLOCKED` becomes a checkpoint, not a discard.** On `BLOCKED` / `NEEDS_CONTEXT`, before
sending the callback, the entrypoint stages and commits the working tree and pushes to
`BRANCH`. It still opens no PR, so the clarification contract is unchanged from the
orchestrator's point of view.

This is load-bearing, not a bonus. Restoring a worker's memory without restoring its tree
is worse than a cold start: the resumed worker would confidently reference edits that no
longer exist. Committing the WIP is what makes resume sound.

It is also the same git-spine handover Praxis already relies on for progress continuity,
applied one state earlier. The costs are accepted: WIP commits appear on the work branch
and land in the accumulated diff the reviewer sees, and an abandoned task's branch is
reclaimed by the existing `branch_sweeper` as it is today.

A clean tree at BLOCKED is normal (the worker asked before editing anything) and is not an
error; the entrypoint skips the commit and proceeds to the callback.

### 4. Replay (orchestrator to worker)

`build_spawn_env` gains a `WORKER_SESSION_ID` variable, threaded from `spawn_agent` the
same way `single_branch` is. It is set only when **all** of the following hold:

1. The re-dispatch follows a resolved clarification (`clarification_state` is
   `answered_by_brain` or `resolved`), not a failure retry.
2. `worker_session_harness` equals the harness being spawned.
3. A session id is stored, which by construction means the previous turn's checkpoint push
   succeeded (see Error handling: the id is only recorded on a successful push).

**`WORKER_SESSION_ID` also drives branch reuse.** Today only `SINGLE_BRANCH=1` reuses an
existing `origin/${BRANCH}`; the two-tier path cuts a fresh branch from base, which would
discard the checkpoint. So the entrypoint's branch setup reuses `origin/${BRANCH}` when
**either** `SINGLE_BRANCH=1` or `WORKER_SESSION_ID` is set, and a resume turn pushes
non-force for the same reason single-branch mode does. Resume and branch continuity are one
mechanism, not two independent gates: restoring memory without restoring the tree is the
failure mode this design exists to prevent.

The entrypoints then invoke:

- agy: `agy ... --conversation "${WORKER_SESSION_ID}" -p "${EFFECTIVE_PROMPT}"`
- OpenCode: `opencode run --session "${WORKER_SESSION_ID}" ...`

The prompt still carries the Bible and the clarification answer. Resume adds the
transcript; it does not replace the existing context contract.

### 5. Storage

**agy needs nothing.** Its conversation store is already at
`/home/agent/.gemini/antigravity-cli/`, inside the `praxis-gemini-creds` volume that
`agent_manager.py:285` mounts read-write. Conversations already survive container death
today, unreferenced and unmanaged.

**OpenCode needs a volume.** Session storage lives in the container home and dies with the
container. Add a named volume `praxis-opencode-sessions` mounted at
`/home/agent/.local/share/opencode`, with `XDG_DATA_HOME` pinned explicitly in the
Dockerfile so the path is not implementation-dependent. A new `OPENCODE_SESSIONS_VOLUME`
setting follows the `GEMINI_CREDS_VOLUME` pattern; unset means no persistence and cold
starts, never an error.

**Growth.** Both stores accumulate across tasks. v1 clears `worker_session_id` on terminal
task status, so a stale id is never replayed, and otherwise leaves the stores alone.

Pruning is explicitly deferred. The orchestrator does not mount either volume, so it cannot
reach these stores from the reconcile loop without spawning a throwaway container, which is
more machinery than the problem currently justifies. The agy store is already unmanaged
today and this spec does not change that. When growth becomes real, the cheap fix is an
entrypoint-side prune (each container already mounts its own store) rather than an
orchestrator-side sweeper.

### 6. Error handling

Every failure path degrades to today's behavior. Resume is an optimization and must never
be able to fail a task.

| Failure | Behavior |
|---|---|
| No session id captured | Next dispatch is cold. Same as today. |
| Harness changed between dispatches | Stored id ignored, cold start. |
| Volume absent or unwritable | No persistence, cold start, no error. |
| agy JSON envelope unparseable | Fall back to text mode, no session id captured. |
| Stale or pruned id rejected by the CLI | Retry once without the flag, then proceed cold. |
| Checkpoint push fails | Log, send the callback anyway, omit the session id from the payload. The next turn is cold and rebuilds from base. |

The last row is the invariant in one line: **a session id is only ever reported after its
checkpoint is safely on the remote.** A failed push therefore suppresses both resume and
branch reuse in the same step, because what must hold is that memory and tree agree, not
that resume happens.

The entrypoint also strips `.praxis-bible.md` handling exactly as the existing commit block
does: it is in `.git/info/exclude`, so a checkpoint `git add -A` cannot stage it.

## Testing

**Unit.** The `build_spawn_env` gate matrix (all three replay conditions, positive and
negative); callback schema accepts and persists `session_id`; harness-mismatch guard;
migration idempotency under re-run; both `extract_session.py` extractors against fixture
JSON including malformed input.

**Shell.** Both entrypoints stay shellcheck-clean under `docker.yml`. The checkpoint block
is exercised by executing the shell fragment against a scratch git repo, not by `bash -n`
(a prior session shipped a `printf` leading-dash bug that syntax-checking missed).

**Live.** A dogfood run is required. Both images need a rebuild regardless, since entrypoint
changes do not hot-reload, and the resume path cannot be verified any other way. The run
should force a real `BLOCKED` and confirm the second turn resumes rather than re-derives.

## Rollout

Entrypoint changes mean **both agent images must be rebuilt**; a stale image runs silently.
The OpenCode volume must be created before first use. No orchestrator config change is
required for agy.

## Future work

- Mid-run ask-back via a worker inbox (`POST /api/tasks/{id}/message` plus an MCP
  `send_message` tool). If built, adopt the constraints surfaced by the Claude Code /
  Codex bridge critique: stable message ids, sender identity, sequence numbers, explicit
  acks, and no implicit tool authorization from a received message.
- Mapping the frozen `core/status_vocab.py` vocabulary onto A2A task states, which would
  make Praxis tasks legible to A2A-speaking callers.
- Entrypoint-side pruning of the OpenCode session volume and the agy conversation store,
  once accumulation is measured to matter.
- A PTY wrapper for agy to close the non-TTY stdout gap (upstream
  `google-antigravity/antigravity-cli#76`), which would also unblock agy as a brain.
