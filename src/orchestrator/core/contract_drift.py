"""Does this diff edit a file the PLAN named but never authorised?

The question this module asks is deliberately not the question
``leaf_validator._check_plan_text_verbatim`` asks, and the difference is the
whole reason this exists.

That rule grades a leaf's PROSE against the plan document at DECOMPOSITION
time, and it was measured over 16 real decompositions on 2026-08-27: it fires
on 19 of 31 faithful leaves, because a decomposer that strips a bullet's
markdown backticks for a plain-text worker prompt, or that elaborates a
requirement into steps a floor model can execute, is doing its job and scores
as drift. Prose similarity measures copying STYLE.

This asks about PATHS, which are exact tokens, and it asks at the MERGE GATE,
where a human adjudicates rather than a gate blocking. Both differences matter:

- A path is a fact. ``src/playground/test_hm.py`` either appears in the diff
  and in the plan's ``Files:`` lines or it does not; there is nothing to tune.
- At the gate a false positive costs one glance. As a validator rule the same
  false positive would spend a brain call on a re-ask and can fail the plan
  after the re-ask budget, which is why the section-scoped and document-scoped
  forms of this were both refused as F3 rules (``docs/gotchas.md``).

The seat is the one that actually held. In the round-7 fabrication every gate
behaved correctly and only the merge gate stopped the work: the reviewer graded
the diff against the leaf's ``plan_text`` and the ``plan_text`` ordered the
rewrite, so ``pass`` was right on the evidence it had, and the human was shown
"review verdict: pass" with nothing saying the diff had rewritten the file the
plan called its contract.

**Two tiers, and the split is derived, not tuned.**

``named_not_authorised``
    The plan document MENTIONS this path and never puts it in a ``Files:``
    line. By construction that is a path the plan talks about for some reason
    other than assigning it as work - the acceptance bar, a contract, a module
    to read. This is the strong signal.

``unmentioned``
    The plan never names the path at all. Usually benign: the decompose
    prompt's own sizing rule tells a leaf to carry a NEW sibling test file, and
    a new module often needs its package ``__init__.py`` edited. Reported, but
    as the weak tier.

Measured on the real artefacts (2026-08-27), which is what justified building
this rather than reasoning about it:

===========================  =============================  ==============
case                         named_not_authorised           unmentioned
===========================  =============================  ==============
playground PR #103           ``src/playground/test_hm.py``  --
playground PR #105 (honest)  --                             --
playground PR #106 (honest)  --                             --
faithful replay leaf 2       --                             ``__init__.py``
===========================  =============================  ==============

The file the plan called its contract lands in the strong tier and the known
benign case lands in the weak one, without a threshold anywhere. **One real
defect is still one real defect**: this discriminates on the evidence available
and that evidence is thin on the positive side. It is advisory for that reason
as well as the design one - it annotates the gate, it never blocks.

Ungradable is a first-class answer, the same way ``core/context_window`` says
"unknown" rather than guessing a number: a plan document that authorises no
path at all cannot answer "was this path authorised", and a task with no plan
document behind it (a bare ``dispatch_task``) was never graded against a plan
in the first place.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field


#: A ``Files:`` line in a plan document, as the decomposition standard asks
#: authors to write it. Leading markdown decoration (``- ``, ``**``, ``#``) is
#: tolerated because real plans carry all three, and the label may be bolded
#: (``**Files**``) on its own line, which is why the colon is optional.
_FILES_LABEL_RE = re.compile(r"^\s*[-*#>\s]*\**\s*files\b", re.IGNORECASE)

#: Anything that looks like a path with an extension. Deliberately loose on the
#: left (a bare ``hm.py`` and a full ``src/playground/hm.py`` both match) and
#: bounded on the right, so a sentence-ending period does not become part of a
#: two-letter extension.
_PATH_RE = re.compile(r"[\w][\w./-]*\.\w{1,5}")

#: Unified-diff file headers. BOTH sides are read: a file the worker CREATED
#: has ``--- /dev/null`` and only the ``+++`` side carries its path, and a
#: created file is exactly the case an "edited a file it should not have"
#: check must not miss (a worker can replace a contract by deleting and
#: re-adding it).
_OLD_RE = re.compile(r"^--- (?:a/)?(.+?)\s*$")
_NEW_RE = re.compile(r"^\+\+\+ (?:b/)?(.+?)\s*$")

_DEV_NULL = "/dev/null"

#: Why a task could not be graded. Both are ANSWERS, not failures, and both are
#: rendered to the human so that "no drift found" and "nobody could look" never
#: read alike.
NO_PLAN_DOCUMENT = (
    "not graded: this task was not dispatched from a plan document, so there is "
    "nothing that could have authorised a path"
)
PLAN_AUTHORISES_NOTHING = (
    "not graded: the plan document carries no 'Files:' line, so it authorises no "
    "path and cannot say whether this diff went outside one"
)


@dataclass(frozen=True)
class ContractDrift:
    """What a task's diff did relative to the paths its plan authorised."""

    gradable: bool
    why_not: str = ""
    named_not_authorised: list[str] = field(default_factory=list)
    unmentioned: list[str] = field(default_factory=list)
    authorised: list[str] = field(default_factory=list)
    #: Paths this diff CREATED that the plan document describes as already
    #: existing ("the package already has X", "next to the existing helper").
    #: Probe 9e (2026-09-05): a plan asserted a `tokenizer.py` with `words`
    #: and `_normalize`; neither existed; the worker invented both, the
    #: reviewer praised it for "reusing _normalize", and the two tiers above
    #: were silent because the path WAS authorised. A false premise in the
    #: plan is the one thing a review graded against the leaf's text cannot
    #: see, and the diff plus the plan's own wording is enough to say it.
    created_described_as_existing: list[str] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        """True when the check RAN and found nothing.

        Never true for an ungradable task: "nothing was found" and "nothing was
        looked for" are the two states this whole module exists to keep apart.
        """
        return (
            self.gradable
            and not self.named_not_authorised
            and not self.unmentioned
            and not self.created_described_as_existing
        )


