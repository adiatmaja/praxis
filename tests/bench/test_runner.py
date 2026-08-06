"""The runner's job is to be boring and to refuse a confounded design.

Three things here are load-bearing and fail SILENTLY if they break, which is
why each gets its own test rather than being left to review:

1. a condition that resolves no ``verify_cmd`` must be refused BEFORE anything
   is dispatched, and EVERY condition resolves one.  ``None`` makes the
   plan-branch verify gate return ``skipped``, and ``skipped`` is
   indistinguishable from ``passed`` to both of its callers, so condition B
   would quietly run as condition C while the record still claims it was gated.
   The ungated arms register the command too, because it also feeds the leaf's
   acceptance floor and the worker's Bible; the gate itself is disabled in the
   orchestrator (tests/bench/test_runner_conditions.py);
2. the poll's terminal set must match this repo's frozen status vocabulary.  A
   task that ends ``superseded`` (a split parent) would otherwise never satisfy
   the poll and burn the whole one-hour ceiling before being recorded as a
   timeout it never had;
3. every instance must carry a ``problem_statement``.  Without one every
   attempt raises inside the runner's broad handler and the whole matrix comes
   back 100 percent errors.
"""

import json
from pathlib import Path
from typing import Any

import pytest

from bench.config import CONDITIONS
from bench.runner import (
    TERMINAL_PLAN_STATUSES,
    TERMINAL_TASK_STATUSES,
    ConfoundedDesignError,
    MissingProblemStatementError,
    MissingVerifyCommandError,
    assert_matched_pair,
    assert_runnable,
    plan_attempts,
    resolve_verify_cmd,
    run_attempt,
)
from orchestrator.core.status_vocab import TERMINAL_STATUSES


BY_CONDITION = {c.key: c for c in CONDITIONS}

PILOT_SAMPLE = Path(__file__).resolve().parents[2] / "bench" / "samples"


def _instance(**overrides: Any) -> dict[str, Any]:
    """A minimally complete sample entry."""
    base = {
        "instance_id": "x__y-1",
        "problem_statement": "the bug",
        "stratum_patch": "small",
        "stratum_repo": "mid",
        "base_commit": "0" * 40,
    }
    base.update(overrides)
    return base


class RecordingClient:
    """A BenchClient stand-in that records every call it is asked to make."""

    def __init__(self, task_states: list[str] | None = None) -> None:
        self.calls: list[tuple[str, Any]] = []
        self._task_states = list(task_states or ["merged"])

    async def register_project(self, **kwargs: Any) -> str:
        self.calls.append(("register_project", kwargs))
        return "project-1"

    async def dispatch(self, repo_url: str, instructions: str, worker: Any) -> str:
        self.calls.append(("dispatch", instructions))
        return "task-1"

    async def get_task(self, task_id: str) -> dict[str, Any]:
        """Walk the scripted states, then STICK on the last one.

        Sticking is load-bearing. An earlier version fell back to ``"merged"``
        once the script drained, which meant a poll that failed to recognize
        the scripted terminal state still ended on the next tick. That made the
        superseded test vacuous: it survived a terminal set with ``superseded``
        removed. A real task never leaves its terminal state, so neither does
        this.
        """
        self.calls.append(("get_task", task_id))
        state = (
            self._task_states.pop(0)
            if len(self._task_states) > 1
            else (self._task_states[0])
        )
        return {"status": state, "attempt": 2}

    async def execute_plan(self, repo_url: str, plan: str, worker: Any) -> str:
        self.calls.append(("execute_plan", plan))
        return "plan-1"

    async def poll_plan(self, plan_id: str) -> dict[str, Any]:
        self.calls.append(("poll_plan", plan_id))
        return {"status": "completed"}

    async def plan_tasks(self, plan_id: str) -> list[dict[str, Any]]:
        self.calls.append(("plan_tasks", plan_id))
        return [
            {"status": "merged", "attempt": 2, "clarification_question": "why?"},
            {"status": "passed", "attempt": 1, "clarification_question": None},
        ]


# --------------------------------------------------------------------------
# The plan's own design guard
# --------------------------------------------------------------------------


@pytest.mark.unit
def test_a_and_c_together_are_a_valid_matched_pair():
    assert_matched_pair(["A", "B", "C"])  # must not raise


