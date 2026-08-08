# Configurations

> **One session. Many models, many harnesses. Arranged the way you work.**

Praxis splits software engineering into four seats: **plan**, **implement**,
**review**, and **verify**. This document is the reference for what can fill
each seat, where that choice lives, and which whole-loop arrangements the shipped
pieces support. "Supported" is not "verified": the arrangements below are
assembled by hand and none is a benchmarked configuration.

Two different things decide a seat, and only one of them has ever had a name in
these docs.

**Capacity** is how much a model can hold. It is what the capability profile
measures and what decomposition sizes each leaf against, so a smaller worker
gets narrower tasks.

**Aptitude** is what *kind* of judgment the seat needs. A larger model of the
same family does not necessarily fix an aptitude mismatch. The axes that matter
in practice: capability tier, modality, domain strength, tool ecosystem,
latency, privacy, availability, and plain preference.

Praxis has always routed on both. Every worked example in the docs happened to
be a cost-tier example, which is why the second dimension was easy to miss.

## What is adjustable

| Knob | Where it lives | Granularity |
|---|---|---|
| Harness | `projects.harness` | per project |
| Harness | `harness` on `dispatch_task` / `execute_plan` | per call |
| Worker model | `projects.model_name` | per project |
| Worker model | `model` on `dispatch_task` / `execute_plan` (required) | per call |
| Brain model (legacy) | `projects.agent_model` | per project, but ignored whenever a role chain resolves the call-site |
| Provider, model, effort | `settings_overrides` key `models.<call_site>` | per call-site, global; applies only when the role declares no chain |
| Model registry and role chains | `models.registry`, `models.roles` in `config/praxis.yaml` | per role |
| Implement escalation ladder | `implement_escalation` in `config/praxis.yaml` | global |
| Worker preset | `worker_presets` in `config/praxis.yaml` | chosen at `praxis init` |
| Default delegated worker | `default_worker_harness`, `default_worker_model` | global |
| Auto-delegate mode | `settings_overrides` key `auto_delegate.enabled` | global |
| Verify command | `projects.verify_cmd` | per project |
| Merge gate or auto-merge | `projects.auto_merge` | per project; protected branches never auto-merge |
| Git backend | project `repo_url` plus `allow_local_repo_paths` | per project, admitted globally |
| Worker endpoint | `LM_STUDIO_URL` | global; it also repoints every router call-site that resolves to the `local` provider |
| Retry and leaf bounds | `max_retries` (per project), `max_leaves_per_plan` (global) | per project or global |

`config/praxis.yaml` is **mounted, not baked**, so editing it takes effect on
`docker compose restart orchestrator` and never needs an image rebuild.

## Three levels of "many"

### Level 1: you choose, per call

`dispatch_task` and `execute_plan` each take a `model` and an optional
`harness`. From a single assistant session you can send one task to one harness
and the next task to another, deliberately, per call.

This is the level that matters if your assistant is locked to one vendor's
models. Every vendor builds it that way, in every direction. Praxis is the seam
that crosses it, and crossing it is a tool argument rather than a second IDE.

### Level 2: Praxis chooses, per call-site

Every brain call in the loop is a named *call-site*: planning a spec, reviewing
a diff, re-reviewing after fixes, deciding what to improve next. Call-sites map
to roles (`plan`, `review`, `implement`), and roles map to ordered chains in
`models.roles`.

A chain falls through to its next entry only when a provider is **unavailable**
(auth failure, rate limit, gateway error). A model that answers badly is not a
fallback trigger; a model that cannot answer at all is.

What the shipped chains actually do: `plan` is `[sonnet, opus]` and `review` is
`[sonnet, haiku]`, so a **default install resolves every routed call-site to the
same mid-tier model** and holds the second entry in reserve as a fallback.
Per-call-site tiering, a cheaper model for re-review and a frontier model for the
improvement loop, lives in `CALL_SITE_DEFAULTS` and applies **only when a role
declares no chain**. Editing `models.roles` is how you change the routing.

The same shadowing governs overrides. **Settings, Models** in the dashboard and
`GET`/`PUT /api/settings/models` write a per-call-site override that takes effect
only when that call-site's role has no chain in `models.roles`. With the shipped
chains in place the override is stored and ignored, so clear the role chain
first.

### Level 3: Praxis re-chooses, on failure

When a leaf fails on capability, the next dispatch reads the next rung of
`implement_escalation`. A rung is a `(harness, model)` pair, not just a model,
so the ladder can move work to a different harness entirely.

The implement seat cannot use the role fallback chains from Level 2, because the
worker model is baked when the container is created. Escalation is a
dispatch-time substitution instead. That is why `implement` looks different from
`plan` and `review` throughout this document.

## Harnesses

The harness is what actually edits code inside the worker container. It is a
seat like any other.

<!-- BEGIN harness-list -->

| Harness | Name | Notes |
|---|---|---|
| `opencode` | OpenCode | The default. |
| `agy` | Antigravity (Gemini) | Auth is a login-seeded Docker volume, not a host credential file. |

<!-- END harness-list -->

