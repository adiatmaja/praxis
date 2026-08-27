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
| `local-lmstudio` | An open-weight model on your own GPU via LM Studio | Nothing beyond LM Studio running |
| `gemini-agy` | Gemini through the agy harness | A one-time interactive login |

<!-- END worker-presets -->

`praxis init` prints this menu during setup, and picks its default by three
rules in order (`_default_preset_choice`), printing which rule fired next to the
choice:

1. **A preset flagged `default: true` in the settings file wins outright.** That
   flag is the deployment saying "I have already done this preset's one-time
   setup", which no other rule can see: an interactive login leaves no evidence
   in the YAML. The shipped file flags `gemini-agy`, which needs exactly such a
   login, so on a stock install that is the offered default.
2. Otherwise, the first preset needing no credential, so holding Enter through
   every prompt cannot land on something `init` could not have configured.
3. Otherwise the first entry on the menu.

The flag changes what is OFFERED, never what is CHECKED. An unmet requirement is
still challenged before the choice is accepted (and non-interactive refuses it
unless `--accept-preset-requirements` says the setup is done), so choosing a
preset you have not set up is an explicit confirmation rather than a silent
misconfiguration. The chosen preset writes
`LM_STUDIO_URL`, `DEFAULT_WORKER_HARNESS`, and `DEFAULT_WORKER_MODEL` together.

Re-running `praxis init` to switch presets is safe. It merges only the keys it
manages and preserves every other key, position, and comment in your `.env`.

Unattended, pass the preset by NAME rather than driving the menu:

```bash
uv run praxis init --non-interactive --preset gemini-agy
```

The name is stable; the menu position is not, and shifts whenever this table
gains or reorders a row. `--non-interactive` does not relax the requirements
check: a preset with an unmet requirement is refused until you pass
`--accept-preset-requirements`, which is the same assertion the interactive
"Choose it anyway?" makes.

## Recommended defaults by capability tier

Configure each seat by the *capability class* it needs, not a specific model;
models churn, tiers don't. Praxis always starts from an existing spec or
`plan.md`, so there is no "write the spec" seat; planning means turning that
artifact into a task graph. Only the autonomous improvement loop reasons
open-ended (it decides what to build next with no human artifact), so it is the
one seat that wants a Frontier model; planning and review are structured jobs a
High-tier model handles, and re-review drops to Low. Example models are as of
July 2026.

| Tier | Fills which seat | Claude | OpenAI · Codex | Gemini | Open-weight (hosted or local) |
|------|------------------|--------|----------------|--------|-------------------------------|
| **Frontier** | Autonomous improve loop | Opus 4.8 · Fable 5 | GPT-5.6 Sol | Gemini 3.1 Pro | GLM-5.2 · DeepSeek V4-Pro |
| **High** | Plan · Implement · Review (first pass) | Sonnet 4.6 | GPT-5.6 Terra | Gemini 3.5 Flash | GLM-5.2 |
| **Medium** | Implement (tightly-scoped leaves) | Haiku 4.5 | GPT-5.6 Luna | Gemini 3.5 Flash | Qwen3.6-27B |
| **Low** | Review (re-review) | Haiku 4.5 | GPT-5.6 Luna | Gemini 3.5 Flash | small local model |
| **none** | Verify | deterministic shell command: no model, any column ||||

