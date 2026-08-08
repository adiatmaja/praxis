# Configurations

> **One session. Many models, many harnesses. Arranged the way you work.**

Praxis splits software engineering into four seats: **plan**, **implement**,
**review**, and **verify**. This document is the reference for what can fill
each seat, where that choice lives, and which whole-loop arrangements are known
to work.

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
| Worker model | `projects.model_name`, `agent_model` | per project |
| Worker model | `model` on `dispatch_task` / `execute_plan` (required) | per call |
| Provider, model, effort | `settings_overrides` key `models.<call_site>` | per call-site, global or per project |
| Model registry and role chains | `models.registry`, `models.roles` in `config/praxis.yaml` | per role |
| Implement escalation ladder | `implement_escalation` in `config/praxis.yaml` | global |
| Worker preset | `worker_presets` in `config/praxis.yaml` | chosen at `praxis init` |
| Default delegated worker | `default_worker_harness`, `default_worker_model` | global |
| Auto-delegate mode | `settings_overrides` key `auto_delegate.enabled` | global |
| Verify command | `projects.verify_cmd` | per project |
| Merge gate or auto-merge | `projects.auto_merge` | per project; protected branches never auto-merge |
| Git backend | project `repo_url` plus `allow_local_repo_paths` | per project, admitted globally |
| Worker endpoint | `LM_STUDIO_URL` | global or per project |
| Retry and loop bounds | `max_retries`, `max_improvement_cycles`, `max_leaves_per_plan` | per project or global |

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

A single plan run already uses several models without anyone configuring
anything. Planning and first-pass review resolve to a mid-tier model, re-review
drops to a cheaper one, task derivation runs on a local model, and the
open-ended improvement loop reaches for a frontier model.

Each of those is a *call-site*. Call-sites map to roles, roles map to ordered
chains in `models.roles`, and a chain falls through to the next entry only when
a provider is unavailable (auth, rate limit, gateway). A model that answers
badly is not a fallback trigger; a model that cannot answer at all is.

Override any single call-site in **Settings, Models** in the dashboard, or over
`GET`/`PUT /api/settings/models`.

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

Two harnesses ship. The seam is general and the population is small; see
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
- Answer `skip` when `praxis init` asks for a GitHub token
- The git backend resolves to the local one, so there is no PR object and no
  credential setup; the merge gate and verify gates behave the same way

This admission is off by default because it lets an authenticated caller point
the orchestrator at any path the container can reach.

## Ceilings

Stated plainly rather than buried.

1. **"Many harnesses" is two.** The seam is general; the population is not
   large. Praxis does not compete on harness breadth.
2. **The harness contract is not written down yet.** Two harnesses ship and the
   seam lives in code. "Add your own harness" is not yet a promise this project
   has earned.
3. **Cross-harness escalation has not been observed live.** The mechanism takes
   a `(harness, model)` pair and the shipped default ladder happens to use one
   harness on both rungs. The capability is real; a verified run is not yet on
   record.
4. **Every seat consumes text.** The reviewer reads a diff, the verifier reads
   an exit code, the planner reads markdown. Nothing here can look at a rendered
   artifact, so Praxis cannot tell you the layout it just shipped is broken.
5. **Presets arrange one seat, not the arrangement.** `worker_presets` covers
   the implement seat. Nothing in first-run setup asks about the brain seat, the
   review seat, the merge gate, or `verify_cmd`; the arrangements above are
   assembled by hand.
