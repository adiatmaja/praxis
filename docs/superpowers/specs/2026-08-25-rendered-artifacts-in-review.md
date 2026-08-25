---
type: spec
status: draft
supersedes: none
related:
  - docs/superpowers/specs/2026-08-08-aptitude-routing-and-artifact-review-design.md
  - docs/superpowers/specs/2026-08-21-micro-edit-lane.md
  - docs/positioning.md
---

# Rendered Artifacts in Review

## Corrections against the brief, 2026-08-25

Read before the body.

**1. This is not a green-field proposal. A parked spec for close to the same
capability already exists, and this document must be read as an amendment to
it, not a rival.** `docs/superpowers/specs/2026-08-08-aptitude-routing-and-
artifact-review-design.md`, section 5, specs "F17, the artifact review seat":
a per-project `artifact_cmd` producer (the `verify_cmd` precedent), a new
`review_artifact` call-site and role routed through the existing `LLMRouter`,
an advisory-blocking verdict (a FAIL forces the human merge gate but never
auto-rejects), a path-glob trigger, and a calibration-safety rule that an
artifact verdict must never count against the worker. It names its single
highest-risk unknown as "which providers can actually fill this seat," and
says explicitly that this must be enumerated before implementation, not
discovered during it.

That unknown is exactly what the field report this spec is built on answers,
for one provider, with a live probe rather than a guess. This document's job
is narrower than F17's: record that datum, record the two new blockers found
while trying to act on it, and record a second design fork F17 did not
consider (see the note on call-site shape below). It does not restate F17's
verdict-handling, trigger, or calibration-safety design, all of which stand as
written. An implementer picking this up should read both documents, in date
order, before writing a plan.

**One deliberate design fork against F17, stated rather than silently
diverging:** F17 puts rendered evidence behind a second, separate call-site
(`review_artifact`) with its own advisory verdict alongside the text review.
This document (following the field-report brief) sketches attaching artifacts
to the SAME `review_diff` call so one reviewer forms one verdict from both the
code and the render together, which is what would have caught the field
report's defect (the diff alone was clean; the render alone would have raised
a question with nothing to attach it to). Both shapes are legitimate and call
for different things: two calls keep visual judgment cleanly separable and
never contaminate the main reviewer's data path if the render is unavailable;
one call lets the reviewer reason about code and appearance jointly, which is
what a human reviewer looking at a screenshot-driven PR actually does. This
spec does not resolve the fork; it is listed under Open Questions.

**2. The two blockers, verified against the code as of this commit:**

- `LEAF_SCHEMA_VERSION = 2` (`src/orchestrator/models/schemas.py:186`), and
  `tests/test_leaf_task.py:217` pins it with `assert LEAF_SCHEMA_VERSION ==
  2`. Any new field on `LeafTask` (`schemas.py:195-218`) changes what
  `model_dump()` returns for every leaf, which breaks
  `tests/test_decompose_golden.py::test_sample_plan_parses_to_expected_leaf_graph`
  against `tests/fixtures/decompose/expected_leaf_graph.json` the moment the
  new field's default value appears in the dumped dict. Regenerating means:
  bump the constant to 3, update the pinned-value test, and re-derive the
  fixture from the sample plan response so the new key appears with its
  default. This is mechanical but is a second file each carrying a frozen
  assertion, not a one-line change.
- `build_argv` (`src/orchestrator/core/llm_router.py:125-134`) returns a
  plain argv list per CLI provider and takes no artifact of any kind; the
  `local` branch (`llm_router.py:213` onward) posts a `messages` array with a
  single `content: <prompt string>` (`llm_router.py:279-283`), also text-only.
  Neither path has anywhere to put an image today. This confirms the brief:
  attaching artifacts to a review is a change to the router's contract, not a
  prompt-string edit.

**3. Brainstorm's un-routed status, verified accurate as stated in the
brief.** `CLAUDE.md`'s own gotcha (`CLAUDE.md:251-252`) reads "Brainstorm is
NOT routed yet (stream-json incompatible with text-mode `build_argv`)," and
`src/orchestrator/core/brainstorm.py:82-90` does invoke `claude -p
--output-format stream-json --verbose`, a different wire format than the
`--output-format text` every routed call-site uses (`llm_router.py:136`,
`opus_bridge.py:278`). No correction needed here; cited because it is the
closest existing precedent for "a call-site's actual wire contract does not
fit the router's one assumed shape," which is the same shape of problem an
image attachment is.