Two harnesses ship. Adding a third is not purely a registry entry: `spawn_agent`
branches on harness id in three places (context detection, credential volume,
session volume), and each harness carries its own Dockerfile and entrypoint. See
[Ceilings](#ceilings).

## Worker presets

A preset names a `(harness, model, endpoint)` triple so that choosing how your
worker runs is one decision instead of three that must agree. Presets are
declared in `worker_presets` in `config/praxis.yaml` and served by
`GET /api/settings/presets`.

<!-- BEGIN worker-presets -->

| Preset | Runs on | Requires |
|---|---|---|
| `hosted-openweight` | An open-weight model on a hosted OpenAI-compatible endpoint | An API key |
| `local-lmstudio` | An open-weight model on your own GPU via LM Studio | Nothing beyond LM Studio running |
| `gemini-agy` | Gemini through the agy harness | A one-time interactive login |

<!-- END worker-presets -->

`praxis init` prints this menu during setup. It defaults to the first preset
whose requirements it can satisfy on its own, so the default is never a preset
that cannot work, and choosing one with an unmet requirement is an explicit
confirmation rather than a silent misconfiguration. The chosen preset writes
`LM_STUDIO_URL`, `DEFAULT_WORKER_HARNESS`, and `DEFAULT_WORKER_MODEL` together.

Re-running `praxis init` to switch presets is safe. It merges only the keys it
manages and preserves every other key, position, and comment in your `.env`.

## Arrangements

A preset arranges the implement seat. An **arrangement** is a whole-loop
configuration: which preset you start from, plus the role chains and gates that
go with it. Arrangements are assembled by hand today.

Each one below names two or more possible fillings on purpose. None of these is
the blessed configuration.

### Subscription brain, open-weight worker

The reference configuration. Judgment-heavy seats run on a capable hosted model
driven through its CLI on a flat-rate subscription; the token-heavy implement
seat runs on an open-weight model you serve yourself.

- Worker preset: `local-lmstudio` or `hosted-openweight`
- `models.roles`: `plan` and `review` on hosted chains, `implement` on `local`
- Merge gate: on, which is the default
- Possible fillings: brain on Claude or on Codex; worker on a local open-weight
  model via LM Studio or on a hosted open-weight endpoint

### Cross-vendor

The brain is one vendor and the hands are another, because the seats want
different things.

- Worker preset: `gemini-agy`, or any preset whose harness differs from the
  brain's vendor
- `default_worker_harness` follows the preset
- Possible fillings: brain on Claude with the worker on agy; brain on Codex with
  the worker on OpenCode driving an open-weight model

### Single-vendor

Every model-driven seat on one provider, usually for billing or policy reasons.

- `models.registry` declares only that provider's models and every chain in
  `models.roles` uses them
- Worker preset: whichever preset drives a harness that vendor supports
- Note: the implement seat is spawn-baked, so single-vendor means the *harness*
  too, not only the model name
- **Not satisfiable today for most vendors.** The two shipped harnesses are
  `opencode`, which drives an OpenAI-compatible endpoint, and `agy`, which is
  Gemini-only. Neither drives Claude or GPT, so "every seat on one vendor" is
  reachable only in the open-weight case. Listed because it is the arrangement
  people ask for, and the honest answer is that a third harness is what would
  unlock it.

### Fully local

No hosted dependency for any model-driven seat.

- Worker preset: `local-lmstudio`
- `models.roles`: every chain resolves to `local`
- Honest caveat: this is the weakest arrangement. Planning and reviewing on a
  small local model removes the judgment the role split exists to buy. It is a
  legitimate privacy or air-gap choice, not a quality-neutral one.

### Evaluate with no GitHub credential

Run the whole loop against a local bare repository, with no GitHub account
involved at all.

- Set `allow_local_repo_paths: true` in `config/praxis.yaml`
- Give the project a filesystem path as its `repo_url`; the repo must be **bare**
- Answer `skip` at the GitHub-token prompt on a fresh install. On a re-run where
  `.env` already holds a `GITHUB_TOKEN`, that prompt does not offer `skip` and a
  blank answer KEEPS the existing token; delete the `GITHUB_TOKEN` line from
  `.env` instead
- The git backend resolves to the local one, so there is no PR object and no
  credential setup; the merge gate and verify gates behave the same way

This admission is off by default because it lets an authenticated caller point
the orchestrator at any path the container can reach.

## Ceilings

Stated plainly rather than buried.

1. **"Many harnesses" is two.** Praxis does not compete on harness breadth, and
   the seam is not as clean as a registry implies: `spawn_agent` carries three
   literal harness-id branches, so a third harness means edits inside it.
2. **The harness contract is not written down yet.** Two harnesses ship and the
   seam lives in code. "Add your own harness" is not yet a promise this project
   has earned.
3. **Cross-harness escalation has not been observed live, though the default
   configuration performs one.** The mechanism takes a `(harness, model)` pair.
   The shipped ladder uses `opencode` on both rungs, but the shipped
   `default_worker_harness` is `agy`, so the first escalation in a default
   install is already an `agy` to `opencode` move. The capability is real and the
   default exercises it; a verified run is not yet on record.
4. **Every seat consumes text.** The reviewer reads a diff, the verifier reads
   an exit code, the planner reads markdown. Nothing here can look at a rendered
   artifact, so Praxis cannot tell you the layout it just shipped is broken.
5. **Presets arrange one seat, and leak into another.** `worker_presets` is
   meant to cover the implement seat, and nothing in first-run setup asks about
   the brain seat, the review seat, the merge gate, or `verify_cmd`; the
   arrangements above are assembled by hand. But one of the three keys a preset
   writes, `LM_STUDIO_URL`, is global, so choosing a preset also repoints every
   router call-site that resolves to the `local` provider. A preset is not as
   contained as its name suggests.
