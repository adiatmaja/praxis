"""Each condition must translate into the exact switches it declares.

Two of these translations fail SILENTLY when they break, which is why they get
their own file rather than being left to review.

1. Condition C is condition B with the MECHANICAL verify gate off, and with
   nothing else moved.  ``project["verify_cmd"]`` drives four things, and only
   three of them are the gate: ``orchestrator_dispatch`` also reads it as the
   leaf's acceptance floor and as the worker's Bible slot, neither of which is
   bench-mode aware.  Registering a project with ``verify_cmd=None`` therefore
   changes what the WORKER IS TOLD, so B versus C would compare verification
   plus a different prompt while the report claimed it compared verification.
   The gate difference has to come from the orchestrator's own bench mode, so
   every condition registers the same resolved command.
2. The bench flags are read by ``core/bench_mode.py`` with ``os.environ`` inside
   the ORCHESTRATOR process.  The runner is a separate process talking REST, so
   it cannot set them.  One invocation cannot hold both polarities, and an
   invocation that mixes gated and ungated conditions has to be refused before
   any container spawns.  Nothing can verify the orchestrator is actually in the
   mode the condition needs: there is no endpoint for it.
"""

from pathlib import Path
from typing import Any

import pytest

from bench.config import CONDITIONS, Condition
from bench.runner import (
    MissingVerifyCommandError,
    MixedBenchModeError,
    assert_matched_pair,
    assert_runnable,
    assert_uniform_bench_mode,
    condition_env,
    condition_project_overrides,
    plan_attempts,
    resolve_verify_cmd,
    run_attempt,
)
from orchestrator.core.bench_mode import verify_gate_disabled


def _condition(key: str) -> Condition:
    return next(c for c in CONDITIONS if c.key == key)


def _instance(**overrides: Any) -> dict[str, Any]:
    """A minimally complete sample entry."""
    base: dict[str, Any] = {
        "instance_id": "x__y-1",
        "problem_statement": "the bug",
        "stratum_patch": "small",
        "stratum_repo": "mid",
        "base_commit": "0" * 40,
    }
    base.update(overrides)
    return base


class RecordingClient:
    """A BenchClient stand-in that records the calls the runner makes."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    async def register_project(self, **kwargs: Any) -> str:
        self.calls.append(("register_project", kwargs))
        return "project-1"

    async def dispatch(self, repo_url: str, instructions: str, worker: Any) -> str:
        self.calls.append(("dispatch", instructions))
        return "task-1"

    async def get_task(self, task_id: str) -> dict[str, Any]:
        self.calls.append(("get_task", task_id))
        return {"status": "merged", "attempt": 1}

    async def execute_plan(self, repo_url: str, plan: str, worker: Any) -> str:
        self.calls.append(("execute_plan", plan))
        return "plan-1"

    async def poll_plan(self, plan_id: str) -> dict[str, Any]:
        self.calls.append(("poll_plan", plan_id))
        return {"status": "completed"}

    async def plan_tasks(self, plan_id: str) -> list[dict[str, Any]]:
        self.calls.append(("plan_tasks", plan_id))
        return []


def _registered(client: RecordingClient) -> dict[str, Any]:
    return next(kw for name, kw in client.calls if name == "register_project")


# --------------------------------------------------------------------------
# The plan's own six: each condition translates to the switches it declares
# --------------------------------------------------------------------------


@pytest.mark.unit
def test_condition_b_runs_with_the_verify_gate_and_no_bench_flags():
    env = condition_env(_condition("B"))
    assert "PRAXIS_BENCH_DISABLE_VERIFY" not in env
    assert condition_project_overrides(_condition("B"))["gate_enabled"] is True


@pytest.mark.unit
def test_condition_c_sets_both_bench_flags():
    env = condition_env(_condition("C"))
    assert env["PRAXIS_BENCH"] == "1"
    assert env["PRAXIS_BENCH_DISABLE_VERIFY"] == "1"


@pytest.mark.unit
def test_condition_a_also_runs_without_a_verify_gate():
    """A is C's matched baseline; both must be gateless."""
    assert condition_project_overrides(_condition("A"))["gate_enabled"] is False
    assert condition_project_overrides(_condition("C"))["gate_enabled"] is False