@pytest.mark.unit
def test_c_without_a_is_refused():
    """C alone cannot be compared to anything; A is its verify-gate twin."""
    with pytest.raises(ConfoundedDesignError, match="A"):
        assert_matched_pair(["B", "C"])


@pytest.mark.unit
def test_a_and_b_alone_is_a_valid_pilot_design():
    assert_matched_pair(["A", "B"])


@pytest.mark.unit
def test_an_unknown_condition_is_refused():
    with pytest.raises(ConfoundedDesignError):
        assert_matched_pair(["A", "Z"])


@pytest.mark.unit
def test_plan_attempts_is_the_full_cross_product():
    instances = [{"instance_id": "i1"}, {"instance_id": "i2"}]
    attempts = plan_attempts(instances, ["A", "B"], ["local-openweight"], seeds=[1])
    assert len(attempts) == 4
    assert {(a.instance["instance_id"], a.condition.key) for a in attempts} == {
        ("i1", "A"),
        ("i1", "B"),
        ("i2", "A"),
        ("i2", "B"),
    }


@pytest.mark.unit
def test_plan_attempts_multiplies_by_seed():
    instances = [{"instance_id": "i1"}]
    attempts = plan_attempts(instances, ["B"], ["local-openweight"], seeds=[1, 2])
    assert sorted(a.seed for a in attempts) == [1, 2]


@pytest.mark.unit
def test_plan_attempts_multiplies_by_worker():
    instances = [{"instance_id": "i1"}]
    attempts = plan_attempts(
        instances, ["B"], ["local-openweight", "hosted-flash"], seeds=[1]
    )
    assert {a.worker.key for a in attempts} == {"local-openweight", "hosted-flash"}


@pytest.mark.unit
def test_plan_attempts_is_deterministically_ordered():
    instances = [{"instance_id": "i2"}, {"instance_id": "i1"}]
    a = plan_attempts(instances, ["B", "A"], ["local-openweight"], seeds=[1])
    b = plan_attempts(instances, ["B", "A"], ["local-openweight"], seeds=[1])
    assert [(x.instance["instance_id"], x.condition.key) for x in a] == [
        (y.instance["instance_id"], y.condition.key) for y in b
    ]


@pytest.mark.unit
def test_every_declared_condition_is_plannable():
    instances = [{"instance_id": "i1"}]
    attempts = plan_attempts(
        instances, [c.key for c in CONDITIONS], ["local-openweight"], seeds=[1]
    )
    assert len(attempts) == len(CONDITIONS)


# --------------------------------------------------------------------------
# The verify_cmd resolution, which is what keeps B from becoming C
# --------------------------------------------------------------------------


@pytest.mark.unit
def test_a_per_instance_verify_cmd_wins_over_the_run_wide_default():
    resolved = resolve_verify_cmd(
        _instance(verify_cmd="pytest tests/test_x.py"),
        BY_CONDITION["B"],
        default="pytest",
    )
    assert resolved == "pytest tests/test_x.py"


@pytest.mark.unit
def test_the_run_wide_default_applies_when_the_instance_has_none():
    resolved = resolve_verify_cmd(_instance(), BY_CONDITION["B"], default="pytest -q")
    assert resolved == "pytest -q"


@pytest.mark.unit
def test_a_gated_condition_with_no_verify_cmd_anywhere_is_refused():
    """None would degrade the gate to 'skipped', which reads as 'passed'."""
    with pytest.raises(MissingVerifyCommandError, match="x__y-1"):
        resolve_verify_cmd(_instance(), BY_CONDITION["B"], default=None)


@pytest.mark.unit
def test_an_empty_verify_cmd_is_refused_the_same_as_a_missing_one():
    with pytest.raises(MissingVerifyCommandError):
        resolve_verify_cmd(_instance(verify_cmd="   "), BY_CONDITION["B"], default=None)


