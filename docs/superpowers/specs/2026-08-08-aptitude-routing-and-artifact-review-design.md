# Aptitude Routing, Configuration Surface, and the Artifact Review Seat

- **Date:** 2026-08-08
- **Status:** PROPOSED
- **Scope:** documentation and framing changes (build now) + two parked feature
  designs, F17 and F18 (spec only, no plan, no code)
- **Relates to:** `docs/superpowers/specs/2026-07-11-capability-engine-roadmap.md`
  (canonical roadmap; F16 is the last existing feature number, so this spec
  claims F17 and F18)

---

## 1. The observed problem

The trigger, in the user's own account: an assistant was asked to get a second
opinion on a visual design question and answered that it could not, because the
other tool was "a separate IDE with no MCP bridge into this session, so there's
no tool I can call." The fallbacks offered were a manual screenshot script for a
human to carry across, or a same-vendor subagent that, in the assistant's own
words, "shares my blind spots."

Two distinct problems are tangled there, and they separate cleanly.

**Problem 1: models are good at different kinds of work, and the docs only
describe one kind.** The assistant was strong at code reasoning and weak at
judging rendered output. That is not a capability *tier* difference; a larger
model of the same family does not necessarily fix it. It is a difference in kind.

**Problem 2: an assistant locked to one vendor cannot reach another vendor's
model, and this is by design in every vendor's product, not a bug in one of
them.** The moment a different kind of judgment is wanted, the developer is
copy-pasting between IDEs by hand.

Praxis already solves problem 2 comprehensively, and says so only in a
Claude-specific way that reads as a single blessed vendor pairing. Praxis does
not solve problem 1 at all for non-text artifacts, and never admits it.

---

## 2. The conceptual move: capacity and aptitude

Praxis's documentation sells exactly one dimension of capability: how much a
worker can hold. Context window keyed on parameter count is what
`capability_profile` returns, and it is what decomposition sizes leaves against.
Call that dimension **capacity**.

The second dimension has no name in the docs and therefore no presence in a
reader's mental model: what *kind* of judgment a seat needs. Call that
**aptitude**. Axes include capability tier, modality, domain strength, tool
ecosystem, latency, privacy, availability, and plain preference.

The important observation is that **the seat abstraction already supports
aptitude routing today.** Per-call-site `{provider, model, effort}` resolution,
role fallback chains, the harness registry, and the per-call harness argument on
the MCP entry points all exist and all work. What does not exist is any
statement that this is what they are *for*. Every worked example in the docs is a
cost-tier example, so a reader reasonably concludes cost tier is the only axis
Praxis understands.

Naming the second dimension costs a paragraph and changes what the product
appears to be.

### 2.1 The spine sentence

> **One session. Many models, many harnesses. Arranged the way you work.**

This is the headline for the new configuration doc, the framing sentence for the
reworked `positioning.md` uniqueness item, and one added sentence in the README's
"Every seat is independently configurable" concept.

---

## 3. What already ships (verified 2026-08-08)

Every claim below was read out of the code before being written here. This
section is the evidence base the documentation changes are allowed to draw on;
nothing outside it may be asserted as shipped.

### 3.1 Level 1: one assistant session, many harnesses, by explicit choice

`DispatchRequest.harness` (`src/orchestrator/models/schemas.py:521`) and
`ExecutePlanRequest.harness` (`schemas.py:602`) are both optional per-call
fields, and `model` is a required per-call field on both. The MCP tools thread
them through: the `dispatch_task` and `execute_plan` tool definitions each
accept a `harness` parameter (`src/mcp_server/server.py:672`, `server.py:731`)
and hand it to the implementation functions, which add it to the REST payload
only when set (`dispatch_task_impl` at `server.py:75` and `server.py:90`, the
`execute_plan` equivalent at `server.py:127` and `server.py:139`).

Consequence: from a single assistant session, one task can be dispatched to
`opencode` and the next to `agy`, deliberately, per call. This is the level that
answers the triggering complaint directly. The bridge the assistant said did not
exist is a tool argument.

### 3.2 Level 2: one plan run, many models, automatically

