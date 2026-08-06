"""The decomposition standard doc is a contract, not prose.

The decompose prompt, the F3 validator, and the benchmark all cite it, so
its rule ids and leaf-type names must stay in lockstep with the code.
"""

from pathlib import Path

import pytest


DOC = Path(__file__).resolve().parents[1] / "docs" / "decomposition-standard.md"


@pytest.mark.unit
def test_standard_doc_exists():
    assert DOC.is_file(), "docs/decomposition-standard.md is missing"


@pytest.mark.unit
def test_standard_doc_cites_every_source():
    text = DOC.read_text(encoding="utf-8")
    for citation in (
        "2502.15964",  # MinionS
        "2605.14163",  # machine-checkable acceptance
        "2309.12499",  # CodePlan
        "2604.07789",  # ORACLE-SWE
        "2505.23419",  # SWE-bench Goes Live (numeric anchors)
        "2311.05772",  # ADaPT
        "2605.15425",  # runtime-structured decomposition
        "2511.09030",  # MAKER
        "2305.05176",  # FrugalGPT
    ):
        assert citation in text, f"standard doc is missing citation {citation}"


@pytest.mark.unit
def test_standard_doc_lists_every_leaf_type():
    from orchestrator.models.schemas import LeafType

    text = DOC.read_text(encoding="utf-8")
    for leaf_type in LeafType:
        assert leaf_type.value in text, (
            f"leaf type {leaf_type.value} is not documented in the standard"
        )


@pytest.mark.unit
def test_standard_doc_states_the_numeric_anchors_are_correlational():
    text = DOC.read_text(encoding="utf-8").lower()
    assert "correlational" in text


@pytest.mark.unit
def test_standard_doc_documents_the_difficulty_thresholds():
    text = DOC.read_text(encoding="utf-8")
    assert "reject_below" in text
    assert "flag_below" in text
    assert "0.35" in text
    assert "0.55" in text


@pytest.mark.unit
def test_standard_doc_names_the_scorer_weights_as_provisional():
    text = DOC.read_text(encoding="utf-8")
    assert "PROVISIONAL" in text or "provisional" in text