@pytest.mark.unit
@pytest.mark.parametrize("condition_key", ["A", "C"])
def test_an_ungated_condition_resolves_the_command_too(condition_key):
    """A and C are the matched no-gate pair, and they still register it.

    The project's ``verify_cmd`` is not only the gate: it is also the leaf's
    acceptance floor and the worker's Bible slot, and neither of those reads
    bench mode. Withholding it here would make B versus C differ in the task
    the worker attempts as well as in the gate. The gate is disabled in the
    orchestrator instead; see tests/bench/test_runner_conditions.py.
    """
    resolved = resolve_verify_cmd(
        _instance(verify_cmd="pytest"), BY_CONDITION[condition_key], default="pytest"
    )
    assert resolved == "pytest"


@pytest.mark.unit
def test_the_matrix_preflight_refuses_a_gated_condition_before_the_run_starts():
    attempts = plan_attempts([_instance()], ["B"], ["local-openweight"], seeds=[1])
    with pytest.raises(MissingVerifyCommandError):
        assert_runnable(attempts, verify_cmd_default=None)


@pytest.mark.unit
def test_the_matrix_preflight_refuses_an_instance_with_no_problem_statement():
    broken = _instance()
    del broken["problem_statement"]
    attempts = plan_attempts([broken], ["A"], ["local-openweight"], seeds=[1])
    # A default is supplied so the verify_cmd resolution, which now refuses for
    # EVERY condition, does not mask the problem-statement refusal under test.
    with pytest.raises(MissingProblemStatementError, match="x__y-1"):
        assert_runnable(attempts, verify_cmd_default="pytest")


@pytest.mark.unit
def test_the_matrix_preflight_passes_a_well_formed_matrix():
    # A and C, not A and B: one invocation cannot mix gated and ungated
    # conditions, because the orchestrator reads the bench flags from its own
    # environment. See test_runner_conditions.py.
    attempts = plan_attempts([_instance()], ["A", "C"], ["local-openweight"], seeds=[1])
    assert_runnable(attempts, verify_cmd_default="pytest")  # must not raise


@pytest.mark.unit
async def test_a_gated_attempt_with_no_verify_cmd_refuses_before_any_api_call(tmp_path):
    """The refusal must precede the dispatch, not be recorded as an error row."""
    attempt = plan_attempts([_instance()], ["B"], ["local-openweight"], seeds=[1])[0]
    client = RecordingClient()
    with pytest.raises(MissingVerifyCommandError):
        await run_attempt(
            attempt,
            client,
            run_id="run-1",
            repo_root=tmp_path,
            out_path=tmp_path / "attempts.jsonl",
            verify_cmd_default=None,
        )
    assert client.calls == []
    assert not (tmp_path / "attempts.jsonl").exists()


@pytest.mark.unit
async def test_a_gated_attempt_passes_the_resolved_verify_cmd_to_the_project(tmp_path):
    attempt = plan_attempts(
        [_instance(verify_cmd="pytest tests/test_x.py")],
        ["B"],
        ["local-openweight"],
        seeds=[1],
    )[0]
    client = RecordingClient()
    await run_attempt(
        attempt,
        client,
        run_id="run-1",
        repo_root=tmp_path,
        out_path=tmp_path / "attempts.jsonl",
        verify_cmd_default=None,
    )
    registered = next(kw for name, kw in client.calls if name == "register_project")
    assert registered["verify_cmd"] == "pytest tests/test_x.py"


@pytest.mark.unit
async def test_an_ungated_attempt_registers_the_verify_cmd_as_well(tmp_path):
    """The ungated arms carry the command; only the orchestrator's gate moves."""
    attempt = plan_attempts(
        [_instance(verify_cmd="pytest")], ["A"], ["local-openweight"], seeds=[1]
    )[0]
    client = RecordingClient()
    await run_attempt(
        attempt,
        client,
        run_id="run-1",
        repo_root=tmp_path,
        out_path=tmp_path / "attempts.jsonl",
        verify_cmd_default="pytest",
    )
    registered = next(kw for name, kw in client.calls if name == "register_project")
    assert registered["verify_cmd"] == "pytest"


# --------------------------------------------------------------------------
# The poll's terminal set, drawn from the frozen vocabulary
# --------------------------------------------------------------------------


@pytest.mark.unit
def test_the_poll_covers_every_frozen_terminal_status():
    """Re-typed string literals drift from the enum; the frozen set cannot."""
    assert TERMINAL_STATUSES <= TERMINAL_TASK_STATUSES


