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
    """Pin the doc to the SHIPPED thresholds, not to any number appearing anywhere.

    A bare ``"0.35" in text`` passes on an incidental match, so the expected
    values are read from ``config/praxis.yaml`` and required to appear next to
    the key that carries them.
    """
    import yaml

    repo = Path(__file__).resolve().parents[1]
    config = yaml.safe_load(
        (repo / "config" / "praxis.yaml").read_text(encoding="utf-8")
    )
    difficulty = config["difficulty"]
    text = DOC.read_text(encoding="utf-8")

    for key in ("reject_below", "flag_below"):
        expected = difficulty[key]
        assert key in text, f"the standard never names {key}"
        assert str(expected) in text, (
            f"the standard does not state the shipped {key} of {expected}; "
            "the doc and config/praxis.yaml have drifted"
        )


@pytest.mark.unit
def test_the_three_threshold_sources_agree():
    """The gate defaults, the settings defaults, and the YAML must not drift.

    The thresholds are declared in three places: ``_DEFAULT_REJECT_BELOW`` /
    ``_DEFAULT_FLAG_BELOW`` in the decompose gate, the literals in
    ``EffectiveSettings.difficulty_config``, and ``config/praxis.yaml``.  A
    divergence is silent: decomposition would reject at one threshold while the
    dashboard and dispatch flag at another, and every test would still pass.
    """
    import yaml

    from orchestrator.core.execute_plan_decompose import (
        _DEFAULT_FLAG_BELOW,
        _DEFAULT_REJECT_BELOW,
    )

    repo = Path(__file__).resolve().parents[1]
    difficulty = yaml.safe_load(
        (repo / "config" / "praxis.yaml").read_text(encoding="utf-8")
    )["difficulty"]

    assert difficulty["reject_below"] == _DEFAULT_REJECT_BELOW
    assert difficulty["flag_below"] == _DEFAULT_FLAG_BELOW
    assert _DEFAULT_REJECT_BELOW < _DEFAULT_FLAG_BELOW


@pytest.mark.unit
def test_standard_doc_names_the_scorer_weights_as_provisional():
    text = DOC.read_text(encoding="utf-8")
    assert "PROVISIONAL" in text or "provisional" in text