def changed_paths(diff: str) -> list[str]:
    """Return every repository path a unified diff touches, in order.

    Args:
        diff: Unified diff text.

    Returns:
        De-duplicated paths, ``/dev/null`` excluded.
    """
    seen: dict[str, None] = {}
    for line in (diff or "").splitlines():
        for pattern in (_OLD_RE, _NEW_RE):
            match = pattern.match(line)
            if match is None:
                continue
            path = match.group(1).strip()
            # A rename or a timestamped header can carry a trailing tab field.
            path = path.split("\t")[0].strip()
            if path and path != _DEV_NULL:
                seen.setdefault(path, None)
    return list(seen)


#: Wording that says a path is ALREADY THERE. Deliberately narrow: a plan
#: that says "Create src/x.py" must never match, and a match needs the cue
#: and the path on the same line.
_EXISTING_CUE_RE = re.compile(
    r"\b(already (?:has|have|exists?|contains?|provides?)|existing|next to the)\b",
    re.IGNORECASE,
)


def created_paths(diff: str) -> list[str]:
    """Return every path a unified diff CREATES (``--- /dev/null`` headers)."""
    created: list[str] = []
    previous_was_dev_null = False
    for line in (diff or "").splitlines():
        old = _OLD_RE.match(line)
        if old is not None:
            previous_was_dev_null = old.group(1).strip() == _DEV_NULL
            continue
        new = _NEW_RE.match(line)
        if new is not None:
            path = new.group(1).strip().split("\t")[0].strip()
            if previous_was_dev_null and path and path != _DEV_NULL:
                created.append(path)
            previous_was_dev_null = False
    return created


def plan_paths_described_as_existing(plan_document: str) -> set[str]:
    """Paths the plan mentions on a line that also says they already exist."""
    out: set[str] = set()
    for line in (plan_document or "").splitlines():
        if _EXISTING_CUE_RE.search(line):
            out.update(_PATH_RE.findall(line))
    return out


def plan_authorised_paths(plan_document: str) -> set[str]:
    """Return every path the plan's own ``Files:`` lines assign as work."""
    out: set[str] = set()
    for line in (plan_document or "").splitlines():
        if _FILES_LABEL_RE.match(line):
            out.update(_PATH_RE.findall(line))
    return out


def plan_mentioned_paths(plan_document: str) -> set[str]:
    """Return every path the plan document names ANYWHERE, authorised or not."""
    return set(_PATH_RE.findall(plan_document or ""))


