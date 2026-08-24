"""Render a bench run into the committed report.

Every caveat section in the template is mandatory by construction: the
renderer fills placeholders in a template that already contains the
headings, so a report without them is not producible.

Substitution uses ``string.Template`` rather than ``str.format``: the
template's prose is published text, not a code literal, and ``str.format``
raises on any literal ``{`` or ``}`` that is not a placeholder. ``Template``
treats braces as ordinary characters, so a stray brace introduced later in
the prose fails a test (see ``tests/bench/test_report.py``) rather than a
live run.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections import defaultdict
from pathlib import Path
from string import Template
from typing import Any

from bench.config import CONDITIONS, PATCH_SIZE_STRATA, REPO_SIZE_STRATA, WORKERS
from bench.stats import mcnemar_exact, resolve_rate, wilson_interval


logger = logging.getLogger(__name__)

# Located relative to this module, never the current working directory: the
# report is rendered by an operator running the CLI from wherever they
# happen to be, and a CWD-relative path would break the moment that is not
# the repo root.
TEMPLATE_PATH = Path(__file__).parent / "templates" / "report.md.tmpl"

StratumKey = tuple[str, str, str]


def per_stratum_table(rows: list[dict[str, Any]]) -> dict[StratumKey, dict[str, Any]]:
    """Aggregate attempts into (patch stratum, repo stratum, condition) cells.

    Errored attempts stay in the denominator: dropping a crashed attempt
    inflates the resolve rate, which is the easiest way to publish a wrong
    number without lying on purpose.

    Args:
        rows: Attempt rows, as read from the JSONL metrics file.

    Returns:
        A dict keyed by ``(stratum_patch, stratum_repo, condition)``, covering
        only combinations that actually occurred in ``rows``. A caller that
        needs the full design grid, including strata with no data at all,
        builds it separately (see ``_stratum_grid``); this function only ever
        reports on what it was given.
    """
    cells: dict[StratumKey, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        key = (row["stratum_patch"], row["stratum_repo"], row["condition"])
        cells[key].append(row)

    table: dict[StratumKey, dict[str, Any]] = {}
    for key, group in cells.items():
        trials = len(group)
        resolved = sum(1 for r in group if r["resolved"])
        tokens = sum(r["brain_tokens"] + r["worker_tokens"] for r in group)
        ci_low, ci_high = wilson_interval(resolved, trials, 0.95)
        table[key] = {
            "trials": trials,
            "resolved": resolved,
            "rate": resolve_rate(resolved, trials),
            "ci_low": ci_low,
            "ci_high": ci_high,
            "tokens": tokens,
            "tokens_per_resolved": tokens / resolved if resolved else float("inf"),
            "plausible_but_wrong": sum(
                1 for r in group if r["plausible"] and not r["resolved"]
            ),
        }
    return table


def _empty_cell() -> dict[str, Any]:
    """The canonical rendering for a stratum/condition combination with no data.

    ``wilson_interval(0, 0)`` returns ``(0.0, 1.0)`` (no evidence) while
    ``resolve_rate(0, 0)`` returns ``0.0`` (a point estimate of zero). The two
    disagree on purpose; rendering both side by side, with the trial count of
    0 alongside them, is what stops a never-run stratum from reading as a
    measured "0 percent resolved".
    """
    ci_low, ci_high = wilson_interval(0, 0, 0.95)
    return {
        "trials": 0,
        "resolved": 0,
        "rate": resolve_rate(0, 0),
        "ci_low": ci_low,
        "ci_high": ci_high,
    }


def _stratum_grid(rows: list[dict[str, Any]]) -> dict[StratumKey, dict[str, Any]]:
    """The full (patch stratum x repo stratum x condition) grid.

    All 9 (patch, repo) strata are populated under the re-cut boundaries in
    ``bench/config.stratum_for``; under the previously published ones only 4
    were, because every SWE-bench Lite gold patch touches exactly 1 file and no
    Lite repo has under 100 tracked files.  The grid is still rendered in full
    rather than from the populated keys, because a cell can also be empty for
    run-time reasons (an aborted half, a condition not run), and a silently
    absent row reads as "not measured" exactly like a measured zero.  An empty
    cell renders via ``_empty_cell``, never as a bare 0 percent.
    """
    populated = per_stratum_table(rows)
    grid: dict[StratumKey, dict[str, Any]] = {}
    for patch in PATCH_SIZE_STRATA:
        for repo in REPO_SIZE_STRATA:
            for condition in CONDITIONS:
                key = (patch, repo, condition.key)
                grid[key] = populated.get(key, _empty_cell())
    return grid


# Default unit fields for rows that predate ``worker``/``seed`` being carried
# on every row (older attempt files, and most existing test fixtures). A
# missing key falls back to these so such a row still pairs exactly as it did
# before ``paired_comparison`` keyed on the full experimental unit: every
# such row collapses to the same default unit, so instance id alone still
# decides the pairing for them, unchanged.
_DEFAULT_WORKER_UNIT = "_unspecified_worker"
_DEFAULT_SEED_UNIT = 0


def paired_comparison(
    rows: list[dict[str, Any]], arm_one: str, arm_two: str
) -> tuple[int, int, float]:
    """Return ``(b, c, p)`` for a within-subject comparison of two conditions.

    Pairing is keyed on the full experimental unit, ``(instance_id, worker,
    seed)``, not on instance id alone. ``bench/config.py`` defines two
    workers and two seeds, and ``bench/runner.py`` accepts comma-separated
    ``--worker``/``--seeds`` into one ``attempts.jsonl``; keying on instance
    id alone let a second seed's row for the same ``(instance, condition)``
    silently OVERWRITE the first in ``by_instance``, erasing a genuine
    discordant pair before this function ever saw it. A row missing
    ``worker``/``seed`` falls back to a fixed default (see
    ``_DEFAULT_WORKER_UNIT``/``_DEFAULT_SEED_UNIT``), so it still pairs by
    instance id alone exactly as before.

    Args:
        rows: Attempt rows, as read from the JSONL metrics file.
        arm_one: The first condition key (e.g. ``"A"``).
        arm_two: The second condition key (e.g. ``"B"``).

    Returns:
        ``b`` (resolved by ``arm_one`` only), ``c`` (resolved by ``arm_two``
        only), and the exact McNemar p-value for the pair. Each
        ``(instance, worker, seed)`` unit contributes at most one pair.
    """
    by_unit: dict[tuple[str, str, int], dict[str, bool]] = defaultdict(dict)
    for row in rows:
        unit = (
            row["instance_id"],
            row.get("worker", _DEFAULT_WORKER_UNIT),
            row.get("seed", _DEFAULT_SEED_UNIT),
        )
        by_unit[unit][row["condition"]] = bool(row["resolved"])
    b = c = 0
    for outcomes in by_unit.values():
        if arm_one not in outcomes or arm_two not in outcomes:
            continue
        if outcomes[arm_one] and not outcomes[arm_two]:
            b += 1
        elif outcomes[arm_two] and not outcomes[arm_one]:
            c += 1
    return b, c, mcnemar_exact(b=b, c=c)


def _render_stratum_table(rows: list[dict[str, Any]]) -> str:
    """Render the full stratum grid, trial count always beside the rate."""
    grid = _stratum_grid(rows)
    lines = [
        "| patch stratum | repo stratum | condition | n | resolved | rate | 95% CI |",
        "|---|---|---|---|---|---|---|",
    ]
    for (patch, repo, condition), cell in grid.items():
        lines.append(
            f"| {patch} | {repo} | {condition} | {cell['trials']} | "
            f"{cell['resolved']} | {cell['rate']:.2f} | "
            f"({cell['ci_low']:.2f}, {cell['ci_high']:.2f}) |"
        )
    return "\n".join(lines)


def _render_mcnemar_table(rows: list[dict[str, Any]]) -> str:
    lines = ["| comparison | b | c | p |", "|---|---|---|---|"]
    for one, two in (("A", "B"), ("B", "C"), ("B", "D")):
        b, c, p = paired_comparison(rows, one, two)
        lines.append(f"| {one} vs {two} | {b} | {c} | p = {p:.4f} |")
    return "\n".join(lines)


def _by_condition(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[row["condition"]].append(row)
    return grouped


def _render_cost_table(rows: list[dict[str, Any]]) -> str:
    """Cost per condition, on a WALL CLOCK basis (confirmed 2026-08-14).

    Wall clock, not tokens, is the real cost axis in this configuration:
    ``AttemptRecord.wall_clock_s`` is a genuine per-attempt measurement, and
    the brain is a rate-limited subscription CLI, not metered per token.
    ``brain_tokens`` and ``worker_tokens`` stay hardcoded to 0 in
    ``bench/runner.py`` (never wired up) and are reported as a fixed
    disclaimer in the template, never as a computed cell here: see
    ``report.md.tmpl``'s Cost section.

    This table groups by CONDITION only, never by worker, so a run mixing
    both ``bench/config.WORKERS`` (the local open-weight worker and the
    hosted ``agy`` worker) puts both workers' wall clock in one cell. Whether
    "tokens cost no money" is therefore NOT something this table can claim on
    its own: see ``_render_cost_note``, which states it only when every
    worker that actually ran is local.

    A condition that resolves nothing must not print a finite per-resolved
    wall clock: zero, or any finite number, would read as "this arm is
    cheap" rather than "this arm never finished anything".
    """
    lines = [
        "| condition | n | resolved | total wall clock (s) | "
        "wall clock per resolved (s) |",
        "|---|---|---|---|---|",
    ]
    for condition, group in sorted(_by_condition(rows).items()):
        trials = len(group)
        resolved = sum(1 for r in group if r["resolved"])
        wall_clock = sum(r["wall_clock_s"] for r in group)
        per_resolved = (
            f"{wall_clock / resolved:.1f}"
            if resolved
            else "infinite (nothing resolved)"
        )
        lines.append(
            f"| {condition} | {trials} | {resolved} | {wall_clock:.1f} | "
            f"{per_resolved} |"
        )
    return "\n".join(lines)


# Harnesses that talk to a local open-weight model on the operator's own
# GPU. ``bench/config.WORKERS`` gives each worker a ``harness`` field but no
# explicit "is this local" flag; ``opencode`` is the harness that runs the
# reference local open-weight model (see ``_render_token_note``: "OpenCode
# talks to LM Studio directly from inside the agent container"), while
# ``agy`` drives the hosted Gemini worker. A worker key not found in
# ``WORKERS`` at all is treated as NOT local: an unrecognized worker is not
# evidence tokens were free.
_LOCAL_HARNESSES = frozenset({"opencode"})


def _worker_is_local(worker_key: str) -> bool:
    """True when ``worker_key`` names a worker driven by a local harness."""
    for worker in WORKERS:
        if worker.key == worker_key:
            return worker.harness in _LOCAL_HARNESSES
    return False


def _render_cost_note(rows: list[dict[str, Any]]) -> str:
    """State the free-tokens claim only when every worker that ran is local.

    ``_render_cost_table`` groups by condition only, so a run mixing the
    local open-weight worker with the hosted ``agy`` worker
    (``bench/config.WORKERS`` defines both) puts both workers' wall clock in
    one cell. The template used to claim unconditionally that "the worker is
    a local open-weight model running on the operator's own GPU, so tokens
    cost no money"; that is false the moment ``hosted-flash`` (Gemini 3.7
    Flash, a metered hosted call) appears in a run. Derived from the rows,
    like ``_render_conditions_note`` and ``_render_token_note`` above it, so
    it cannot assert what did not actually run.

    Args:
        rows: Attempt rows, as read from the JSONL metrics file.

    Returns:
        Markdown prose stating the free-tokens claim, a qualified version of
        it, or its opposite, depending on which workers ran.
    """
    present = sorted({r["worker"] for r in rows})
    if not present:
        return "No attempts are in this run, so no claim about worker cost can be made."
    local = [w for w in present if _worker_is_local(w)]
    hosted = [w for w in present if not _worker_is_local(w)]
    if not hosted:
        return (
            "Every worker in this run is a local open-weight model on the "
            "operator's own GPU, so tokens cost no money and GPU hours do; "
            "wall clock is the real cost axis here."
        )
    if not local:
        return (
            f"Every worker in this run ({', '.join(hosted)}) is a hosted, "
            "metered model, so the wall-clock cost above does NOT mean "
            "tokens were free."
        )
    return (
        f"This run mixes a local open-weight worker ({', '.join(local)}) with "
        f"a hosted, metered worker ({', '.join(hosted)}). The cost table "
        "above reports wall clock for both, but that is not a free-tokens "
        "claim: the hosted worker's tokens ARE metered."
    )


def _render_plausible_table(rows: list[dict[str, Any]]) -> str:
    lines = ["| condition | plausible but wrong | n |", "|---|---|---|"]
    for condition, group in sorted(_by_condition(rows).items()):
        wrong = sum(1 for r in group if r["plausible"] and not r["resolved"])
        lines.append(f"| {condition} | {wrong} | {len(group)} |")
    return "\n".join(lines)


def _render_conditions_note(rows: list[dict[str, Any]]) -> str:
    """State which arms actually ran, and what a gateless run cannot answer.

    The Design table above it lists every condition the design DEFINES, which
    for a two-arm run describes arms that never executed. Derived from the rows
    so it cannot be forgotten, and so it cannot disagree with the tables below.

    Args:
        rows: Attempt rows, as read from the JSONL metrics file.

    Returns:
        Markdown prose naming the executed and unexecuted conditions.
    """
    order = [c.key for c in CONDITIONS]
    ran = [k for k in order if any(r["condition"] == k for r in rows)]
    absent = [k for k in order if k not in ran]

    parts = [f"Conditions actually run: {', '.join(ran) or 'none'}."]
    if absent:
        parts.append(f"Not run: {', '.join(absent)}.")
    # The gate is what condition B and D carry; with neither present, nothing in
    # this run exercised it, so no reading of these numbers may be about it.
    if not any(c.verify_gate for c in CONDITIONS if c.key in ran):
        parts.append(
            "The verify-gate ablation was NOT performed in this run: no gated "
            "arm ran, so `verify_cmd` was registered identically across arms "
            "and never executed."
        )
    return " ".join(parts)


def _render_token_note(rows: list[dict[str, Any]]) -> str:
    """Say whether tokens were measured, derived from the rows themselves.

    Cost moved to wall clock on 2026-08-14 and token accounting was deferred
    until a metered provider is actually in play, so today every row carries
    zero and this renders the disclaimer. It is NOT hardwired into the
    template, for the same reason ``_render_conditions_note`` is not: a fixed
    claim would keep asserting the counters are missing after somebody wires
    them up, and nothing would catch it. A report that ALWAYS says a thing was
    not measured is exactly as false as one that never says it.

    Args:
        rows: Attempt rows, as read from the JSONL metrics file.

    Returns:
        Markdown prose either disclaiming token accounting or reporting it.
    """
    brain = sum(r["brain_tokens"] for r in rows)
    worker = sum(r["worker_tokens"] for r in rows)
    if brain or worker:
        return (
            f"Token counts: {brain} brain, {worker} worker, "
            f"{brain + worker} total. Wall clock remains the primary cost "
            "axis; these are reported because the rows carry them."
        )
    return (
        "Token counts are UNMEASURED. `brain_tokens` and `worker_tokens` are "
        "stubs hardcoded to 0 in `bench/runner.py`: OpenCode talks to LM Studio "
        "directly from inside the agent container, so the orchestrator never "
        "observes those calls, and the agent-done callback carries no usage "
        "field. Token accounting is deferred until a metered provider is "
        "actually in play."
    )


def build_report(rows: list[dict[str, Any]], run_id: str, model_cutoff: str) -> str:
    """Render the full report markdown.

    Args:
        rows: Attempt rows, as read from the JSONL metrics file.
        run_id: Identifier for this bench run, used in the title.
        model_cutoff: Training data cutoff for the worker model(s), stated in
            the contamination note.

    Returns:
        The rendered markdown report. The four honesty headings
        (Contamination, Correlational anchors, Failure-class analysis,
        Predicted shape) are text baked into the template itself, so this
        function cannot omit them by accident; only editing the template can.
    """
    workers = ", ".join(sorted({r["worker"] for r in rows})) or "unknown"
    template = Template(TEMPLATE_PATH.read_text(encoding="utf-8"))
    return template.substitute(
        run_id=run_id,
        rows=len(rows),
        conditions_note=_render_conditions_note(rows),
        stratum_table=_render_stratum_table(rows),
        mcnemar_table=_render_mcnemar_table(rows),
        cost_table=_render_cost_table(rows),
        cost_note=_render_cost_note(rows),
        token_note=_render_token_note(rows),
        plausible_table=_render_plausible_table(rows),
        workers=workers,
        model_cutoff=model_cutoff,
        failure_analysis="TO BE FILLED BY HAND: see Task 17 step 4.",
        limitations="TO BE FILLED BY HAND: see Task 17 step 5.",
    )


def main(argv: list[str] | None = None) -> int:
    """CLI: render a graded run into a markdown report file."""
    parser = argparse.ArgumentParser(description="Render a bench report")
    parser.add_argument("--run", required=True)
    parser.add_argument("--model-cutoff", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    graded = Path(args.run) / "graded.jsonl"
    rows = [
        json.loads(line)
        for line in graded.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        build_report(rows, Path(args.run).name, args.model_cutoff), encoding="utf-8"
    )
    logger.info("wrote %s from %d rows", out_path, len(rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
