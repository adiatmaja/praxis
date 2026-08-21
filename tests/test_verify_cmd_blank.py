"""An all-whitespace ``verify_cmd`` must never report the gate as passed.

The load-bearing invariant: **the verify gate may only report ``passed`` for a
command it actually executed.** ``""`` and ``None`` have always meant "not
configured" and are caught by the falsy guard at every read site. ``"   "`` is
TRUTHY, so it sailed past all of them, reached
``asyncio.create_subprocess_shell``, and a blank shell command exits 0. The
gate then logged "verify gate passed", memoized the wave as verified, and
closed no-change leaves with the evidence string "verify passed on <branch>" --
every one of those a claim about a check that never ran. That is the worst
failure shape in this codebase: not a gate that breaks loudly, but one that
greens work on evidence it never gathered.

Three layers, one per defect surface, each tested here:

1. **Boundary** (``models/schemas.py``) -- the value is refused with a 422
   before it can reach the ``projects`` row at all. ``""``/``None`` keep
   working unchanged: ``None`` is "leave this field alone" on a PATCH
   (``update_project`` dumps with ``exclude_none=True``) and ``""`` is the only
   way to clear a command that is already set.
2. **Runtime** (``core/verify_gate.normalize_verify_cmd`` at the three read
   sites) -- a row already in a live SQLite database can carry ``"   "``, so
   the boundary fix alone leaves existing installs lying. A blank now reads as
   "not configured" and takes the existing ``skipped`` path.
3. **Unreachable-by-construction** (``run_verify`` itself) -- raises rather
   than shelling a blank. It cannot fire in production because all three read
   sites normalize first; it exists so a call site written LATER fails loudly
   instead of silently greening.

Every test below asserts on the STATUS carried forward (``skipped`` rather than
``passed``, an unmemoized wave, the no-changes evidence string) plus the fact
that ``run_verify`` was never reached, never on a log string alone.
"""
# ruff: noqa: S101

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import AsyncClient
from pydantic import ValidationError

import orchestrator.core.orchestrator_review as review_module
from orchestrator.core.event_bus import EventBus
from orchestrator.core.orchestrator import Orchestrator
from orchestrator.core.orchestrator_review import (
    _SKIP_NO_VERIFY_CMD,
    _PlanVerifyResult,
)
from orchestrator.core.verify_gate import normalize_verify_cmd, run_verify
from orchestrator.database import Database
from orchestrator.models.schemas import ProjectCreate, ProjectUpdate
from tests.conftest import seed_user


_REVIEW_LOGGER = "orchestrator.core.orchestrator_review"

# The value under test: non-empty, so every falsy guard in the codebase passes
# it through, and yet `sh -c "   "` exits 0.
_BLANK = "   "