@pytest.mark.unit
def test_only_condition_d_enables_adaptive_split():
    for key in ("A", "B", "C"):
        assert condition_project_overrides(_condition(key))["adaptive_split"] is False
    assert condition_project_overrides(_condition("D"))["adaptive_split"] is True


@pytest.mark.unit
def test_condition_d_keeps_the_verify_gate():
    """Finer granularity must be paired with MORE verification, not less."""
    assert condition_project_overrides(_condition("D"))["gate_enabled"] is True


@pytest.mark.unit
def test_no_condition_env_leaks_a_bench_flag_when_the_gate_is_on():
    for key in ("B", "D"):
        assert condition_env(_condition(key)) == {}


@pytest.mark.unit
def test_condition_c_env_actually_flips_the_orchestrator_switch(monkeypatch):
    """The translation is pinned against the switch itself, not against names.

    ``verify_gate_disabled`` refuses either flag alone, so a half-set
    ``condition_env`` returns False here and condition C runs GATED under C's
    label.  Applying the returned mapping and asking the real function is the
    only assertion that cannot be satisfied by a plausible-looking dict.
    """
    for key, value in condition_env(_condition("C")).items():
        monkeypatch.setenv(key, value)
    assert verify_gate_disabled() is True


@pytest.mark.unit
def test_condition_b_env_leaves_the_orchestrator_switch_alone(monkeypatch):
    monkeypatch.delenv("PRAXIS_BENCH", raising=False)
    monkeypatch.delenv("PRAXIS_BENCH_DISABLE_VERIFY", raising=False)
    for key, value in condition_env(_condition("B")).items():
        monkeypatch.setenv(key, value)
    assert verify_gate_disabled() is False


# --------------------------------------------------------------------------
# The unconfounded realization: the gate moves, the worker's task does not
# --------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("condition_key", ["A", "C", "D"])
def test_every_condition_resolves_the_same_verify_cmd_as_condition_b(condition_key):
    """Mechanism 2: the project row is identical across arms.

    ``project["verify_cmd"]`` is also the leaf's acceptance floor and the
    worker's Bible slot, and neither of those reads bench mode.  Withholding
    the command from the ungated arms would change what the worker is asked to
    do, which is a confound B versus C cannot survive and no number reports.
    """
    gated = resolve_verify_cmd(_instance(), _condition("B"), default="pytest -q")
    assert resolve_verify_cmd(_instance(), _condition(condition_key), "pytest -q") == (
        gated
    )


@pytest.mark.unit
@pytest.mark.parametrize("condition_key", ["A", "B", "C", "D"])
def test_a_missing_verify_cmd_is_refused_for_every_condition(condition_key):
    """Every arm needs the command now, so every arm refuses without one."""
    with pytest.raises(MissingVerifyCommandError, match="x__y-1"):
        resolve_verify_cmd(_instance(), _condition(condition_key), default=None)


@pytest.mark.unit
@pytest.mark.parametrize("condition_key", ["A", "C"])
def test_an_ungated_condition_never_resolves_to_none(condition_key):
    resolved = resolve_verify_cmd(
        _instance(verify_cmd="pytest tests/test_x.py"),
        _condition(condition_key),
        default=None,
    )
    assert resolved == "pytest tests/test_x.py"


