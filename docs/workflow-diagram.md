# Praxis – End-to-End Workflow Diagram

Swimlane diagram scoped by **User · Brain (Claude Code) · Praxis Orchestrator · Coding Agent
(Docker) · Local Git · Origin Git (GitHub)**.

> **Reading tip:** open this file in a monospace-rendered viewer (GitHub, VS Code with a
> fixed-width font, or any terminal) so the box-drawing characters align correctly.

```
╔══════════════════╦═══════════════════════════╦═══════════════════════════════╦══════════════════════════════════╦══════════════════╦══════════════════════════════════╗
║      USER        ║   BRAIN (Claude Code)     ║    PRAXIS ORCHESTRATOR        ║     CODING AGENT (DOCKER)        ║   LOCAL GIT      ║      ORIGIN GIT (GitHub)         ║
║                  ║  your AI subscription     ║   FastAPI + SQLite + SSE      ║  OpenCode / Aider / OpenHands    ║  your checkout   ║  branches · PRs · source of truth║
║                  ║  calls Praxis via MCP     ║                               ║  + Local LLM (LM Studio)         ║  NEVER touched   ║                                  ║
╠══════════════════╬═══════════════════════════╬═══════════════════════════════╬══════════════════════════════════╬══════════════════╬══════════════════════════════════╣
║                  ║                           ║                               ║                                  ║                  ║                                  ║
║  "use praxis to  ║                           ║                               ║                                  ║                  ║                                  ║
║  implement       ║                           ║                               ║                                  ║  git push        ║                                  ║
║  plan.md on      ║                           ║                               ║                                  ║  (required first)║                                  ║
║  my-repo with    ║                           ║                               ║                                  ║  ─────────────────────────────────────────────────────►  ║
║  <local-model>"  ║                           ║                               ║                                  ║                  ║  origin/main (up to date)        ║
║        │         ║                           ║                               ║                                  ║                  ║                                  ║
║        ╰────────►║ 1. read plan.md           ║                               ║                                  ║                  ║                                  ║
║                  ║    decompose into tasks   ║                               ║                                  ║                  ║                                  ║
║                  ║    build task graph       ║                               ║                                  ║                  ║                                  ║
║                  ║    call execute_plan()    ║                               ║                                  ║                  ║                                  ║
║                  ║    or dispatch_task() ────╼ 2. receive plan via MCP       ║                                  ║                  ║                                  ║
║                  ║         MCP tool call     ║    derive tasks from plan.md  ║                                  ║                  ║                                  ║
║                  ║         returns           ║    store in DB (status:active)║                                  ║                  ║                                  ║
║                  ║         {task_id,         ║                               ║                                  ║                  ║                                  ║
║                  ║          dashboard_url} ◄─╼                               ║                                  ║                  ║                                  ║
║                  ║                           ║                               ║                                  ║                  ║                                  ║
║  Dashboard ◄ SSE ╫─ live logs (monitor only) ║ 3. create plan branch ─────────────────────────────────────────────────────────────╼ push plan/date-slug branch       ║
║                  ║                           ║                               ║                                  ║                  ║                                  ║
║                  ║                           ║ 4. dispatch tasks             ║                                  ║                  ║                                  ║
║                  ║                           ║    dep-ordered, parallel      ║                                  ║                  ║                                  ║
║                  ║                           ║         │                     ║                                  ║                  ║                                  ║
║                  ║                           ║         ╰───────────────────────────────────╼ spawn container   ║                  ║                                  ║
║                  ║                           ║                               ║   git clone ◄────────────────║                  ║  clone from origin               ║
║                  ║                           ║                               ║   checkout -b agent/task-slug    ║                  ║                                  ║
║                  ║                           ║                               ║   run harness + task prompt      ║                  ║                                  ║
║                  ║                           ║                               ║   local LLM edits files          ║                  ║                                  ║
║                  ║                           ║                               ║   git commit                     ║                  ║                                  ║
║                  ║                           ║                               ║   push + open PR ─────────────────────────────────────────────────────────────────►  ║
║                  ║                           ║                               ║        │                         ║                  ║  agent/task-slug → plan branch   ║
║                  ║                           ║ 5. callback: agent-done ◄─────╫────────╯                         ║                  ║                                  ║
║                  ║                           ║    fetch PR diff ◄─────────────────────────║                  ║  via gh pr diff                   ║
║                  ║                           ║    route to Brain for review  ║                                  ║                  ║                                  ║
║                  ║                           ║         ╰─────────────────────╼                                  ║                  ║                                  ║
║                  ║  6. review_diff           ║                               ║                                  ║                  ║                                  ║
║                  ║     judge the PR diff     ║                               ║                                  ║                  ║                                  ║
║                  ║     verdict: pass / fail  ║                               ║                                  ║                  ║                                  ║
║                  ║         ╰─────────────────╼ return verdict                ║                                  ║                  ║                                  ║
║                  ║                           ║         │                     ║                                  ║                  ║                                  ║
║                  ║                           ║   ╔═════╧══════╗             ║                                  ║                  ║                                  ║
║                  ║                           ║   ║   PASS?    ║             ║                                  ║                  ║                                  ║
║                  ║                           ║   ╚══╤══════╤══╝             ║                                  ║                  ║                                  ║
║                  ║                           ║ YES  │      │ NO             ║                                  ║                  ║                                  ║
║                  ║                           ║      │      ╰── post review comment ─────────────────────────────────────────────►  PR comment (retry context)     ║
║                  ║                           ║      │          task → FAILED ║                                  ║                  ║                                  ║
║                  ║                           ║      │          retry < 3? ────╼ re-spawn w/ feedback          ║                  ║                                  ║
║                  ║                           ║      │          (loops to step 4)                              ║                  ║                                  ║
║                  ║                           ║      │                       ║                                  ║                  ║                                  ║
║                  ║    [BLOCKED path]         ║      │  agent reports        ║                                  ║                  ║                                  ║
║                  ║                           ║      │  NEEDS_CLARIFICATION  ║                                  ║                  ║                                  ║
║                  ║                           ║      │  route to Brain ─────╼                                   ║                  ║                                  ║
║                  ║  answer_clarification     ║      │                       ║                                  ║                  ║                                  ║
║                  ║  confident? ──yes─────────╼ re-dispatch w/ Q&A injected  ║                                  ║                  ║                                  ║
║                  ║  not confident            ║      │ park: awaiting_human  ║                                  ║                  ║                                  ║
║  Dashboard ◄ SSE ╫─ task_needs_clarify        ║      │                       ║                                  ║                  ║                                  ║
║  (intervene) OR  ║                           ║      │                       ║                                  ║                  ║                                  ║
║  poll_task()     ║                           ║      │                       ║                                  ║                  ║                                  ║
║  POST /clarify ──╫───────────────────────────╼ re-dispatch w/ human answer  ║                                  ║                  ║                                  ║
║                  ║                           ║      │                       ║                                  ║                  ║                                  ║
║                  ║  poll_task() ─────────────╼ status / PR url / logs        ║                                  ║                  ║                                  ║
║                  ║  (loop or watch dashboard)║                               ║                                  ║                  ║                                  ║
║                  ║                           ║                               ║                                  ║                  ║                                  ║
║                  ║                           ║  7. squash-merge PR ──────────────────────────────────────────────────────────────►  agent branch → plan branch    ║
║                  ║                           ║     delete agent branch ───────────────────────────────────────────────────────────╼ delete agent/task-slug          ║
║                  ║                           ║     next task? → step 4       ║                                  ║                  ║                                  ║
║                  ║                           ║         │                     ║                                  ║                  ║                                  ║
║                  ║                           ║  all tasks merged?            ║                                  ║                  ║                                  ║
║                  ║                           ║  open integration PR ──────────────────────────────────────────────────────────────╼ PR: plan branch → main          ║
║                  ║                           ║         │                     ║                                  ║                  ║                                  ║
║  Dashboard ◄ SSE ╫─ plan_completed            ║         │                     ║                                  ║                  ║                                  ║
║  (notify)        ║                           ║         │                     ║                                  ║                  ║                                  ║
║                  ║  poll_task returns         ║         │                     ║                                  ║                  ║                                  ║
║                  ║  status: merged ───────────╼ confirmed                     ║                                  ║                  ║                                  ║
║                  ║                           ║                               ║                                  ║                  ║                                  ║
║  8. review PR    ║                           ║                               ║                                  ║                  ║                                  ║
║  on GitHub and   ║───────────────────────────╫───────────────────────────────╫──────────────────────────────────╫──────────────────────────────────────────────────►  ║
║  merge (or       ║                           ║                               ║                                  ║  git pull        ║  ← human approves + merges PR   ║
║  auto-merge)     ║                           ║                               ║                                  ║  (your choice,   ║                                  ║
║                  ║                           ║                               ║                                  ║  your timing)    ║                                  ║
║                  ║                           ║                               ║                                  ║                  ║                                  ║
╠══════════════════╩═══════════════════════════╩═══════════════════════════════╩══════════════════════════════════╩══════════════════╩══════════════════════════════════╣
║ KEY                                                                                                                                                                  ║
║  ────╼  push / send    ◄────  pull / fetch    ╰────  calls / invokes                                                                                                ║
║  ← SSE  server-sent event streamed to dashboard   Dashboard = monitoring + intervention surface, NOT the entry point                                                 ║
║  LOCAL GIT is NEVER touched by agents — agents clone from ORIGIN, push to ORIGIN; your checkout only changes when YOU pull after merging.                           ║
╚══════════════════════════════════════════════════════════════════════════════════════════════════════════════════════════════════════════════════════════════════════╝
```

## Notes

- **Entry point is always the Brain (Claude Code)** — the user never calls the orchestrator
  directly. The Brain reads `plan.md`, decomposes it into tasks, then calls Praxis via MCP
  (`execute_plan` / `dispatch_task`).
- **Dashboard is a monitoring surface, not the entry point** — it shows live SSE logs and
  lets you intervene (clarify blocked tasks, approve plans) but you don't start runs from it.
- **LOCAL GIT is never touched by agents** — coding agents clone exclusively from ORIGIN, push
  branches and PRs back to ORIGIN. Your local checkout only changes when you explicitly
  `git pull` after merging.
- **Two-tier branching** — `plan/{date}-{slug}` is the integration branch grouping all task
  branches; each task gets its own `agent/{task-slug}` branch that is squash-merged into the
  plan branch upon review PASS, then deleted.
- **Retry loop** — on review FAIL the task re-enters step 4 (up to 3 attempts). Each
  re-spawn includes the review comment as feedback in the agent prompt.
- **BLOCKED path** — if an agent reports `NEEDS_CLARIFICATION`, the Brain first attempts
  `answer_clarification`; if not confident it parks the task and routes an SSE event to the
  dashboard so a human can respond via `POST /clarify`.
