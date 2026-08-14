"""The report template's caveats are structural, not a matter of remembering."""

from __future__ import annotations

import json
import re
from string import Template
from typing import Any

import pytest

from bench.config import CONDITIONS, PATCH_SIZE_STRATA, REPO_SIZE_STRATA
from bench.report import TEMPLATE_PATH, build_report, per_stratum_table


def _row(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "run_id": "full-01",
        "instance_id": "a__b-1",
        "condition": "B",
        "worker": "local-openweight",
        "seed": 1,
        "stratum_patch": "medium",
        "stratum_repo": "mid",
        "resolved": True,
        "plausible": True,
        "leaf_count": 3,
        "leaf_retries": 0,
        "whole_task_retries": 0,
        "clarifications": 0,
        "human_gate_touches": 0,
        "brain_tokens": 1000,
        "worker_tokens": 4000,
        "wall_clock_s": 100.0,
        "error": None,
    }
    base.update(overrides)
    return base


def _norm(text: str) -> str:
    """Collapse whitespace so a hard-wrapped template line matches a sentence
    asserted as one contiguous string.

    The template's prose is hard-wrapped across source lines like every other
    doc in this repo; markdown renders a single ``\\n`` inside a paragraph as
    a space, but the raw string still carries the newline. Normalizing here,
    rather than reflowing the template to keep sentences on one line, keeps
    the check meaningful (still the full sentence, not a short vacuous
    fragment) without making the template's line width a test dependency.
    """
    return re.sub(r"\s+", " ", text)


MANDATORY_HEADINGS = (
    "Contamination",
    "Correlational anchors",
    "Failure-class analysis",
    "Predicted shape",
)


@pytest.mark.unit
def test_per_stratum_table_groups_by_cell_and_condition() -> None:
    rows = [
        _row(condition="A", resolved=False),
        _row(condition="B", resolved=True),
        _row(condition="B", instance_id="a__b-2", resolved=False),
    ]
    table = per_stratum_table(rows)
    key = ("medium", "mid", "B")
    assert table[key]["trials"] == 2
    assert table[key]["resolved"] == 1
    assert 0.0 < table[key]["ci_low"] < table[key]["rate"] < table[key]["ci_high"] < 1.0


@pytest.mark.unit
def test_errored_rows_count_in_the_denominator() -> None:
    """Dropping a crashed attempt inflates the rate. It stays in trials."""
    rows = [
        _row(resolved=True),
        _row(instance_id="a__b-2", resolved=False, error="timeout"),
    ]
    table = per_stratum_table(rows)
    assert table[("medium", "mid", "B")]["trials"] == 2


@pytest.mark.unit
def test_cost_per_resolved_task_is_reported_not_cost_per_attempt() -> None:
    rows = [
        _row(resolved=True, brain_tokens=1000, worker_tokens=1000),
        _row(
            instance_id="a__b-2", resolved=False, brain_tokens=1000, worker_tokens=1000
        ),
    ]
    table = per_stratum_table(rows)
    cell = table[("medium", "mid", "B")]
    # 4000 tokens spent, 1 resolved.
    assert cell["tokens_per_resolved"] == pytest.approx(4000.0)


@pytest.mark.unit
def test_cost_per_resolved_is_infinite_when_nothing_resolved() -> None:
    """Reporting 0 there would read as free, which is the opposite of true."""
    rows = [_row(resolved=False, brain_tokens=1000, worker_tokens=1000)]
    cell = per_stratum_table(rows)[("medium", "mid", "B")]
    assert cell["tokens_per_resolved"] == float("inf")


@pytest.mark.unit
def test_the_report_contains_every_mandatory_honesty_section(tmp_path: Any) -> None:
    rows = [_row(condition="A", resolved=False), _row(condition="B", resolved=True)]
    path = tmp_path / "rows.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    report = build_report(rows, run_id="full-01", model_cutoff="2026-03")
    for heading in MANDATORY_HEADINGS:
        assert heading in report


_ABLATION_NOT_RUN = (
    "The verify-gate ablation was NOT performed in this run: no gated arm ran, "
    "so `verify_cmd` was registered identically across arms and never executed."
)