`CALL_SITE_DEFAULTS` in `core/llm_router.py` already routes a single run across
several models with no user configuration: planning and first-pass review on a
mid-tier model, re-review dropping to a cheap one, task derivation on a local
model, and the open-ended improvement loop on a frontier model. Per-call-site
overrides live in `settings_overrides` under `models.<call_site>`, resolve
through `EffectiveSettings.call_site_config`, and are managed through
`GET/PUT /api/settings/models` and the dashboard **Settings → Models** tab.

Role fallback chains resolve ahead of those overrides:
`EffectiveSettings.call_site_chain` maps a call-site to a role via
`core/roles.ROLE_OF_CALL_SITE` (frozen and golden-tested), then to an ordered
registry chain. `LLMRouter.run` falls back along that chain only on
unavailability, per `core/provider_errors.is_unavailability`.

### 3.3 Level 3: one plan run, many harnesses, automatically

`EscalationPair` is a `(harness, model)` pair, not a bare model
(`core/escalation.py:20-24`). A leaf that fails on capability re-dispatches at
the next rung of the `implement_escalation` ladder in `config/praxis.yaml`, and
a rung names both a harness and a model. The engine can therefore move work
across harnesses mid-plan on its own initiative.

The implement seat cannot ride the router's role fallback chain, because the
worker model is baked at container spawn; escalation is a dispatch-time
substitution instead. That distinction is already documented in the module
docstring and must survive into the user-facing doc, because it is the reason
`implement` is absent from router-driven behavior.

### 3.4 The rest of the adjustable surface

Collected here because no single document currently lists it, which is the
root cause of the user's question:

| Knob | Where it lives | Granularity |
|---|---|---|
| Harness | `projects.harness`, `ProjectCreate/Update.harness` | per project |
| Harness | `DispatchRequest`/`ExecutePlanRequest.harness` | per call |
| Worker model | `projects.model_name` / `agent_model` | per project |
| Worker model | `DispatchRequest.model` (required) | per call |
| Provider, model, effort | `settings_overrides` key `models.<call_site>` | per call-site, global or per project |
| Role fallback chain | `EffectiveSettings.call_site_chain`, `core/roles` | per role |
| Implement escalation ladder | `implement_escalation` in `config/praxis.yaml` | global |
| Default delegated worker | `default_worker_harness`, `default_worker_model` | global |
| Auto-delegate mode | `settings_overrides` key `auto_delegate.enabled` | global |
| Verify command | `projects.verify_cmd` | per project |
| Merge gate versus auto-merge | `projects.auto_merge`, `core/merge_policy` | per project, protected branches never auto-merge |
| Git backend | `core/git_backend.resolve_backend`, `Settings.allow_local_repo_paths` | per project repo_url, admission gated globally |
| Worker endpoint | `lm_studio_url` | global or per project |
| Retry and loop bounds | `max_retries`, `max_improvement_cycles`, `max_leaves_per_plan` | per project or global |

### 3.5 Level 4: first-run setup already arranges the worker seat

Discovered while planning this work, after the first draft of this spec was
committed. The named-preset mechanism ships end to end:

- `worker_presets` in `config/praxis.yaml` declares named
  `(harness, model, endpoint, requires)` entries. Three ship today:
  `hosted-openweight` (opencode / glm-4.7 / a hosted OpenAI-compatible
  endpoint), `local-lmstudio` (opencode / qwen3.6-27b / LM Studio), and
  `gemini-agy` (agy / Gemini 3.6 Flash (High) / interactive login).
- `EffectiveSettings.worker_presets()` resolves them and
  `GET /api/settings/presets` (`src/orchestrator/api/presets.py`) serves them.
- `praxis init` prints the menu, defaults to the first preset whose `requires`
  list is empty so the default is never a preset that cannot work
  (`_default_preset_index`), forces an explicit confirmation for an
  unsatisfiable choice (`_confirm_unmet_requirements`), and writes
  `LM_STUDIO_URL`, `DEFAULT_WORKER_HARNESS`, and `DEFAULT_WORKER_MODEL`
  together from the chosen preset (`_managed_values`).

This is the strongest single piece of evidence for the spine sentence, because
it means arranging a seat is already a first-run experience rather than a
documentation exercise. It also means the documentation must describe
`worker_presets` as a shipped mechanism and extend it, never invent a parallel
preset vocabulary.

