"""Summarize past worker outcomes by task shape, for the review brain.

We feed a compact summary (not raw rows) into the plan-review prompt so the
capability gate calibrates to what THIS model actually achieved on THIS repo.
With no history, returns a sentinel so the brain relies on the declared profile.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from orchestrator.core.failure_taxonomy import FailureClass, counts_against_worker


_ATTRIBUTABLE_FAIL_CLASSES = tuple(
    fc.value for fc in FailureClass if counts_against_worker(fc)
)


async def fetch_recent_outcomes(
    db: Any,
    model_name: str,
    project_id: str | None,
    limit: int = 100,
) -> list[dict]:
    """Fetch recent task outcomes scoped to a model (and optionally a project).

    Only passes and attributable failures (those counted against the worker)
    are returned.  Non-attributable failures such as ``provider_error`` are
    excluded so the brain does not penalise the model for infrastructure issues.

    When ``project_id`` is provided, the query first tries project-scoped rows.
    If none are found, it falls back to model-wide rows.

    Args:
        db: Database object with ``fetch_all(query, params)``.
        model_name: Worker model name to filter on.
        project_id: Optional project id for tighter scoping.
        limit: Maximum number of rows to return.

    Returns:
        List of row dicts compatible with ``summarize_outcomes``.
    """
    # Only `?` placeholders are interpolated (one per attributable failure
    # class); every value is bound via the params tuple, so this is not a SQL
    # injection vector despite the f-string.
    attributable_placeholders = ",".join("?" * len(_ATTRIBUTABLE_FAIL_CLASSES))
    base_sql = (
        # Placeholders only; all values are parameterized (see params_base).
        "SELECT * FROM task_outcomes WHERE model_name = ? AND source = 'run'"  # nosec B608
        " AND (outcome = 'pass' OR (outcome = 'fail'"
        f" AND failure_class IN ({attributable_placeholders})))"
        " ORDER BY created_at DESC LIMIT ?"
    )

    params_base: list[Any] = [model_name, *_ATTRIBUTABLE_FAIL_CLASSES, limit]

    if project_id is not None:
        scoped_sql = base_sql.replace(
            "WHERE model_name = ?",
            "WHERE model_name = ? AND project_id = ?",
            1,
        )
        scoped_params = [model_name, project_id, *_ATTRIBUTABLE_FAIL_CLASSES, limit]
        rows = await db.fetch_all(scoped_sql, tuple(scoped_params))
        if rows:
            return [dict(r) for r in rows]

    return [dict(r) for r in await db.fetch_all(base_sql, tuple(params_base))]


def _measured(value: object) -> int:
    """Return a measurement as an int, treating "not measured" as zero.

    Zero is correct HERE and only here: this feeds a running maximum, so an
    unmeasured row must contribute nothing rather than raise or invent a size.
    It is not written anywhere, which is what separates it from the 0 the
    outcome recorder used to store as though it were a measurement.

    Args:
        value: A nullable count from a ``task_outcomes`` row.

    Returns:
        The count, or 0 when it is None or unusable.
    """
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def summarize_outcomes(runs: list[dict]) -> str:
    """Return a short per-task-type pass/fail summary.

    ``files_touched`` and ``loc_delta`` are NULLABLE and a NULL is a real
    answer: the review path records one whenever the verify gate failed before
    any diff was fetched, because the size of that change was never measured.
    ``int(r.get("files_touched", 0))`` raised on it, and the default hid the
    bug: the key IS present, so the default never applied. One such row for a
    model made every later ``decompose_plan`` for that model raise before the
    brain was called.

    Args:
        runs: Rows with ``task_type``, ``outcome`` ("pass"/"fail"), and
            ``files_touched`` / ``loc_delta``, either of which may be None
            meaning "not measured".
    """
    if not runs:
        return "(no prior run history for this model)"
    by_type: dict[str, dict[str, int]] = defaultdict(
        lambda: {"pass": 0, "fail": 0, "max_files": 0, "max_loc": 0}  # nosec B105
    )
    for r in runs:
        t = by_type[r.get("task_type") or "unknown"]
        outcome = "pass" if r.get("outcome") == "pass" else "fail"
        t[outcome] += 1
        t["max_files"] = max(t["max_files"], _measured(r.get("files_touched")))
        t["max_loc"] = max(t["max_loc"], _measured(r.get("loc_delta")))
    lines = ["Observed local-model outcomes by task type:"]
    for ttype, s in sorted(by_type.items()):
        lines.append(
            f"- {ttype}: pass: {s['pass']}, fail: {s['fail']} "
            f"(largest seen: {s['max_files']} files / {s['max_loc']} LOC)"
        )
    return "\n".join(lines)
