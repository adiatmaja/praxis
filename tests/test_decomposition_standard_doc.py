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


# ---------------------------------------------------------------------------
# The doc claims WHERE each rule is enforced. Those claims are checkable.
# ---------------------------------------------------------------------------


def _f3_rules() -> set[str]:
    """The rule calls that make up F3, read out of validate_leaves itself."""
    import inspect

    from orchestrator.core import leaf_validator

    body = inspect.getsource(leaf_validator.validate_leaves)
    return {
        line.strip()
        for line in body.splitlines()
        if "_check_" in line and not line.strip().startswith("def ")
    }


@pytest.mark.unit
def test_the_doc_credits_f3_only_with_rules_f3_actually_has():
    """The doc claimed F3 enforces rule 1, the context-pack size rule.

    ``validate_leaves`` is the whole of F3 and has no token or length check at
    all, so a reader budgeting on that claim had no bound. Rule 1 reaches the
    loop as a difficulty FEATURE instead. If a real size rule is ever added to
    F3 this goes red, and the doc must be updated with it.
    """
    rules = _f3_rules()
    # Rules 3 and 6, which the doc does claim, are really there.
    assert any("_check_verification" in rule for rule in rules)
    assert any("_check_leaf_template" in rule for rule in rules)
    # Rule 1 is not, in any spelling.
    assert not [
        rule
        for rule in rules
        if any(word in rule for word in ("token", "context", "budget", "length"))
    ]

    text = DOC.read_text(encoding="utf-8")
    assert "Rule 1 has NO F3 rule" in text
    assert "context_ratio" in text
    assert "core/difficulty.py" in text


@pytest.mark.unit
def test_the_doc_says_rule_four_has_only_a_file_count_proxy():
    """Rule 4 asks for dependency locality; F3 counts files, which rule 4 calls
    insufficient in its own wording. The doc used to present the two as the
    same thing."""
    rules = _f3_rules()
    assert any("_check_max_files" in rule for rule in rules)
    text = DOC.read_text(encoding="utf-8")
    assert "Rule 4 has only a PROXY in F3" in text
    assert "Dependency locality is not measured anywhere." in text


@pytest.mark.unit
@pytest.mark.parametrize(
    ("row_label", "token"),
    [
        # The retry budget is seeded on the child ROW, so the module named must
        # be one that mentions attempts. core/leaf_split.py, which the doc used
        # to credit, contains no attempt or retry logic at all.
        ("Retry budget for split children", "attempt"),
        # The 2-to-4 check is a model validator, not prompt text. leaf_triage.py
        # contains the SENTENCE "between 2 and 4 children" in its prompt, so a
        # token that only matches prose would not have caught the misattribution.
        ("Children per split", "<= 4"),
    ],
    ids=["retry-budget", "children-per-split"],
)
def test_a_hard_bound_names_a_module_that_contains_the_bound(
    row_label: str, token: str
):
    """Section 6 says where each bound lives; the named file must contain it."""
    import re

    repo = Path(__file__).resolve().parents[1]
    text = DOC.read_text(encoding="utf-8")
    row = next(
        line for line in text.splitlines() if line.startswith(f"| {row_label} |")
    )
    modules = [
        ref
        for ref in re.findall(r"`([^`]+)`", row)
        if "/" in ref or ref.startswith(("core.", "models."))
    ]
    assert modules, f"the {row_label} row names no module"

    # A row may name several modules (the validator plus the caller that
    # reaches it). At least ONE of them must actually contain the bound.
    resolved: dict[str, str] = {}
    for ref in modules:
        dotted = ref.replace("/", ".").split(".")
        # "core/task_queue.insert_split_children" -> src/orchestrator/core/task_queue.py
        for cut in range(len(dotted), 0, -1):
            candidate = repo / "src" / "orchestrator" / ("/".join(dotted[:cut]) + ".py")
            if candidate.is_file():
                resolved[ref] = candidate.read_text(encoding="utf-8")
                break
    assert resolved, f"the {row_label} row names no file that exists: {modules}"
    assert [ref for ref, body in resolved.items() if token in body], (
        f"{row_label} names {list(resolved)}, none of which contains {token!r}"
    )


@pytest.mark.unit
def test_the_doc_states_how_verbatim_and_the_template_coexist():
    """The one shape that satisfies both halves must be written down.

    A plan_text that is only a verbatim excerpt is HARD-rejected for having no
    labels, and the standard never said so, which is how a decomposition could
    fail every leaf while obeying the instruction it was given.
    """
    text = DOC.read_text(encoding="utf-8")
    assert "labelled skeleton whose `Steps` section carries" in text
    assert "HARD-rejects" in text


@pytest.mark.unit
def test_the_doc_admits_split_children_are_not_validated():
    """Policy 1 governs the hypothesis; nothing governs the correction.

    ``validate_leaves`` has exactly one call site, so everything the adaptive
    split produces reaches dispatch without passing F3. If a second call site
    ever appears, this goes red and the doc should stop saying otherwise.
    """
    import subprocess

    repo = Path(__file__).resolve().parents[1]
    hits = subprocess.run(  # noqa: S603
        ["git", "grep", "-c", "validate_leaves(", "--", "src/"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    ).stdout
    callers = {
        line.split(":")[0]
        for line in hits.splitlines()
        if line and "leaf_validator.py" not in line
    }
    assert callers == {"src/orchestrator/core/execute_plan_decompose.py"}

    # Collapsed, because the sentence is wrapped in the doc and a raw substring
    # would go green or red on where the line break happens to land.
    text = " ".join(DOC.read_text(encoding="utf-8").split())
    assert "split children reach dispatch without passing F3 at all" in text
