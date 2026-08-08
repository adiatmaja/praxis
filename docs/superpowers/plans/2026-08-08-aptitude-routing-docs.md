# Aptitude Routing Documentation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Document the configuration surface Praxis already has, so a reader learns that every seat is a choice made on aptitude and not only on cost tier, and so the shipped-but-undocumented worker preset mechanism stops being invisible.

**Architecture:** One new reference document, `docs/configurations.md`, carrying the spine sentence "one session, many models, many harnesses, arranged the way you work." Four existing documents are sharpened and link to it rather than absorbing it. A guard test pins the new doc's harness and preset lists to the code they describe, so the doc cannot silently drift when a harness or preset is added or removed.

**Tech Stack:** Markdown, pytest, the existing `core/harnesses.REGISTRY` and `core/settings_file.load_yaml_settings` as the sources of truth the guard test reads.

**Source spec:** `docs/superpowers/specs/2026-08-08-aptitude-routing-and-artifact-review-design.md`, Part A only. Parts B and C (F17, F18) stay parked specs and no task here touches them beyond adding their roadmap entries.

**Standing rule for every task:** no claim may be written that section 3 of the spec does not establish as shipped. If a task's prose tempts you past that line, the correct move is to weaken the prose, not to widen the claim.

---

### Task 1: Guard test pinning the new doc to the code it describes

**Files:**
- Create: `tests/test_docs_configurations.py`

**Depends on:** None

The doc's most drift-prone content is its list of harnesses and its list of worker presets, because both grow by editing code or YAML somewhere else entirely. The spec's ceiling is that "many harnesses" is two today; a doc that silently says two after a third ships is worse than no doc. This test makes the lists set-equal to their sources.

The doc marks each list with HTML comment delimiters so the test parses a precise region rather than grepping the whole file, which would match prose mentions and produce false passes.

- [ ] **Step 1: Write the failing test**

