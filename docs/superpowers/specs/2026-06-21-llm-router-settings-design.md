# Provider-Agnostic LLM Router + Per-Call-Site Models Settings

**Date:** 2026-06-21
**Status:** Design — approved for planning
**Spec:** 3 of 3 (soft-depends on Spec 1 — see Roadmap)

## Problem

Every "brain" call in Praxis goes through `claude -p` (Anthropic CLI, subscription), and
the three reasoning call-sites collapse to a single resolved model, while two more
(`brainstorm.run_turn`, `brainstorm.generate_plan`) run `claude` with **no `--model` flag at
all**. This hard-codes the orchestrator to Claude Opus and offers no per-operation control.
Two consequences:

- No model tiering: a light re-review burns the same model as a deep first plan (violates the
  project's own `performance.md` — re-reviews should drop to Haiku).
- The project cannot be opened-source as a *general* orchestrator: another user wanting GPT,
  Gemini, or a local model as the main brain would have to edit code.

## Goal

Make Praxis a **dedicated orchestrator, not a Claude-only one**: every LLM call-site is
configurable — provider, model, and effort — with sensible defaults shown and a **Reset to
defaults** action. The owner keeps using Opus everywhere; an open-source user swaps the brain
provider without touching code.

### In scope
- A **provider-agnostic router**: each call-site resolves to `{ provider, model, effort }`.
- **CLI-based providers** (start with four): `claude`, `agy` (Gemini), `codex` (GPT),
  `aider` (local).
- **Per-call-site configuration** for every brain call-site:
  `plan_spec`, `review_diff` (first-pass + re-review tiers), `analyze_improvements`,
  `classify_doc`, `brainstorm.run_turn`, `brainstorm.generate_plan`, `context_sync`,
  `derive_tasks`.
- **Settings → Models** tab: one row per call-site (provider + model + effort), each showing
  the **default value**, plus **Reset to defaults** (per-row and all).
- **Global defaults with per-project override**, reusing the existing `effective_settings`
  resolution pattern (override → global → built-in default).

### Out of scope
- Non-CLI/HTTP provider backends beyond the four CLIs (future).
- `derive_tasks` stays a direct schema-constrained local API call (chat CLIs don't emit
  schema-constrained JSON cleanly) — it appears in the Models tab as provider `local`, but
  routes through the structured-output path, not a CLI.

## Architecture

```
  call-site ──► effective_settings.resolve(call_site, project)
                       │  override → global → default
                       ▼
                 { provider, model, effort }
                       │
        ┌──────────────┼───────────────┬───────────────┐
        ▼              ▼               ▼               ▼
     claude          agy            codex           aider
   (subscription)  (Gemini)        (GPT)           (local)
        └──────────── LLMRouter.run(prompt, cfg) ──────────┘
                       returns text  →  _extract_json (unchanged)
```

The router replaces `OpusBridge._run_claude_raw`'s hard-coded `["claude", "-p", …]` with a
provider-dispatched command builder. Rate-limit handling stays for the `claude` provider;
other providers report their own failures.

## Components

### `core/llm_router.py` (new)
- `PROVIDERS = {"claude": ..., "agy": ..., "codex": ..., "aider": ...}` — each maps a
  `{model, effort, prompt}` to an argv + output parser.
- `async def run(call_site, prompt, project_id) -> str` — resolves config via
  `effective_settings`, builds argv, runs the subprocess, returns text.
- Defaults table (`CALL_SITE_DEFAULTS`): the tier map from the model-tiering policy —
  `plan_spec`/`analyze_improvements` → claude/opus; `review_diff` first → claude/sonnet,
  re-review → claude/haiku; `classify_doc` → claude/haiku; `derive_tasks` → local; brainstorm
  + context_sync → claude/sonnet.

### `core/opus_bridge.py` (refactor)
- Each method (`plan_spec`, `review_diff`, `analyze_improvements`, `classify_doc`) calls
  `router.run(call_site, …)` instead of `_run_claude`. Keep `_extract_json` and rate-limit
  state. `review_diff` gains a `tier` arg (`first` | `rereview`) that selects the call-site.

### `core/brainstorm.py` (refactor)
- `run_turn` and `generate_plan` route through the configured provider/model instead of a
  bare `claude` invocation (fixes the no-`--model` gap).

### Settings persistence + API (`api/settings.py`, schema)
- Store per-call-site config in `config/praxis.yaml` (global) and an optional per-project
  override (project row JSON column or YAML). Read via `effective_settings`.
- `GET /api/settings/models` → current resolved config + defaults; `PUT` to update;
  `POST /api/settings/models/reset` (per-row or all).

### Frontend — Settings → Models tab (`web/index.html`)
- New tab beside Global/Project. One row per call-site: provider dropdown, model input,
  effort selector; default shown as placeholder/badge; per-row **Reset**; **Reset all**.
- A scope toggle (Global vs current Project) mirroring the existing settings tabs.

## Data Flow

1. A call-site fires → `effective_settings.resolve(call_site, project)` → `{provider, model,
   effort}`.
2. `LLMRouter.run` builds the provider argv (`claude -p --model …`, `agy …`, `codex …`,
   `aider …`) and executes.
3. Output text → existing JSON extraction → unchanged downstream logic.

## Error Handling

- Unknown/unconfigured provider → 400 from the settings API on save; router raises a clear
  error if a stored config references a missing provider.
- Provider CLI not installed / non-zero exit → surface as the call-site's existing failure
  path (e.g. review failure, plan failure), with stderr captured; for `claude`, keep the
  rate-limit detection + auto-resume.
- Reset to defaults always restores the built-in `CALL_SITE_DEFAULTS` (cannot be corrupted by
  a bad saved config).

## Testing

- **Router unit tests**: each provider builds the expected argv for a given `{model, effort}`;
  unknown provider raises; `claude` rate-limit path preserved.
- **Resolution tests**: per-call-site override → global → default precedence (mirrors existing
  `effective_settings` tests).
- **Settings API tests**: GET returns resolved + defaults; PUT persists; reset restores
  defaults (per-row and all).
- **Bridge refactor regression**: `plan_spec` / `review_diff` (both tiers) / `classify_doc`
  still produce correct JSON via the router (mocked subprocess).
- Maintain ≥ 80% coverage.

## Dependency

**Soft-depends on Spec 1** — the Models tab must include the `derive_tasks` call-site that
Spec 1 introduces. Independent of Spec 2. Merge order: 1 → 3 (2 and 3 are independent of each
other).