@pytest.mark.unit
def test_a_gateless_run_says_the_ablation_was_not_performed() -> None:
    """The Design table lists four arms; a run of two must not imply four.

    Derived from the rows rather than written by hand, because an operator who
    forgets this sentence publishes a report whose design table is the only
    statement about which arms exist, and it describes arms that never ran.
    """
    rows = [_row(condition="A"), _row(condition="C", instance_id="a__b-2")]
    report = build_report(rows, run_id="pilot-1", model_cutoff="2026-03")
    assert "Conditions actually run: A, C." in report
    assert "Not run: B, D." in report
    assert _ABLATION_NOT_RUN in report


@pytest.mark.unit
def test_a_gated_run_does_not_claim_the_ablation_was_skipped() -> None:
    """The absence assertion: the wrong sentence must be provably gone.

    A substring check alone would stay green if the sentence were emitted
    unconditionally, which is exactly how a false claim ships inside a
    mandatory honesty heading.
    """
    rows = [
        _row(condition="A"),
        _row(condition="B", instance_id="a__b-2"),
        _row(condition="C", instance_id="a__b-3"),
    ]
    report = build_report(rows, run_id="full-01", model_cutoff="2026-03")
    assert "Conditions actually run: A, B, C." in report
    assert "Not run: D." in report
    assert _ABLATION_NOT_RUN not in report


@pytest.mark.unit
def test_a_run_of_every_arm_claims_nothing_was_skipped() -> None:
    rows = [
        _row(condition=c.key, instance_id=f"a__b-{i}") for i, c in enumerate(CONDITIONS)
    ]
    report = build_report(rows, run_id="full-01", model_cutoff="2026-03")
    assert "Conditions actually run: A, B, C, D." in report
    assert "Not run:" not in report
    assert _ABLATION_NOT_RUN not in report


_TOKENS_UNMEASURED = (
    "Token counts are UNMEASURED. `brain_tokens` and `worker_tokens` are "
    "stubs hardcoded to 0 in `bench/runner.py`: OpenCode talks to LM Studio "
    "directly from inside the agent container, so the orchestrator never "
    "observes those calls, and the agent-done callback carries no usage "
    "field. Token accounting is deferred until a metered provider is "
    "actually in play."
)


@pytest.mark.unit
def test_the_report_labels_token_counts_unmeasured_and_says_why() -> None:
    """Cost moved to wall clock (confirmed 2026-08-14); tokens are no longer
    a computed cell at all, only a fixed disclaimer. Silently dropping that
    disclaimer is exactly as misleading as printing a false zero, so it must
    render regardless of what token fields the rows happen to carry.
    """
    rows = [_row(condition="A", resolved=True, brain_tokens=9999, worker_tokens=9999)]
    report = build_report(rows, run_id="pilot-1", model_cutoff="2026-03")
    assert _TOKENS_UNMEASURED in _norm(report)


@pytest.mark.unit
def test_the_template_structurally_carries_the_tokens_unmeasured_sentence() -> None:
    """Structural, not incidental: present on disk in the template, the same
    guarantee the mandatory honesty headings rely on (see
    ``test_the_template_file_itself_contains_every_mandatory_heading``).
    """
    text = TEMPLATE_PATH.read_text(encoding="utf-8")
    assert _TOKENS_UNMEASURED in _norm(text)


@pytest.mark.unit
def test_the_report_names_the_model_cutoff_in_the_contamination_note() -> None:
    report = build_report([_row()], run_id="full-01", model_cutoff="2026-03")
    assert "2026-03" in report
    assert "SWE-rebench" in report


@pytest.mark.unit
def test_the_report_states_the_predicted_shape_up_front() -> None:
    """Stating the expectation before the numbers is what stops post-hoc storytelling."""
    report = build_report([_row()], run_id="full-01", model_cutoff="2026-03")
    assert "mid-difficulty" in report


@pytest.mark.unit
def test_the_report_reports_the_plausible_but_wrong_rate() -> None:
    rows = [_row(resolved=False, plausible=True), _row(instance_id="a__b-2")]
    report = build_report(rows, run_id="full-01", model_cutoff="2026-03")
    assert "plausible" in report.lower()


@pytest.mark.unit
def test_the_report_renders_the_ab_and_bc_mcnemar_p_values() -> None:
    rows = [
        _row(condition="A", instance_id=f"i{i}", resolved=False) for i in range(5)
    ] + [_row(condition="B", instance_id=f"i{i}", resolved=True) for i in range(5)]
    report = build_report(rows, run_id="full-01", model_cutoff="2026-03")
    assert "A vs B" in report
    assert "p =" in report


# --- Additional tests for the corrections above the plan's minimum ---