Neither correction changes the shape of what follows enough to invalidate it;
they change what implementing it costs, which is the point of writing this
down before a plan is written.

---

## The problem, from a live field report (2026-08-25)

A first-time user dispatched a screenshot-driven UI/UX audit of a 9-page
vanilla HTML/CSS/JS app to the `agy` harness. The worker changed a shared
component in a stylesheet:

```diff
-.s-alert { ... display: flex; gap: var(--mtel-sp-2); align-items: flex-start; }
+.s-alert { ... line-height: 1.5; display: block; }
```

That change was correct. It fixed a real defect: raw text nodes inside the
alert became independent flex items, and a paragraph rendered as five
unreadable vertical slivers.

About 300 lines further down the same file, `.mtel-demo-banner`, a documented
layout modifier on `.s-alert`, used `justify-content: center`.
`justify-content` only applies inside a flex container. The parent stopped
being one, so the rule went silently inert, and a compliance disclaimer moved
from centred to left-aligned on three pages. Nothing in the five changed
lines shows this: the affected selector is not in the diff at all.

The review gate passed the change and said:

> "FINDINGS.md is thorough and honest... No rules from the task spec were
> violated: mtel-calc.js untouched, no toFixed() added, no English-notation
> numbers introduced, no Inter font, no overflow:hidden added to fix overflow
> issues."

Every one of those statements is true, and the review was not careless. The
defect was simply not present in the diff the reviewer was given. A property
in a different selector, in a block the diff never shows, silently became a
no-op; nothing textual signals that.

It was caught only by re-rendering. Re-running the project's screenshot sweep
produced 93 of 119 changed images, with the survey, review, and closure shots
changed at every viewport including desktop, which is the signature of a
global component change rather than a scoped fix.

The reporter's own framing, borrowed from the audited project's guidance, is
the right one: **a check that cannot fire is worse than no check.** A review
gate that structurally cannot observe a defect class returns green on every
instance of it, and that green then reads as verification when it is a diff
summary that never had the evidence in front of it.

---

## The datum this spec is built on

`agy` demonstrably reads PNGs. Before proposing anything, the reporter probed
this directly: pointed it at one screenshot with no other context, and it
returned an accurate description naming real Indonesian UI strings visible in
that image. This is a capability of a harness Praxis already dispatches to
and does not currently exploit anywhere in the loop. Every seat in Praxis
today consumes text: the planner reads markdown, the worker reads a repo, the
reviewer reads a diff, the verifier reads an exit code. This is the first
concrete evidence that at least one already-integrated provider can do more
than that, which is what makes this worth specifying now rather than
speculatively.

---

## Why this is a spec, not an implementation

Two blockers, restated from the corrections above in their design
consequence rather than their mechanical one:

**The contract change is not free even though it is small.** Adding
`artifacts: str | None = None` (a directory path, per the brief) to
`LeafTask` is one line, but it is one line inside a contract with two frozen
guards (the version constant and the golden fixture), both of which exist
specifically so a contract change is never silent. That is the system
working as designed; it still means "add a field" is a three-file commit
(`schemas.py`, the version-pin test, the fixture), not a one-file one, and
whoever implements this should budget for that rather than discover it.

**The router has no capability model, only a per-provider if/elif.**
`build_argv` (`llm_router.py:125`) and the `local` HTTP path (`llm_router.py:
267` onward) each hard-code what a provider's wire format looks like; nothing
in `core/roles.py` or `CALL_SITE_DEFAULTS` (`llm_router.py:64`) records what a
provider or a call-site can carry. Attaching an image to a review prompt is
therefore not "pass one more string": it needs the router to know, per
provider, whether images are supported at all, and per call-site, whether the
call is asking for them. A design sketch, not a decision:

- A call-site that wants artifacts states so, likely a boolean or a set on
  the call-site's entry in `CALL_SITE_DEFAULTS` or a new small registry
  alongside it, the same place a call-site already states its default
  `{provider, model, effort}`.