@pytest.mark.unit
async def test_the_ungated_arm_registers_a_real_verify_cmd(tmp_path: Path):
    """The end of the chain: the project row condition C actually creates.

    This is the assertion that mechanism 1 cannot satisfy.  If the runner ever
    withholds the command from an ungated condition again, C's worker gets a
    different Bible from B's worker and the ablation stops measuring
    verification.
    """
    attempts = plan_attempts(
        [_instance(verify_cmd="pytest tests/test_x.py")],
        ["A", "C"],
        ["local-openweight"],
        seeds=[1],
    )
    attempt = next(a for a in attempts if a.condition.key == "C")
    client = RecordingClient()
    record = await run_attempt(
        attempt,
        client,
        run_id="run-1",
        repo_root=tmp_path,
        out_path=tmp_path / "attempts.jsonl",
        verify_cmd_default=None,
    )
    assert record.error is None
    assert _registered(client)["verify_cmd"] == "pytest tests/test_x.py"


@pytest.mark.unit
async def test_the_ungated_arm_registers_the_same_row_as_the_gated_arm(tmp_path: Path):
    """B and C differ in the orchestrator's mode, never in the project row."""
    instance = _instance(verify_cmd="pytest tests/test_x.py")
    rows: dict[str, Any] = {}
    for keys, wanted in ((["B"], "B"), (["A", "C"], "C")):
        attempt = next(
            a
            for a in plan_attempts([instance], keys, ["local-openweight"], seeds=[1])
            if a.condition.key == wanted
        )
        client = RecordingClient()
        await run_attempt(
            attempt,
            client,
            run_id="run-1",
            repo_root=tmp_path,
            out_path=tmp_path / f"{wanted}.jsonl",
            verify_cmd_default=None,
        )
        rows[wanted] = _registered(client)
    assert rows["B"]["verify_cmd"] == rows["C"]["verify_cmd"]
    # adaptive_split is what BenchClient turns into max_retries; B and C must
    # agree on it too, or the ablation moves the triage path as well as the gate.
    assert rows["B"]["adaptive_split"] == rows["C"]["adaptive_split"]


# --------------------------------------------------------------------------
# The bench flags live in the ORCHESTRATOR's environment, not the runner's
# --------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("keys", [["A", "B"], ["B", "C", "A"], ["C", "D", "A"]])
def test_an_invocation_mixing_gated_and_ungated_conditions_is_refused(keys):
    with pytest.raises(MixedBenchModeError):
        assert_uniform_bench_mode(keys)


@pytest.mark.unit
@pytest.mark.parametrize("keys", [["A"], ["B"], ["A", "C"], ["B", "D"], []])
def test_a_uniform_invocation_is_accepted(keys):
    assert_uniform_bench_mode(keys)  # must not raise


@pytest.mark.unit
def test_the_mixed_mode_refusal_names_the_restart_and_both_flags():
    """The operator cannot act on the refusal without the exact instruction."""
    with pytest.raises(MixedBenchModeError) as excinfo:
        assert_uniform_bench_mode(["A", "B"])
    message = str(excinfo.value)
    assert "PRAXIS_BENCH=1" in message
    assert "PRAXIS_BENCH_DISABLE_VERIFY=1" in message
    assert "restart" in message.lower()


@pytest.mark.unit
def test_the_matrix_preflight_refuses_a_mixed_gate_matrix():
    """The check has to run before any container spawns, not at analysis time."""
    attempts = plan_attempts([_instance()], ["A", "B"], ["local-openweight"], seeds=[1])
    with pytest.raises(MixedBenchModeError):
        assert_runnable(attempts, verify_cmd_default="pytest")


@pytest.mark.unit
def test_the_matrix_preflight_accepts_a_uniform_matrix():
    attempts = plan_attempts([_instance()], ["A", "C"], ["local-openweight"], seeds=[1])
    assert_runnable(attempts, verify_cmd_default="pytest")  # must not raise


@pytest.mark.unit
def test_design_validity_and_runnability_are_separate_checks():
    """``A,B,C`` is a VALID design and an INVALID single invocation.

    ``assert_matched_pair`` answers "can these arms be compared", which does not
    change.  ``assert_uniform_bench_mode`` answers "can one orchestrator process
    execute them", which is a different question with a different answer.
    """
    assert_matched_pair(["A", "B", "C"])  # must not raise
    with pytest.raises(MixedBenchModeError):
        assert_uniform_bench_mode(["A", "B", "C"])