@pytest.mark.unit
def test_the_template_file_itself_contains_every_mandatory_heading() -> None:
    """The headings must be structural: present in the template ON DISK.

    ``assert heading in report`` alone would be satisfied by a single stray
    mention anywhere in a rendered document. Reading the committed template
    directly is what proves a report without these sections is not
    producible, rather than merely "happened to be there this time".
    """
    text = TEMPLATE_PATH.read_text(encoding="utf-8")
    for heading in MANDATORY_HEADINGS:
        assert heading in text


@pytest.mark.unit
def test_the_template_discloses_that_the_upper_boundaries_were_re_cut() -> None:
    """The provenance claim must match the boundaries actually used.

    The template used to say flatly that the stratum boundaries come from
    arXiv 2505.23419. Two of them no longer do: SWE-bench Lite is filtered to
    single-file patches, so the published 100-line patch cut and 100-file repo
    cut leave two buckets structurally empty and were re-cut to 15 and to
    500/2000. A published report repeating the old sentence would be making a
    false provenance claim about its own design, which no number in the report
    could contradict. The disclosure is asserted against the template ON DISK,
    for the same reason the honesty headings are.

    A bare ``"re-cut" in text`` is NOT enough and was measured to be vacuous:
    the word appears twice, so reverting the load-bearing sentence to the old
    unqualified provenance claim left the check green. The full sentences are
    asserted instead, plus the absence of the sentence that was wrong.
    """
    text = TEMPLATE_PATH.read_text(encoding="utf-8")
    assert "take their LOWER cuts from SWE-bench Goes Live!" in text
    assert "The two UPPER cuts are re-cut for this corpus" in text
    assert "The stratum boundaries come from SWE-bench Goes Live!" not in text
    for boundary in ("5 to 15", "500 to 2000"):
        assert boundary in text, boundary
    assert "before any outcome was observed" in text


