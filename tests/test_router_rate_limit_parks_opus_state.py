"""A rate limit surfaced through the ROUTER must reach the state machine.

``opus_state`` is the row that makes a subscription throttle a self-healing
wait instead of a failure: ``plan_and_activate`` reads it, queues, and returns
without spending an attempt.  It was written in exactly one place --
``OpusBridge._check_and_handle_rate_limit``, reachable only from the legacy
``_run_claude`` -- and on a stock install ``main.py`` always wires the router,
so ``plan_spec`` never went near it.  The queue-and-resume branch therefore
could not fire on the only path production takes: dead code guarding a
five-hour wait.

Every test here drives the REAL ``LLMRouter`` with only the subprocess (or the
local endpoint's HTTP call) faked, because the defect lived precisely in the
seam between the router and the bridge.  A double standing in for either one
would have hidden it -- an earlier version of the canary in
``test_planner_failure_is_terminal.py`` replaced ``_execute_one`` wholesale and
proved nothing about the code that actually runs.

Every state assertion is a TRANSITION observed in one body, never a steady
state, because 'available' is also where the row starts: a test that ends on
'available' cannot on its own tell "correctly declined to park" from "parking
is dead everywhere".  Tests that assert a throttle DID park show 'available'
before and 'rate_limited' after; tests that assert something did NOT park end
with ``_assert_parking_still_works``, which drives a known throttle through the
same machinery and would go red if the arm under test had simply been deleted.
That control is not decoration -- an adversarial review of the first version
found four negative tests here still passing with the parking arm of
``_run_routed`` removed outright.
"""

# ruff: noqa: S101

from __future__ import annotations

import json
import sys
from pathlib import Path
from shutil import which as _real_which
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from orchestrator.core.llm_router import (
    LLMRouter,
    ProviderAuthError,
    ProviderOutputError,
    ProviderRateLimitError,
)
from orchestrator.core.opus_bridge import OpusBridge
from orchestrator.core.orchestrator import Orchestrator
from orchestrator.core.provider_errors import is_unavailability
from orchestrator.core.task_queue import TaskQueue
from orchestrator.database import Database
from orchestrator.models.schemas import CapabilityProfile, PlanStatus, TaskStatus


#: What a throttled Claude subscription actually says.  Reproduced from the
#: field report; the wording is only half the rule, the non-zero exit is the
#: other half (see ``opus_bridge.is_rate_limited``).
_THROTTLE_TEXT = "Claude usage limit reached. Your limit will reset at 3pm."

_VALID_PLAN: dict[str, Any] = {
    "plan_summary": "Auth",
    "plan_slug": "auth",
    "tasks": [
        {
            "title": "Login",
            "slug": "login",
            "description": "Build login",
            "depends_on": [],
        }
    ],
}


@pytest.fixture(autouse=True)
def _stub_which(mocker: Any) -> None:
    """The router resolves argv[0] through ``shutil.which`` before spawning."""
    mocker.patch(
        "orchestrator.core.llm_router.shutil.which", side_effect=lambda name: name
    )


@pytest.fixture(autouse=True)
def _planner_workspace_in_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep planner clones out of the developer's own ``data/`` directory."""
    monkeypatch.setattr(
        "orchestrator.core.orchestrator._planner_workspace_base",
        lambda: tmp_path / "planner-workspaces",
    )


