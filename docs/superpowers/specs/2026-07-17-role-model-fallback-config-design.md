# Role-Model Fallback Config — Design

- **Date:** 2026-07-17
- **Status:** Draft (approved in brainstorming, pending spec review)
- **Topic:** User-facing configuration of a model registry, per-role priority
  fallback chains, and use-case-relevant capability estimates, exposed via
  onboarding, `praxis config`, and the dashboard.

## Problem

Praxis routes brain calls at 11 fine-grained **call-sites**
(`CALL_SITE_DEFAULTS` in `core/llm_router.py`), each resolving to exactly one
`{provider, model, effort}`. There are three gaps:

1. **No fallback.** If the primary model is rate-limited or its provider is
   unauthenticated/down, the call fails (or, for the `claude` brain, the whole
   loop pauses 5h via `opus_bridge`). There is no "try the next model" path.
2. **No model catalog.** Models are free-text strings scattered across
   call-site defaults and overrides. There is no single "what models are
   registered on Praxis" view to manage.
3. **No capability signal.** Operators pick models blind — there is no
   at-a-glance estimate of how a model performs on the software-engineering
   work Praxis actually does.

This design adds a **model registry**, **per-role ordered fallback chains**, and
a **bundled, use-case-relevant capability snapshot**, surfaced through the CLI
(`praxis config`), a first-run onboarding prompt, and the dashboard — for
consistency across every entry point.

## Goals

- Register named models once; reference them everywhere by name.
- Configure each model-bearing role with an ordered fallback list (first =
  priority); on unavailability, automatically try the next.
- Show coding-relevant capability estimates (SWE-bench Verified, agentic
  coding, speed, price) next to each registered model, sourced offline from a
  bundled snapshot of artificialanalysis.ai data.