### 3.6 Honest ceilings on the claim

These constrain the wording and must appear in the docs, not only here.

1. **"Many harnesses" is two.** `core/harnesses.py` registers `opencode` and
   `agy`. `positioning.md` already concedes Praxis does not compete on harness
   breadth against a neighbor with 23 or more. The claim is about the seam being
   general, never about the population being large.
2. **The harness contract is undocumented.** `docs/harness-contract.md` does not
   exist; it is roadmap item S3. So "add your own harness" is not yet a claim
   Praxis has earned. The honest phrasing is that two harnesses ship and the
   seam is in code, with the written contract named as pending work.
3. **Cross-harness escalation is coded for but plausibly never exercised.** The
   shipped default ladder is `opencode/glm-4.7` then `opencode/qwen3.6-27b`,
   the same harness on both rungs. The mechanism is typed for a harness switch;
   no run is known to have performed one. Documented as a capability of the
   mechanism, with live verification tracked as an open item (section 7).
4. **Every seat consumes text.** The reviewer reads a diff, the verifier reads
   an exit code, the planner reads markdown. Nothing in Praxis can look at a
   rendered artifact. This is the unaddressed half of the triggering problem and
   becomes a named tradeoff plus F17.
5. **Presets arrange one seat, not the arrangement.** `worker_presets` covers
   the implement seat only: harness, model, endpoint. Nothing in first-run setup
   asks about the brain seat, the review seat, the merge gate, or `verify_cmd`.
   The docs may say "choose how your worker runs," never "choose how your whole
   loop runs." Closing that gap is F18.

---

## 4. Part A: documentation change set (build now)

Constraints inherited from the roadmap section 7 framing rules: the README stays
short by deliberate design, framing sharpens without material word-count growth;
"open-weight model" not "local model"; MCP-first; the agent-orchestrator
comparison lives in `positioning.md` and is never named in the README.

### A1. `docs/positioning.md`

1. In "The core reason Praxis exists," add one paragraph introducing capacity
   versus aptitude and listing the aptitude axes. The existing four-role
   justification is a kind-of-judgment argument; this extends it without
   replacing it.
2. **Rewrite** existing uniqueness item 3, currently "Provider escape hatch via
   MCP." It reads as a Claude-specific escape hatch. The generalized version:
   every vendor's assistant is locked to that vendor's models by design, in
   every direction, and Praxis is the seam that crosses it. Body is the three
   verified levels from section 3.1 to 3.3, each citing its code, because the
   citations are what make the claim survive scrutiny. Rewriting rather than
   appending keeps the list at six items.
3. Add a new entry to "Honest tradeoffs": **no non-text seat**, phrased as in
   section 3.6 item 4, pointing at F17.
4. Extend the "Positioning guidance" footer with the standing rule that examples
   are always plural and multi-directional, and that no vendor pairing is ever
   presented as the canonical one. A single worked example implies a blessed
   configuration and discourages every other one.
5. Link to the new `docs/configurations.md` rather than absorbing its content.

### A2. `README.md`

1. "Every seat is independently configurable": add one sentence naming the
   aptitude axes and stating that the choice is what a seat is good at, not only
   what it costs. Link to `docs/configurations.md`.
2. Demote the existing "Where Gemini fits" paragraph from a rule to field notes
   that churn. It currently states a specific vendor pairing in the register of
   architecture, which is precisely the failure mode this spec exists to fix.
   Content is retained; framing changes.
3. No new sections. Net growth of two or three sentences plus one link.

### A3. New `docs/configurations.md`

Headline is the spine sentence. Structure:

1. **The knob table** from section 3.4, verbatim in substance, as the reference
   nobody currently has.
2. **The three levels** from sections 3.1 to 3.3, written for a user rather than
   as evidence: what happens by default, what you can choose per call, and what
   the engine will do on its own.
3. **The shipped worker presets**, documented as the real mechanism they are:
   the `worker_presets` block, its `(name, label, harness, model, endpoint,
   requires)` fields, the three entries that ship, `GET /api/settings/presets`,
   and what `praxis init` does with a choice. This section describes existing
   behavior and invents nothing.