@pytest.fixture(autouse=True)
def _no_bench_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bench mode disables the gate wholesale and would mask everything here."""
    monkeypatch.delenv("PRAXIS_BENCH", raising=False)
    monkeypatch.delenv("PRAXIS_BENCH_DISABLE_VERIFY", raising=False)


# ---------------------------------------------------------------------------
# Layer 3 first, because the other two rest on it: run_verify must never shell
# a blank command, whatever a future caller hands it.
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_run_verify_refuses_a_whitespace_only_command(tmp_path: Any) -> None:
    """The guard that makes a forgotten normalization loud instead of green.

    Without it, this exact call returns ``(True, "")``: the shell runs nothing
    and exits 0, which every caller reads as a passing verification.
    """
    with pytest.raises(ValueError, match="blank verify command") as excinfo:
        await run_verify(str(tmp_path), _BLANK)

    # The message has to point at the fix, not merely complain.
    assert "normalize_verify_cmd" in str(excinfo.value)


@pytest.mark.unit
async def test_run_verify_refuses_an_empty_command(tmp_path: Any) -> None:
    """``""`` is the same fact as ``"   "`` once it reaches this layer.

    Callers are supposed to have collapsed it onto ``None`` and skipped; that
    one got here at all is a bug in the caller either way.
    """
    with pytest.raises(ValueError, match="blank verify command"):
        await run_verify(str(tmp_path), "")


@pytest.mark.unit
async def test_run_verify_still_runs_a_real_command(tmp_path: Any) -> None:
    """The guard must not be so eager that it refuses ordinary commands."""
    passed, output = await run_verify(str(tmp_path), "echo praxis-gate-ran")

    assert passed is True
    assert "praxis-gate-ran" in output


@pytest.mark.unit
@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, None),
        ("", None),
        ("   ", None),
        ("\t\n ", None),
        ("pytest -q", "pytest -q"),
        # Returned UNCHANGED, not stripped: the command is echoed verbatim into
        # logs and the worker Bible, and silently rewriting operator config is
        # its own small lie.
        ("  pytest -q  ", "  pytest -q  "),
    ],
)
def test_normalize_verify_cmd_collapses_only_the_blank_cases(
    value: str | None, expected: str | None
) -> None:
    assert normalize_verify_cmd(value) == expected


# ---------------------------------------------------------------------------
# Layer 1: the boundary. A whitespace-only verify_cmd is refused with a 422,
# and the two values that already mean "not configured" keep working.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def mock_preflight_remote(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """POST /api/projects does a remote preflight; nothing here needs a network."""
    m = AsyncMock(return_value=[])
    monkeypatch.setattr("orchestrator.api.projects.preflight_remote", m)
    return m


@pytest.mark.unit
def test_project_create_refuses_a_whitespace_only_verify_cmd() -> None:
    with pytest.raises(ValidationError) as excinfo:
        ProjectCreate(name="App", repo_url="https://github.com/u/a", verify_cmd=_BLANK)

    rendered = str(excinfo.value)
    assert "verify_cmd" in rendered
    assert "must contain a command" in rendered


@pytest.mark.unit
def test_project_update_refuses_a_whitespace_only_verify_cmd() -> None:
    with pytest.raises(ValidationError) as excinfo:
        ProjectUpdate(verify_cmd=_BLANK)

    rendered = str(excinfo.value)
    assert "verify_cmd" in rendered
    assert "must contain a command" in rendered


@pytest.mark.unit
def test_none_and_empty_verify_cmd_are_still_accepted_unchanged() -> None:
    """The regression guard on the fix itself.

    ``None`` and ``""`` are load-bearing: ``None`` is the create default and
    the PATCH "leave it alone" signal, ``""`` is the only way to clear a
    configured command. A validator that rejected them would break both flows
    while looking like a stricter, better version of this one.
    """
    assert ProjectCreate(name="A", repo_url="https://github.com/u/a").verify_cmd is None
    assert (
        ProjectCreate(
            name="A", repo_url="https://github.com/u/a", verify_cmd=""
        ).verify_cmd
        == ""
    )
    assert ProjectUpdate().verify_cmd is None
    assert ProjectUpdate(verify_cmd="").verify_cmd == ""
    assert ProjectUpdate(verify_cmd="pytest -q").verify_cmd == "pytest -q"


@pytest.mark.integration
async def test_create_project_with_a_blank_verify_cmd_is_a_422(
    client: AsyncClient,
    db: Database,
    auth_headers: dict[str, str],
) -> None:
    """The endpoint, not just the model: the layer an operator actually hits."""
    await seed_user(db)

    response = await client.post(
        "/api/projects",
        json={
            "name": "App",
            "repo_url": "https://github.com/u/a",
            "model_name": "m",
            "verify_cmd": _BLANK,
        },
        headers=auth_headers,
    )

    assert response.status_code == 422, response.text
    assert "must contain a command" in response.text
    # And nothing was written: a 422 that still persisted the row would leave
    # the very state layer 2 exists to survive.
    rows = await db.fetch_all("SELECT id FROM projects")
    assert rows == []


@pytest.mark.integration
async def test_patching_a_blank_verify_cmd_is_a_422_and_leaves_the_row_alone(
    client: AsyncClient,
    db: Database,
    auth_headers: dict[str, str],
) -> None:
    await seed_user(db)
    created = await client.post(
        "/api/projects",
        json={
            "name": "App",
            "repo_url": "https://github.com/u/a",
            "model_name": "m",
            "verify_cmd": "pytest -q",
        },
        headers=auth_headers,
    )
    project_id = created.json()["id"]

    response = await client.patch(
        f"/api/projects/{project_id}",
        json={"verify_cmd": _BLANK},
        headers=auth_headers,
    )

    assert response.status_code == 422, response.text
    still = await client.get(f"/api/projects/{project_id}", headers=auth_headers)
    assert still.json()["verify_cmd"] == "pytest -q"


@pytest.mark.integration
async def test_patching_an_empty_verify_cmd_still_clears_it(
    client: AsyncClient,
    db: Database,
    auth_headers: dict[str, str],
) -> None:
    """``""`` is the clear operation and must survive the new validator."""
    await seed_user(db)
    created = await client.post(
        "/api/projects",
        json={
            "name": "App",
            "repo_url": "https://github.com/u/a",
            "model_name": "m",
            "verify_cmd": "pytest -q",
        },
        headers=auth_headers,
    )
    project_id = created.json()["id"]

    response = await client.patch(
        f"/api/projects/{project_id}",
        json={"verify_cmd": ""},
        headers=auth_headers,
    )

    assert response.status_code == 200, response.text
    assert response.json()["verify_cmd"] == ""


# ---------------------------------------------------------------------------
# Layer 2: the runtime read sites. A row already in a live database can carry
# "   ", so each of the three gates must read it as "not configured".
# ---------------------------------------------------------------------------


def _orchestrator(bus: EventBus) -> Orchestrator:
    """A bare Orchestrator for the gate methods, which touch no real DB."""
    return Orchestrator(
        task_queue=AsyncMock(),
        agent_manager=MagicMock(),
        opus_bridge=AsyncMock(),
        git_ops=AsyncMock(),
        event_bus=bus,
    )


class _CheckoutStub:
    """A backend whose checkout always succeeds, so only the gate is under test.

    ``name = "local"`` routes ``_verify_plan_branch`` down the credential-less
    path, which is the shortest route from the normalization to ``run_verify``.
    """

    name = "local"

    async def checkout(self, ref: Any, dest: str) -> str:
        return dest


def _spy_run_verify(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Replace ``run_verify`` with a fake that PASSES, and record its commands.

    Returning ``True`` is deliberate: it reproduces the pre-fix world exactly
    (a blank shell command exits 0), so a normalization that is missing shows
    up as a ``passed`` verdict rather than as an error from the layer-3 guard.
    Each layer must be provable on its own.
    """
    calls: list[str] = []

    async def _fake(checkout_dir: str, cmd: str, timeout: float = 600.0) -> Any:
        calls.append(cmd)
        return True, "THIS COMMAND RAN NOTHING"

    monkeypatch.setattr(review_module, "run_verify", _fake)
    return calls