- A provider states what it can carry. For `local`, this is close to free:
  an OpenAI-compatible vision model accepts image content blocks in the same
  `messages` array `llm_router.py:280` already builds, so the change there is
  additive. For `agy`, the field report is evidence the underlying CLI can
  read an image, but nothing here yet confirms whether `agy`'s
  non-interactive `--print` path (already known to be limited as a brain,
  per `CLAUDE.md`'s `agy` gotcha) accepts a file argument the same way it
  accepts a prompt string in argv (`llm_router.py:142-149`). That has to be
  probed, not assumed, before it is claimed as a supported path. `claude` and
  `codex` are unverified either way.
- **What happens when a role chain resolves to a provider that cannot carry
  images is the operative question, not a footnote.** A role chain
  (`config/praxis.yaml`'s `review: [sonnet, haiku]`, resolved by
  `EffectiveSettings.call_site_chain`) can fall through providers on
  unavailability already (`core/provider_errors.is_unavailability`); an
  artifact-bearing call needs the same fallback to trigger on "cannot carry
  images" as it does on "is down," or a review silently reviews text-only
  with the artifacts dropped on the floor and no one told. Silent
  degradation here reproduces exactly the failure this spec exists to close:
  a check that looks like it ran and did not.

Neither blocker is a reason not to build this. Both are reasons this needed
a spec before a plan: the size of the change is not visible from the diff of
the change itself, which is a small irony given the subject.

---

## The honesty defect rendered evidence would also have caught

The same worker's findings report, in its preamble, confabulated a viewport
list: it claimed `1366x768` and `1920x1080` were captured when neither was.
Every per-finding citation later in the same document was accurate; only the
summary sentence was invented. A reviewer reading the report and not the
renders absorbs a false claim about coverage that nothing in the text
contradicts.

This is a second, independent argument for the same feature. Rendered
artifacts are not only evidence for a defect class text cannot describe (the
inert-property case above); they are also a check the reviewer can perform
against a claim, rather than prose the reviewer has no choice but to trust.
A reviewer with the actual screenshots in front of it can notice a report
claims a viewport that is not among them. A reviewer with only the report has
no way to notice anything.

---

## What was rejected in favour of this, and why

Two cheaper alternatives came out of the same field report. Both are worth
recording here so a later reader does not re-propose them without knowing
they were already weighed.

**A blast-radius heuristic, being implemented separately in this same
remediation batch.** Count repo-wide occurrences of each changed selector or
symbol and hand the reviewer that number: "this change modifies `.s-alert`,
which occurs 373 times." The reporter believed this alone would have caught
the case. It is real signal and cheap to compute, but it is a prompt to look
harder, not a way to see the thing itself: a high occurrence count says a
selector is widely used, not that a specific downstream rule went inert. It
is a complement to rendered evidence, not a substitute for it, and should
ship regardless of this spec's fate.

**A mechanical "property went inert" CSS lint. Rejected.** A rule that
flagged `justify-content` outside a flex container would be a
language-specific check, in a language-agnostic orchestrator, covering
exactly one property family in exactly one language, with no closed form for
the general class ("a declaration whose effect depends on an ancestor
property that changed elsewhere in the same diff" has no finite rule set).
Praxis already has the right seam for a repo-specific mechanical check of
this kind: `verify_cmd`, which runs on the checkout before the reviewer sees
the diff (`orchestrator_review.py:455-500`) and is exactly where a project
that wants this should put its own linter, stylelint config, or visual
regression tool. Building a bespoke inert-property detector into the engine
would duplicate a seam that already exists and covers the general case this
one instance is a member of.

---

## Open questions

1. **The call-site-shape fork above** (fold into the existing `review_diff`
   call versus F17's separate `review_artifact` call) is unresolved here.
   Whoever writes the implementing plan should settle it against both specs
   together, not re-derive it from only one.
2. **Cost per review when images are attached.** Vision-capable calls are
   typically priced and token-accounted differently from text; no number
   exists yet for any provider Praxis would route this through.
3. **Where the field belongs: task or project.** The brief's `artifacts`
   field is per-task (on `LeafTask` and the dispatch contracts). F17's
   `artifact_cmd` is per-project, matching `verify_cmd`. A worker-driven UI
   audit is naturally per-task (the worker decides what to render, as in the
   field report); a project with a fixed screenshot sweep is naturally
   per-project. These may both be needed rather than being alternatives.
4. **What happens on a harness that cannot produce artifacts at all.**
   Whether an absent `artifacts` directory at review time is silently
   skipped (matching how an unconfigured `verify_cmd` is "not configured,
   never a pass") or is itself a signal worth surfacing.
5. **Before/after pairs, or only after.** The field report's clearest signal
   was a DIFF of renders (93 of 119 images changed) rather than any single
   image. Whether the reviewer needs both states or only the resulting one
   is not settled by anything in this document.