@pytest.mark.unit
def test_superseded_is_terminal_for_the_poll():
    assert "superseded" in TERMINAL_TASK_STATUSES


@pytest.mark.unit
def test_the_gated_passed_state_is_terminal_for_the_poll():
    assert "passed" in TERMINAL_TASK_STATUSES


@pytest.mark.unit
def test_the_plan_poll_terminal_set_is_the_plan_vocabulary():
    assert frozenset({"completed", "failed", "rejected"}) == TERMINAL_PLAN_STATUSES


@pytest.mark.unit
async def test_a_superseded_task_ends_the_poll_instead_of_timing_out(
    tmp_path, monkeypatch
):
    monkeypatch.setattr("bench.runner._POLL_INTERVAL_S", 0)
    # A short ceiling so a terminal set that has dropped 'superseded' fails in
    # seconds instead of spinning for the real one-hour attempt timeout, which
    # is exactly the burn this test exists to prevent.
    monkeypatch.setattr("bench.runner._ATTEMPT_TIMEOUT_S", 2)
    attempt = plan_attempts([_instance()], ["A"], ["local-openweight"], seeds=[1])[0]
    client = RecordingClient(task_states=["in_progress", "superseded"])
    record = await run_attempt(
        attempt,
        client,
        run_id="run-1",
        repo_root=tmp_path,
        out_path=tmp_path / "attempts.jsonl",
        verify_cmd_default="pytest",
    )
    assert record.error is None
    assert record.whole_task_retries == 1


@pytest.mark.unit
async def test_a_gated_terminal_is_recorded_as_a_human_gate_touch(tmp_path):
    """'passed' means parked at the merge gate, not delivered. Say so."""
    attempt = plan_attempts([_instance()], ["A"], ["local-openweight"], seeds=[1])[0]
    client = RecordingClient(task_states=["passed"])
    record = await run_attempt(
        attempt,
        client,
        run_id="run-1",
        repo_root=tmp_path,
        out_path=tmp_path / "attempts.jsonl",
        verify_cmd_default="pytest",
    )
    assert record.human_gate_touches == 1


@pytest.mark.unit
async def test_every_attempt_is_recorded_even_when_the_client_raises(tmp_path):
    class Exploding(RecordingClient):
        async def register_project(self, **kwargs: Any) -> str:
            msg = "orchestrator is down"
            raise RuntimeError(msg)

    attempt = plan_attempts([_instance()], ["A"], ["local-openweight"], seeds=[1])[0]
    record = await run_attempt(
        attempt,
        Exploding(),
        run_id="run-1",
        repo_root=tmp_path,
        out_path=tmp_path / "attempts.jsonl",
        verify_cmd_default="pytest",
    )
    assert record.error is not None
    assert "orchestrator is down" in record.error
    assert (tmp_path / "attempts.jsonl").read_text(encoding="utf-8").strip()


# --------------------------------------------------------------------------
# The decompose-branch leaf metrics, which the published retry numbers derive
# from
# --------------------------------------------------------------------------


@pytest.mark.unit
async def test_the_decompose_branch_records_all_four_leaf_metrics(tmp_path):
    """RecordingClient.plan_tasks is built to exercise all four computations:

    one task at attempt 2 (one retry), one at attempt 1 (no retry); one
    carrying a ``clarification_question``, one without; one ``merged``
    (not gated), one ``passed`` (gated). Condition C decomposes with the
    mechanical gate disabled in the ORCHESTRATOR, so it still resolves and
    registers a ``verify_cmd`` exactly like every other condition.
    """
    # C requires its matched no-gate pair A in the same plan_attempts call.
    attempts = plan_attempts([_instance()], ["A", "C"], ["local-openweight"], seeds=[1])
    attempt = next(a for a in attempts if a.condition.key == "C")
    client = RecordingClient()
    out_path = tmp_path / "attempts.jsonl"
    record = await run_attempt(
        attempt,
        client,
        run_id="run-1",
        repo_root=tmp_path,
        out_path=out_path,
        verify_cmd_default="pytest",
    )
    assert record.leaf_count == 2
    assert record.leaf_retries == 1
    assert record.clarifications == 1
    assert record.human_gate_touches == 1

    written = json.loads(out_path.read_text(encoding="utf-8").strip())
    assert written["leaf_count"] == 2
    assert written["leaf_retries"] == 1
    assert written["clarifications"] == 1
    assert written["human_gate_touches"] == 1


