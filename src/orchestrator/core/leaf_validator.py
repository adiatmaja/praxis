"""Deterministic leaf-task validator.

Validates decomposed leaf tasks against the capability profile and the
original plan text before dispatch.  Rules are split into HARD (block
dispatch) and SOFT (warnings only).
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any

from orchestrator.core.leaf_templates import missing_sections
from orchestrator.models.schemas import CapabilityProfile, LeafTask, LeafType


logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Defaults (overridable via CapabilityProfile attributes)
# ---------------------------------------------------------------------------

_DEFAULT_MAX_FILES = 5
_DEFAULT_MAX_LOC = 300
_DEFAULT_MAX_CHECKLIST = 12
_DEFAULT_MAX_DEP_DEPTH = 4
_DEFAULT_VERIFICATION_MIN_LEN = 5
_DEFAULT_VERBATIM_THRESHOLD = 0.70

# Phrases that signal a vague, non-actionable task.
_VAGUE_PHRASES = [
    r"\bimprove\b",
    r"\boptimize\b",
    r"\brefactor\b",
    r"\bclean up\b",
    r"\btidy\b",
    r"\bfix stuff\b",
    r"\bmake better\b",
    r"\bpolish\b",
    r"\benhance\b",
    r"\bgeneral improvements\b",
]

# Vague phrases a declared leaf type makes precise, and must therefore not be
# warned about. "refactor" in a REFACTOR_RENAME leaf is the accurate name for
# the work, not a failure to say what the work is: that type carries a HARD
# `Renames` section requirement, so a leaf that reached this rule has already
# stated its exact old-to-new symbol table. Without the exemption the warning
# is unfixable by construction -- the feedback reads "contains vague phrase
# matching '\brefactor\b'" on a leaf that genuinely IS a refactor -- so the
# informed re-ask cannot converge and only ends when the round budget runs out.
_TYPE_EXEMPT_PHRASES: dict[LeafType, frozenset[str]] = {
    LeafType.REFACTOR_RENAME: frozenset({r"\brefactor\b"}),
}

# Verification commands that are clearly not runnable.
_NON_RUNNABLE_PATTERNS = [
    r"\bmanually\b",
    r"\bvisually\b",
    r"\bby eye\b",
    # The space used to sit INSIDE the pattern, after the \b, which requires a
    # word character before it: "eyeball the output" was not matched at all.
    r"\beyeball\b",
    r"\bread through\b",
    r"\binspect\b",
    r"\breview\b",
]

# A verification that carries a real runnable command is runnable regardless of
# any manual-verb prose (or file paths) around it. Without this guard the blunt
# keyword scan above false-positives on legitimate commands whose surrounding
# text contains "review"/"inspect" (pervasive in this codebase: `review_task`,
# `test_orchestrator_review.py`, "no regression in existing review tests"). A
# backtick-wrapped command or a known runner token counts as a runnable signal.
_RUNNABLE_SIGNAL = re.compile(
    r"`[^`]+`"  # any backtick-wrapped command
    r"|\b(pytest|uv\s+run|npm|pnpm|yarn|make|go\s+test|cargo|ruff|mypy|tox|"
    r"pre-commit|python3?\s+-m|bash\s|sh\s|\./)",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class Violation:
    """A single validation finding."""

    rule: str
    task_id: str
    severity: str = "hard"
    message: str = ""


@dataclass
class ValidationResult:
    """Accumulator for validation findings."""

    hard: list[Violation] = field(default_factory=list)
    soft: list[Violation] = field(default_factory=list)

    @property
    def dispatchable(self) -> bool:
        """True when there are no HARD violations."""
        return len(self.hard) == 0

    @property
    def clean(self) -> bool:
        """True when there are no violations at all."""
        return len(self.hard) == 0 and len(self.soft) == 0

    def add(self, violation: Violation) -> None:
        if violation.severity == "hard":
            self.hard.append(violation)
        else:
            self.soft.append(violation)


# ---------------------------------------------------------------------------
# Graph helpers
# ---------------------------------------------------------------------------


def _detect_cycles(leaves: list[LeafTask]) -> list[list[str]]:
    """Return list of cycles (each cycle is a list of task ids)."""
    task_ids = {lt.id for lt in leaves}
    adj: dict[str, list[str]] = {
        lt.id: [d for d in lt.depends_on if d in task_ids] for lt in leaves
    }

    _white, _gray, _black = 0, 1, 2
    color: dict[str, int] = dict.fromkeys(adj, _white)
    cycles: list[list[str]] = []

    def dfs(node: str, path: list[str]) -> None:
        color[node] = _gray
        path.append(node)
        for nb in adj.get(node, []):
            if color[nb] == _gray:
                idx = path.index(nb)
                cycles.append(path[idx:])
            elif color[nb] == _white:
                dfs(nb, path)
        path.pop()
        color[node] = _black

    for tid in adj:
        if color[tid] == _white:
            dfs(tid, [])

    return cycles


def _max_dep_depth(leaves: list[LeafTask]) -> dict[str, int]:
    """Return {task_id: depth} where depth is the longest chain ending at that task."""
    task_ids = {lt.id for lt in leaves}
    adj: dict[str, list[str]] = {
        lt.id: [d for d in lt.depends_on if d in task_ids] for lt in leaves
    }

    memo: dict[str, int] = {}

    def depth(tid: str, visiting: set[str]) -> int:
        if tid in memo:
            return memo[tid]
        if tid in visiting:
            return 0
        visiting.add(tid)
        deps = adj.get(tid, [])
        if not deps:
            memo[tid] = 0
        else:
            memo[tid] = 1 + max(depth(d, visiting) for d in deps)
        visiting.discard(tid)
        return memo[tid]

    for tid in adj:
        depth(tid, set())

    return memo


# ---------------------------------------------------------------------------
# Fuzzy matching
# ---------------------------------------------------------------------------


def _ratio(a: str, b: str) -> float:
    """Normalized similarity between two strings."""
    return SequenceMatcher(None, a, b).ratio()


def _verbatim_coverage(section: str, plan_text: str) -> float:
    """Fraction of *section*'s non-blank lines reproduced inside *plan_text*.

    Args:
        section: The source plan section this leaf was cut from.
        plan_text: The leaf's contract text.

    Returns:
        0.0 to 1.0.  An empty section returns 1.0: nothing was asked for, so
        nothing can be missing.
    """
    lines = [line.strip() for line in section.splitlines() if line.strip()]
    if not lines:
        return 1.0
    return sum(1 for line in lines if line in plan_text) / len(lines)


def _section_for_task(source: str, leaf: LeafTask) -> str:
    """Extract the section of the source plan that corresponds to *leaf*.

    Args:
        source: The externally-authored plan text.
        leaf: The leaf whose section is wanted.

    Returns:
        The source lines under the heading that names this leaf, or ``""`` when
        no heading names it.  ``""`` is the honest answer and the caller skips
        it: returning the WHOLE document instead makes the verbatim ratio
        measure what fraction of the plan this one leaf is, not whether the
        leaf quoted its own section, so on any multi-task plan every leaf reads
        as a mismatch no matter how faithfully it copied.  Praxis says
        "unknown" rather than guessing (see ``core/context_window`` and
        ``core/verify_gate.normalize_verify_cmd``).
    """
    title = leaf.title.strip()
    if not title:
        return ""

    # The title may sit ANYWHERE in the heading line, not just straight after
    # the hashes: Praxis's own authored plans head their tasks "### Task 3:
    # <title>" (core/execute_plan_decompose._PLAN_TASK_HEADER_RE is the same
    # shape), and anchoring the title to the hashes never matched one of them.
    # The lookarounds replace a trailing \b, which could never match a title
    # ending in ')', '"', '.' or '?': \b after a non-word character demands a
    # word character next, and the end of a heading line never supplies one.
    heading_re = re.compile(
        r"^#{1,6}\s+.*?(?<!\w)" + re.escape(title) + r"(?!\w)",
        re.MULTILINE | re.IGNORECASE,
    )
    match = heading_re.search(source)
    if not match:
        logger.debug(
            "No plan heading names leaf %s (%r); skipping the verbatim check",
            leaf.id,
            title,
        )
        return ""

    start = match.end()
    next_heading = re.search(r"^#{1,6}\s+", source[start:], re.MULTILINE)
    end = start + next_heading.start() if next_heading else len(source)

    return source[start:end].strip()


# ---------------------------------------------------------------------------
# Rule implementations
# ---------------------------------------------------------------------------


def _check_duplicate_ids(
    leaves: list[LeafTask],
    result: ValidationResult,
) -> None:
    """HARD: no two leaves in one set may carry the same ``id``.

    Nothing upstream enforces this.  ``LeafTask.id`` is a bare ``str``, the
    decompose prompt never asks for uniqueness, and the triage prompt asks only
    that a child's ``depends_on`` name its SIBLINGS.  A repeated id is therefore
    a shape the brain can legitimately return, and it collapses every map keyed
    on the id at once -- the sibling edge in ``core/leaf_split``, the adjacency
    ``_detect_cycles`` walks, the slug the capability events name, and the
    per-child difficulty score -- each of them silently, to whichever leaf
    happened to come LAST.

    The collapse is invisible from outside: the graph reads healthy, the
    misordered leaf fails on its own verification, and ``task_outcomes`` records
    that failure against the WORKER, which feeds the miswiring back into
    capability calibration as evidence the model is weaker than it is.  So this
    is a gate, not a repair: an id that names two leaves names no leaf.
    """
    for task_id, count in Counter(lt.id for lt in leaves).items():
        if count > 1:
            result.add(
                Violation(
                    rule="duplicate_id",
                    task_id=task_id,
                    message=(
                        f"id '{task_id}' is shared by {count} leaves; every "
                        "dependency, event and score keyed on it would name "
                        "only the last one"
                    ),
                )
            )


def _check_dangling_dep(
    leaves: list[LeafTask],
    result: ValidationResult,
) -> None:
    task_ids = {lt.id for lt in leaves}
    for leaf in leaves:
        for dep in leaf.depends_on:
            if dep not in task_ids:
                result.add(
                    Violation(
                        rule="dangling_dep",
                        task_id=leaf.id,
                        message=f"depends on unknown task '{dep}'",
                    )
                )


def _check_dep_cycles(
    leaves: list[LeafTask],
    result: ValidationResult,
) -> None:
    cycles = _detect_cycles(leaves)
    for cycle in cycles:
        for tid in cycle:
            result.add(
                Violation(
                    rule="dep_cycle",
                    task_id=tid,
                    message=f"cycle: {' -> '.join(cycle)}",
                )
            )


def _check_dep_depth(
    leaves: list[LeafTask],
    profile: CapabilityProfile,
    result: ValidationResult,
) -> None:
    max_depth = getattr(profile, "max_dep_depth", _DEFAULT_MAX_DEP_DEPTH)
    depths = _max_dep_depth(leaves)
    for tid, d in depths.items():
        if d > max_depth:
            result.add(
                Violation(
                    rule="dep_depth",
                    task_id=tid,
                    message=f"dependency depth {d} exceeds limit {max_depth}",
                )
            )


def _check_max_files(
    leaves: list[LeafTask],
    profile: CapabilityProfile,
    result: ValidationResult,
) -> None:
    limit = getattr(profile, "max_files_touched", _DEFAULT_MAX_FILES)
    for leaf in leaves:
        if len(leaf.files) > limit:
            result.add(
                Violation(
                    rule="max_files",
                    task_id=leaf.id,
                    message=f"{len(leaf.files)} files exceeds limit {limit}",
                )
            )


def _check_max_loc(
    leaves: list[LeafTask],
    profile: CapabilityProfile,
    result: ValidationResult,
) -> None:
    limit = getattr(profile, "max_loc_delta", _DEFAULT_MAX_LOC)
    for leaf in leaves:
        if leaf.estimated_loc is not None and leaf.estimated_loc > limit:
            result.add(
                Violation(
                    rule="max_loc",
                    task_id=leaf.id,
                    message=f"estimated LOC {leaf.estimated_loc} exceeds limit {limit}",
                )
            )


def verification_defect(value: str | None) -> str | None:
    """Return why *value* is unusable as an acceptance check, or None if it is fine.

    This is the single decision behind BOTH the HARD ``verification`` rule and
    :func:`is_runnable_verification`, so the validator and the dispatch path can
    never disagree about what counts as a real check.

    Args:
        value: A leaf's ``verification`` string, or None.

    Returns:
        A human-readable reason, or None when the value is acceptable.
    """
    if value is None or not value.strip():
        return "missing verification command"
    if len(value.strip()) < _DEFAULT_VERIFICATION_MIN_LEN:
        return "missing or too short verification command"
    if _RUNNABLE_SIGNAL.search(value):
        # Carries a real command; manual-verb prose around it is fine.
        return None
    for pat in _NON_RUNNABLE_PATTERNS:
        if re.search(pat, value, re.IGNORECASE):
            return f"verification is not runnable: '{value}'"
    return None


def is_runnable_verification(value: str | None) -> bool:
    """True when *value* is an acceptance check the HARD rule would accept.

    Deliberately no stricter than the rule it mirrors: a value that passes
    ``validate_leaves`` must also pass here, or a leaf that was validated would
    be treated as junk at dispatch.

    Args:
        value: A leaf's ``verification`` string, or None.

    Returns:
        True when the value may be used as a worker's acceptance floor.
    """
    return verification_defect(value) is None


def normalize_verification(verification: Any) -> str | None:
    """Return the leaf's acceptance check as a string, or None.

    ``plan_task`` is raw brain JSON on every path but decomposition, so
    ``verification`` can be any shape, and this must never raise: a
    ``TypeError`` here aborts a whole loop pass. A non-string used to reach the
    worker's acceptance floor as a Python repr, and a ``{"cmd": "pytest -q"}``
    repr even beat a configured project ``verify_cmd``. Treating it as absent
    falls back to that command instead.

    Lives here rather than beside its first caller because THREE places now ask
    the same question of the same column -- the dispatch path building the
    worker's acceptance floor, :func:`shell_command_for_verification`, and
    through it the review path -- and ``orchestrator_dispatch`` imports
    ``orchestrator_review``, so a helper the review path could import cannot
    live there. Two copies would let a worker be told one check and judged
    against another, the same failure ``plan_graph.declared_paths`` exists to
    prevent for edit locations.

    Args:
        verification: The raw ``verification`` value from the plan task.

    Returns:
        The check, or None when the value is not a non-blank string.
    """
    if not isinstance(verification, str) or not verification.strip():
        return None
    return verification


# Program names a leaf's own ``verification`` may start with for Praxis to run
# it ITSELF. Build and test tooling only: a network client or a container
# runtime has no business being a leaf's acceptance check, and this list is the
# place that stays reviewable.
_VERIFICATION_RUNNERS = frozenset(
    {
        "bash",
        "bundle",
        "cargo",
        "cmake",
        "composer",
        "ctest",
        "dart",
        "deno",
        "dotnet",
        "flutter",
        "go",
        "gradle",
        "grep",
        "hatch",
        "java",
        "javac",
        "julia",
        "make",
        "mvn",
        "mypy",
        "nim",
        "node",
        "nox",
        "npm",
        "npx",
        "phpunit",
        "pip",
        "pipx",
        "pnpm",
        "poetry",
        "pre-commit",
        "pyright",
        "pytest",
        "python",
        "rake",
        "rg",
        "rscript",
        "rspec",
        "ruff",
        "rustc",
        "sh",
        "swift",
        "test",
        "tox",
        "uv",
        "uvx",
        "yarn",
        "zig",
        "zsh",
    }
)

# ``PYTHONPATH=src pytest -q`` names the program in its SECOND token. Anything
# that is not an assignment ends the prefix.
_ENV_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")

# Trailing version digits on an interpreter name: ``python3``, ``python3.11``.
_VERSION_SUFFIX = "0123456789."


def shell_command_for_verification(value: Any) -> str | None:
    """Return *value* as a command Praxis may run itself, or None for "not one".

    DELIBERATELY NARROWER than :func:`is_runnable_verification`, and the
    difference is the whole point. That predicate asks "is this bad enough to
    block a leaf", so it accepts any string of five characters or more that
    carries no manual verb -- ``"the module imports cleanly"`` passes it. That
    is correct for its job (``orchestrator_dispatch`` states, in its own words,
    that the demotion "only catches prose the HARD rule recognizes as junk, and
    deliberately goes no further") and catastrophic for this one: handing that
    sentence to a shell yields ``the: command not found``, exit 127, and a task
    FAILED on evidence Praxis fabricated about a worker. The same asymmetry is
    already recorded one module over, where ``difficulty`` keeps a stricter
    private signal and says the two "must not be merged".

    So a value is accepted only when it NAMES a program: after an optional
    ``VAR=value`` prefix, the first token is a path (``./scripts/check.sh``) or
    one of :data:`_VERIFICATION_RUNNERS`. Everything else is reported as absent.
    That direction is the safe one BY CONSTRUCTION -- the caller's "no runnable
    check" arm never fails a task -- so an unrecognised runner costs a signal,
    while a recognised sentence costs a false accusation.

    This does NOT make the string safe; nothing here is a security boundary. A
    leaf's verification is brain output, and ``pytest -q; anything`` starts with
    an accepted token. It is trusted on exactly the ground the plan document
    itself is: the operator asked Praxis to execute this plan, and the worker
    container is already told to run this same string. What this narrows is
    ACCURACY, not privilege.

    Args:
        value: The raw ``verification`` value from the plan task.

    Returns:
        The command to run, unwrapped from a single pair of backticks and
        stripped, or None when the value does not name a program.
    """
    command = normalize_verification(value)
    if command is None:
        return None
    command = command.strip()
    # A brain that wrote ```pytest -q``` meant the command, not the quoting.
    if len(command) > 2 and command.startswith("`") and command.endswith("`"):
        command = command[1:-1].strip()
    # A surviving backtick means prose WRAPPED a command rather than being one
    # ("run `pytest -q` and confirm"), and picking the command out of a sentence
    # is a guess. A newline means several steps, same answer.
    if not command or "\n" in command or "`" in command:
        return None
    # Never looser than the rule that let the leaf through in the first place.
    if not is_runnable_verification(command):
        return None
    tokens = command.split()
    while tokens and _ENV_ASSIGNMENT.match(tokens[0]):
        tokens.pop(0)
    if not tokens:
        return None
    # A program whose path contains a space is QUOTED, and the quote is part of
    # the shell's syntax, not of the name. Found by running this against a real
    # repository, where the leaf's own check was
    # ``"C:\\...\\python.exe" -c "import leaf"`` and was reported as no check at
    # all. Both separators, because the same string is written both ways.
    head = tokens[0].strip("\"'")
    if "/" in head or "\\" in head:
        return command
    if head.lower().rstrip(_VERSION_SUFFIX) in _VERIFICATION_RUNNERS:
        return command
    return None


def _check_verification(
    leaves: list[LeafTask],
    result: ValidationResult,
) -> None:
    for leaf in leaves:
        defect = verification_defect(leaf.verification)
        if defect is not None:
            result.add(
                Violation(
                    rule="verification",
                    task_id=leaf.id,
                    message=defect,
                )
            )


def _check_escalate_mismatch(
    leaves: list[LeafTask],
    profile: CapabilityProfile,
    result: ValidationResult,
) -> None:
    escalate_types = getattr(profile, "escalate_task_types", [])
    if not escalate_types:
        return
    for leaf in leaves:
        if (
            leaf.task_type
            and leaf.task_type in escalate_types
            and not leaf.needs_stronger_model
        ):
            result.add(
                Violation(
                    rule="escalate_mismatch",
                    task_id=leaf.id,
                    message=f"task type '{leaf.task_type}' should escalate",
                )
            )


def _check_leaf_template(
    leaves: list[LeafTask],
    result: ValidationResult,
) -> None:
    """HARD: every leaf's plan_text carries its type's required sections.

    Rule 2 of the standard (no branching decision left to the worker) is
    enforced structurally: a leaf that does not state its Goal, Files, Steps,
    and Acceptance has left scoping judgment to the worker by omission.
    """
    for leaf in leaves:
        absent = missing_sections(leaf.plan_text, leaf.leaf_type)
        if absent:
            result.add(
                Violation(
                    rule="leaf_template",
                    task_id=leaf.id,
                    message=(
                        f"leaf_type '{leaf.leaf_type.value}' requires plan_text "
                        f"sections that are missing: {', '.join(absent)}"
                    ),
                )
            )


def _check_plan_text_verbatim(
    leaves: list[LeafTask],
    source: str,
    result: ValidationResult,
) -> None:
    threshold = _DEFAULT_VERBATIM_THRESHOLD
    for leaf in leaves:
        section = _section_for_task(source, leaf)
        if not section:
            continue
        # Two ways to be faithful, because plan_text is a labelled SKELETON
        # that carries the source lines, not a bare excerpt (see the decompose
        # prompt). The symmetric ratio measures how much of plan_text IS the
        # section, so the Goal/Files/Steps/Acceptance labels count against it,
        # and on a short section they outweigh the lines that were copied: a
        # faithful copy of a one-line section scores about 0.36. Line coverage
        # asks the question the prompt actually asks -- are the plan's own
        # lines in there -- and does not care how much scaffolding surrounds
        # them. A paraphrase still fails both.
        if (
            _ratio(leaf.plan_text, section) < threshold
            and _verbatim_coverage(section, leaf.plan_text) < threshold
        ):
            result.add(
                Violation(
                    rule="plan_text_verbatim",
                    task_id=leaf.id,
                    severity="soft",
                    message="plan_text does not closely match source plan section",
                )
            )


def _check_file_overlap(
    leaves: list[LeafTask],
    result: ValidationResult,
) -> None:
    dep_edges: set[tuple[str, str]] = set()
    for leaf in leaves:
        for dep in leaf.depends_on:
            dep_edges.add((dep, leaf.id))
            dep_edges.add((leaf.id, dep))

    file_owners: dict[str, list[str]] = {}
    for leaf in leaves:
        for f in leaf.files:
            file_owners.setdefault(f, []).append(leaf.id)

    for fpath, owners in file_owners.items():
        if len(owners) < 2:
            continue
        for i in range(len(owners)):
            for j in range(i + 1, len(owners)):
                a, b = owners[i], owners[j]
                if (a, b) not in dep_edges:
                    result.add(
                        Violation(
                            rule="file_overlap",
                            task_id=a,
                            severity="soft",
                            message=f"shares file '{fpath}' with {b} without dep edge",
                        )
                    )


def _check_checklist_size(
    leaves: list[LeafTask],
    profile: CapabilityProfile,
    result: ValidationResult,
) -> None:
    limit = getattr(profile, "max_checklist_items", _DEFAULT_MAX_CHECKLIST)
    for leaf in leaves:
        if len(leaf.checklist) > limit:
            result.add(
                Violation(
                    rule="checklist_size",
                    task_id=leaf.id,
                    severity="soft",
                    message=f"{len(leaf.checklist)} checklist items exceeds limit {limit}",
                )
            )


def _check_vague_phrase(
    leaves: list[LeafTask],
    result: ValidationResult,
) -> None:
    for leaf in leaves:
        text = f"{leaf.title} {leaf.description} {leaf.plan_text}".lower()
        exempt = _TYPE_EXEMPT_PHRASES.get(leaf.leaf_type, frozenset())
        for pat in _VAGUE_PHRASES:
            if pat in exempt:
                continue
            if re.search(pat, text, re.IGNORECASE):
                result.add(
                    Violation(
                        rule="vague_phrase",
                        task_id=leaf.id,
                        severity="soft",
                        message=f"contains vague phrase matching '{pat}'",
                    )
                )
                break


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def validate_leaves(
    opus_plan: dict,
    profile: CapabilityProfile,
    source_plan: str,
    leaves: list[LeafTask] | None = None,
) -> ValidationResult:
    """Run all HARD and SOFT validation rules against decomposed leaf tasks.

    Args:
        opus_plan: The opus plan dict (used for context; structure validated
            implicitly via the leaves list).
        profile: Capability profile defining limits.
        source_plan: Original plan.md text for verbatim checks.
        leaves: Parsed leaf tasks. If *None*, returns a clean result.

    Returns:
        ValidationResult with accumulated violations.
    """
    result = ValidationResult()

    if not leaves:
        return result

    # HARD rules. Identity FIRST, and alone if it fires: every rule below is
    # keyed on ``leaf.id``, so on a repeated id they grade a graph that does not
    # exist -- ``_detect_cycles`` in particular turns a sibling edge into a self
    # edge and reports a cycle nobody wrote. One real finding beats a list of
    # invented ones, and the feedback the re-ask carries is this result.
    _check_duplicate_ids(leaves, result)
    if result.hard:
        return result

    _check_dangling_dep(leaves, result)
    _check_dep_cycles(leaves, result)
    _check_dep_depth(leaves, profile, result)
    _check_max_files(leaves, profile, result)
    _check_max_loc(leaves, profile, result)
    _check_verification(leaves, result)
    _check_escalate_mismatch(leaves, profile, result)
    _check_leaf_template(leaves, result)

    # SOFT rules
    _check_plan_text_verbatim(leaves, source_plan, result)
    _check_file_overlap(leaves, result)
    _check_checklist_size(leaves, profile, result)
    _check_vague_phrase(leaves, result)

    return result


def validate_split_children(
    children: list[LeafTask],
    profile: CapabilityProfile,
) -> ValidationResult:
    """Grade the children of an adaptive split against the rules that apply.

    ``validate_leaves`` grades a WHOLE graph before any of it has run.  Split
    children arrive mid-flight, on a plan whose other leaves may already be
    merged, and carry sibling-scoped ``depends_on`` that
    ``leaf_split.rewire_plan_for_split`` has not yet rewritten into plan slugs.
    Three rules therefore measure something that is not true here, so this runs
    the rest by CALLING THE SAME rule functions ``validate_leaves`` calls.  A
    second copy of any rule would let the hypothesis and its correction be
    graded differently, which is the drift ``core/leaf_templates`` exists to
    prevent.

    Applied, HARD:

    - ``duplicate_id``: FIRST, and alone if it fires.  Two children sharing one
      id collapse all four id-keyed maps the caller builds next, so this is the
      only place the shape can be caught before something acts on it.
    - ``leaf_template``: the point of the exercise.  The triage prompt renders
      the same ``core/leaf_templates`` block the decompose prompt does, and
      until this ran nothing graded the answer.
    - ``verification``: rule 3 of the standard.  A child with no runnable
      acceptance check has the project command silently substituted at dispatch
      (:func:`normalize_verification`), so the worker is graded on a check its
      own contract never named.
    - ``max_files`` / ``max_loc``: the sizing rules.  A split happens BECAUSE
      the parent was mis-sized, so an oversized child repeats the one failure
      that is already known to have occurred on this leaf.
    - ``escalate_mismatch``: per-leaf and profile-driven.  Inert unless the
      profile names ``escalate_task_types``, and where it fires it means the
      same thing for a child as for any other leaf.
    - ``dep_cycle``: over the SIBLING set, the one graph rule that is both
      meaningful and unguarded here.  Two children pointing at each other
      survive rewiring intact and neither ever becomes dispatchable, so the
      plan stalls for good with nothing raised anywhere.

    Applied, SOFT (recorded for the log, never blocking):

    - ``checklist_size`` and ``vague_phrase``: per-leaf, unchanged in meaning.
    - ``file_overlap``: siblings sharing a file with no dep edge between them
      get separate ``agent/`` branches and collide at merge.

    Not applied, because each would measure the wrong thing rather than merely
    repeat itself:

    - ``dangling_dep``: a child dep naming neither a sibling nor anything else
      resolvable is DROPPED by ``rewire_plan_for_split``, deliberately, and the
      parent's inherited deps already cover the ordering.  Rejecting a whole
      split over a fact the very next function repairs would trade a usable
      graph for a plain retry of the leaf that has already failed twice.
    - ``dep_depth``: the depth measurable inside a 2-to-4 child set is a
      FRAGMENT of the child's real chain, because the parent's own inherited
      depth is added only during rewiring.  The number would not be the leaf's
      depth, and a HARD rejection quoting a wrong number is worse than none.
    - ``plan_text_verbatim``: a child is a NEW contract the triage brain
      authored, not an excerpt of the source plan, and no plan heading names
      it.  ``_section_for_task`` would return ``""`` and the rule would skip
      every child; not calling it says so instead of implying it ran.

    Args:
        children: The brain's replacement leaves, before any graph rewiring.
        profile: The same capability profile the triage prompt was built from,
            so a child is graded against the limits the brain was shown.

    Returns:
        A ``ValidationResult``.  ``dispatchable`` is the caller's gate; ``soft``
        is for the log.
    """
    result = ValidationResult()

    # HARD rules. ``duplicate_id`` first and alone, for the reason given in
    # ``validate_leaves`` and in the rule's own docstring: it is the one finding
    # that must be reported BEFORE any id-keyed map is built, and the split path
    # builds four of them the moment this returns dispatchable.
    _check_duplicate_ids(children, result)
    if result.hard:
        return result

    _check_leaf_template(children, result)
    _check_verification(children, result)
    _check_max_files(children, profile, result)
    _check_max_loc(children, profile, result)
    _check_escalate_mismatch(children, profile, result)
    _check_dep_cycles(children, result)

    # SOFT rules
    _check_checklist_size(children, profile, result)
    _check_vague_phrase(children, result)
    _check_file_overlap(children, result)

    return result


def format_violations_feedback(result: ValidationResult) -> str:
    """Render violations as human-readable feedback text.

    Returns an empty string when the result is clean.
    """
    if result.clean:
        return ""

    lines: list[str] = []

    if result.hard:
        lines.append("HARD violations (block dispatch):")
        for v in result.hard:
            msg = (
                f"  [{v.rule}] task '{v.task_id}': {v.message}"
                if v.message
                else f"  [{v.rule}] task '{v.task_id}'"
            )
            lines.append(msg)

    if result.soft:
        lines.append("SOFT warnings:")
        for v in result.soft:
            msg = (
                f"  [{v.rule}] task '{v.task_id}': {v.message}"
                if v.message
                else f"  [{v.rule}] task '{v.task_id}'"
            )
            lines.append(msg)

    return "\n".join(lines)
