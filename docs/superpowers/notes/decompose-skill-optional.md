# Optional (deferred): `decompose` client skill

**Status:** deferred — not scheduled. Capture only. Revisit anytime.

## Idea

A client-side `SKILL.md` (`decompose`) that auto-surfaces in a skill-aware harness
(Claude Code, Codex, Gemini/Antigravity) and drives the Praxis decomposition flow
without the operator having to remember the tool sequence.

Intended behavior:

1. Resolve the target worker model + harness for the repo:
   - read it from Praxis via the read-back MCP tool (`get_project`/`list_projects`),
   - fall back to asking the user only when the project is unconfigured.
2. Call the existing `execute_plan` MCP tool with that model.
3. `poll_plan` and report the resulting leaves / capability flags.

## Why it is optional (not in the base spec)

- The **load-bearing logic** ships through the MCP server (the `get_project` read-back
  tool + the `praxis://guide/orchestration` resource). Every Praxis-as-MCP user gets
  that automatically, with zero install.
- The skill is an **opt-in accelerator**: it only reaches developers who separately
  install it into their harness, and each harness discovers skills slightly
  differently. It auto-surfaces (nice), but it is not guaranteed reach.
- If built, it must **absorb** the decompose guidance from
  `praxis://guide/orchestration` rather than duplicate it, or the two will drift.

## When to build it

Build only if/when auto-surfacing in a specific harness becomes worth the per-harness
install + maintenance cost. Until then the MCP resource guide covers the same workflow
for all clients.

## Related audit item

The broader "which other functions become skills" question is reframed in the base spec
as "which functions need read-back / better MCP-resource guidance" (portable vehicle).
Any function that turns out to genuinely benefit from auto-surfacing can be added here as
a future optional skill.