```python
"""Guard: docs/configurations.md must match the code it documents.

The harness table and the worker preset table describe registries that live
elsewhere (``core/harnesses.REGISTRY`` and the ``worker_presets`` block in the
settings YAML).  Either can grow without anyone reopening the doc, and a doc
that understates the harness count is worse than no doc, because the spec's
honest-ceiling language depends on that count being true.

Both regions are delimited by HTML comments so this test reads exactly the
table it means to check.  Grepping the whole file would match prose mentions
of ``opencode`` and pass on a doc that never listed it.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from orchestrator.core.harnesses import REGISTRY
from orchestrator.core.settings_file import load_yaml_settings


DOC = Path(__file__).resolve().parents[1] / "docs" / "configurations.md"


def _delimited_ids(text: str, marker: str) -> set[str]:
    """Return the backticked identifiers inside a BEGIN/END comment region.

    Args:
        text: The full document text.
        marker: The marker name, e.g. ``harness-list``.

    Returns:
        Every backtick-quoted token inside the region.

    Raises:
        AssertionError: If the region is missing or unterminated, which is
            itself a doc defect worth failing on.
    """
    pattern = rf"<!-- BEGIN {marker} -->(.*?)<!-- END {marker} -->"
    match = re.search(pattern, text, re.DOTALL)
    assert match is not None, f"docs/configurations.md is missing the {marker} region"
    return set(re.findall(r"`([^`]+)`", match.group(1)))


@pytest.fixture
def doc_text() -> str:
    assert DOC.exists(), "docs/configurations.md does not exist"
    return DOC.read_text(encoding="utf-8")


def test_harness_list_matches_registry(doc_text: str) -> None:
    assert _delimited_ids(doc_text, "harness-list") == set(REGISTRY)


def test_worker_preset_list_matches_settings_yaml(doc_text: str) -> None:
    presets = load_yaml_settings().get("worker_presets") or []
    names = {str(entry["name"]) for entry in presets}
    assert names, "settings YAML declares no worker_presets to document"
    assert _delimited_ids(doc_text, "worker-presets") == names
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_docs_configurations.py -v`

Expected: both tests FAIL at the fixture with `AssertionError: docs/configurations.md does not exist`.

If instead you see an `ImportError` on `load_yaml_settings`, stop and check the real symbol name in `src/orchestrator/core/settings_file.py` before continuing; the rest of this plan assumes that loader returns the parsed YAML as a dict.

- [ ] **Step 3: Commit the failing test**

```bash
git add tests/test_docs_configurations.py
git commit -m "test: pin docs/configurations.md to the harness and preset registries"
```

---

### Task 2: Create `docs/configurations.md`

**Files:**
- Create: `docs/configurations.md`
- Test: `tests/test_docs_configurations.py` (from Task 1)

**Depends on:** Task 1

- [ ] **Step 1: Write the document**

Create `docs/configurations.md` with exactly this content:

````markdown
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

Override any single call-site in **Settings → Models** in the dashboard, or over
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
````

- [ ] **Step 2: Run the guard test to verify it passes**

Run: `uv run pytest tests/test_docs_configurations.py -v`

Expected: both tests PASS.

If `test_worker_preset_list_matches_settings_yaml` fails, the preset names in the delimited table do not set-match `worker_presets` in `config/praxis.yaml`. Fix the table to match the YAML; do not edit the YAML to match the table.

- [ ] **Step 3: Prove the guard actually guards (mutation check)**

A guard test that passes for the wrong reason is worse than none. Break it deliberately and confirm it fails.

```bash
# Temporarily remove one harness id from the delimited region
sed -i 's/| `agy` | Antigravity (Gemini) |/| REMOVED | Antigravity (Gemini) |/' docs/configurations.md
uv run pytest tests/test_docs_configurations.py::test_harness_list_matches_registry -v
```

Expected: FAIL, with the assertion showing `{'opencode'}` against `{'opencode', 'agy'}`.

Restore it with the inverse edit. Do **not** reach for `git checkout --` here: the doc is untracked at this point so it would fail anyway, and on a file holding uncommitted work it destroys it.

```bash
sed -i 's/| REMOVED | Antigravity (Gemini) |/| `agy` | Antigravity (Gemini) |/' docs/configurations.md
uv run pytest tests/test_docs_configurations.py -v
```

Expected: PASS. Confirm the restore actually landed before moving on:

```bash
grep -c '`agy`' docs/configurations.md
```

Expected: at least `1`.

- [ ] **Step 4: Commit**

```bash
git add docs/configurations.md
git commit -m "docs: add the configuration reference, including the shipped worker presets"
```

---

### Task 3: Sharpen `docs/positioning.md`

**Files:**
- Modify: `docs/positioning.md` (the "core reason" section, uniqueness item 3, the tradeoffs list, the guidance footer)

**Depends on:** Task 2

- [ ] **Step 1: Add the capacity/aptitude paragraph**

In "The core reason Praxis exists", immediately after the bulleted list of the four roles (the list ending with the Verification bullet), insert:

```markdown
Seats also differ by **aptitude**, not only by how much capability they need.
Capacity is how much a model can hold, and it is what decomposition sizes tasks
against. Aptitude is what kind of judgment the seat wants: modality, domain
strength, tool ecosystem, latency, privacy, availability, or plain preference. A
bigger model of the same family does not necessarily fix an aptitude mismatch,
which is why the seat is the unit of choice rather than the tier.
[docs/configurations.md](configurations.md) is the reference for what can fill
each seat and where that choice lives.
```

- [ ] **Step 2: Rewrite uniqueness item 3**

Replace item 3 of "What is genuinely unique" in full. The current text is:

```markdown
3. **Provider escape hatch via MCP.** Aider/Roo Code can use local models, but
   neither lets a *Claude-locked assistant* delegate to a local worker from
   inside that assistant. Praxis does, through MCP `dispatch_task`.
```

Replace it with (the opening sentence is the spine sentence this whole change set carries; keep it verbatim):

```markdown
3. **Cross-vendor seat routing, in every direction.** One session, many models,
   many harnesses, arranged the way you work. Every vendor's assistant
   is locked to that vendor's models. That is a design choice each of them
   makes, not a defect in one of them, and it means the moment a different
   kind of judgment is wanted the developer is moving work between IDEs by
   hand. Praxis is the seam, and it holds at three levels, each shipping today:
   a `harness` and `model` argument on `dispatch_task` and `execute_plan`, so
   one assistant session dispatches to different harnesses per call; per
   call-site routing, so a single plan run already spans several models with no
   configuration; and a `(harness, model)` escalation ladder, so the engine
   moves a failing leaf across harnesses on its own. Aider and Roo Code can use
   open-weight models, but neither lets a vendor-locked assistant delegate out
   of its own vendor from inside that assistant. Examples here are deliberately
   plural: no pairing is the blessed one.
```

- [ ] **Step 3: Add the honest tradeoff**

Append to the "Honest tradeoffs (do not hide these)" numbered list, after item 4:

```markdown
5. **No non-text seat.** Every seat consumes text: the reviewer reads a diff,
   the verifier reads an exit code, the planner reads markdown. Judgment that
   needs a rendered artifact has nowhere to sit, so Praxis cannot tell you the
   layout it just shipped is broken. Designed as F17 in the capability-engine
   roadmap; not built.
```

- [ ] **Step 4: Extend the positioning guidance footer**

Append to the final "Positioning guidance" paragraph:

```markdown
Route examples by **aptitude**, and keep them plural and multi-directional: a
single worked vendor pairing reads as the blessed configuration and quietly
discourages every other one, which is the opposite of the claim. The
configuration surface itself lives in [docs/configurations.md](configurations.md);
positioning links to it rather than restating it.
```

- [ ] **Step 5: Verify no forbidden claim slipped in**

Run:

```bash
grep -n "add your own harness\|bring your own harness\|any harness you like\|plug in any harness" docs/positioning.md
```

Expected: no output. The harness contract doc does not exist, so "add your own harness" is not a claim this project has earned yet. Note that the phrase "many harnesses" IS permitted and expected here: it appears in the spine sentence added in Step 2, and it describes routing across the harnesses that ship rather than promising you can author new ones.

Run:

```bash
uv run pytest tests/test_docs_configurations.py -v
```

Expected: PASS (unchanged; this confirms nothing in this task broke the doc's delimited regions).

- [ ] **Step 6: Commit**

```bash
git add docs/positioning.md
git commit -m "docs: name aptitude in positioning and generalize the cross-vendor claim"
```

---

### Task 4: Sharpen `README.md`

**Files:**
- Modify: `README.md` (the "Every seat is independently configurable" concept, the "Where Gemini fits" paragraph, the Documentation table)

**Depends on:** Task 2

The README stays short by deliberate design. Net growth here is about three sentences and one table row.

- [ ] **Step 1: Extend the seat-configurability concept**

Find the paragraph beginning `**Every seat is independently configurable.**` and append to it:

```markdown
Choose each seat on what it is *good at*, not only on what it costs: modality,
domain strength, tool ecosystem, latency, privacy, and availability are all
legitimate reasons to fill one seat differently from its neighbor. See
[docs/configurations.md](docs/configurations.md) for every knob, the shipped
worker presets, and known-good whole-loop arrangements.
```

- [ ] **Step 2: Demote the Gemini paragraph from rule to field note**

Find the paragraph beginning `**Where Gemini fits: worker seats, not the planner.**` and change only its opening sentence. Replace:

```markdown
**Where Gemini fits: worker seats, not the planner.**
```

with:

```markdown
**Field notes on specific providers.** These are observations that churn with
model releases, not architecture; the seats accept any provider. **Gemini:
worker seats, not the planner.**
```

Leave the rest of the paragraph exactly as it is. The content is useful; only its register changes.

- [ ] **Step 3: Add the doc to the Documentation table**

In the `## Documentation` table, insert this row directly after the `Decomposition standard` row:

```markdown
| Configuration surface (seats, presets, arrangements) | [docs/configurations.md](docs/configurations.md) |
```

- [ ] **Step 4: Verify the README did not grow a section and the link resolves**

Run:

```bash
grep -c "^## " README.md
```

Expected: `9`. If it is 10, a new section was added; remove it. The change is sentences and a table row, never a section.

Run:

```bash
test -f docs/configurations.md && echo "link target exists"
```

Expected: `link target exists`.

- [ ] **Step 5: Commit**

```bash
git add README.md
git commit -m "docs: point the README at the configuration surface and demote the vendor note"
```

---

### Task 5: Index the new doc in `CLAUDE.md`

**Files:**
- Modify: `CLAUDE.md` (the `## Documentation` list)

**Depends on:** Task 2

A doc that no index names is a doc nobody finds, which is the exact failure that let `worker_presets` ship undocumented.

- [ ] **Step 1: Add the index line**

In the `## Documentation` section, insert directly after the `**Decomposition standard (cited contract):**` line:

```markdown
- **Configuration surface (seats, presets, arrangements):** `docs/configurations.md`
```

- [ ] **Step 2: Verify**

Run:

```bash
grep -n "docs/configurations.md" CLAUDE.md README.md docs/positioning.md
```

Expected: one hit in each of the three files.

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: index the configuration reference in CLAUDE.md"
```

---

### Task 6: Record F17 and F18 in the roadmap

**Files:**
- Modify: `docs/superpowers/specs/2026-07-11-capability-engine-roadmap.md` (section 4 features, section 7 documentation table)

**Depends on:** None

This task only touches the roadmap and is independent of the doc work, so it can run concurrently with Task 1.

- [ ] **Step 1: Add F17 and F18 to section 4**

Insert immediately after the F16 paragraph and before the `### Deliberately NOT building` heading:

```markdown
### F17. Artifact review seat (non-text judgment), Tier 3

Every seat consumes text, so judgment needing a rendered artifact has nowhere to
sit. A per-project `artifact_cmd` (exact `verify_cmd` precedent: optional, absent
means no-op) writes files; a new `review_artifact` call-site and role routes them
through the existing `LLMRouter`, so it inherits fallback chains, per-project
overrides, and the Settings → Models UI. A FAIL forces the human merge gate
(`diff_guard` precedent), never an auto-reject, because the judgment is
subjective. Verdicts must NOT feed `failure_taxonomy.counts_against_worker` or
taste contaminates calibration. Open risk: the router's text-mode `build_argv`
cannot carry a file, so the implementing spec must enumerate which providers can
actually fill the seat rather than assume. Design:
`docs/superpowers/specs/2026-08-08-aptitude-routing-and-artifact-review-design.md`.

### F18. Arrangements: presets past the worker seat, Tier 3 (small)

`worker_presets` already ships end to end (YAML, `EffectiveSettings`,
`GET /api/settings/presets`, and a requirement-aware `praxis init` menu) but
arranges the implement seat only. F18 extends the same declarative pattern to a
named `arrangements` block carrying role chains, the merge-gate default, and
`verify_cmd`, reusing `_default_preset_index` and `_confirm_unmet_requirements`
rather than growing a parallel path. The arrangements documented in
`docs/configurations.md` are the input. Design: same spec as F17.
```

- [ ] **Step 2: Add the documentation table rows in section 7**

In the section 7 table, insert these rows after the `docs/positioning.md` row:

```markdown
| New `docs/configurations.md` | The configuration surface in one place: every knob, the three levels of multi-model/multi-harness behavior, the shipped `worker_presets`, hand-assembled arrangements, and the honest ceilings. Named in the README and CLAUDE.md indexes. |
| `README.md` (aptitude) | One sentence on choosing seats by aptitude rather than cost tier; demote the per-vendor paragraph from rule to field note; link the configuration doc. No new sections. |
```

- [ ] **Step 3: Verify**

Run:

```bash
grep -n "^### F1[678]" docs/superpowers/specs/2026-07-11-capability-engine-roadmap.md
```

Expected: three lines, F16 then F17 then F18, in that order.

- [ ] **Step 4: Commit**

```bash
git add docs/superpowers/specs/2026-07-11-capability-engine-roadmap.md
git commit -m "docs: record F17 artifact review seat and F18 arrangements in the roadmap"
```

---

### Task 7: Add the launch draft (private file, never committed)

**Files:**
- Modify: `docs/social-launch-drafts.md`

**Depends on:** None

**This file is gitignored and carries the line "Private file. Gitignored. Do not commit." Nothing in this task is staged or committed.** The final step verifies that.

- [ ] **Step 1: Append the new draft**

Add at the end of the file:

```markdown
---

## 6. Vendor Lock-In Post (any platform, short form)

> Tone: one observation, one consequence, one thing that exists. No hype.

---

Your AI assistant can only reach its own vendor's models. Every vendor builds it
that way, so this is not a complaint about one of them.

It stops mattering right up until the moment you want a different kind of
judgment than the one you are paying for. Then you are moving work between two
IDEs by hand.

Praxis treats planning, implementation, review, and verification as four
separate seats, and any seat can be filled by any provider or coding harness.
From one assistant session you can dispatch one task to one harness and the next
to another, per call. A single plan run already spans several models without you
configuring anything. And when a task fails because it was too hard for the
worker, the engine re-dispatches it at the next rung of a ladder that can name a
different harness entirely.

Honest about the limits: two harnesses ship today, and every seat still reads
text, so nothing here can look at a screenshot and tell you the layout is wrong.
That one is designed and not built.

Repo: https://github.com/adiatmaja/praxis
```

- [ ] **Step 2: Verify the file is still ignored and unstaged**

Run:

```bash
git check-ignore -v docs/social-launch-drafts.md && git status --short docs/social-launch-drafts.md
```

Expected: the `check-ignore` line names the `.gitignore` rule, and `git status --short` prints **nothing** for that path. If the path appears in `git status`, stop: the file is not actually ignored and the private-content rule has been broken.

- [ ] **Step 3: No commit**

There is deliberately no commit step in this task.

---

## Parallel Execution Map

- **Wave 1:** Task 1, Task 6, Task 7 (no dependencies, run in parallel)
- **Wave 2:** Task 2 (depends on Task 1)
- **Wave 3:** Task 3, Task 4, Task 5 (each depends on Task 2, and each touches a different file, so run in parallel)

---

## Notes

**Why the guard test exists.** The spec's honest-ceiling language depends on the
harness count being true. `worker_presets` shipped with an API endpoint, a CLI
menu, and requirement-aware defaults, and appeared in zero documentation files;
the first draft of the spec proposed building it. The guard makes the two most
drift-prone lists in the new doc fail loudly instead of aging quietly.

**Out of scope, found during the sweep.** `GET /api/doctor` is absent from the
API reference in `docs/deployment.md`. That is diagnostics rather than
configuration, so it is recorded here and deliberately not fixed by this plan.

**Not in this plan.** F17 and F18 get roadmap entries in Task 6 and nothing
else. No code is written for either. The bench pilot and product Phase B keep
the critical path.
````

---

## Execution record, 2026-08-08

**Status: COMPLETE.** All 7 tasks executed. Gate green: `ruff format --check`
253 files clean, `ruff check` clean, `mypy src/ bench/` clean on 105 files,
`pytest --cov=orchestrator` 2014 passed at 92%.

### Commits, in order

| SHA | What |
|---|---|
| `9d797d5` | Task 1, guard test pinning the doc to the harness and preset registries |
| `4201d30` | Task 2, `docs/configurations.md` |
| `57c2219` | Task 3, `docs/positioning.md` aptitude paragraph, item 3 rewrite, tradeoff 5, guidance footer |
| `161bf69` | Task 4, README seat sentence, vendor paragraph demoted, Documentation row |
| `feb2d4e` | Task 5, `CLAUDE.md` index line |
| `1ec5587` | Task 6, F17 and F18 in the roadmap |
| `1a69111` | Adversarial review corrections across four files plus guard hardening |
| `04f95f7` | Line-ending normalization, split out so the correction diff stayed reviewable |

Task 7 (the launch draft) is deliberately uncommitted: `docs/social-launch-drafts.md`
is gitignored and marked private. Verified still unstaged.

### Execution deviations from the plan

- **Waves were run sequentially, not in parallel.** The Parallel Execution Map
  groups by file independence, which is not git independence: concurrent agents
  in one tree race on the index lock, and one agent's `git commit` can sweep up
  another's staged file. The tasks were small enough that serializing cost little.
- Every dispatch was diffed independently rather than trusted from the agent's
  own report. No scope creep was found in any of the six.

### Defects in this plan's own text, found during execution

1. **Task 1's adaptation note was wrong about the failure mode.** It predicted an
   `ImportError` on `load_yaml_settings`. The real mismatch is a call-signature
   error: the function exists but requires a `path`. The implementer resolved it
   correctly using `config_file_path()`, which is better than the plan's text
   because that helper is the single place the config path is decided.
2. **Task 3's verification grep would have banned the spine sentence.** It
   forbade "many harnesses" while Task 3 Step 2 adds exactly that phrase. Caught
   in plan self-review before dispatch and narrowed to the unearned claim
   ("add your own harness") only.
3. **Task 5's expected grep count was wrong.** It predicted one hit per file
   across three files; `positioning.md` and `README.md` each carry two. Harmless,
   but the implementer had to reason past it.

### Vacuous-guard findings, exposed by mutation

The Task 1 guard passed for weaker reasons than its own docstring claimed. Adversarial
review demonstrated it would stay green when: the delimited region held pure prose
rather than a table; a row carried the wrong display name; a preset's `requires`
list flipped (which changes the `praxis init` default); a duplicate marker shadowed
a corrected region; and, most importantly, a third harness shipped while the
Ceilings section still said "is two", which is the exact drift the docstring
promised to catch.

Hardened to five tests. All four new guards were mutation-checked by breaking the
doc, confirming the specific test fails, and restoring; the restore was verified
byte-identical by hash.

### The serious finding: a false claim in three documents

`CALL_SITE_DEFAULTS` is unreachable in the shipped configuration.
`EffectiveSettings.call_site_chain` falls through to it ONLY when the role chain
is empty, and `config/praxis.yaml` ships `plan: [sonnet, opus]` and
`review: [sonnet, haiku]`, so every routed call-site resolves to
`claude-sonnet-4-6`. The written claim, "a single plan run already uses several
models without anyone configuring anything," was false: a default install uses
one. Three of its four specifics were false.

`CLAUDE.md` already carried the gotcha that states this resolution order. The
governing spec's section 3.2 was written contradicting it, and both documents
copied the spec. Fixed at the source and in both copies.

Same shadowing has a live product consequence, recorded but not fixed: the
dashboard's Settings, Models tab writes per-call-site overrides that the router
ignores, and `GET /api/settings/models` reads them back so the UI renders a saved
value that has no effect.

### Other corrections the review forced

`agent_model` is the brain model and is inert under the router, not the worker
model; there is no per-project call-site override (`call_site_config` ignores
`project_id`) nor per-project worker endpoint (`spawn_agent` reads the global);
`max_improvement_cycles` is stored and never read; the `skip` answer at the
GitHub-token prompt is only offered on a fresh `.env`; `LM_STUDIO_URL` is global
so a worker preset leaks into the brain seats; the default configuration already
crosses harnesses on its first escalation (`agy` worker, `opencode` ladder);
Single-vendor is not satisfiable with the two shipped harnesses; "known to work"
overclaimed the evidence base; "the seam is general" ignored three literal
harness-id branches in `spawn_agent`.

### Still open

Six code defects are recorded as open item 4 in the design spec and were NOT
fixed, because this was a documentation pass. Highest value first: unvalidated
`harness` on `DispatchRequest`/`ExecutePlanRequest` reaching `REGISTRY[...]` as a
`KeyError` the dispatch loop does not catch; the dead Settings, Models control;
the `hosted-openweight` preset whose endpoint yields `https://api.z.ai/v1/v1`,
which is the first preset `praxis init` offers.

F17 (artifact review seat) and F18 (arrangements) remain specs with no plan and
no code. The bench pilot keeps the critical path; this phase did not displace it.