def _matches(changed: str, known: set[str]) -> bool:
    """True when *changed* is the same file as one the plan names.

    A plan writes ``src/playground/hm.py`` and sometimes just ``hm.py``, while
    a diff always carries the repository-root path. Comparing on equality alone
    made every abbreviated plan reference read as unauthorised. Suffix matching
    is bounded to a path BOUNDARY so that ``src/a/test_hm.py`` never satisfies
    a plan that only named ``hm.py``.
    """
    if changed in known:
        return True
    return any(
        changed.endswith("/" + name) or name.endswith("/" + changed) for name in known
    )


def assess(diff: str, plan_document: str | None) -> ContractDrift:
    """Compare a task's diff against the paths its plan document authorises.

    Args:
        diff: Unified diff text for the task's own work.
        plan_document: The externally-authored plan the task came from, or
            None when the task was not dispatched from one.

    Returns:
        A :class:`ContractDrift`. ``gradable`` False carries ``why_not`` and
        no findings; the caller must render the reason rather than silence.
    """
    if not plan_document or not plan_document.strip():
        return ContractDrift(gradable=False, why_not=NO_PLAN_DOCUMENT)

    authorised = plan_authorised_paths(plan_document)
    if not authorised:
        return ContractDrift(gradable=False, why_not=PLAN_AUTHORISES_NOTHING)

    mentioned = plan_mentioned_paths(plan_document)
    named: list[str] = []
    unmentioned: list[str] = []
    for path in changed_paths(diff):
        if _matches(path, authorised):
            continue
        if _matches(path, mentioned):
            named.append(path)
        else:
            unmentioned.append(path)

    described = plan_paths_described_as_existing(plan_document)
    phantom = [p for p in created_paths(diff) if _matches(p, described)]

    return ContractDrift(
        gradable=True,
        named_not_authorised=named,
        unmentioned=unmentioned,
        authorised=sorted(authorised),
        created_described_as_existing=phantom,
    )


def summary_line(drift: ContractDrift) -> str:
    """One sentence for a human at the merge gate.

    Written for the reader who is about to approve a pull request and has the
    review's own ``pass`` in front of them. It states what the diff did and
    what the plan said, and it does NOT tell them the work is wrong: the strong
    tier caught a real fabrication once and would also fire on a leaf that
    legitimately had to touch a file the plan mentioned in passing.
    """
    if not drift.gradable:
        return drift.why_not
    if drift.clean:
        return "Plan paths: this diff stayed inside the paths the plan authorised."
    parts: list[str] = []
    if drift.created_described_as_existing:
        parts.append(
            "the plan describes "
            + ", ".join(drift.created_described_as_existing)
            + " as already existing, but this diff created it from nothing - the "
            'plan\'s premise was false and whatever the worker "reused" is its '
            "own invention; read the file before approving"
        )
    if drift.named_not_authorised:
        parts.append(
            "this diff edits "
            + ", ".join(drift.named_not_authorised)
            + ", which the plan NAMES but never authorises for any task - check "
            "it is not the plan's acceptance contract before approving"
        )
    if drift.unmentioned:
        parts.append(
            "it also touches "
            + ", ".join(drift.unmentioned)
            + ", which the plan does not name at all (often a new sibling file, "
            "and usually fine)"
        )
    return "Plan paths: " + "; ".join(parts) + "."


def decode_payload(value: object) -> dict[str, object] | None:
    """Turn the stored column into a dict, or None for "not checked".

    The single decoder. Both the raw ``GET /api/tasks/{id}`` route and
    ``TaskResponse`` call it, so the CLI, the dashboard and MCP cannot end up
    parsing this three different ways.

    Anything that will not decode becomes None rather than raising: the field
    is advisory, and a corrupt row must not be able to 500 the endpoint that
    lists a plan's tasks.
    """
    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or not value:
        return None
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError):
        return None
    return decoded if isinstance(decoded, dict) else None


def as_payload(drift: ContractDrift) -> dict[str, object]:
    """Render a drift verdict for storage on the task row and for the API."""
    return {
        "gradable": drift.gradable,
        "why_not": drift.why_not,
        "named_not_authorised": list(drift.named_not_authorised),
        "unmentioned": list(drift.unmentioned),
        "created_described_as_existing": list(drift.created_described_as_existing),
        "summary": summary_line(drift),
    }
