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

    This is the mapper for an INSTANCE-KEYED report only: ``report`` must map
    an instance id straight to that instance's fields, the shape of the
    official harness's PER-INSTANCE report file
    (``logs/run_evaluation/<run_id>/<model>/<instance_id>/report.json``). It
    is never a valid mapper for the harness's AGGREGATE run report, whose
    top-level keys are things like ``resolved_ids`` and ``submitted_ids``,
    never an instance id; see ``is_aggregate_report`` and ``grade_instance``,
    which recognizes that shape and grades from list membership instead.

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


# The two marker keys of the official harness's AGGREGATE run report
# (swebench/harness/reporting.py, fetched 2026-08-25): present at top level,
# both sorted lists of instance-id strings, and never a valid instance id
# themselves.  An instance-keyed per-instance report never carries these
# names as keys, so their presence, as lists, is a positive recognizer for
# the aggregate shape rather than a guess.
_AGGREGATE_MARKER_KEYS: tuple[str, ...] = ("resolved_ids", "submitted_ids")

# Directory root the official harness writes per-instance report.json files
# under, relative to the harness's own cwd:
# ``<log_root>/<run_id>/<model_name with "/" -> "__">/<instance_id>/report.json``.
DEFAULT_LOG_ROOT = Path("logs/run_evaluation")


def is_aggregate_report(report: Mapping[str, Any]) -> bool:
    """True when ``report`` is the harness's run-level summary, not per-instance detail.

    Recognized POSITIVELY: both marker keys must be present and be lists.
    A report that is neither this shape nor the instance-keyed shape must be
    treated as unrecognized (see ``grade_records``), never silently graded as
    an empty aggregate: an aggregate with both lists missing would grade
    every instance unresolved, which is precisely the bug this module fixes.
    """
    return all(isinstance(report.get(key), list) for key in _AGGREGATE_MARKER_KEYS)


def is_instance_keyed_report(report: Mapping[str, Any]) -> bool:
    """True when ``report`` maps instance ids straight to their own fields.

    Every value must itself be a mapping carrying the ``resolved`` key; that
    is what distinguishes a genuine per-instance report from an aggregate
    (whose values are lists of ids) or an unrelated JSON object. An empty
    mapping is NOT recognized as this shape: it carries no evidence either
    way, and treating it as instance-keyed would let a truly empty file slip
    past the shape check in ``grade_records`` uncaught.
    """
    return bool(report) and all(
        isinstance(value, Mapping) and "resolved" in value for value in report.values()
    )


def instance_report_path(
    log_root: Path, run_id: str, model_name: str, instance_id: str
) -> Path:
    """The official harness's per-instance report path for one instance.

    ``model_name`` has ``/`` replaced with ``__``, matching the harness's own
    directory-safe substitution (fetched from ``reporting.py`` 2026-08-25).
    """
    safe_model = model_name.replace("/", "__")
    return log_root / run_id / safe_model / instance_id / "report.json"


