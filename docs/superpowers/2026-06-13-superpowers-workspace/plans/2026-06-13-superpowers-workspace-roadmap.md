# Superpowers Workspace — Plan Roadmap

> Combined execution order for the [Superpowers Workspace epic](../specs/2026-06-13-superpowers-workspace-design.md)
> and the [Configurable Agent Model](../specs/2026-06-13-configurable-agent-model-design.md) spec.
> Each plan is self-contained; run them in this order.

| # | Plan | Unit | Why this order |
|---|------|------|----------------|
| 1 | [Dashboard layout fix](2026-06-13-plan-1-dashboard-layout-fix.md) | A | Quick visible win; unblocks all dashboard rendering |
| 2 | [Configurable agent model](2026-06-13-plan-2-configurable-agent-model.md) | — | Small; fixes `opus_bridge` `--model` gap **before** Plan 4 rewires it |
| 3 | [Docs-aware Specs & Plans views](2026-06-13-plan-3-docs-aware-views.md) | B | Establishes docs-as-source-of-truth + indexer the later plans rely on |
| 4 | [Superpowers lifecycle](2026-06-13-plan-4-superpowers-lifecycle.md) | C | Interactive Create-Spec chat, plan generation, execution checkbox sync |
| 5 | [Context Sync](2026-06-13-plan-5-context-sync.md) | D | Closes the loop; keeps CLAUDE.md/MEMORY.md fresh after execution |

## Cross-plan dependencies

```
  Plan 1 (A) ── independent
  Plan 2 (model) ── independent ──┐
  Plan 3 (B) ── needs Plan 2 (Haiku via --model) ──┐
  Plan 4 (C) ── needs Plan 3 (docs index) ─────────┤
  Plan 5 (D) ── needs Plan 4 (clone/commit helpers)┘
```

## Cross-cutting principles (all plans)

- Subscription `claude -p` + local OSS (LM Studio) only — **never** an Anthropic API key.
- Docs own spec/plan content; SQLite owns runtime state + a thin index.
- Plans are fully self-contained (each task runs in a fresh, memoryless container).
- Human review gates: spec commit, plan trigger, and context-sync approval.

## Verify-during-implementation flags

- `claude -p` reasoning-**effort** flag (Plan 2) — confirm it exists or drop effort.
- `claude -p --output-format stream-json` event **schema** (Plan 4) — confirm field shapes.
- `claude plugin install` subcommand + `superpowers` / `claude-md-management` skill names
  (Plans 4–5) — confirm against the installed CLI/plugins.