**Open-weight is not the same as local:** GLM-5.2 and DeepSeek V4-Pro are
Frontier-class open-weight models you can serve hosted (e.g. [z.ai](https://z.ai/))
or locally (LM Studio · Ollama); a small local model is the cost floor for the
implement and review seats.

### Field notes on specific providers

These are observations that churn with model releases, not architecture; the
seats accept any provider. **Gemini: worker seats, not the planner.** Claude,
GPT/Codex, and Frontier-class open-weight models are the strong choices for
planning and the autonomous improve loop. Gemini is the exception to reach for
elsewhere: in practice it plans poorly, but its mid-tier models (Gemini 3.5
Flash) are moderately capable at scoped implementation, review, and everything
that is not the plan itself, trading higher token usage for that reach. So
point Gemini at Implement / Review / Verify and keep Plan and Improve on
Claude, GPT, or a frontier model. As an implementer Gemini drives the
Antigravity coding agent as a harness, which is the seat it is wired into here.
As a **brain** it is currently not usable at all: the `agy` CLI's `--print` only
renders to an interactive TTY and yields no capturable stdout otherwise, so a
routed call raises `ProviderOutputError`. Treat the Gemini-as-brain guidance
above as what to do once that is resolved, not as something you can select
today. See [gotchas.md](gotchas.md) and the provider table in
[deployment.md](deployment.md).

Field observation from daily use: Claude is excellent at workflow and systems
reasoning but has weak visual judgment, including when asked to *repair* an
existing interface, where Gemini is noticeably stronger. Because the harness
and model are set per project (and per call-site under **Settings → Models**),
you can point the implement seat at Gemini via the `agy` harness for the UI
work and keep planning and review on Claude, without changing anything else
about the loop. The same lever applies to any split you find: a language, a
framework, a codebase a particular model knows well.

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

- Worker preset: `local-lmstudio`, or a hosted OpenAI-compatible endpoint you
  configure yourself and have verified serves the model string you name
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

**The path has to resolve identically in TWO namespaces, not one.**
`core/preflight._preflight_local` checks the path with `Path.exists()` inside
the orchestrator container. `core/agent_manager.local_repo_volume` then hands
that same string to the Docker daemon as a bind-mount **source**, which the
daemon resolves in the HOST (or, on Docker Desktop, the Linux VM) namespace,
not the orchestrator's. A plain host path that only makes sense on one side
(`C:/Users/you/repos/x.git` is invisible inside the orchestrator container;
`/app/repos/x.git` means nothing to the daemon, which creates an empty
directory there instead of finding the repo) satisfies neither.

**An identity mount is nonetheless enough, on Linux and on Docker Desktop
alike.** On Linux those two namespaces are the same filesystem, so a plain
absolute path satisfies both. On Docker Desktop, `/run/desktop/mnt/host/
<drive>/...` is valid simultaneously as a daemon bind source AND as a path
the orchestrator container can see (verified against Docker 29.6.1 / Compose
v5.3.0, both as a direct bind mount and through compose) -- so the same
single path works there too. Set one variable:

| Variable | Namespace | Linux | Docker Desktop for Windows |
|----------|-----------|-------|-----------------------------|
| `LOCAL_REPOS_PATH` | what the **orchestrator** mounts and sees; also the required prefix for every local project's `repo_url` | `/home/you/repos` | `/run/desktop/mnt/host/c/Users/you/repos` |

`LOCAL_REPOS_HOST_PATH` is the escape hatch, not the normal path: compose
defaults it to `LOCAL_REPOS_PATH`'s value, so set it separately only when the
Docker daemon's bind-mount **source** must be a different string from what the
orchestrator sees (for example, a Windows path you would rather hand the
daemon directly than resolve through the VM share prefix).

Set the variable(s) in `.env`, then run `docker compose up -d` (never
`restart`) to pick up the changed bind mount -- a mount is baked in at
container CREATE, and this is one of the keys `env_drift` cannot see drift
in, since it is never forwarded into the container's own environment. On
Docker Desktop the `/run/desktop/mnt/host/<drive>/...` prefix is the VM's
share path onto the Windows filesystem; a project's `repo_url` must be given
under that prefix, because that is the path the orchestrator (and the
preflight check) actually resolves.

**This configuration is exercised on Linux; Docker Desktop for Windows is a
less-travelled path for it.** A first real user hit the namespace split
exactly as described above (field report, 2026-08-25) before either variable
existed; treat a fresh Windows report against this area as plausible even
after this fix ships.

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