@pytest.fixture(autouse=True)
def _no_clone(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the planner's repository clone; none of these tests need one."""

    def _fake_clone(repo_url: str, dest: str, token: str, depth: int = 50) -> None:  # noqa: ARG001
        return None

    monkeypatch.setattr("orchestrator.core.orchestrator.clone_with_token", _fake_clone)


def _cli_proc(mocker: Any, *, stdout: bytes, stderr: bytes, returncode: int) -> Any:
    proc = mocker.AsyncMock()
    proc.communicate = mocker.AsyncMock(return_value=(stdout, stderr))
    proc.returncode = returncode
    return proc


def _router_over(mocker: Any, proc: Any, provider: str = "claude") -> LLMRouter:
    """A REAL router whose only fake is the process the provider CLI spawns."""
    mocker.patch(
        "asyncio.create_subprocess_exec", new=mocker.AsyncMock(return_value=proc)
    )

    async def _chain(call_site: str, project_id: str | None) -> list[dict]:  # noqa: ARG001
        return [{"provider": provider, "model": "claude-sonnet-4-6", "effort": None}]

    return LLMRouter(_chain)


async def _status(db: Database) -> str:
    row = await db.fetch_one("SELECT status FROM opus_state WHERE id = 1")
    assert row is not None
    return str(row["status"])


async def _queued(db: Database) -> list[dict[str, Any]]:
    row = await db.fetch_one("SELECT queued_actions FROM opus_state WHERE id = 1")
    assert row is not None
    return list(json.loads(row["queued_actions"]))


async def _assert_parking_still_works(db: Database, mocker: Any) -> None:
    """Positive control: prove, in THIS body, that parking is not simply broken.

    Mandatory in every test whose verdict is "the state stayed 'available'".
    That assertion cannot on its own tell "correctly declined to park" from
    "parking is dead everywhere", because 'available' is also where the row
    starts -- and an adversarial review found four tests here passing with the
    parking arm of ``_run_routed`` deleted outright.  Running a known throttle
    through the same machinery afterwards makes the difference observable.

    Deliberately the LAST step of a test rather than the first: it leaves the
    row on 'rate_limited', so a negative assertion accidentally ordered after
    it would fail loudly instead of reading a stale green.

    Args:
        db: The same database the negative half asserted against.
        mocker: The test's mocker, used to re-point the subprocess fake.
    """
    proc = _cli_proc(mocker, stdout=_THROTTLE_TEXT.encode(), stderr=b"", returncode=1)
    bridge = OpusBridge(db, router=_router_over(mocker, proc))

    with pytest.raises(ProviderRateLimitError):
        await bridge.plan_spec("spec", "https://r")

    assert await _status(db) == "rate_limited", (
        "the positive control did not park: this test's negative assertion "
        "proves nothing, because parking is broken for every provider"
    )


async def _project_and_plan(db: Database) -> tuple[TaskQueue, str, dict[str, Any]]:
    await db.execute(
        "INSERT INTO users (id, name, token_hash) VALUES (?, ?, ?)",
        ("u1", "User", "hash"),
    )
    await db.execute(
        """INSERT INTO projects (id, user_id, name, repo_url, model_name)
           VALUES (?, ?, ?, ?, ?)""",
        ("p1", "u1", "App", "https://github.com/u/a", "deepseek"),
    )
    task_queue = TaskQueue(db)
    plan_id = await task_queue.create_plan(
        "p1", "Build auth", spec_path="docs/superpowers/specs/auth.md"
    )
    project = await db.fetch_one("SELECT * FROM projects WHERE id = 'p1'")
    assert project is not None
    return task_queue, plan_id, project


def _orchestrator(task_queue: TaskQueue, opus: Any) -> Orchestrator:
    spec_reader = AsyncMock()
    spec_reader.read_doc.return_value = "Build auth"
    return Orchestrator(
        task_queue=task_queue,
        agent_manager=MagicMock(),
        opus_bridge=opus,
        git_ops=AsyncMock(),
        event_bus=MagicMock(),
        spec_reader=spec_reader,
    )


# --------------------------------------------------------------------------
# The router parks.
# --------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.parametrize("stream", ["stdout", "stderr"])
async def test_a_throttle_through_the_real_router_parks_opus_state(
    db: Database, mocker: Any, stream: str
) -> None:
    """The whole defect, in one assertion pair.

    Delete the ``ProviderRateLimitError`` arm from ``_run_routed`` and the row
    stays 'available', which is where it started -- the exact silence this
    file exists to remove.

    Parametrized over the stream deliberately: ``claude`` prints its usage
    message to STDOUT, and the router's own ``RuntimeError`` carries only
    stderr.  A fix that classified the raised exception's TEXT would pass the
    stderr case and silently miss every real throttle.
    """
    payload = _THROTTLE_TEXT.encode()
    proc = _cli_proc(
        mocker,
        stdout=payload if stream == "stdout" else b"",
        stderr=payload if stream == "stderr" else b"",
        returncode=1,
    )
    bridge = OpusBridge(db, router=_router_over(mocker, proc))

    assert await _status(db) == "available"
    with pytest.raises(ProviderRateLimitError):
        await bridge.plan_spec("spec", "https://github.com/u/a")

    assert await _status(db) == "rate_limited"
    row = await db.fetch_one(
        "SELECT rate_limited_at, resume_at FROM opus_state WHERE id = 1"
    )
    assert row is not None
    assert row["rate_limited_at"]
    assert row["resume_at"]


@pytest.mark.integration
async def test_the_operator_facing_message_quotes_the_stream_that_carries_it(
    db: Database, mocker: Any
) -> None:
    """A commit whose thesis is "the evidence was unquotable" must not drop it.

    This message reaches ``plans.error`` verbatim through
    ``_unavailable_reason``, and it is the only place a human learns WHY the
    plan is waiting.  On a host with a ``~/.claude`` hook, stderr carries an
    unrelated warning while the throttle is on stdout, so "stderr, else stdout"
    -- which reads perfectly plausible -- names the warning and discards the
    evidence.  Detection is by TYPE and is unaffected either way, so nothing
    but this assertion would notice.
    """
    proc = _cli_proc(
        mocker,
        stdout=_THROTTLE_TEXT.encode(),
        stderr=b"warning: VPN killswitch bypassed for this session",
        returncode=1,
    )
    bridge = OpusBridge(db, router=_router_over(mocker, proc))

    with pytest.raises(ProviderRateLimitError) as caught:
        await bridge.plan_spec("spec", "https://r")

    assert _THROTTLE_TEXT in str(caught.value)


@pytest.mark.integration
@pytest.mark.parametrize(
    "call",
    [
        "plan_spec",
        "review_diff",
        "answer_clarification",
        "analyze_improvements",
        "classify_doc",
    ],
)
async def test_every_routed_brain_call_site_parks(
    db: Database, mocker: Any, call: str
) -> None:
    """One fixed call-site is a fix nobody can rely on.

    Every seat that routes through ``LLMRouter`` reads and is gated by the same
    single ``opus_state`` row, so a throttle at any of them is the same
    five-hour wait.  Move the parking into ``plan_spec`` alone and four of these
    five go red.
    """
    proc = _cli_proc(mocker, stdout=_THROTTLE_TEXT.encode(), stderr=b"", returncode=1)
    bridge = OpusBridge(db, router=_router_over(mocker, proc))
    invocations = {
        "plan_spec": lambda: bridge.plan_spec("spec", "https://r"),
        "review_diff": lambda: bridge.review_diff("diff", "task"),
        "answer_clarification": lambda: bridge.answer_clarification("q?", "task"),
        "analyze_improvements": lambda: bridge.analyze_improvements("summary"),
        "classify_doc": lambda: bridge.classify_doc("# doc"),
    }

    assert await _status(db) == "available"
    with pytest.raises(ProviderRateLimitError):
        await invocations[call]()

    assert await _status(db) == "rate_limited"


@pytest.mark.integration
async def test_an_ordinary_provider_failure_does_not_park(
    db: Database, mocker: Any
) -> None:
    """A broken planner must stay broken and visible, not queue for five hours.

    Parking on any non-zero exit would convert every real fault into a silent
    wait.  Widen the predicate to "the provider failed" and this goes red.
    """
    proc = _cli_proc(mocker, stdout=b"", stderr=b"Blocked by policy", returncode=1)
    bridge = OpusBridge(db, router=_router_over(mocker, proc))

    with pytest.raises(RuntimeError) as caught:
        await bridge.plan_spec("spec", "https://r")

    assert not isinstance(caught.value, ProviderRateLimitError)
    assert await _status(db) == "available"

    await _assert_parking_still_works(db, mocker)


@pytest.mark.integration
async def test_a_successful_answer_that_discusses_limits_is_not_a_throttle(
    db: Database, mocker: Any
) -> None:
    """The planner's own words are not evidence about the subscription.

    A spec about rate limiting decomposes into a plan whose text says "rate
    limit".  Read that as a throttle and submitting such a spec parks the brain
    for five hours.  Detect on the response text rather than on a FAILED call
    and this goes red.
    """
    answer = json.dumps(
        {
            "plan_summary": "Add rate limit handling",
            "plan_slug": "rate-limit",
            "tasks": [
                {
                    "title": "Handle the usage limit",
                    "slug": "usage-limit",
                    "description": "Too many requests must back off",
                    "depends_on": [],
                }
            ],
        }
    )
    proc = _cli_proc(mocker, stdout=answer.encode(), stderr=b"", returncode=0)
    bridge = OpusBridge(db, router=_router_over(mocker, proc))

    graph = await bridge.plan_spec("spec", "https://r")

    assert graph["plan_slug"] == "rate-limit"
    assert await _status(db) == "available"

    await _assert_parking_still_works(db, mocker)


@pytest.mark.integration
@pytest.mark.parametrize("failure", ["unreachable", "http_429"])
async def test_a_local_endpoint_failure_never_parks_the_global_state(
    db: Database, mocker: Any, failure: str
) -> None:
    """An LM Studio outage is not a subscription that resets in five hours.

    ``opus_state`` models one thing: a throttle that clears on its own.  A dead
    local endpoint clears when somebody starts it, and parking on it would gate
    every OTHER provider behind a wait nothing will end.

    The positive control in the same body is load-bearing: without it this test
    passes just as well when parking is broken everywhere, because 'available'
    is also the state the row starts in.
    """
    url = "http://lm:1234/v1/chat/completions"
    if failure == "unreachable":
        boom: Any = httpx.ConnectError("connection refused")
        mocker.patch("httpx.AsyncClient.post", side_effect=boom)
    else:
        mocker.patch(
            "httpx.AsyncClient.post",
            new=mocker.AsyncMock(
                return_value=httpx.Response(
                    429,
                    request=httpx.Request("POST", url),
                    text="Too Many Requests",
                )
            ),
        )

    async def _local_chain(call_site: str, project_id: str | None) -> list[dict]:  # noqa: ARG001
        return [{"provider": "local", "model": "qwen", "effort": None}]

    local_bridge = OpusBridge(
        db, router=LLMRouter(_local_chain, lm_studio_url="http://lm:1234")
    )

    assert await _status(db) == "available"
    with pytest.raises((httpx.ConnectError, httpx.HTTPStatusError)):
        await local_bridge.plan_spec("spec", "https://r")
    assert await _status(db) == "available"

    await _assert_parking_still_works(db, mocker)


@pytest.mark.integration
async def test_an_auth_failure_does_not_park(db: Database, mocker: Any) -> None:
    """A dead session needs a human to log in; waiting it out never ends it."""
    proc = _cli_proc(
        mocker,
        stdout=b"",
        stderr=b"ERROR: refresh token was revoked. Please log out and sign in.",
        returncode=0,
    )
    bridge = OpusBridge(db, router=_router_over(mocker, proc, provider="codex"))

    with pytest.raises(ProviderAuthError):
        await bridge.plan_spec("spec", "https://r")

    assert await _status(db) == "available"

    await _assert_parking_still_works(db, mocker)


@pytest.mark.integration
async def test_an_empty_answer_does_not_park(db: Database, mocker: Any) -> None:
    """``agy --print`` yields nothing non-interactively; that is not a throttle."""
    proc = _cli_proc(mocker, stdout=b"   ", stderr=b"", returncode=0)
    bridge = OpusBridge(db, router=_router_over(mocker, proc, provider="agy"))

    with pytest.raises(ProviderOutputError):
        await bridge.plan_spec("spec", "https://r")

    assert await _status(db) == "available"

    await _assert_parking_still_works(db, mocker)


@pytest.mark.integration
async def test_the_shipped_two_entry_role_chain_parks_after_exhausting_it(
    db: Database, mocker: Any
) -> None:
    """The shape a stock install actually resolves, which one entry does not test.

    ``config/praxis.yaml`` ships ``plan: [sonnet, opus]`` and a YAML role chain
    SHADOWS ``CALL_SITE_DEFAULTS`` entirely, so the planner resolves to TWO
    claude entries. One subscription throttles both.

    The chain interacts with this fix in a way a single-entry test cannot see:
    ``is_unavailability`` now answers True for ``ProviderRateLimitError``, so
    ``LLMRouter.run`` FALLS THROUGH to the next entry instead of raising at the
    first. A throttle therefore costs two CLI invocations on the first tick,
    and the state must be parked by the time the last one gives up -- and then
    the queue branch must stop the second tick spending any at all.
    """
    proc = _cli_proc(mocker, stdout=_THROTTLE_TEXT.encode(), stderr=b"", returncode=1)
    spawn = mocker.patch(
        "asyncio.create_subprocess_exec", new=mocker.AsyncMock(return_value=proc)
    )

    async def _shipped_plan_chain(call_site: str, project_id: str | None) -> list[dict]:  # noqa: ARG001
        return [
            {"provider": "claude", "model": "claude-sonnet-4-6", "effort": None},
            {"provider": "claude", "model": "claude-opus-4-8", "effort": "high"},
        ]

    task_queue, plan_id, project = await _project_and_plan(db)
    bridge = OpusBridge(db, router=LLMRouter(_shipped_plan_chain))
    orchestrator = _orchestrator(task_queue, bridge)

    assert await _status(db) == "available"
    await orchestrator.plan_and_activate(plan_id, project)

    assert spawn.await_count == 2, "the whole chain must be tried before giving up"
    assert await _status(db) == "rate_limited"

    await orchestrator.plan_and_activate(plan_id, project)

    # Tick two spends nothing: the queue branch fired.
    assert spawn.await_count == 2
    plan = await task_queue.get_plan(plan_id)
    assert plan is not None
    assert plan["status"] == PlanStatus.PENDING
    assert plan["plan_attempts"] == 0


@pytest.mark.integration
async def test_a_real_throttled_process_parks_the_state(
    db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one mocked line in this file, removed.

    Every other test here fakes ``asyncio.create_subprocess_exec``, and that
    single line is the one that could hide a TOTAL failure of this fix: a
    ``MagicMock`` answers ``.returncode`` and ``.communicate()`` in whatever
    shape the test asks for, so detection could be reading fields a real
    ``asyncio.subprocess.Process`` never produces and every assertion above
    would still be green.

    Here the subprocess is real: a real interpreter, spawned through the real
    ``create_subprocess_exec``, resolved through the real ``shutil.which``,
    fed the real prompt on the real stdin pipe, exiting 1 with the throttle
    text on the real stdout pipe. Only ``build_argv`` is stood in for, and only
    to name a binary that exists on every machine -- it is argv construction,
    not the seam under test.
    """
    script = tmp_path / "throttled_cli.py"
    script.write_text(
        "import sys\n"
        "sys.stdin.buffer.read()\n"
        "sys.stdout.write('Claude usage limit reached. Resets at 3pm.')\n"
        "sys.exit(1)\n",
        encoding="utf-8",
    )
    # Undo the autouse stub: this test resolves the binary for real.
    monkeypatch.setattr(
        "orchestrator.core.llm_router.shutil.which", _real_which, raising=False
    )
    monkeypatch.setattr(
        "orchestrator.core.llm_router.build_argv",
        lambda provider, model, effort, prompt="": [sys.executable, str(script)],  # noqa: ARG005
    )

    async def _chain(call_site: str, project_id: str | None) -> list[dict]:  # noqa: ARG001
        return [{"provider": "claude", "model": "", "effort": None}]

    bridge = OpusBridge(db, router=LLMRouter(_chain))

    assert await _status(db) == "available"
    with pytest.raises(ProviderRateLimitError):
        await bridge.plan_spec("spec", "https://r")

    assert await _status(db) == "rate_limited"


# --------------------------------------------------------------------------
# The legacy path is untouched, and both paths share ONE writer.
# --------------------------------------------------------------------------


@pytest.mark.integration
async def test_the_legacy_cli_path_still_parks_exactly_as_before(
    db: Database, mocker: Any
) -> None:
    """``_run_claude`` keeps its rate-limit handling, through the same writer.

    Documented behaviour (CLAUDE.md: "legacy ``_run_claude`` fallback keeps
    rate-limit handling"), so extracting the parking half must not move it.
    """
    proc = _cli_proc(mocker, stdout=_THROTTLE_TEXT.encode(), stderr=b"", returncode=1)
    mocker.patch(
        "asyncio.create_subprocess_exec", new=mocker.AsyncMock(return_value=proc)
    )
    bridge = OpusBridge(db)  # no router: the legacy arm

    assert await _status(db) == "available"
    with pytest.raises(RuntimeError):
        await bridge.plan_spec("spec", "https://r")

    assert await _status(db) == "rate_limited"


@pytest.mark.unit
async def test_the_two_paths_write_through_one_helper(
    db: Database, mocker: Any
) -> None:
    """Neuter the single writer and BOTH paths go quiet.

    This is what stops the fix being "a third path that also writes": if the
    router path could park without ``_park_rate_limited``, the legacy half of
    this assertion would still pass.
    """
    parked = mocker.patch.object(
        OpusBridge, "_park_rate_limited", new=AsyncMock(return_value=None)
    )
    proc = _cli_proc(mocker, stdout=_THROTTLE_TEXT.encode(), stderr=b"", returncode=1)

    router_bridge = OpusBridge(db, router=_router_over(mocker, proc))
    with pytest.raises(ProviderRateLimitError):
        await router_bridge.plan_spec("spec", "https://r")
    legacy_bridge = OpusBridge(db)
    with pytest.raises(RuntimeError):
        await legacy_bridge.plan_spec("spec", "https://r")

    assert parked.await_count == 2
    assert await _status(db) == "available"


@pytest.mark.unit
def test_a_rate_limit_is_an_unavailability_by_type_not_by_wording() -> None:
    """``_planned_graph_or_reported`` must not charge an attempt for a throttle.

    The evidence may be a stdout-only message the exception never quotes, so
    ``is_unavailability`` has to recognise the class itself.  Delete the
    ``ProviderRateLimitError`` branch and this goes red on a message carrying
    no limit wording at all.
    """
    wordless = ProviderRateLimitError("claude", "claude failed (exit 1)")

    assert is_unavailability(wordless) is True


# --------------------------------------------------------------------------
# What parking actually buys: the queue-and-resume branch.
# --------------------------------------------------------------------------


@pytest.mark.integration
async def test_once_parked_the_planner_is_never_called_and_the_action_is_queued(
    db: Database,
) -> None:
    """The branch the whole state row exists to serve.

    Before parking worked this could not fire at all on the router path.
    Assert the planner is not called AND the ledger records it: either alone
    passes for the wrong reason (a planner that is never called because the
    plan vanished, a ledger entry written by a tick that also planned).
    """
    task_queue, plan_id, project = await _project_and_plan(db)
    await db.execute(
        "UPDATE opus_state SET status = 'rate_limited', resume_at = ? WHERE id = 1",
        ("2999-01-01T00:00:00+00:00",),
    )
    bridge = OpusBridge(db)
    bridge.plan_spec = AsyncMock(  # type: ignore[method-assign]
        side_effect=AssertionError("the planner was called while parked")
    )

    await _orchestrator(task_queue, bridge).plan_and_activate(plan_id, project)

    assert await _queued(db) == [
        {"action": "plan", "plan_id": plan_id, "project_id": "p1"}
    ]
    plan = await task_queue.get_plan(plan_id)
    assert plan is not None
    assert plan["status"] == PlanStatus.PENDING
    assert plan["plan_attempts"] == 0


@pytest.mark.integration
async def test_the_ledger_records_one_entry_per_action_not_one_per_tick(
    db: Database,
) -> None:
    """A five-hour throttle at the shipped 5s interval is 3600 ticks.

    Before parking worked the queue branch never fired, so nothing ever grew.
    Now it fires on every pass, and a plain append would rewrite an
    ever-growing JSON blob 3600 times per plan and report a nonsense
    ``queued_count`` on ``/api/status``.  Drop the de-duplication in
    ``queue_action`` and this goes red on the second tick.
    """
    task_queue, plan_id, project = await _project_and_plan(db)
    await db.execute(
        "UPDATE opus_state SET status = 'rate_limited', resume_at = ? WHERE id = 1",
        ("2999-01-01T00:00:00+00:00",),
    )
    orchestrator = _orchestrator(task_queue, OpusBridge(db))

    for _tick in range(5):
        await orchestrator.plan_and_activate(plan_id, project)

    assert len(await _queued(db)) == 1

    # A DIFFERENT action is still recorded: de-duplication must not become
    # "only ever remember one thing".
    await OpusBridge(db).queue_action({"action": "review", "task_id": "t9"})
    assert len(await _queued(db)) == 2


@pytest.mark.integration
async def test_nothing_replays_a_queued_action_the_pending_plan_row_does(
    db: Database, mocker: Any
) -> None:
    """The honest shape of "resume", pinned so nobody trusts the queue.

    ``get_queued_actions`` has no production caller: the queue is a LEDGER, and
    the replay is the loop finding the plan still PENDING on the next pass.  It
    is pinned here because a queue that looks like a work list and is not one
    is how somebody comes to route real work through it.

    Delete the queue-clearing in ``is_available`` and the second half goes red:
    ``/api/status`` would report a queued action forever after the limit
    cleared.
    """
    # The premise guard, over the WHOLE of `src/` -- not just
    # `src/orchestrator/`, because `src/cli/` and `src/mcp_server/` are equally
    # able to build a drainer, and a pin that cannot see them is the same
    # can't-fail guard this file has already been caught shipping once.
    from orchestrator.core import opus_bridge as bridge_module

    src_root = Path(bridge_module.__file__).parents[2]
    assert src_root.name == "src", src_root
    consumers = sorted(
        str(path.relative_to(src_root))
        for path in src_root.rglob("*.py")
        if path.name != "opus_bridge.py"
        and any(
            name in path.read_text(encoding="utf-8")
            for name in ("get_queued_actions", "clear_queued_actions")
        )
    )
    assert consumers == [], (
        f"something now consumes the queue ({consumers}); this test's premise, "
        "and the ledger-not-a-work-list design it pins, need revisiting"
    )

    task_queue, plan_id, project = await _project_and_plan(db)
    await db.execute(
        "UPDATE opus_state SET status = 'rate_limited', resume_at = ? WHERE id = 1",
        ("2000-01-01T00:00:00+00:00",),  # already elapsed
    )
    await OpusBridge(db).queue_action(
        {"action": "plan", "plan_id": plan_id, "project_id": "p1"}
    )
    bridge = OpusBridge(db)
    bridge.plan_spec = AsyncMock(return_value=_VALID_PLAN)  # type: ignore[method-assign]

    await _orchestrator(task_queue, bridge).plan_and_activate(plan_id, project)

    # Replayed -- by the PLAN ROW, not by anything reading the ledger.
    plan = await task_queue.get_plan(plan_id)
    assert plan is not None
    assert plan["status"] == PlanStatus.ACTIVE
    assert bridge.plan_spec.await_count == 1
    # And the ledger no longer claims work is waiting.
    assert await _queued(db) == []
    assert await _status(db) == "available"


# --------------------------------------------------------------------------
# A throttle anywhere in a fallback chain is not lost.
# --------------------------------------------------------------------------


async def _mixed_chain(call_site: str, project_id: str | None) -> list[dict]:  # noqa: ARG001
    """A subscription CLI backed by a local endpoint.

    Not exotic: ``docs/configurations.md`` presents exactly this arrangement,
    and it is the only shape in which the last entry can fail DIFFERENTLY from
    the first.  On the shipped claude-only chains a throttle hits every entry,
    which is why this gap survived the first fix.
    """
    return [
        {"provider": "claude", "model": "claude-sonnet-4-6", "effort": None},
        {"provider": "local", "model": "qwen", "effort": None},
    ]


@pytest.mark.integration
async def test_a_throttle_at_a_non_final_chain_position_still_parks(
    db: Database, mocker: Any
) -> None:
    """The throttle must survive a LATER entry failing for another reason.

    ``run`` remembered a non-final unavailability in ``last_exc`` and then
    raised whatever the final entry threw, so a throttle at position 0 followed
    by a dead LM Studio at position 1 surfaced as an ``httpx.ConnectError``:
    not a ``ProviderRateLimitError``, so ``_run_routed`` never parked and the
    plan was charged a retry for a subscription limit.

    Both failures reach the operator.  The TYPE is the throttle, because that
    is the actionable one and the one the state machine can wait out; the
    entry that actually ended the attempt is named in the message and chained
    as ``__cause__`` so nothing is silently discarded.
    """
    proc = _cli_proc(mocker, stdout=_THROTTLE_TEXT.encode(), stderr=b"", returncode=1)
    mocker.patch(
        "asyncio.create_subprocess_exec", new=mocker.AsyncMock(return_value=proc)
    )
    mocker.patch(
        "httpx.AsyncClient.post",
        side_effect=httpx.ConnectError("connection refused"),
    )
    bridge = OpusBridge(
        db, router=LLMRouter(_mixed_chain, lm_studio_url="http://lm:1234")
    )

    assert await _status(db) == "available"
    with pytest.raises(ProviderRateLimitError) as caught:
        await bridge.plan_spec("spec", "https://r")

    assert await _status(db) == "rate_limited"
    assert _THROTTLE_TEXT in str(caught.value)
    assert "connection refused" in str(caught.value)
    assert isinstance(caught.value.__cause__, httpx.ConnectError)


@pytest.mark.integration
async def test_an_all_claude_chain_reports_only_the_last_entrys_failure(
    db: Database, mocker: Any
) -> None:
    """The shipped shape must read exactly as it did before.

    ``plan: [sonnet, opus]`` throttles at BOTH entries, so combining them would
    hand the operator the same sentence twice for one subscription limit.  The
    combination is for two DIFFERENT failures, and only for those.
    """
    proc = _cli_proc(mocker, stdout=_THROTTLE_TEXT.encode(), stderr=b"", returncode=1)
    mocker.patch(
        "asyncio.create_subprocess_exec", new=mocker.AsyncMock(return_value=proc)
    )

    async def _shipped_plan_chain(call_site: str, project_id: str | None) -> list[dict]:  # noqa: ARG001
        return [
            {"provider": "claude", "model": "claude-sonnet-4-6", "effort": None},
            {"provider": "claude", "model": "claude-opus-4-8", "effort": "high"},
        ]

    bridge = OpusBridge(db, router=LLMRouter(_shipped_plan_chain))

    with pytest.raises(ProviderRateLimitError) as caught:
        await bridge.plan_spec("spec", "https://r")

    message = str(caught.value)
    assert message.count("rate limited") == 1, message
    assert "then gave up at" not in message
    assert await _status(db) == "rate_limited"


@pytest.mark.integration
async def test_a_chain_that_recovers_after_a_throttle_does_not_park(
    db: Database, mocker: Any
) -> None:
    """A fallback that ANSWERS is the chain doing its job, not an outage.

    Parking here would gate every other seat -- including the ``local`` one
    that just answered -- behind a five-hour wait nothing about this call
    needs.  The throttle is still recorded: ``run`` publishes a
    ``model_fallback`` event naming ``ProviderRateLimitError`` as the reason.

    The positive control is load-bearing: 'available' is where the row starts,
    so without it this passes just as well when parking is dead everywhere.
    """
    proc = _cli_proc(mocker, stdout=_THROTTLE_TEXT.encode(), stderr=b"", returncode=1)
    mocker.patch(
        "asyncio.create_subprocess_exec", new=mocker.AsyncMock(return_value=proc)
    )
    url = "http://lm:1234/v1/chat/completions"
    mocker.patch(
        "httpx.AsyncClient.post",
        new=mocker.AsyncMock(
            return_value=httpx.Response(
                200,
                request=httpx.Request("POST", url),
                json={"choices": [{"message": {"content": json.dumps(_VALID_PLAN)}}]},
            )
        ),
    )
    bridge = OpusBridge(
        db, router=LLMRouter(_mixed_chain, lm_studio_url="http://lm:1234")
    )

    graph = await bridge.plan_spec("spec", "https://r")

    assert graph["plan_slug"] == "auth"
    assert await _status(db) == "available"

    await _assert_parking_still_works(db, mocker)


# --------------------------------------------------------------------------
# The two seats that used to call ``router.run`` directly.
# --------------------------------------------------------------------------


async def _pending_execute_plan(db: Database) -> tuple[TaskQueue, str, dict[str, Any]]:
    """A project whose execute-plan plan is PENDING, awaiting decomposition."""
    task_queue, plan_id, project = await _project_and_plan(db)
    await db.execute(
        "UPDATE plans SET source = 'execute-plan', pending_input = ? WHERE id = ?",
        (
            json.dumps(
                {
                    "plan": "### Task 1\nDo the thing.",
                    "model": "qwen3.8-27b",
                    "context": None,
                    "local_context": None,
                    "branch": "plan/execute-do-the-thing",
                }
            ),
            plan_id,
        ),
    )
    return task_queue, plan_id, project


def _decompose_orchestrator(
    task_queue: TaskQueue, bridge: OpusBridge, router: LLMRouter
) -> Orchestrator:
    """An Orchestrator wired the way ``main.py`` wires one: bridge AND router.

    ``_llm_router`` is the object this seat used to call directly, and it is
    left in place on purpose -- the fix is which of the two the seat reaches
    for, so removing the old one would decide the test's verdict by
    construction.
    """
    settings = AsyncMock()
    settings.capability_profile.return_value = CapabilityProfile(
        model_name="qwen3.8-27b", parameter_count_b=27.0, context_window=32000
    )
    orchestrator = _orchestrator(task_queue, bridge)
    orchestrator._effective_settings = settings
    orchestrator._llm_router = router
    return orchestrator


@pytest.mark.integration
async def test_a_throttle_at_the_decompose_seat_parks_and_stops_re_attempting(
    db: Database, mocker: Any
) -> None:
    """The decompose seat of the flagship feature, on a five-hour throttle.

    ``decompose_plan`` called ``router.run`` directly, so nothing parked, and
    the exception it raised is not a ``PlanReviewError`` -- it escaped
    ``decompose_pending_execute_plan`` into ``run_once``'s per-plan
    quarantine.  The plan stayed PENDING and re-attempted on EVERY tick: about
    3600 throttled ``claude`` spawns over one five-hour window, with `praxis
    status` reporting the brain available throughout.

    The flat spawn count across the later ticks is the assertion that pins
    that, not the state row: parking is only worth anything here because the
    ``is_available`` branch at the top of the seat then short-circuits.
    """
    proc = _cli_proc(mocker, stdout=_THROTTLE_TEXT.encode(), stderr=b"", returncode=1)
    spawn = mocker.patch(
        "asyncio.create_subprocess_exec", new=mocker.AsyncMock(return_value=proc)
    )

    async def _chain(call_site: str, project_id: str | None) -> list[dict]:  # noqa: ARG001
        return [{"provider": "claude", "model": "claude-sonnet-4-6", "effort": None}]

    router = LLMRouter(_chain)
    task_queue, plan_id, project = await _pending_execute_plan(db)
    orchestrator = _decompose_orchestrator(
        task_queue, OpusBridge(db, router=router), router
    )

    assert await _status(db) == "available"
    # Must not raise: an escaping exception is the quarantined-and-retried
    # shape this fix exists to remove.
    await orchestrator.decompose_pending_execute_plan(plan_id, project)

    assert await _status(db) == "rate_limited"
    assert spawn.await_count == 1
    plan = await task_queue.get_plan(plan_id)
    assert plan is not None
    # Waiting, not failed, and no attempt consumed.
    assert plan["status"] == PlanStatus.PENDING
    assert plan["plan_attempts"] == 0
    assert "rate limit" in str(plan["error"]).lower()

    for _tick in range(5):
        await orchestrator.decompose_pending_execute_plan(plan_id, project)

    assert spawn.await_count == 1, (
        "the parked state must stop the seat spending a CLI invocation per "
        "tick; this is the ~3600-spawn defect"
    )
    assert await _queued(db) == [
        {"action": "execute_plan", "plan_id": plan_id, "project_id": "p1"}
    ]


@pytest.mark.integration
async def test_an_ordinary_decompose_failure_neither_parks_nor_defers(
    db: Database, mocker: Any
) -> None:
    """A broken brain at the decompose seat must stay broken and visible.

    The deferral is for the ONE failure that heals itself.  Widen it to any
    provider error and a genuinely broken planner becomes a silent five-hour
    wait -- the same inversion the parking predicate itself is gated against.

    This used to be observed as ``pytest.raises(RuntimeError)``, because at the
    time escaping the method WAS the only alternative to deferring.  That made
    "it raises" and "it is visible" the same fact, and they never were: the
    exception went to ``run_once``'s per-plan quarantine, which logs and moves
    on, leaving the plan PENDING with ``error: null`` to be re-decomposed on
    every tick.  The seat now charges the failure an attempt and writes the
    reason to the row, so the claim is stated against what an operator can
    actually read.
    """
    proc = _cli_proc(mocker, stdout=b"", stderr=b"Blocked by policy", returncode=1)

    async def _chain(call_site: str, project_id: str | None) -> list[dict]:  # noqa: ARG001
        return [{"provider": "claude", "model": "claude-sonnet-4-6", "effort": None}]

    router = _router_over(mocker, proc)
    task_queue, plan_id, project = await _pending_execute_plan(db)
    orchestrator = _decompose_orchestrator(
        task_queue, OpusBridge(db, router=router), router
    )

    await orchestrator.decompose_pending_execute_plan(plan_id, project)

    assert await _status(db) == "available", (
        "an ordinary failure must not park the brain: parking it turns a "
        "broken planner into a silent five-hour wait"
    )
    plan = await task_queue.get_plan(plan_id)
    assert plan is not None
    assert plan["status"] == PlanStatus.PENDING
    # Charged, not deferred. A deferral leaves the count at 0, and a count that
    # never moves is a plan that re-attempts every tick forever.
    assert plan["plan_attempts"] == 1
    assert "Blocked by policy" in str(plan["error"])
    assert "rate limit" not in str(plan["error"]).lower()

    await _assert_parking_still_works(db, mocker)


@pytest.mark.integration
async def test_a_throttle_at_the_triage_seat_parks_and_decides_nothing(
    orchestrator_fixture: tuple[Any, str, dict[str, Any]],
    db: Database,
    mocker: Any,
) -> None:
    """A throttle is not a triage outcome, and ``human`` is irreversible.

    ``triage_leaf`` caught ``Exception`` and answered
    ``TriageDecision(decision="human")``, which ``_run_leaf_triage`` applies by
    failing the task terminally.  A five-hour wait that heals itself was
    therefore converted into a permanent human gate on a leaf that had two
    retries left -- and the only trace was one log line.

    The leaf now stays REVIEWING with no triage decision stamped, so when the
    limit clears the next pass reviews and triages it exactly as it would
    have.  REVIEWING counts as active, so the plan neither completes nor
    reports itself stalled while it waits.
    """
    orchestrator, task_id, project = orchestrator_fixture
    proc = _cli_proc(mocker, stdout=_THROTTLE_TEXT.encode(), stderr=b"", returncode=1)
    router = _router_over(mocker, proc)
    orchestrator._opus = OpusBridge(db, router=router)
    # Left in place deliberately; see _decompose_orchestrator.
    orchestrator._llm_router = router

    await orchestrator._tq.retry_task(task_id)
    await orchestrator._tq.update_task_status(task_id, TaskStatus.REVIEWING)
    task = await orchestrator._tq.get_task(task_id)
    assert task is not None
    plan = await orchestrator._tq.get_plan(task["plan_id"])
    assert plan is not None

    assert await _status(db) == "available"
    handled = await orchestrator._run_leaf_triage(
        dict(task), project, dict(plan), "it failed", 1, 4, "diff"
    )

    assert handled is True, "the caller must not fall through to the retry path"
    assert await _status(db) == "rate_limited"
    after = await orchestrator._tq.get_task(task_id)
    assert after is not None
    assert after["status"] == TaskStatus.REVIEWING
    assert after["triage_decision"] is None, (
        "a throttle must not burn the one triage call this leaf gets"
    )
    assert after["attempt"] == task["attempt"]


@pytest.mark.integration
async def test_an_ordinary_triage_failure_still_falls_back_to_human(
    orchestrator_fixture: tuple[Any, str, dict[str, Any]],
    db: Database,
    mocker: Any,
) -> None:
    """Triage must still never wedge a task on anything else.

    "Never raises" was the module's whole contract, and exactly one exception
    is now carved out of it.  Carve out two and a provider outage stops being
    a human gate and starts being an unhandled exception in the review path.
    """
    orchestrator, task_id, project = orchestrator_fixture
    proc = _cli_proc(mocker, stdout=b"", stderr=b"Blocked by policy", returncode=1)
    router = _router_over(mocker, proc)
    orchestrator._opus = OpusBridge(db, router=router)
    orchestrator._llm_router = router

    await orchestrator._tq.retry_task(task_id)
    await orchestrator._tq.update_task_status(task_id, TaskStatus.REVIEWING)
    task = await orchestrator._tq.get_task(task_id)
    assert task is not None
    plan = await orchestrator._tq.get_plan(task["plan_id"])
    assert plan is not None

    handled = await orchestrator._run_leaf_triage(
        dict(task), project, dict(plan), "it failed", 1, 4, "diff"
    )

    assert handled is True
    after = await orchestrator._tq.get_task(task_id)
    assert after is not None
    assert after["status"] == TaskStatus.FAILED
    assert after["triage_decision"] == "human"
    assert await _status(db) == "available"

    await _assert_parking_still_works(db, mocker)
