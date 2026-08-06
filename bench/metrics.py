"""The benchmark's record: one JSONL row per task-attempt.

Append-only, one line of JSON per row, committed with the report.  Every
attempt is recorded including crashes: silently dropping a failed attempt
inflates the resolve rate, which is the easiest way to publish a wrong number
without lying on purpose.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, fields
from pathlib import Path


@dataclass(frozen=True)
class AttemptRecord:
    """One instance, run once, under one condition, by one worker."""

    run_id: str
    instance_id: str
    condition: str
    worker: str
    seed: int
    stratum_patch: str
    stratum_repo: str

    # Primary outcome, from the OFFICIAL SWE-bench grader. Never self-graded.
    resolved: bool
    # The patch applied and the project built, whether or not it is correct.
    plausible: bool

    leaf_count: int
    leaf_retries: int
    whole_task_retries: int
    clarifications: int
    human_gate_touches: int

    brain_tokens: int
    worker_tokens: int
    wall_clock_s: float

    error: str | None = None

    @property
    def plausible_but_wrong(self) -> bool:
        """A patch that applies and builds but does not resolve the issue.

        AutoCodeRover (arXiv 2404.05427) found 35 percent of plausible patches
        wrong; a worker gaming surface checks shows up exactly here.
        """
        return self.plausible and not self.resolved

    @property
    def total_retries(self) -> int:
        """Leaf-scoped plus whole-task-scoped retries."""
        return self.leaf_retries + self.whole_task_retries

    @property
    def total_tokens(self) -> int:
        """Brain plus worker tokens, for cost per RESOLVED task."""
        return self.brain_tokens + self.worker_tokens


def append_record(path: Path, record: AttemptRecord) -> None:
    """Append one row. Creates the file and its parent if needed.

    ``newline="\\n"`` is required: with the default newline translation,
    Windows rewrites every ``\\n`` written to the file to ``os.linesep``
    (``\\r\\n``), which would silently commit CRLF rows into the published
    JSONL artifact even though the repository convention is LF.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(asdict(record), sort_keys=True) + "\n")


def read_records(path: Path) -> list[AttemptRecord]:
    """Read every row. A missing file is an empty run, not an error."""
    if not Path(path).is_file():
        return []
    known = {f.name for f in fields(AttemptRecord)}
    rows: list[AttemptRecord] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        data = json.loads(line)
        rows.append(AttemptRecord(**{k: v for k, v in data.items() if k in known}))
    return rows
