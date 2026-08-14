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
   any container spawns.
3. Which arm the orchestrator is ACTUALLY in is a fact about a different
   process, and ``GET /api/status`` is where it comes from.  An ``A,C``
   invocation aimed at an orchestrator started without the flags would otherwise
   write rows carrying the right condition label and the wrong arm; nothing
   downstream recomputes the gate, so no number in the report could contradict
   them.  The comparison therefore has to happen before the first spawn, and a
   mismatch has to abort rather than warn.
"""

import argparse
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from bench.config import CONDITIONS, Condition
from bench.runner import (
    BenchClient,
    MissingVerifyCommandError,
    MixedBenchModeError,
    OrchestratorBenchModeError,
    _main_async,
    assert_matched_pair,
    assert_runnable,
    assert_uniform_bench_mode,
    condition_env,
    condition_project_overrides,
    main,
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


# --------------------------------------------------------------------------
# The orchestrator's REAL mode, compared before the first container spawns
# --------------------------------------------------------------------------
#
# The invocation being self-consistent was never the hard part.  The hard part
# is that the orchestrator is a different process, and an ``A,C`` invocation
# aimed at one started WITHOUT the flags produces rows carrying the right
# condition label and the wrong arm.  Nothing downstream recomputes the gate, so
# no number in the report can contradict them.  ``/api/status`` now reports the
# orchestrator's real mode and the runner refuses a mismatch before it spawns.

# What GET /api/status reports for the two arms an invocation can require.
GATELESS_ORCHESTRATOR: dict[str, Any] = {
    "bench_mode": True,
    "verify_gate_disabled": True,
}
GATED_ORCHESTRATOR: dict[str, Any] = {
    "bench_mode": False,
    "verify_gate_disabled": False,
}

# Every REST call that mutates the orchestrator or leads to a container. The
# refusal must happen with NONE of these recorded.
SPAWNING_CALLS = frozenset({"register_project", "dispatch", "execute_plan"})


class ModeReportingClient(RecordingClient):
    """A RecordingClient that also answers the bench-mode probe."""

    def __init__(self, reported: dict[str, Any]) -> None:
        super().__init__()
        self._reported = reported

    async def orchestrator_bench_mode(self) -> dict[str, Any]:
        self.calls.append(("orchestrator_bench_mode", None))
        return dict(self._reported)

    async def close(self) -> None:
        self.calls.append(("close", None))


def _write_sample(tmp_path: Path) -> Path:
    sample = tmp_path / "sample.json"
    sample.write_text(
        json.dumps({"instances": [_instance(verify_cmd="pytest -q")]}),
        encoding="utf-8",
    )
    return sample


def _args(tmp_path: Path, conditions: str) -> argparse.Namespace:
    return argparse.Namespace(
        sample=str(_write_sample(tmp_path)),
        conditions=conditions,
        worker="local-openweight",
        seeds="1",
        run_id="run-1",
        out=str(tmp_path / "attempts.jsonl"),
        repos=str(tmp_path),
        verify_cmd="pytest -q",
        api="http://bench.invalid",
        token="tok",
    )


def _always(client: RecordingClient) -> Callable[..., RecordingClient]:
    """A ``BenchClient`` factory that hands ``_main_async`` this exact client."""

    def factory(*_args: Any, **_kwargs: Any) -> RecordingClient:
        return client

    return factory


def _spawning(client: RecordingClient) -> list[str]:
    return [name for name, _ in client.calls if name in SPAWNING_CALLS]


@pytest.mark.unit
@pytest.mark.parametrize("condition_key", ["A", "B", "C", "D"])
@pytest.mark.parametrize(
    "orchestrator_is_gateless",
    [True, False],
    ids=["gateless-orchestrator", "gated-orchestrator"],
)
def test_every_condition_is_checked_against_the_orchestrators_real_mode(
    condition_key: str, orchestrator_is_gateless: bool
):
    """All eight cells, with the requirement taken from ``Condition`` itself.

    Deriving the expectation from ``verify_gate`` rather than restating it means
    a future condition added to ``CONDITIONS`` with the wrong polarity cannot
    make this test agree with the bug.
    """
    reported = GATELESS_ORCHESTRATOR if orchestrator_is_gateless else GATED_ORCHESTRATOR
    condition_needs_gateless = not _condition(condition_key).verify_gate
    if condition_needs_gateless == orchestrator_is_gateless:
        assert_uniform_bench_mode([condition_key], reported=reported)  # must not raise
    else:
        with pytest.raises(OrchestratorBenchModeError):
            assert_uniform_bench_mode([condition_key], reported=reported)


@pytest.mark.unit
@pytest.mark.parametrize(
    "reported",
    [
        {"bench_mode": True, "verify_gate_disabled": False},
        {"bench_mode": False, "verify_gate_disabled": True},
    ],
    ids=["bench-flag-only", "disable-flag-only"],
)
def test_a_half_set_orchestrator_environment_is_refused_for_the_gateless_pair(
    reported: dict[str, Any],
):
    """A half-set environment is the silent-gating case this exists to catch.

    ``verify_gate_disabled()`` refuses either flag alone, so an operator who set
    one gets a GATED orchestrator wearing condition C's label.  Reporting the
    two booleans separately is what makes that state nameable.
    """
    with pytest.raises(OrchestratorBenchModeError):
        assert_uniform_bench_mode(["A", "C"], reported=reported)


@pytest.mark.unit
@pytest.mark.parametrize(
    "reported",
    [
        {},
        {"bench_mode": True},
        {"verify_gate_disabled": True},
        {"bench_mode": None, "verify_gate_disabled": None},
        {"bench_mode": "1", "verify_gate_disabled": "1"},
        {"bench_mode": 1, "verify_gate_disabled": 1},
    ],
    ids=[
        "no-fields-at-all",
        "only-bench-mode",
        "only-verify-gate-disabled",
        "both-null",
        "strings-not-booleans",
        "ints-not-booleans",
    ],
)
def test_an_orchestrator_that_does_not_report_booleans_is_refused(
    reported: dict[str, Any],
):
    """An orchestrator too old to report its mode must REFUSE, never pass.

    A missing key read with ``.get`` is ``None``, and ``None`` is falsy: a
    comparison written the obvious way would silently classify an unknown mode
    as "gate is on" and wave condition B straight through.  Non-boolean values
    are refused for the same reason ``core/bench_mode`` refuses a truthiness
    check: ``"0"`` and ``0`` are not the same thing to ``bool()``.
    """
    with pytest.raises(OrchestratorBenchModeError):
        assert_uniform_bench_mode(["A", "C"], reported=reported)


@pytest.mark.unit
def test_the_orchestrator_mode_refusal_names_both_flags_and_the_restart():
    """The operator cannot act on the refusal without the exact instruction."""
    with pytest.raises(OrchestratorBenchModeError) as excinfo:
        assert_uniform_bench_mode(["A", "C"], reported=GATED_ORCHESTRATOR)
    message = str(excinfo.value)
    assert "PRAXIS_BENCH=1" in message
    assert "PRAXIS_BENCH_DISABLE_VERIFY=1" in message
    assert "restart" in message.lower()


@pytest.mark.unit
def test_a_gated_invocation_refusal_says_to_start_with_neither_flag():
    """The mirror instruction: B and D need the flags UNSET, not set."""
    with pytest.raises(OrchestratorBenchModeError) as excinfo:
        assert_uniform_bench_mode(["B"], reported=GATELESS_ORCHESTRATOR)
    message = str(excinfo.value)
    assert "PRAXIS_BENCH" in message
    assert "PRAXIS_BENCH_DISABLE_VERIFY" in message
    assert "restart" in message.lower()


@pytest.mark.unit
@pytest.mark.parametrize(
    "keys", [["A", "B"], ["B", "C", "A"]], ids=["A-and-B", "B-C-and-A"]
)
def test_a_mixed_invocation_is_still_refused_as_mixed(keys: list[str]):
    """The self-contradiction check must win: there is no mode that fits both."""
    with pytest.raises(MixedBenchModeError):
        assert_uniform_bench_mode(keys, reported=GATELESS_ORCHESTRATOR)


@pytest.mark.unit
@pytest.mark.parametrize("keys", [["A"], ["B"], ["A", "C"], ["B", "D"], []])
def test_omitting_the_reported_mode_keeps_the_old_behavior(keys: list[str]):
    """The existing call sites pass no report and must keep working."""
    assert_uniform_bench_mode(keys)  # must not raise


@pytest.mark.unit
def test_an_empty_condition_set_needs_no_orchestrator_mode():
    assert_uniform_bench_mode([], reported=GATED_ORCHESTRATOR)  # must not raise


# --------------------------------------------------------------------------
# Ordering: the refusal happens BEFORE anything is registered or dispatched
# --------------------------------------------------------------------------


@pytest.mark.unit
async def test_a_mismatched_orchestrator_spawns_nothing_at_all(
    tmp_path: Path, monkeypatch
):
    """The whole point: refuse before the run mutates anything.

    A check that ran after the first attempt would leave a project registered, a
    container spawned and one row already on disk under a label describing an
    arm that never ran.  Asserting the recorded call list is empty is what makes
    "spawns nothing" a fact rather than a reading of the source.
    """
    client = ModeReportingClient(GATED_ORCHESTRATOR)
    monkeypatch.setattr("bench.runner.BenchClient", _always(client))

    with pytest.raises(OrchestratorBenchModeError):
        await _main_async(_args(tmp_path, "A,C"))

    assert _spawning(client) == []
    assert not (tmp_path / "attempts.jsonl").exists()


@pytest.mark.unit
async def test_a_matching_orchestrator_runs_the_whole_matrix(
    tmp_path: Path, monkeypatch
):
    """The counterweight, without which the refusal test above is vacuous.

    If this harness could never spawn anything, ``_spawning(client) == []``
    would pass for the wrong reason.  Same client, same arguments, only the
    reported mode differs, and here every call must happen.
    """
    client = ModeReportingClient(GATELESS_ORCHESTRATOR)
    monkeypatch.setattr("bench.runner.BenchClient", _always(client))

    result = await _main_async(_args(tmp_path, "A,C"))

    assert result == 0
    spawned = _spawning(client)
    assert "register_project" in spawned
    assert "dispatch" in spawned
    assert "execute_plan" in spawned
    assert (tmp_path / "attempts.jsonl").exists()


@pytest.mark.unit
async def test_the_mode_probe_precedes_every_spawning_call(tmp_path: Path, monkeypatch):
    """Ordering asserted positionally, not just by absence.

    On the HAPPY path nothing is refused, so an assertion counting calls cannot
    see a check that moved after the first spawn.  The probe's index must be
    lower than every spawning call's index.
    """
    client = ModeReportingClient(GATELESS_ORCHESTRATOR)
    monkeypatch.setattr("bench.runner.BenchClient", _always(client))

    await _main_async(_args(tmp_path, "A,C"))

    names = [name for name, _ in client.calls]
    assert "orchestrator_bench_mode" in names
    probe_at = names.index("orchestrator_bench_mode")
    spawn_indexes = [i for i, name in enumerate(names) if name in SPAWNING_CALLS]
    assert spawn_indexes, names
    assert probe_at < min(spawn_indexes), names


@pytest.mark.unit
def test_the_refusal_never_returns_a_success_code(tmp_path: Path, monkeypatch):
    """``main`` must not hand ``sys.exit`` a zero for a run that never ran.

    The refusal propagates out of ``main`` exactly as the other refusals in this
    module do, so ``sys.exit(main())`` is never reached and the process exits
    non-zero with the message on stderr.
    """
    client = ModeReportingClient(GATED_ORCHESTRATOR)
    monkeypatch.setattr("bench.runner.BenchClient", _always(client))
    sample = _write_sample(tmp_path)

    with pytest.raises(OrchestratorBenchModeError):
        main(
            [
                "--sample",
                str(sample),
                "--conditions",
                "A,C",
                "--verify-cmd",
                "pytest -q",
                "--repos",
                str(tmp_path),
                "--out",
                str(tmp_path / "attempts.jsonl"),
                "--api",
                "http://bench.invalid",
            ]
        )

    assert _spawning(client) == []


# --------------------------------------------------------------------------
# The wire: where the reported mode actually comes from
# --------------------------------------------------------------------------


def _status_client(body: dict[str, Any], seen: list[str]) -> BenchClient:
    """A BenchClient whose transport answers /api/status with ``body``."""

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(200, json=body)

    client = BenchClient(base_url="http://bench.invalid", token="tok")
    client._client = httpx.AsyncClient(  # noqa: SLF001 - swapping the transport
        base_url="http://bench.invalid",
        headers={"Authorization": "Bearer tok"},
        transport=httpx.MockTransport(handler),
    )
    return client


@pytest.mark.unit
async def test_the_client_reads_the_two_booleans_from_the_status_endpoint():
    seen: list[str] = []
    client = _status_client(
        {"bench_mode": True, "verify_gate_disabled": True, "opus_state": {}}, seen
    )
    try:
        reported = await client.orchestrator_bench_mode()
    finally:
        await client.close()

    assert seen == ["/api/status"]
    assert reported["bench_mode"] is True
    assert reported["verify_gate_disabled"] is True
    # And the gateless pair is satisfied by exactly this report.
    assert_uniform_bench_mode(["A", "C"], reported=reported)  # must not raise


@pytest.mark.unit
async def test_an_orchestrator_without_the_fields_is_refused_end_to_end():
    """The upgrade case, from the wire through to the refusal.

    An orchestrator predating this endpoint answers 200 with no such keys. That
    must reach the check as something it refuses, not as a pair of falsy values
    that happen to look like a gated orchestrator.
    """
    seen: list[str] = []
    client = _status_client({"opus_state": {}, "active_agents": 0}, seen)
    try:
        reported = await client.orchestrator_bench_mode()
    finally:
        await client.close()

    with pytest.raises(OrchestratorBenchModeError):
        assert_uniform_bench_mode(["A", "C"], reported=reported)
    with pytest.raises(OrchestratorBenchModeError):
        assert_uniform_bench_mode(["B"], reported=reported)


# --------------------------------------------------------------------------
# The protocol document must not still say this step is unverified
# --------------------------------------------------------------------------


BENCH_README = Path(__file__).resolve().parents[2] / "bench" / "README.md"


def _readme_prose() -> str:
    """bench/README.md as one line of prose, with markdown emphasis removed.

    The file hard-wraps, so a sentence spans lines and a naive substring test
    would miss it wherever the wrap happens to fall.  Collapsing whitespace and
    dropping ``*`` and backticks lets whole SENTENCES be asserted, which a bare
    keyword check cannot do: "bench mode" appears either way, before and after
    the correction, so it proves nothing.
    """
    text = BENCH_README.read_text(encoding="utf-8")
    return " ".join(text.replace("*", "").replace("`", "").split())


@pytest.mark.unit
@pytest.mark.parametrize(
    "stale_sentence",
    [
        (
            "There is no API exposing the orchestrator's bench mode, so the "
            "runner cannot check that the restart actually happened."
        ),
        (
            "This is the one manual step in the protocol, and it is unverified "
            "by design rather than by oversight; adding an endpoint for it is "
            "out of scope here."
        ),
        "The restart requirement, which nothing verifies for you",
        "a half-set environment yields a silently gated condition C",
    ],
    ids=[
        "no-such-api",
        "unverified-by-design",
        "nothing-verifies-heading",
        "silently-gated",
    ],
)
def test_the_bench_readme_no_longer_claims_the_restart_is_unverified(
    stale_sentence: str,
):
    """Each of these was TRUE before the endpoint existed and is false now.

    Asserted as absence of the exact stale sentence rather than presence of a
    keyword, because a document keeps reading fluently while contradicting the
    code, and a reader who believes it will skip the check that now exists.
    """
    assert stale_sentence not in _readme_prose()


@pytest.mark.unit
@pytest.mark.parametrize(
    "current_sentence",
    [
        (
            "GET /api/status exposes the orchestrator's bench mode, so the "
            "runner does check that the restart actually happened."
        ),
        (
            "The comparison runs before the first project is registered, so a "
            "mismatch aborts with a non-zero exit having spawned nothing."
        ),
        ("Performing the restart is still the operator's job; confirming it is not."),
    ],
    ids=["endpoint-exists", "refused-before-any-spawn", "who-does-what"],
)
def test_the_bench_readme_states_what_is_now_checked(current_sentence: str):
    """The replacement has to say the three things an operator acts on."""
    assert current_sentence in _readme_prose()


@pytest.mark.unit
@pytest.mark.parametrize("field", ["bench_mode", "verify_gate_disabled"])
def test_the_bench_readme_names_both_reported_fields(field: str):
    """Naming them is what lets an operator check by hand with curl."""
    assert field in _readme_prose()