4. **Arrangements**, which are whole-loop configurations layered on top of a
   worker preset, named by workflow rather than by vendor, each stating which
   worker preset it starts from and which additional `config/praxis.yaml` keys
   (`models.registry`, `models.roles`, `implement_escalation`,
   `allow_local_repo_paths`) it changes, and each listing two or more possible
   vendor fillings so no pairing reads as blessed:
   - Subscription brain plus open-weight worker (the reference configuration)
   - Cross-vendor: brain on one vendor, implementation harness on another
   - Single-vendor: every seat on one provider's models
   - Fully local: open-weight models for every model-driven seat
   - Evaluate with no GitHub credential: local bare repo backend, which ships
     today behind `allow_local_repo_paths`

   Each arrangement is stated as concrete key-value writes, because that is
   exactly what F18 would later emit. The word "arrangement" is used rather than
   "preset" so the doc never blurs a shipped `worker_presets` entry with a
   whole-loop configuration that is currently assembled by hand.
5. **Ceilings**, from section 3.6, stated plainly rather than buried, including
   that arrangements are hand-assembled today while worker presets are not.

### A4. `docs/social-launch-drafts.md`

**This file is gitignored and marked "Private file. Do not commit."** The edit is
made in place and is not committed; no part of this spec's commit may include it.

Add one draft in the file's existing per-platform format, built on the
vendor-lock-in framing rather than on any one vendor pairing, with the
triggering anecdote appearing as one instance among several.

### A5. Roadmap spec section 7

Add rows to the documentation table for `docs/configurations.md` and for the
`positioning.md` and README changes above, so the canonical roadmap continues to
describe the real documentation plan. Add F17 and F18 to section 4.

---

## 5. Part B: F17, the artifact review seat (parked, spec only)

**Problem.** Every seat consumes text, so judgment requiring a rendered artifact
has no seat. Praxis cannot tell you the layout it just shipped is broken.

**Shape.** Deliberately generic. Not "screenshots," not any named vendor.

- **Producer.** A per-project `artifact_cmd`, following the `verify_cmd`
  precedent exactly: optional, and absent means the whole feature is a no-op. It
  runs where `verify_cmd` runs and writes files to a known output directory.
  Whether those are page renders at three viewports, a generated PDF, a chart, or
  an accessibility report is the project's business. Praxis has no opinion.
- **Consumer.** A new `review_artifact` call-site and a matching role in
  `core/roles.MODEL_ROLES` and `ROLE_OF_CALL_SITE`, resolved through the existing
  `LLMRouter` and `call_site_chain`. It inherits fallback chains, per-project
  overrides, and the Settings → Models UI at no additional cost, and it can be
  pointed at any provider the router supports.
- **Transport, the one genuinely new seam.** The router's text-mode `build_argv`
  cannot carry a file. The `local` provider over an OpenAI-compatible endpoint
  accepts image content blocks natively, so any vision-capable open-weight model
  served locally fills the seat immediately. CLI providers require per-provider
  file-argument support that may not exist. **The implementing spec must
  enumerate which providers can actually fill this seat at the time of writing
  and mark the rest unimplemented rather than assumed.** This is the highest-risk
  unknown in F17 and is not to be discovered during implementation.
- **Verdict handling.** A FAIL forces the human merge gate regardless of the text
  review verdict, reusing the `core/diff_guard` precedent. Advisory-blocking,
  never auto-rejecting, because this judgment is subjective.
- **Trigger.** Gated on the task's changed files intersecting a per-project path
  glob, so it does not run on every task.
- **Calibration safety.** Artifact verdicts must not count against the worker in
  `core/failure_taxonomy.counts_against_worker`. A subjective visual verdict
  feeding the capability calibration loop would poison learned limits with
  taste. This is recorded now because it is the kind of coupling that is
  invisible until the data is already contaminated.

**Non-goals.** Praxis does not capture screenshots, ship a browser, bundle a
browser-automation library, or hold an opinion about your design system. It runs
your command and routes the output to your model.

---

## 6. Part C: F18, arrangements (parked, spec only)

Extending the shipped preset mechanism past the one seat it covers.

