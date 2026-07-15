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
    attributable_placeholders = ",".join("?" * len(_ATTRIBUTABLE_FAIL_CLASSES))
    base_sql = (
        "SELECT * FROM task_outcomes WHERE model_name = ? AND source = 'run'"
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


def summarize_outcomes(runs: list[dict]) -> str:
    """Return a short per-task-type pass/fail summary.

    Args:
        runs: Rows with ``task_type``, ``files_touched``, ``loc_delta``,
            ``outcome`` ("pass"/"fail").
    """
    if not runs:
        return "(no prior run history for this model)"
    by_type: dict[str, dict[str, int]] = defaultdict(
        lambda: {"pass": 0, "fail": 0, "max_files": 0, "max_loc": 0}  # nosec B105 — dict keys, not passwords
    )
    for r in runs:
        t = by_type[r.get("task_type", "unknown")]
        outcome = "pass" if r.get("outcome") == "pass" else "fail"
        t[outcome] += 1
        t["max_files"] = max(t["max_files"], int(r.get("files_touched", 0)))
        t["max_loc"] = max(t["max_loc"], int(r.get("loc_delta", 0)))
    lines = ["Observed local-model outcomes by task type:"]
    for ttype, s in sorted(by_type.items()):
        lines.append(
            f"- {ttype}: pass: {s['pass']}, fail: {s['fail']} "
            f"(largest seen: {s['max_files']} files / {s['max_loc']} LOC)"
        )
    return "\n".join(lines)
