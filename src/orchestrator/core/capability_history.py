"""Summarize past worker outcomes by task shape, for the review brain.

We feed a compact summary (not raw rows) into the plan-review prompt so the
capability gate calibrates to what THIS model actually achieved on THIS repo.
With no history, returns a sentinel so the brain relies on the declared profile.
"""

from __future__ import annotations

from collections import defaultdict


def summarize_outcomes(runs: list[dict]) -> str:
    """Return a short per-task-type pass/fail summary.

    Args:
        runs: Rows with ``task_type``, ``files_touched``, ``loc_delta``,
            ``outcome`` ("pass"/"fail").
    """
    if not runs:
        return "(no prior run history for this model)"
    by_type: dict[str, dict[str, int]] = defaultdict(
        lambda: {"pass": 0, "fail": 0, "max_files": 0, "max_loc": 0}
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
