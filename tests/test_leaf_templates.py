"""The per-type section table is read by both the prompt and the validator.

If these drift, the brain is asked for one shape and graded on another.
"""

import pytest

from orchestrator.core.leaf_templates import (
    BASE_SECTIONS,
    REQUIRED_SECTIONS,
    missing_sections,
    render_template_block,
)
from orchestrator.models.schemas import LeafType


@pytest.mark.unit
def test_every_leaf_type_has_a_section_tuple():
    assert set(REQUIRED_SECTIONS) == set(LeafType)


@pytest.mark.unit
def test_every_type_requires_the_four_base_sections():
    for leaf_type, sections in REQUIRED_SECTIONS.items():
        assert set(BASE_SECTIONS).issubset(set(sections)), leaf_type


@pytest.mark.unit
def test_bugfix_repro_additionally_requires_reproduction():
    assert "Reproduction" in REQUIRED_SECTIONS[LeafType.BUGFIX_REPRO]


@pytest.mark.unit
def test_refactor_rename_additionally_requires_renames():
    assert "Renames" in REQUIRED_SECTIONS[LeafType.REFACTOR_RENAME]


@pytest.mark.unit
def test_generic_requires_only_the_base_sections():
    assert REQUIRED_SECTIONS[LeafType.GENERIC] == BASE_SECTIONS


@pytest.mark.unit
def test_missing_sections_accepts_markdown_headings():
    text = "## Goal\nx\n## Files\nsrc/a.py\n## Steps\n1. do it\n## Acceptance\n`pytest`"
    assert missing_sections(text, LeafType.FUNCTION_ADD) == []


@pytest.mark.unit
def test_missing_sections_accepts_bold_labels():
    text = (
        "**Goal:** x\n**Files:** src/a.py\n**Steps:** 1. do\n**Acceptance:** `pytest`"
    )
    assert missing_sections(text, LeafType.FUNCTION_ADD) == []


@pytest.mark.unit
def test_missing_sections_accepts_plain_colon_labels():
    text = "Goal: x\nFiles: src/a.py\nSteps: do it\nAcceptance: run `pytest`"
    assert missing_sections(text, LeafType.FUNCTION_ADD) == []


@pytest.mark.unit
def test_missing_sections_is_case_insensitive():
    text = "goal: x\nFILES: src/a.py\nsteps: do it\nacceptance: `pytest`"
    assert missing_sections(text, LeafType.FUNCTION_ADD) == []


@pytest.mark.unit
def test_missing_sections_is_case_insensitive_for_every_label_form():
    # The plain-colon case above would still pass if IGNORECASE were lost on
    # only the heading and bold branches, so exercise all three forms.
    text = "## GOAL\nx\n**files:** src/a.py\n### Steps\n1. do\n**ACCEPTANCE:** `pytest`"
    assert missing_sections(text, LeafType.FUNCTION_ADD) == []


@pytest.mark.unit
def test_missing_sections_reports_every_absent_section_in_order():
    text = "Goal: ship it"
    assert missing_sections(text, LeafType.FUNCTION_ADD) == [
        "Files",
        "Steps",
        "Acceptance",
    ]


@pytest.mark.unit
def test_missing_sections_reports_the_type_specific_extra():
    text = "Goal: x\nFiles: a.py\nSteps: do\nAcceptance: `pytest`"
    assert missing_sections(text, LeafType.BUGFIX_REPRO) == ["Reproduction"]


@pytest.mark.unit
def test_missing_sections_does_not_match_a_word_inside_prose():
    # "Goal:" appearing mid-line, not at the start of a line, must not
    # satisfy the Goal section requirement.
    text = "Random preamble. Goal: this looks like a label but is only inline prose."
    assert "Goal" in missing_sections(text, LeafType.GENERIC)


@pytest.mark.unit
def test_render_template_block_names_every_type_and_its_extras():
    block = render_template_block()
    for leaf_type in LeafType:
        assert leaf_type.value in block
    assert "Reproduction" in block
    assert "Renames" in block


@pytest.mark.unit
def test_render_template_block_attaches_each_extra_to_its_own_type():
    # Membership in the whole block is not enough: swapping Reproduction and
    # Renames between the two types would leave that assertion green while
    # telling the brain the wrong shape.
    lines = {
        leaf_type: next(
            line
            for line in render_template_block().splitlines()
            if line.startswith(f'- "{leaf_type.value}"')
        )
        for leaf_type in LeafType
    }
    assert "Reproduction" in lines[LeafType.BUGFIX_REPRO]
    assert "Renames" in lines[LeafType.REFACTOR_RENAME]
    for leaf_type, line in lines.items():
        for extra in ("Reproduction", "Renames"):
            if extra not in REQUIRED_SECTIONS[leaf_type]:
                assert extra not in line, f"{leaf_type.value} must not claim {extra}"
