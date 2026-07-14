"""Deterministic leaf-task validator.

Validates decomposed leaf tasks against the capability profile and the
original plan text before dispatch.  Rules are split into HARD (block
dispatch) and SOFT (warnings only).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher

from orchestrator.models.schemas import CapabilityProfile, LeafTask


logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Defaults (overridable via CapabilityProfile attributes)
# ---------------------------------------------------------------------------

_DEFAULT_MAX_FILES = 5
_DEFAULT_MAX_LOC = 300
_DEFAULT_MAX_CHECKLIST = 12
_DEFAULT_MAX_DEP_DEPTH = 3
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

# Verification commands that are clearly not runnable.
_NON_RUNNABLE_PATTERNS = [
    r"\bmanually\b",
    r"\bvisually\b",
    r"\bby eye\b",
    r"\b eyeball\b",
    r"\bread through\b",
    r"\binspect\b",
    r"\breview\b",
]


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


def _section_for_task(source: str, leaf: LeafTask) -> str:
    """Extract the section of the source plan that corresponds to *leaf*."""
    heading_re = re.compile(
        r"^#{1,6}\s+" + re.escape(leaf.title) + r"\b",
        re.MULTILINE | re.IGNORECASE,
    )
    match = heading_re.search(source)
    if not match:
        return source

    start = match.end()
    next_heading = re.search(r"^#{1,6}\s+", source[start:], re.MULTILINE)
    end = start + next_heading.start() if next_heading else len(source)

    return source[start:end].strip()


# ---------------------------------------------------------------------------
# Rule implementations
# ---------------------------------------------------------------------------


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


def _check_verification(
    leaves: list[LeafTask],
    result: ValidationResult,
) -> None:
    min_len = _DEFAULT_VERIFICATION_MIN_LEN
    for leaf in leaves:
        v = leaf.verification
        if v is None or len(v.strip()) < min_len:
            result.add(
                Violation(
                    rule="verification",
                    task_id=leaf.id,
                    message="missing or too short verification command"
                    if v and len(v.strip()) >= 1
                    else "missing verification command",
                )
            )
            continue
        for pat in _NON_RUNNABLE_PATTERNS:
            if re.search(pat, v, re.IGNORECASE):
                result.add(
                    Violation(
                        rule="verification",
                        task_id=leaf.id,
                        message=f"verification is not runnable: '{v}'",
                    )
                )
                break


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
        if _ratio(leaf.plan_text, section) < threshold:
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
        for pat in _VAGUE_PHRASES:
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

    # HARD rules
    _check_dangling_dep(leaves, result)
    _check_dep_cycles(leaves, result)
    _check_dep_depth(leaves, profile, result)
    _check_max_files(leaves, profile, result)
    _check_max_loc(leaves, profile, result)
    _check_verification(leaves, result)
    _check_escalate_mismatch(leaves, profile, result)

    # SOFT rules
    _check_plan_text_verbatim(leaves, source_plan, result)
    _check_file_overlap(leaves, result)
    _check_checklist_size(leaves, profile, result)
    _check_vague_phrase(leaves, result)

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