# --- 2a: the per-task gate in ReviewMixin.review_task -----------------------


@pytest.mark.unit
async def test_the_per_task_gate_never_passes_on_a_blank_verify_cmd(
    orchestrator_fixture: tuple[Orchestrator, str, dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Read site 1 of 3: ``review_task``.

    With a successful PR-head checkout and a blank command, the pre-fix code
    shelled it, got exit 0, and logged "verify gate passed" before handing the
    diff to the brain. The honest report is the skip that ``""``/``None``
    already produce.
    """
    orch, task_id, project = orchestrator_fixture
    local = dict(project)
    local["verify_cmd"] = _BLANK
    calls = _spy_run_verify(monkeypatch)
    orch._git.clone_pr_head.side_effect = None
    orch._git.clone_pr_head.return_value = "/tmp/x"  # noqa: S108

    with caplog.at_level(logging.INFO, logger=_REVIEW_LOGGER):
        await orch.review_task(task_id, local)

    assert calls == [], (
        "the per-task gate shelled a blank verify command; a blank shell "
        f"command exits 0, so this reports a pass having run nothing: {calls!r}"
    )
    assert not any("verify gate passed" in m for m in caplog.messages), (
        f"the gate reported a pass for a command it never ran: {caplog.messages}"
    )
    assert any(
        "verify gate skipped" in m and _SKIP_NO_VERIFY_CMD in m for m in caplog.messages
    ), caplog.messages


# --- 2b: the plan-branch funnel (_verify_plan_branch) -----------------------


@pytest.mark.unit
async def test_the_plan_branch_gate_reports_skipped_for_a_blank_verify_cmd(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Read site 2 of 3: the funnel both plan-level callers go through.

    ``resolve_no_change_run`` and ``on_plan_completed`` each read the raw
    column and hand it straight here, so normalizing in this one place is what
    keeps the two of them from drifting apart.
    """
    orch = _orchestrator(EventBus())
    monkeypatch.setattr(orch, "_resolve_backend", lambda _repo_url: _CheckoutStub())
    calls = _spy_run_verify(monkeypatch)

    result = await orch._verify_plan_branch("/repos/app.git", "plan/x", _BLANK)

    assert result.status == "skipped", (
        "the plan-branch gate reported a verdict for a command that runs "
        f"nothing: {result!r}"
    )
    assert result.reason == _SKIP_NO_VERIFY_CMD
    assert calls == [], f"a blank command reached the shell: {calls!r}"


@pytest.mark.unit
async def test_a_blank_verify_cmd_never_becomes_no_changes_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The consequence that made this worth two layers.

    ``resolve_no_change_run`` closes a leaf as a genuine no-op and records WHY.
    A blank command made the branch report ``passed``, so the leaf was closed
    claiming "verify passed on plan/x" -- a specific, false, permanently stored
    statement about evidence that was never gathered. The honest evidence
    string is the one a project with no verify command already gets.
    """
    bus = EventBus()
    queue = bus.subscribe()
    orch = _orchestrator(bus)
    monkeypatch.setattr(orch, "_resolve_backend", lambda _repo_url: _CheckoutStub())
    calls = _spy_run_verify(monkeypatch)

    closed = await orch.resolve_no_change_run(
        "t1",
        {"repo_url": "/repos/app.git", "default_branch": "main", "verify_cmd": _BLANK},
        {"plan_branch_name": "plan/x"},
    )

    assert closed is True
    events = [queue.get_nowait() for _ in range(queue.qsize())]
    no_changes = next(e for e in events if e["type"] == "task_no_changes")
    assert no_changes["verify_status"] == "skipped", (
        "a no-op leaf was closed on a verify verdict for a command that never "
        f"ran: {no_changes!r}"
    )
    assert "verify passed" not in no_changes["reason"], (
        f"the stored evidence claims a check that never ran: {no_changes['reason']!r}"
    )
    assert calls == [], f"a blank command reached the shell: {calls!r}"


# --- 2c: the per-wave cross-leaf gate in DispatchMixin ----------------------


@pytest.mark.unit
async def test_the_wave_gate_does_not_memoize_a_blank_verify_cmd_as_verified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Read site 3 of 3: ``_wave_verify_gate``.

    This one short-circuits on its own falsy check BEFORE reaching the plan
    branch funnel, so it needs its own normalization: without it the blank was
    truthy, the gate ran, and the wave was memoized as verified at this
    ``merged_count``. That memo then greens every later dispatch decision for
    the same wave without re-checking anything.

    Asserting the memo (and that the funnel was never entered) is what makes
    this test see the dispatch-side fix specifically. ``_wave_verify_gate``
    returns ``True`` for a skip AND for a pass, so its return value alone
    cannot tell the two apart.
    """
    orch = _orchestrator(EventBus())
    entered: list[str | None] = []

    async def _fake_plan_verify(
        repo_url: str, plan_branch: str, verify_cmd: str | None
    ) -> _PlanVerifyResult:
        entered.append(verify_cmd)
        return _PlanVerifyResult("passed", output="THIS COMMAND RAN NOTHING")

    monkeypatch.setattr(orch, "_verify_plan_branch", _fake_plan_verify)

    proceed = await orch._wave_verify_gate(
        "plan-1",
        {"plan_branch_name": "plan/x"},
        {"repo_url": "/repos/app.git", "verify_cmd": _BLANK},
        merged_count=1,
    )

    assert proceed is True
    assert entered == [], (
        "the wave gate ran the plan-branch gate on a blank command; a blank "
        f"shell command exits 0, so the wave gets memoized as verified: {entered!r}"
    )
    assert orch._wave_verify_state == {}, (
        "the wave was memoized as verified against a command that never ran: "
        f"{orch._wave_verify_state!r}"
    )


@pytest.mark.unit
async def test_the_wave_gate_still_runs_a_real_verify_cmd(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The mirror image: normalizing must not switch the gate off entirely."""
    orch = _orchestrator(EventBus())
    entered: list[str | None] = []

    async def _fake_plan_verify(
        repo_url: str, plan_branch: str, verify_cmd: str | None
    ) -> _PlanVerifyResult:
        entered.append(verify_cmd)
        return _PlanVerifyResult("passed", output="1 passed")

    monkeypatch.setattr(orch, "_verify_plan_branch", _fake_plan_verify)

    proceed = await orch._wave_verify_gate(
        "plan-1",
        {"plan_branch_name": "plan/x"},
        {"repo_url": "/repos/app.git", "verify_cmd": "pytest -q"},
        merged_count=1,
    )

    assert proceed is True
    assert entered == ["pytest -q"]
    assert orch._wave_verify_state == {"plan-1": (1, True)}