# --------------------------------------------------------------------------
# The attempt timeout, which must produce a recorded row per branch, never a
# dropped attempt
# --------------------------------------------------------------------------


@pytest.mark.unit
async def test_a_dispatch_attempt_that_never_reaches_terminal_status_times_out(
    tmp_path, monkeypatch
):
    """The dispatch/task-poll branch's ``while ... else`` sets the timeout error.

    ``RecordingClient.get_task`` sticks on its last scripted state rather than
    falling back to a terminal one once the script drains (see its docstring),
    so a single-element ``task_states`` list stays non-terminal forever and the
    poll can only end by hitting the deadline.
    """
    monkeypatch.setattr("bench.runner._POLL_INTERVAL_S", 0)
    monkeypatch.setattr("bench.runner._ATTEMPT_TIMEOUT_S", 0.1)
    attempt = plan_attempts([_instance()], ["A"], ["local-openweight"], seeds=[1])[0]
    client = RecordingClient(task_states=["in_progress"])
    out_path = tmp_path / "attempts.jsonl"
    record = await run_attempt(
        attempt,
        client,
        run_id="run-1",
        repo_root=tmp_path,
        out_path=out_path,
        verify_cmd_default="pytest",
    )
    assert record.error == "attempt timed out"
    written = json.loads(out_path.read_text(encoding="utf-8").strip())
    assert written["error"] == "attempt timed out"


@pytest.mark.unit
async def test_a_decompose_attempt_that_never_reaches_terminal_status_times_out(
    tmp_path, monkeypatch
):
    """The decompose/plan-poll branch's ``while ... else`` sets the timeout error.

    Unlike ``RecordingClient.poll_plan`` (which always reports ``completed``),
    this stub returns a non-terminal ``running`` status on every call, forever,
    so the poll can only end by hitting the deadline rather than by silently
    landing on a terminal state.
    """
    monkeypatch.setattr("bench.runner._POLL_INTERVAL_S", 0)
    monkeypatch.setattr("bench.runner._ATTEMPT_TIMEOUT_S", 0.1)

    class NeverTerminalPlanClient(RecordingClient):
        async def poll_plan(self, plan_id: str) -> dict[str, Any]:
            self.calls.append(("poll_plan", plan_id))
            return {"status": "running"}

    # C requires its matched no-gate pair A in the same plan_attempts call.
    attempts = plan_attempts([_instance()], ["A", "C"], ["local-openweight"], seeds=[1])
    attempt = next(a for a in attempts if a.condition.key == "C")
    client = NeverTerminalPlanClient()
    out_path = tmp_path / "attempts.jsonl"
    record = await run_attempt(
        attempt,
        client,
        run_id="run-1",
        repo_root=tmp_path,
        out_path=out_path,
        verify_cmd_default="pytest",
    )
    assert record.error == "attempt timed out"
    written = json.loads(out_path.read_text(encoding="utf-8").strip())
    assert written["error"] == "attempt timed out"


# --------------------------------------------------------------------------
# The problem statement, which the committed sample has to carry
# --------------------------------------------------------------------------


@pytest.mark.unit
def test_every_committed_pilot_instance_carries_a_problem_statement():
    """Without it every attempt errors and the matrix reports 100 percent errors."""
    for path in sorted(PILOT_SAMPLE.glob("*.json")):
        sample = json.loads(path.read_text(encoding="utf-8"))
        for entry in sample["instances"]:
            statement = entry.get("problem_statement")
            assert statement, f"{path.name}: {entry['instance_id']} has none"


@pytest.mark.unit
def test_the_committed_sample_is_lf_and_carries_a_base_commit():
    for path in sorted(PILOT_SAMPLE.glob("*.json")):
        assert b"\r\n" not in path.read_bytes(), f"{path.name} is CRLF"
        sample = json.loads(path.read_text(encoding="utf-8"))
        for entry in sample["instances"]:
            assert len(entry["base_commit"]) == 40