**Corrected scope.** The first draft of this spec proposed building a preset
questionnaire. That was wrong: the mechanism ships (section 3.5). `praxis init`
already presents a named worker-preset menu, defaults to one it can satisfy,
confirms unsatisfiable choices, and writes harness, model, and endpoint
together. F18 is therefore not "build presets." It is "extend the existing
preset mechanism past the one seat it currently covers."

**Problem.** `worker_presets` arranges the implement seat only. First-run setup
never asks what fills the brain seat, what reviews, whether you want to approve
every merge, or whether you have a verify command. A user finishes `praxis init`
with a configured worker and an entirely default everything-else, which is
exactly the arrangement they were never asked about.

**Shape.** Extend the same declarative pattern outward rather than adding a
parallel one:

- A second YAML block declaring named **arrangements**, each naming a
  `worker_preset` plus the `models.roles` chains, `auto_merge` default, and
  `verify_cmd` that go with it. Same shape as `worker_presets`: a `requires`
  list so an arrangement needing a credential `init` cannot collect is never the
  default, reusing `_default_preset_index` and `_confirm_unmet_requirements`
  rather than reimplementing that logic.
- One additional `init` prompt offering the arrangement menu, with the existing
  worker-preset menu remaining as the narrower choice.
- A `GET /api/settings/arrangements` endpoint mirroring the existing presets
  endpoint.

The arrangements documented in A3 are the input to this feature. Writing them as
prose first is deliberate: it validates that each one is expressible as concrete
key-value writes before any code assumes it is.

**Deterministic, never LLM-interpreted.** Free-text intent would need a model to
interpret it, and `init` is the command that configures the model. Asking the
thing being set up to perform its own setup is a chicken-and-egg failure, and it
would also make first-run setup fail in exactly the situation where the user has
nothing configured yet. The questionnaire is a fixed decision tree over a fixed
preset set.

**Inherited invariants that constrain the design.**

- `init` merges only `MANAGED_KEYS` into `.env`, preserving every other key,
  position, and comment. Presets needing more than those six keys write to
  `config/praxis.yaml` instead.
- `config/praxis.yaml` is mounted rather than baked, so a preset write takes
  effect on an orchestrator restart and never needs an image rebuild.
- `init` refuses to run outside the repo root and is re-runnable and
  non-destructive. Both properties must hold for the questionnaire path too, so
  re-running to change presets is safe.

**Why this belongs with the positioning work.** Arranging seats is the spine
sentence expressed as first-run UX. The documentation makes the claim; `init` is
where a user first experiences it. The shipped worker-preset menu already proves
the pattern works, which is the strongest possible argument for extending it and
the reason F18 is low-risk rather than speculative.

---

## 7. Open items

Tracked, not resolved by this spec.

1. **Cross-harness escalation has never been observed live.** The default ladder
   uses one harness on both rungs. A live run that escalates across harnesses
   would upgrade section 3.3 from "the mechanism supports it" to "verified."
   Until then the docs describe the mechanism, not an observed behavior.
2. **README and CLAUDE.md disagree about agy as a brain.** The README states agy
   emits capturable non-interactive output as of agy v1.1.0; `CLAUDE.md` still
   carries the older gotcha that agy is unusable as a brain because `--print`
   only renders to an interactive TTY. One of these is stale. This spec flags
   the drift and deliberately does not resolve it, because resolving it requires
   a live agy run, not a doc edit.
3. **`docs/harness-contract.md` (roadmap S3) does not exist**, which caps what
   the pluggability claim may say. Writing it would let the docs claim "add your
   own harness," which they currently may not.
4. **Shipped-but-undocumented features may not be limited to one.** The worker
   preset menu was built, wired through the API, and given requirement-aware
   defaults, and this spec's first draft still proposed building it, because no
   user-facing document mentions it. That is the same failure this spec exists
   to fix, found inside the spec itself. Before `docs/configurations.md` is
   called finished, someone should sweep `config/praxis.yaml` and the `/api`
   routers for other capabilities no document names. `models.registry` and
   `models.roles` are the immediate suspects.

---

## 8. Sequencing

Part A ships now as a documentation pass. Parts B and C remain specs with no
plan and no code. The bench pilot and product Phase B keep the critical path;
this spec deliberately does not insert an implementation cycle ahead of them.
