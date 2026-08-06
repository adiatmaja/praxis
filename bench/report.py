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

from bench.config import CONDITIONS, PATCH_SIZE_STRATA, REPO_SIZE_STRATA
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

    Verified over the full SWE-bench Lite instance set: only 4 of the 9
    (patch, repo) strata are ever populated (every Lite gold patch touches
    exactly 1 file, and no Lite repo has under 100 tracked files), so most
    cells here are empty by design in any report built from that corpus. An
    empty cell renders via ``_empty_cell``, never as a bare 0 percent.
    """
    populated = per_stratum_table(rows)
    grid: dict[StratumKey, dict[str, Any]] = {}
    for patch in PATCH_SIZE_STRATA:
        for repo in REPO_SIZE_STRATA:
            for condition in CONDITIONS:
                key = (patch, repo, condition.key)
                grid[key] = populated.get(key, _empty_cell())
    return grid


def paired_comparison(
    rows: list[dict[str, Any]], arm_one: str, arm_two: str
) -> tuple[int, int, float]:
    """Return ``(b, c, p)`` for a within-subject comparison of two conditions.

    Args:
        rows: Attempt rows, as read from the JSONL metrics file.
        arm_one: The first condition key (e.g. ``"A"``).
        arm_two: The second condition key (e.g. ``"B"``).

    Returns:
        ``b`` (resolved by ``arm_one`` only), ``c`` (resolved by ``arm_two``
        only), and the exact McNemar p-value for the pair.
    """
    by_instance: dict[str, dict[str, bool]] = defaultdict(dict)
    for row in rows:
        by_instance[row["instance_id"]][row["condition"]] = bool(row["resolved"])
    b = c = 0
    for outcomes in by_instance.values():
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
    """Cost per resolved task. A condition that resolves nothing is infinite."""
    lines = [
        "| condition | tokens | resolved | tokens per resolved |",
        "|---|---|---|---|",
    ]
    for condition, group in sorted(_by_condition(rows).items()):
        tokens = sum(r["brain_tokens"] + r["worker_tokens"] for r in group)
        resolved = sum(1 for r in group if r["resolved"])
        per = f"{tokens / resolved:.0f}" if resolved else "infinite (nothing resolved)"
        lines.append(f"| {condition} | {tokens} | {resolved} | {per} |")
    return "\n".join(lines)


def _render_plausible_table(rows: list[dict[str, Any]]) -> str:
    lines = ["| condition | plausible but wrong | n |", "|---|---|---|"]
    for condition, group in sorted(_by_condition(rows).items()):
        wrong = sum(1 for r in group if r["plausible"] and not r["resolved"])
        lines.append(f"| {condition} | {wrong} | {len(group)} |")
    return "\n".join(lines)


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
        stratum_table=_render_stratum_table(rows),
        mcnemar_table=_render_mcnemar_table(rows),
        cost_table=_render_cost_table(rows),
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
