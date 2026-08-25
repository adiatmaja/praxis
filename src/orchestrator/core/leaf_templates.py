"""Per-leaf-type ``plan_text`` section requirements.

Single source of truth read by three consumers that must never drift: the
decompose prompt in ``core/plan_review.py`` (which asks the brain for these
sections), the triage prompt in ``core/leaf_triage.py`` (which asks for them
again when a failed leaf is split into children), and the F3 validator in
``core/leaf_validator.py`` (which grades every one of those answers).  See
``docs/decomposition-standard.md`` section 3.

Any surface that RESTATES the section list instead of rendering it here will
drift silently: the brain is asked for one shape and graded on another, and
the re-ask carries the requirement it was never told about.
"""

from __future__ import annotations

import re

from orchestrator.models.schemas import LeafType


# Sections every leaf type must carry, in the order they should appear.
BASE_SECTIONS: tuple[str, ...] = ("Goal", "Files", "Steps", "Acceptance")

# Additional sections a specific type must carry, appended after the base four.
_EXTRA_SECTIONS: dict[LeafType, tuple[str, ...]] = {
    LeafType.BUGFIX_REPRO: ("Reproduction",),
    LeafType.REFACTOR_RENAME: ("Renames",),
}

REQUIRED_SECTIONS: dict[LeafType, tuple[str, ...]] = {
    leaf_type: BASE_SECTIONS + _EXTRA_SECTIONS.get(leaf_type, ())
    for leaf_type in LeafType
}

# What each type is for, rendered into the prompt so the brain can choose.
_TYPE_PURPOSE: dict[LeafType, str] = {
    LeafType.BUGFIX_REPRO: "fixing observed wrong behavior",
    LeafType.FUNCTION_ADD: "adding a function or method inside an existing module",
    LeafType.ENDPOINT_ADD: "adding an API route or handler",
    LeafType.REFACTOR_RENAME: "moving or renaming symbols with no behavior change",
    LeafType.TEST_ADD: "adding tests for code that already exists",
    LeafType.CONFIG_CHANGE: "editing configuration, compose, or CI files",
    LeafType.DOC_CHANGE: "editing documentation only",
    LeafType.GENERIC: "nothing above fits (scored with a difficulty penalty)",
}


def _section_pattern(section: str) -> re.Pattern[str]:
    """Match a section label at the start of a line, in the three legal forms.

    Legal forms are a markdown heading (``## Goal``), a bold label
    (``**Goal:**``), and a plain colon label (``Goal:``).  Anchoring to the
    start of a line is what stops the word appearing inside prose from
    satisfying the requirement.
    """
    name = re.escape(section)
    return re.compile(
        rf"^\s*(?:\#{{1,6}}\s*{name}\b|\*\*{name}:?\*\*\s*:?|{name}\s*:)",
        re.IGNORECASE | re.MULTILINE,
    )


def missing_sections(plan_text: str, leaf_type: LeafType) -> list[str]:
    """Return the required sections absent from ``plan_text``, in order.

    Args:
        plan_text: The leaf's verbatim contract text.
        leaf_type: The declared shape of the leaf.

    Returns:
        Section names that are required for this type but not present.  Empty
        when the leaf satisfies its template.
    """
    text = plan_text or ""
    return [
        section
        for section in REQUIRED_SECTIONS[leaf_type]
        if not _section_pattern(section).search(text)
    ]


def render_template_block() -> str:
    """Render the leaf-type table for injection into the decompose prompt."""
    lines = [
        'LEAF TYPES (pick exactly one per leaf, set it as "leaf_type"):',
    ]
    for leaf_type in LeafType:
        extras = _EXTRA_SECTIONS.get(leaf_type, ())
        extra_text = f"; also requires: {', '.join(extras)}" if extras else ""
        lines.append(f'- "{leaf_type.value}": {_TYPE_PURPOSE[leaf_type]}{extra_text}')
    lines.append("")
    lines.append(
        'Every leaf\'s "plan_text" MUST contain these sections as line-leading '
        f"labels: {', '.join(BASE_SECTIONS)}. A leaf whose plan_text is missing "
        "a required section is rejected automatically and re-asked."
    )
    return "\n".join(lines)