@pytest.mark.unit
def test_the_template_is_located_relative_to_the_module_not_the_cwd(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """A CWD-relative template path breaks the moment the CLI runs from
    anywhere but the repo root; the bench is run by an operator from a shell.
    """
    monkeypatch.chdir(tmp_path)
    report = build_report([_row()], run_id="full-01", model_cutoff="2026-03")
    assert "Contamination" in report


@pytest.mark.unit
def test_build_report_handles_an_empty_rows_list_without_raising() -> None:
    report = build_report([], run_id="full-01", model_cutoff="2026-03")
    for heading in MANDATORY_HEADINGS:
        assert heading in report


@pytest.mark.unit
def test_every_template_placeholder_is_actually_supplied_and_substituted() -> None:
    """Every ``$name`` the template declares must be in the substitution map,
    and none may survive unsubstituted in the rendered output.

    ``string.Template.substitute`` already raises ``KeyError`` for a missing
    key (so ``build_report`` succeeding at all proves every declared
    placeholder was supplied); this test additionally scans the rendered
    output for leftover ``$identifier`` syntax to catch a placeholder that
    silently failed to expand.
    """
    text = TEMPLATE_PATH.read_text(encoding="utf-8")
    declared = Template(text).get_identifiers()
    assert declared, "the template should declare at least one placeholder"

    report = build_report([_row()], run_id="full-01", model_cutoff="2026-03")
    assert not re.search(r"\$\{?[A-Za-z_][A-Za-z0-9_]*\}?", report)


@pytest.mark.unit
def test_a_stray_brace_in_the_template_does_not_break_rendering() -> None:
    """``string.Template`` treats ``{`` and ``}`` as ordinary characters,
    unlike ``str.format``, which would raise on the first unescaped brace in
    published prose.
    """
    text = (
        TEMPLATE_PATH.read_text(encoding="utf-8") + "\nliteral { brace } at the end.\n"
    )
    rendered = Template(text).substitute(
        run_id="x",
        rows=0,
        conditions_note="",
        stratum_table="",
        mcnemar_table="",
        cost_table="",
        plausible_table="",
        workers="w",
        model_cutoff="2026-01",
        failure_analysis="",
        limitations="",
    )
    assert "literal { brace } at the end." in rendered


@pytest.mark.unit
def test_empty_stratum_cells_render_as_no_evidence_not_a_measured_zero() -> None:
    """5 of the 9 (patch, repo) strata are unpopulated in every SWE-bench
    Lite run; each must read "no evidence", not "0 percent resolved".
    """
    rows = [
        _row(stratum_patch="medium", stratum_repo="mid", condition="B", resolved=True)
    ]
    report = build_report(rows, run_id="full-01", model_cutoff="2026-03")
    assert "| small | tiny | A | 0 | 0 | 0.00 | (0.00, 1.00) |" in report
    assert "| small | tiny | A | 0 | 0 | 0.00 | 0.00 |" not in report


@pytest.mark.unit
def test_the_stratum_table_covers_the_full_design_grid() -> None:
    """Every (patch stratum, repo stratum, condition) combination the design
    declares appears somewhere in the table, populated or not.
    """
    report = build_report([_row()], run_id="full-01", model_cutoff="2026-03")
    for patch in PATCH_SIZE_STRATA:
        for repo in REPO_SIZE_STRATA:
            for condition in CONDITIONS:
                assert f"| {patch} | {repo} | {condition.key} |" in report


@pytest.mark.unit
def test_every_stratum_row_carries_its_trial_count_beside_the_rate() -> None:
    """A rate rendered without its trial count cannot distinguish "never
    run" from "ran, resolved nothing"; the column must always be present.
    """
    report = build_report([_row()], run_id="full-01", model_cutoff="2026-03")
    header = (
        "| patch stratum | repo stratum | condition | n | resolved | rate | 95% CI |"
    )
    assert header in report


@pytest.mark.unit
def test_cost_table_reports_wall_clock_total_and_per_resolved() -> None:
    """Wall clock, not tokens, is the primary cost axis (confirmed 2026-08-14):
    ``AttemptRecord.wall_clock_s`` is a real per-attempt measurement, unlike
    ``brain_tokens``/``worker_tokens`` which ``bench/runner.py`` hardcodes to 0.

    Pinned to exact numbers so a mutation that divides by trial count instead
    of resolved count is caught: 360.0s total over 3 trials, 2 resolved,
    divides to 180.0 (correct) rather than 120.0 (the trials-divisor bug).
    """
    rows = [
        _row(condition="A", resolved=True, wall_clock_s=100.0),
        _row(condition="A", instance_id="a__b-2", resolved=True, wall_clock_s=200.0),
        _row(condition="A", instance_id="a__b-3", resolved=False, wall_clock_s=60.0),
    ]
    report = build_report(rows, run_id="full-01", model_cutoff="2026-03")
    assert "| A | 3 | 2 | 360.0 | 180.0 |" in report
    assert "120.0" not in report


@pytest.mark.unit
def test_cost_table_wall_clock_per_resolved_is_infinite_when_nothing_resolved() -> None:
    """A condition that resolved nothing must not print a finite per-resolved
    wall clock: zero, or any other finite number, would read as cheap or free.
    """
    rows = [_row(condition="A", resolved=False, wall_clock_s=500.0)]
    report = build_report(rows, run_id="full-01", model_cutoff="2026-03")
    assert "| A | 1 | 0 | 500.0 | infinite (nothing resolved) |" in report
    assert "| A | 1 | 0 | 500.0 | 0.0 |" not in report
    assert "| A | 1 | 0 | 500.0 | 500.0 |" not in report


_ABSOLUTE_RATES_NOT_COMPARABLE = (
    "Absolute resolve rates in this report are NOT comparable to published "
    "SWE-bench numbers."
)
_ONLY_BETWEEN_ARM_DELTAS = (
    "Only BETWEEN-ARM deltas, comparisons among conditions run in the same "
    "invocation, are interpretable from this report."
)


@pytest.mark.unit
def test_the_limitations_section_states_resolve_rates_are_not_comparable() -> None:
    """Confirmed 2026-08-14, a separate decision from the wall-clock one: no
    per-instance environment is provisioned, so absolute numbers here do not
    mean what a published SWE-bench number means; only within-run deltas do.
    """
    report = build_report([_row()], run_id="full-01", model_cutoff="2026-03")
    normalized = _norm(report)
    assert _ABSOLUTE_RATES_NOT_COMPARABLE in normalized
    assert "no per-instance environment" in normalized
    assert _ONLY_BETWEEN_ARM_DELTAS in normalized


@pytest.mark.unit
def test_the_template_structurally_carries_the_non_comparability_sentence() -> None:
    """Structural, not incidental: present on disk in the template, the same
    guarantee the other mandatory honesty caveats rely on.
    """
    text = _norm(TEMPLATE_PATH.read_text(encoding="utf-8"))
    assert _ABSOLUTE_RATES_NOT_COMPARABLE in text
    assert _ONLY_BETWEEN_ARM_DELTAS in text
