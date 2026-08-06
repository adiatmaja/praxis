"""Extract each attempt's patch and grade it with the OFFICIAL SWE-bench harness.

Praxis never grades itself.  This module produces the prediction file the
upstream harness expects, shells out to it, and reads its report back.  The only
judgment here is the mapping from the harness's fields to ``resolved`` and
``plausible``.

The one thing this module must not get wrong is the BASE each patch is taken
against.  ``git diff <base>...<result>`` is the whole prediction, so a base that
is merely plausible rather than correct yields a patch that is empty or wrong,
the harness grades it unresolved, and every condition reports a low number that
is indistinguishable from an honest one.  The base is therefore threaded in from
the committed sample (``base_commit``, the same sha ``bench/prepare.py`` pins
``refs/heads/main`` to) and a record whose instance has no known base is
REFUSED.  It is never probed from the prepared repo: a bare repo has no working
tree, so no marker file can live in it and any such probe silently falls back to
a default.
"""

from __future__ import annotations

import argparse
import json
import logging
import subprocess  # nosec B404 - git and the official harness are the interface
import sys
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from bench.metrics import AttemptRecord, read_records


logger = logging.getLogger(__name__)


class MissingBaseCommitError(Exception):
    """Raised when a record's instance has no base commit in the sample."""


@dataclass(frozen=True)
class GradeResult:
    """What the official harness said about one instance."""

    resolved: bool
    plausible: bool
    applied: bool


def extract_patch(bare: Path, base: str, head: str = "main") -> str:
    """Return ``git diff base...head`` from the prepared bare repo."""
    result = subprocess.run(  # nosec B603 - fixed argv, no shell
        ["git", "-C", str(bare), "diff", f"{base}...{head}"],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


def load_bases(sample_path: Path) -> dict[str, str]:
    """Return ``{instance_id: base_commit}`` from a committed sample file.

    Args:
        sample_path: Path to a ``bench/samples/*.json`` file.

    Returns:
        The base commit for every instance in the sample.
    """
    sample = json.loads(Path(sample_path).read_text(encoding="utf-8"))
    return {entry["instance_id"]: entry["base_commit"] for entry in sample["instances"]}


def write_predictions(
    records: Sequence[AttemptRecord],
    repo_root: Path,
    out_path: Path,
    model_name: str,
    bases: Mapping[str, str],
) -> Path:
    """Write the prediction JSONL the official harness consumes.

    Args:
        records: The run's attempt records, one row written per record.
        repo_root: Directory holding the prepared bare repos.
        out_path: Where to write the prediction file.
        model_name: Value for the harness's ``model_name_or_path`` field.
        bases: ``{instance_id: base_commit}``, from ``load_bases``.

    Returns:
        ``out_path``.

    Raises:
        MissingBaseCommitError: If a record's instance has no known base.  A
            guessed base produces an empty or wrong patch that the harness
            grades unresolved, so the run must stop rather than publish it.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    # newline="\n" for the same reason as bench/metrics.append_record: on
    # Windows the default translation would write CRLF into a published
    # artifact that the repository convention says is LF.
    with out_path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            base = bases.get(record.instance_id)
            if not base:
                message = (
                    f"no base_commit for {record.instance_id!r}; pass the sample "
                    "this run was drawn from with --sample. Grading against a "
                    "guessed base yields an empty patch and a silent 0 percent."
                )
                raise MissingBaseCommitError(message)
            bare = repo_root / f"{record.instance_id}.git"
            patch = extract_patch(bare, base=base) if bare.is_dir() else ""
            if not bare.is_dir():
                logger.warning("no prepared repo at %s; recording an empty patch", bare)
            handle.write(
                json.dumps(
                    {
                        "instance_id": record.instance_id,
                        "model_name_or_path": model_name,
                        "model_patch": patch,
                    }
                )
                + "\n"
            )
    return out_path


def select_report(candidates: Sequence[Path]) -> Path | None:
    """Pick one harness report deterministically from the matching files.

    ``Path.glob`` order is arbitrary, so taking the first match silently grades
    against whichever file the filesystem happened to hand back when a re-run
    left more than one behind.  The newest is the one this invocation just
    produced, with the name as a tiebreak because Windows mtime resolution is
    coarse enough for two writes to land on the same stamp.

    Args:
        candidates: Report files that matched the run id.

    Returns:
        The chosen report, or ``None`` when nothing matched.
    """
    ordered = sorted(candidates, key=lambda p: (p.stat().st_mtime, p.name))
    if not ordered:
        return None
    if len(ordered) > 1:
        logger.warning(
            "%d report files matched: %s; grading against the newest, %s",
            len(ordered),
            ", ".join(p.name for p in ordered),
            ordered[-1].name,
        )
    return ordered[-1]


def parse_official_report(
    report: dict[str, dict[str, Any]], instance_id: str
) -> GradeResult:
    """Map the harness's per-instance fields onto our two outcome flags.

    A missing instance grades as unresolved and not plausible: an attempt that
    crashed before producing a patch must count against its condition, not
    disappear from the denominator.
    """
    entry = report.get(instance_id)
    if entry is None:
        return GradeResult(resolved=False, plausible=False, applied=False)
    applied = bool(entry.get("patch_successfully_applied"))
    return GradeResult(
        resolved=bool(entry.get("resolved")),
        plausible=applied,
        applied=applied,
    )


def main(argv: list[str] | None = None) -> int:
    """CLI: build predictions, invoke the official harness, merge results back."""
    parser = argparse.ArgumentParser(description="Grade a bench run officially")
    parser.add_argument("--run", required=True, help="run directory")
    parser.add_argument(
        "--sample",
        required=True,
        help="the sample this run was drawn from; supplies each base_commit",
    )
    parser.add_argument("--repos", default="bench/.work/repos")
    parser.add_argument("--dataset", default="princeton-nlp/SWE-bench_Lite")
    parser.add_argument(
        "--harness",
        default="swebench.harness.run_evaluation",
        help="python -m target for the official evaluation harness",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    run_dir = Path(args.run)
    records = read_records(run_dir / "attempts.jsonl")
    predictions = write_predictions(
        records,
        Path(args.repos),
        run_dir / "predictions.jsonl",
        "praxis",
        bases=load_bases(Path(args.sample)),
    )

    logger.info("invoking the official harness on %s", predictions)
    subprocess.run(  # nosec B603 - operator-provided module name, no shell
        [
            sys.executable,
            "-m",
            args.harness,
            "--dataset_name",
            args.dataset,
            "--predictions_path",
            str(predictions),
            "--run_id",
            run_dir.name,
        ],
        check=True,
    )

    report_path = select_report(sorted(Path.cwd().glob(f"*{run_dir.name}*.json")))
    if report_path is None:
        logger.error("official harness produced no report file; nothing graded")
        return 1
    report = json.loads(report_path.read_text(encoding="utf-8"))

    graded = []
    for record in records:
        result = parse_official_report(report, record.instance_id)
        graded.append(
            {
                # asdict() writes the 17 primary fields; plausible_but_wrong,
                # total_retries and total_tokens are properties and derive from
                # these, so they are recomputed rather than stored.
                **asdict(record),
                "resolved": result.resolved,
                "plausible": result.plausible,
            }
        )
    with (run_dir / "graded.jsonl").open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(
            "\n".join(json.dumps(row, sort_keys=True) for row in graded) + "\n"
        )
    logger.info("graded %d attempts into %s", len(graded), run_dir / "graded.jsonl")
    return 0


if __name__ == "__main__":
    sys.exit(main())