def grade_instance(
    aggregate: Mapping[str, Any],
    instance_id: str,
    log_root: Path,
    run_id: str,
    model_name: str,
) -> GradeResult:
    """Grade one instance against an AGGREGATE report, preferring detail when it exists.

    The aggregate settles ``resolved`` by list membership directly. It does
    NOT carry ``patch_successfully_applied`` at all, so ``plausible``/
    ``applied`` are read from the more specific per-instance report file when
    one is on disk, and inferred from the aggregate's id lists otherwise:
    ``empty_patch_ids``/``error_ids`` settle it False, a resolved instance
    necessarily applied so ``resolved_ids`` settles it True, and anything left
    over is genuinely unknown and recorded False with a warning naming the
    instance, so the understatement is visible rather than silently assumed.

    Args:
        aggregate: The harness's aggregate run report (see
            ``is_aggregate_report``).
        instance_id: The instance being graded.
        log_root: Root directory the harness writes per-instance reports
            under; an argument (rather than hardcoded) so a test can point it
            at ``tmp_path``.
        run_id: The harness run id, second path segment of the per-instance
            report.
        model_name: The ``model_name_or_path`` value predictions were written
            under; same string ``write_predictions`` was called with.

    Returns:
        The graded result. An instance absent from ``submitted_ids`` is a
        grading FAULT (Praxis submitted it and the harness never accounted
        for it), logged as an error and graded conservatively unresolved.
    """
    path = instance_report_path(log_root, run_id, model_name, instance_id)
    if path.is_file():
        try:
            detail = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            logger.warning("could not parse per-instance report %s: %s", path, exc)
        else:
            return parse_official_report(detail, instance_id)

    submitted_ids = aggregate.get("submitted_ids", [])
    if instance_id not in submitted_ids:
        logger.error(
            "%s is missing from submitted_ids and has no per-instance report "
            "at %s; the official harness never accounted for this instance, "
            "which is a grading fault, not an honest unresolved attempt",
            instance_id,
            path,
        )
        return GradeResult(resolved=False, plausible=False, applied=False)

    resolved = instance_id in aggregate.get("resolved_ids", [])
    if resolved:
        applied = True
    elif instance_id in aggregate.get("empty_patch_ids", []) or instance_id in (
        aggregate.get("error_ids", [])
    ):
        applied = False
    else:
        logger.warning(
            "%s: aggregate report does not settle whether the patch applied "
            "(absent from resolved_ids, empty_patch_ids, and error_ids); "
            "recording applied=False conservatively",
            instance_id,
        )
        applied = False
    return GradeResult(resolved=resolved, plausible=applied, applied=applied)


def grade_records(
    records: Sequence[AttemptRecord],
    report: Mapping[str, Any],
    log_root: Path,
    run_id: str,
    model_name: str,
) -> list[dict[str, Any]] | None:
    """Grade every record against one official-harness report.

    Args:
        records: The run's attempt records.
        report: The harness's report, either shape (see ``is_aggregate_report``
            / ``is_instance_keyed_report``).
        log_root: Root directory of per-instance reports (``grade_instance``).
        run_id: The harness run id.
        model_name: The ``model_name_or_path`` predictions were written under.

    Returns:
        One graded dict per record (``asdict(record)`` with ``resolved`` and
        ``plausible`` overwritten), or ``None`` when ``report`` matches
        NEITHER recognized shape. Grading every attempt as unresolved against
        an unrecognized report is the silent-zero failure this module exists
        to prevent, so the caller must refuse to write a graded file at all
        rather than publish one full of False.
    """
    if is_aggregate_report(report):

        def grade_one(instance_id: str) -> GradeResult:
            return grade_instance(report, instance_id, log_root, run_id, model_name)

    elif is_instance_keyed_report(report):

        def grade_one(instance_id: str) -> GradeResult:
            return parse_official_report(dict(report), instance_id)

    else:
        logger.error(
            "unrecognized official-harness report shape: expected either the "
            "aggregate run report (resolved_ids/submitted_ids as lists) or an "
            "instance-keyed per-instance report (each value a mapping with a "
            "'resolved' key); refusing to grade %d attempts against it rather "
            "than silently scoring every one unresolved",
            len(records),
        )
        return None

    graded = []
    for record in records:
        result = grade_one(record.instance_id)
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
    return graded


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
    parser.add_argument(
        "--log-root",
        default=str(DEFAULT_LOG_ROOT),
        help="root the official harness writes per-instance reports under",
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

    graded = grade_records(records, report, Path(args.log_root), run_dir.name, "praxis")
    if graded is None:
        logger.error(
            "official report at %s matched no recognized shape; %s NOT written",
            report_path,
            run_dir / "graded.jsonl",
        )
        return 1
    with (run_dir / "graded.jsonl").open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(
            "\n".join(json.dumps(row, sort_keys=True) for row in graded) + "\n"
        )
    logger.info("graded %d attempts into %s", len(graded), run_dir / "graded.jsonl")
    return 0


if __name__ == "__main__":
    sys.exit(main())