- Expose all of the above via API, CLI, onboarding, and dashboard.
- Change nothing for existing installs until they opt in (YAML defaults derived
  from today's `CALL_SITE_DEFAULTS`).

## Non-Goals

- **Implement-role fallback chain in v1.** The worker (`implement`) model is
  baked into the container env at spawn (`agent_model`), not routed through
  `LLMRouter`. v1 gives `implement` the registry + capability display and a
  single selected model; wiring its fallback chain through the spawn path is a
  follow-up.
- Live/runtime dependency on any external capability API.
- Per-project role chains (global only for v1; the resolution seam leaves room
  for a per-project layer later, mirroring existing override patterns).

## Decisions (from brainstorming)

| Fork | Decision |
|------|----------|
| Config granularity | **Role-level** (plan / implement / review). `verify` is a deterministic shell gate, no model. |
| Fallback trigger | **Unavailability only** — rate limit, `ProviderAuthError`, provider/gateway error (5xx/403/429). Bad output / task failure does NOT fall back. |
| Registry relationship | **Registry is source of truth**; role chains reference models by name. |
| Capability data source | **Bundled snapshot**, manually refreshable; offline-first, no runtime dependency. |
| Capability metric focus | **Use-case-relevant**: SWE-bench Verified, agentic-coding/tool-use, speed, price — not generic "intelligence". |

## Data Model & Storage

Follows the existing layering: git-tracked YAML defaults in `config/praxis.yaml`
+ runtime overrides in `settings_overrides` (JSON), resolved by
`EffectiveSettings`.

### a) Model registry

Named catalog. Each entry:

```json
{ "name": "opus", "provider": "claude", "model": "claude-opus-4-8", "effort": "high" }
```

- Defaults: `config/praxis.yaml` under `models.registry:`.
- Runtime overrides: `settings_overrides` key `registry` (JSON array).
- `name` is the stable key referenced by role chains; `provider` must be a
  known router provider (`claude` / `codex` / `agy` / `local`).

### b) Role → fallback chain

Three model-bearing roles: `plan`, `implement`, `review`. Each maps to an
**ordered list of registry names**, first = priority:

```yaml
plan:      [opus, sonnet]
implement: [local-qwen, sonnet]
review:    [sonnet, haiku]
```

- Defaults: `config/praxis.yaml` under `models.roles:`.
- Runtime overrides: `settings_overrides` key `roles` (JSON object).
- Each chain must have ≥1 entry; every name must exist in the registry.

### c) Capability snapshot

Bundled read-only reference data at `config/model_capabilities.json`, keyed by
**model id** (not registry name, so multiple registry entries for the same
model share one capability row):

```json
{
  "claude-opus-4-8": {
    "swe_bench_verified": 0.79,
    "agentic_coding": "high",
    "speed_tps": 62,
    "price_per_mtok_blended": 15.0,
    "source": "artificialanalysis.ai",
    "as_of": "2026-07-17"
  }
}
```

Advisory only. A model with no entry renders "no data", never an error.

### Call-site → role mapping

The 11 call-sites keep working; each maps to one role internally. Frozen in a
`ROLE_OF_CALL_SITE` table (golden-tested like `core/status_vocab.py`, so adding
a call-site forces a role assignment):

| Role | Call-sites |
|------|-----------|
| `plan` | `plan_spec`, `plan_review`, `derive_tasks`, `analyze_improvements`, `answer_clarification`, `classify_doc`, `context_sync`, `brainstorm_run_turn`, `brainstorm_generate_plan` |
| `review` | `review_diff_first`, `review_diff_rereview` |
| `implement` | worker model (`agent_model`; not router-driven in v1) |

`analyze_improvements` is the one open-ended reasoning seat; it stays on `plan`
but its own call-site override (below) can still pin it to opus/high.
`CALL_SITE_DEFAULTS` remains the final fallback when a role chain is not
configured, so nothing breaks for existing users.

## Fallback Execution (router)

`LLMRouter.run(call_site, ...)` changes from "resolve one config" to "resolve an
ordered chain, try each until one is available":

1. `call_site → role`; resolve the role's chain to a list of
   `{provider, model, effort}` via the registry. **Empty chain →** today's
   `CALL_SITE_DEFAULTS[call_site]` (single entry), so behavior is unchanged for
   anyone who never configures this.
2. Try entry #1. On an **unavailability** error, try #2, then #3…
   - Unavailability = `ProviderAuthError`, rate-limit signatures (the
     `opus_bridge` patterns + provider 429), or a provider/gateway error
     (5xx / 403 / 429). Reuse `ReconcileMixin.is_provider_error`, lifted to a
     shared helper (e.g. `core/provider_errors.py`).
   - `ProviderOutputError`, malformed JSON, and non-availability `RuntimeError`
     are **not** fallback triggers — they propagate immediately, preserving
     worker failure attribution.
3. Emit an SSE `model_fallback` event (e.g. "plan: opus rate-limited → trying
   sonnet") so dashboard/CLI show why a non-primary model ran.
4. **Chain exhausted** (every entry unavailable): raise. The existing
   `opus_bridge` 5h rate-limit pause remains the final backstop when the
   exhausted chain's failures were rate limits, so autonomous loops still
   self-heal after the window.

Precedence for a call-site's effective config:
**role chain → per-call-site override (`models.<call_site>`) → `CALL_SITE_DEFAULTS`.**

## Surfaces

### API (extends the `settings` router)

- `GET/PUT /api/settings/registry` — list / upsert / delete registered models.
- `GET/PUT /api/settings/roles` — get / set each role's ordered chain
  (validates every name exists in the registry; rejects empty chains).
- `GET /api/settings/capabilities` — bundled snapshot joined onto registry
  entries.
- `POST /api/settings/capabilities/refresh` — optional re-pull; no-op with a
  clear message when no network/key.

### CLI

Add a `praxis` console script (alias of the same Typer app) with a `config`
sub-app:

- `praxis config show` — rich tables: registry + role chains + capability badges.
- `praxis config` (no args) — interactive menu: add/remove model, reorder a
  role's chain, view capabilities.
- Non-interactive escape hatches: `praxis config add-model …`,
  `praxis config set-role plan opus,sonnet`,
  `praxis config refresh-capabilities`.

### Onboarding

On first run when the `registry` override is absent, the CLI prints
"No models configured yet — run `praxis config` to set up your roles" and offers
to launch the wizard inline. The dashboard shows a one-time banner linking to
the config screen. Detection = registry override absent; YAML defaults still
seed a working baseline so Praxis runs before any onboarding.

### Dashboard

Grow **Settings → Models** into **Models & Roles**:

- *Registered Models* table: name, provider, model, effort, capability badges
  (SWE-bench %, speed, price), edit/delete, "Add model".
- *Roles* section: 3 rows (plan/implement/review), each an ordered,
  drag-to-reorder chip list drawn from the registry, with a fallback-order hint.
- Capability "Refresh" button + `as_of` date.

## Error Handling

- `PUT /roles`: 422 on a chain naming an unregistered model or an empty chain.
- Deleting a registry model referenced by a chain: 409 listing referencing
  roles; force-delete removes it from chains.
- Capability snapshot advisory: missing entry → "no data", never an error.
- `refresh-capabilities` with no network/key: soft-fail, keep existing snapshot,
  return a message.

## Migration / Back-Compat

- No schema change — reuse `settings_overrides` (keys `registry`, `roles`) as
  JSON blobs, consistent with existing `models.<call_site>` overrides.
- Ships YAML defaults derived from today's `CALL_SITE_DEFAULTS`
  (opus / sonnet / haiku / local), so existing installs behave identically until
  edited.
- The old `GET/PUT /api/settings/models` (per-call-site) stays as a power-user
  layer; role chains resolve first, call-site override second,
  `CALL_SITE_DEFAULTS` last.

## Testing (TDD, 80%+)

- **Router:** chain resolution; fallback on each unavailability class;
  no-fallback on bad-output; exhaustion → raise + pause backstop; empty-chain →
  legacy default; `model_fallback` event emitted.
- **API:** registry CRUD; role validation (unknown name, empty chain,
  delete-in-use 409); capability join.
- **CLI:** `config show` render; `set-role` parse; empty-registry onboarding
  gate.
- **Capability loader:** missing file; missing model entry; refresh soft-fail.
- **Golden:** `ROLE_OF_CALL_SITE` frozen — adding a call-site without a role
  assignment fails the exhaustiveness test.

## Open Follow-Ups (explicitly deferred)

- Implement-role fallback through the spawn path.
- Per-project role chains.
- Automated capability refresh (scheduled / CI) once a data source is settled.
