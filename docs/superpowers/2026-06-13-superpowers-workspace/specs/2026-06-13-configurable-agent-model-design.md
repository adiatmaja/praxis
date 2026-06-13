# Praxis Configurable Agent Model

**Date**: 2026-06-13
**Status**: Approved
**Scope**: `src/orchestrator/config.py`, `core/opus_bridge.py`, `database.py`,
`api/projects.py`, `web/index.html`

## Big Picture

Today every Claude reasoning call hard-codes nothing — `opus_bridge._run_claude_raw()` runs
`claude -p <prompt>` without a `--model` flag, so it silently uses `claude`'s default, and the
`agent_model_name` setting in `config.py` (stale at `claude-opus-4-6`) is never applied. This
feature lets the user choose the Claude reasoning model and effort, per-project with a global
default, and actually wires that choice into the CLI invocation.

## Features

- **Per-project model + global default** — each project picks a model; unset falls back to a
  global default in `config.py`.
- **Governs all reasoning calls** — planning, review, brainstorming, and Context Sync use the
  configured model. Docs-classification stays pinned to Haiku.
- **Preset menu + custom selector** — presets (Opus, Opus low-effort, Sonnet, Haiku) plus a
  free-form model id field, in project settings and as a global default.
- **Wire `--model` into `claude -p`** — fix the latent gap so the setting is no longer inert.

## Data Model

Add to `projects`: `agent_model TEXT NULL`, `agent_model_effort TEXT NULL`. Null means
"use global default". Inline `ALTER TABLE` migration (no ORM), consistent with existing
migration style.

## Config

`config.py` gains `agent_model: str = "claude-opus-4-8"` (replacing the stale
`agent_model_name = "claude-opus-4-6"`) and `agent_model_effort: str | None = None`. These are
the global fallback.

## Invocation

`opus_bridge` resolves the model per call: **project setting → global default**, then passes
`--model <resolved>` (and the effort flag, if applicable) to `claude -p`. A small helper
resolves `(model, effort)` for a given project id. All reasoning entry points (plan, review,
brainstorm relay, context-sync) route through it. Docs-classification ignores it and stays on
`claude-haiku-4-5`.

**Open implementation detail:** the `--model` flag is certain; the exact `claude -p` flag (or
model alias) for *reasoning effort* must be verified during implementation. If the CLI has no
effort flag, presets collapse to model ids and `agent_model_effort` is dropped.

## API

`GET /api/projects/{id}` returns `agent_model` / `agent_model_effort`. The existing project
create/update endpoints accept them. A global default is read from config (exposed via
`/api/status` or a settings endpoint for the UI selector).

## UI

Project settings form gains a model selector: a preset dropdown mapping labels to
`(model_id, effort)` pairs, plus a free-form "custom model id" field. A global-default
selector lives in the app settings area. Selected model is shown on the project/plan context
so the user always knows which model is reasoning.

## Acceptance

- A project with `agent_model` set causes `claude -p --model <that>` to be invoked for
  planning/review/brainstorming/context-sync.
- Unset project falls back to the global default.
- The global default is `claude-opus-4-8` (no longer the stale 4-6 id).
- Docs-classification still uses Haiku regardless of the setting.
- Preset and custom selections both round-trip through the API and persist.

## Out of Scope

- The implement-step model (`projects.model_name` via LM Studio) — unchanged.
- Anthropic API-key access — forbidden (see LLM access policy).
