"""Every doctor row must claim exactly what was measured, and nothing more.

Doctor is the front door to every problem in Praxis, so a row that overstates
its evidence is worse than a missing row: it sends the operator to fix
something that is not broken and rules out the thing that is. Eight such
overstatements are pinned here, each with a scenario in which only that one
fix's revert turns it red.

The pattern behind all eight is the same. A fact was DERIVED (from a version
probe, from a provider name, from the absence of an image label) and then
rendered as though it had been OBSERVED. The cure is never a new measurement,
it is a sentence that names what the measurement was.
"""
# ruff: noqa: S101

from __future__ import annotations

import json
from typing import Any

import pytest
from httpx import AsyncClient

from orchestrator.core.doctor import CHECKS, CheckResult, CheckStatus, run_checks
from orchestrator.core.doctor_probes import (
    PROVIDER_KIND_CLI,
    PROVIDER_KIND_LOCAL,
    PROVIDER_KIND_UNKNOWN,
    planner_label,
    probe_planner_cli,
)
from tests.test_api_doctor import (
    _FakeImage,
    _install_fake_docker,
    _install_fake_worker_endpoint,
)


def _rows(body: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {check["check_id"]: check for check in body["checks"]}


def _label_of(check_id: str) -> str:
    return next(check.label for check in CHECKS if check.check_id == check_id)


def _quiet_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub Docker and LM Studio so no test here waits on real IO."""
    _install_fake_docker(monkeypatch, lambda _tag: _FakeImage())
    _install_fake_worker_endpoint(monkeypatch, ["qwen3.8-27b"])


def _stub_planner_probe(monkeypatch: pytest.MonkeyPatch, **fields: Any) -> None:
    """Answer the CLI-state probe with a fixed dict, spawning nothing."""
    from orchestrator.api import doctor as doctor_api

    async def _fake(name: str) -> dict[str, Any]:
        return {"cli_available": True, "authenticated": True, **fields}

    monkeypatch.setattr(doctor_api, "_probe_provider", _fake)


async def _point_the_plan_role_at(
    client: AsyncClient, provider: str, model: str = ""
) -> None:
    """Configure the plan role the way an operator would, via overrides.

    Writes the same two ``settings_overrides`` rows
    ``EffectiveSettings.registered_models`` and ``role_chains`` read, so the
    resolution under test is the production one.
    """
    es = client.app.state.effective_settings
    await es.set_override(
        "models.registry",
        json.dumps(
            [
                {
                    "name": "planner-under-test",
                    "provider": provider,
                    "model": model,
                    "effort": None,
                }
            ]
        ),
    )
    await es.set_override("models.roles", json.dumps({"plan": ["planner-under-test"]}))


# --- 1. "authenticated" was never measured for a claude planner -------------


@pytest.mark.unit
def test_an_unmeasured_login_state_is_never_reported_as_authenticated():
    """`claude` has no auth command, so `authenticated` means "on PATH".

    Rendering that as "installed and authenticated" is the whole defect: a
    claude CLI that is present but logged out produced a RED asserting the
    session was fine and blaming a hook that does not exist.
    """
    result = probe_planner_cli(
        cli_available=True,
        authenticated=True,
        auth_measured=False,
        prompt_ok=False,
        provider="claude",
        model="claude-sonnet-4-6",
    )

    assert result.status is CheckStatus.RED
    assert "installed and authenticated" not in result.detail, (
        f"the row asserts a session nobody logged into: {result.detail!r}"
    )
    assert "login state was not checked" in result.detail


@pytest.mark.unit
def test_a_refused_prompt_presents_both_causes_when_auth_was_not_measured():
    """Both candidates, neither ruled out: not logged in, or something refused.

    The hook remedy stays, because a blocking hook is real and common here.
    What may not stay is the SUPPRESSION of the login instruction, which the
    probe did on the strength of an "authenticated" flag nothing had measured.
    """
    hint = probe_planner_cli(
        cli_available=True,
        authenticated=True,
        auth_measured=False,
        prompt_ok=False,
        provider="claude",
        login_hint="claude login",
    ).hint

    assert "claude login" in hint, "the login cause was ruled out unmeasured"
    # `.env.container`, the gitignored env_file compose declares. This pinned
    # `docker-compose.yml` until that remedy moved: editing a TRACKED file
    # leaves a fresh clone holding a permanent local diff, so it is a remedy
    # nobody can follow twice.
    assert ".env.container" in hint, "the hook remedy must survive"
    assert "Not in .env" in hint, "the hook remedy must still say where NOT to put it"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("provider", "measured"),
    [
        pytest.param("claude", False, id="claude-has-no-auth-command"),
        pytest.param("codex", True, id="codex-runs-login-status"),
    ],
)
async def test_probe_provider_reports_whether_the_login_state_was_measured(
    mocker: pytest.MonkeyPatch, provider: str, measured: bool
) -> None:
    """The gathering half: the flag has to come from the command table.

    `_PROVIDER_CMDS` is where "this provider can be asked" lives. A caller
    cannot tell a derived `authenticated` from a measured one unless this
    layer says which it was.
    """
    import orchestrator.api.system as sys_mod

    sys_mod._provider_probe_cache.clear()

    async def _fake_exec(*args: object, **kwargs: object) -> object:
        proc = mocker.MagicMock()
        proc.returncode = 0
        proc.wait = mocker.AsyncMock(return_value=0)
        return proc

    mocker.patch("asyncio.create_subprocess_exec", new=_fake_exec)
    result = await sys_mod._probe_provider(provider)

    assert result["authenticated"] is True
    assert result["auth_measured"] is measured


@pytest.mark.integration
async def test_the_row_rules_out_login_only_when_the_probe_measured_it(
    client: AsyncClient, auth_headers: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The seam: `auth_measured` has to travel from the probe into the row.

    Asserted in BOTH directions on purpose. Dropping the keyword at the call
    site falls back to the default and only the True case notices; hardcoding
    it True only the False case notices.
    """
    from orchestrator.api import doctor as doctor_api
    from orchestrator.api.system import RoundTripResult

    async def _refused(target: Any) -> RoundTripResult:
        return RoundTripResult(ok=False, error="Invalid API key")

    _quiet_environment(monkeypatch)
    monkeypatch.setattr(doctor_api, "probe_provider_roundtrip", _refused)

    _stub_planner_probe(monkeypatch, auth_measured=False, login_hint="claude login")
    unmeasured = _rows((await client.get("/api/doctor", headers=auth_headers)).json())

    _stub_planner_probe(monkeypatch, auth_measured=True, login_hint="claude login")
    measured = _rows((await client.get("/api/doctor", headers=auth_headers)).json())

    assert unmeasured["planner_cli"]["status"] == "red"
    assert "claude login" in unmeasured["planner_cli"]["hint"], (
        "an unmeasured login state must leave the login cause on the table"
    )
    assert "claude login" not in measured["planner_cli"]["hint"], (
        "a measured login state rules the login cause out"
    )


# --- 2. a planner nothing round-tripped is not a pass -----------------------


@pytest.mark.unit
def test_a_planner_with_no_round_trip_is_amber_not_green():
    """`probe_provider_roundtrip` has a round trip for `claude` only.

    An `agy` planner therefore lands here with `authenticated` derived from
    `agy help` exiting 0, while `core/harnesses.py` says agy needs an
    interactive `agy login`. Empty credentials read as a clean GREEN.
    """
    result = probe_planner_cli(
        cli_available=True,
        authenticated=True,
        auth_measured=False,
        prompt_ok=None,
        provider="agy",
        model="gemini-3-flash",
    )

    assert result.status is CheckStatus.AMBER, (
        f"nothing was verified, so this cannot be a pass: {result.detail!r}"
    )
    assert "no test prompt was made" in result.detail


# --- 3. an unreachable daemon is a gathering failure, not an image verdict ---


@pytest.mark.integration
async def test_the_image_rows_are_amber_when_the_daemon_is_unreachable(
    client: AsyncClient, auth_headers: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """No image was inspected, so no image verdict may be rendered.

    Both rows used to go RED, and the freshness one auto-filled the registry
    hint asserting "an entrypoint changed since the image was built", a cause
    nothing established. Both remedies are docker commands that cannot run
    while the daemon is down. `api/doctor._degraded` already states the rule
    this violated.
    """
    from orchestrator.api import doctor as doctor_api

    def _down() -> Any:
        message = "Error while fetching server API version"
        raise ConnectionError(message)

    monkeypatch.setattr(doctor_api, "_in_container", lambda: False)
    monkeypatch.setattr(doctor_api.docker, "from_env", _down)
    _install_fake_worker_endpoint(monkeypatch, ["qwen3.8-27b"])

    rows = _rows((await client.get("/api/doctor", headers=auth_headers)).json())

    assert rows["docker_daemon"]["status"] == "red", (
        "the daemon row is where the real verdict and the real remedy live"
    )
    for check_id in ("agent_images", "agent_image_freshness"):
        row = rows[check_id]
        assert row["status"] == "amber", f"{check_id} invented a verdict: {row}"
        assert "docker_daemon" in row["detail"], (
            f"{check_id} must point at the row that explains it: {row['detail']!r}"
        )
    assert "entrypoint changed" not in rows["agent_image_freshness"]["hint"]


# --- 4a. an unrecognised provider is not a benign non-CLI provider ----------


@pytest.mark.unit
def test_an_unrecognised_planner_provider_is_red_not_benign():
    """Provider names are free text; a typo used to render "nothing to fix".

    `planner_provider_is_cli` answered False for ANY provider `build_argv`
    does not know, so `cluade` took the `local` branch: an amber whose hint
    said there was nothing to fix, while every plan would raise
    `UnknownProviderError` on the first brain call.
    """
    result = probe_planner_cli(
        cli_available=False,
        authenticated=False,
        provider="cluade",
        provider_kind=PROVIDER_KIND_UNKNOWN,
    )

    assert result.status is CheckStatus.RED
    assert "cluade" in result.detail
    assert "nothing to fix" not in result.hint.lower()
    assert result.hint


@pytest.mark.integration
async def test_a_typo_in_the_planner_provider_reaches_the_row_as_unknown(
    client: AsyncClient, auth_headers: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The seam: the API must classify into three kinds, not two.

    `planner_provider_kind` is the classifier; a caller that keeps asking a
    boolean question cannot express this row.
    """
    _quiet_environment(monkeypatch)
    await _point_the_plan_role_at(client, provider="cluade", model="whatever")

    row = _rows((await client.get("/api/doctor", headers=auth_headers)).json())[
        "planner_cli"
    ]

    assert row["status"] == "red", f"a typo'd provider read as benign: {row}"
    assert "cluade" in row["detail"]


# --- 4b. coverage the worker_endpoint row does not provide ------------------


@pytest.mark.unit
def test_a_local_planner_is_not_promised_coverage_the_worker_row_lacks():
    """The worker_endpoint row skips the probe for a non-local-LLM harness.

    The shipped default worker harness is `agy`, which talks to Google
    directly, so that row answers "not applicable" without probing anything.
    Under that combination a `local` planner's endpoint is checked by no row
    at all, and this hint used to say it was covered.
    """
    hint = probe_planner_cli(
        cli_available=False,
        authenticated=False,
        provider="local",
        provider_kind=PROVIDER_KIND_LOCAL,
        endpoint_checked_elsewhere=False,
        endpoint="http://host.docker.internal:1234",
    ).hint

    assert "worker_endpoint row covers" not in hint
    assert "http://host.docker.internal:1234" in hint, (
        "nothing else prints this URL in that configuration"
    )


@pytest.mark.unit
def test_a_local_planner_keeps_the_promise_when_the_worker_row_does_probe():
    """The other branch: with a local-LLM worker, that row DOES probe it.

    Pinned so the fix cannot be "delete the sentence": the coverage claim is
    correct exactly when the worker harness uses the same endpoint.
    """
    hint = probe_planner_cli(
        cli_available=False,
        authenticated=False,
        provider="local",
        provider_kind=PROVIDER_KIND_LOCAL,
        endpoint_checked_elsewhere=True,
        endpoint="http://host.docker.internal:1234",
    ).hint

    assert "worker_endpoint" in hint
    assert "no row checked" not in hint


@pytest.mark.integration
async def test_a_local_planner_under_an_agy_worker_is_told_nobody_probed_it(
    client: AsyncClient, auth_headers: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The seam: whether that row probes anything is a WORKER fact.

    It has to travel from the harness registry into the planner row, or the
    promise is made from a constant.
    """
    _quiet_environment(monkeypatch)
    monkeypatch.setattr(
        client.app.state.effective_settings,
        "auto_delegate_worker",
        lambda: {"harness": "agy", "model": "Gemini 3.6 Flash (High)"},
    )
    await _point_the_plan_role_at(client, provider="local")

    body = (await client.get("/api/doctor", headers=auth_headers)).json()
    rows = _rows(body)

    assert rows["worker_endpoint"]["status"] == "green"
    assert "not applicable" in rows["worker_endpoint"]["detail"], (
        "precondition: that row probed nothing in this configuration"
    )
    assert "no row checked" in rows["planner_cli"]["hint"], (
        f"the planner row promised coverage nothing provided: {rows['planner_cli']}"
    )


# --- 6 and 7. a label is a status line too ----------------------------------


@pytest.mark.unit
def test_the_git_credential_label_states_what_it_measures():
    """The probe asks whether a token is non-empty. A revoked PAT is non-empty.

    "Git credential usable" rendered `OK` beside a credential nobody had used
    for anything. The probe's own detail, "credential configured", was already
    honest; only the label overreached.
    """
    label = _label_of("git_credential").lower()

    assert "usable" not in label
    assert "configured" in label


@pytest.mark.unit
async def test_the_planner_label_cannot_contradict_the_detail_it_carries():
    """`OK | Planner CLI answers a test prompt | no test prompt was made`.

    The label and the detail render on the same line, so a label that asserts
    one branch's outcome contradicts every other branch. The label names the
    seat; the detail carries the verdict.
    """
    unprobed = probe_planner_cli(cli_available=True, authenticated=True)
    probe_map: dict[str, Any] = {
        check.check_id: (
            lambda check_id=check.check_id: CheckResult(
                check_id=check_id, status=CheckStatus.GREEN, detail="ok"
            )
        )
        for check in CHECKS
    }
    probe_map["planner_cli"] = lambda: unprobed

    results = await run_checks(probe_map)

    row = next(r for r in results if r.check_id == "planner_cli")
    assert row.label, "the row must still be named"
    assert "no test prompt was made" in row.detail
    assert "test prompt" not in row.label.lower(), (
        f"the label promises what the detail denies: {row.label!r} / {row.detail!r}"
    )


# --- 8. an empty model on a provider that has no CLI ------------------------


@pytest.mark.unit
def test_an_empty_model_on_a_non_cli_provider_is_not_a_cli_default():
    """`{provider: local, model: ""}` is a shipped registry entry.

    What actually happens is that `llm_router` omits the model key and the
    endpoint answers with whatever it has loaded. "the CLI's default model"
    names a CLI the very next clause of the same row says does not exist.
    """
    spelled = planner_label("local", "", None, provider_kind=PROVIDER_KIND_LOCAL)

    assert "CLI" not in spelled, f"there is no CLI in this configuration: {spelled!r}"
    assert "local" in spelled
    assert "loaded" in spelled


@pytest.mark.unit
def test_an_empty_model_on_a_cli_provider_still_names_the_cli_default():
    """The other branch: a CLI provider with no `--model` DOES fall back."""
    spelled = planner_label("claude", "", None, provider_kind=PROVIDER_KIND_CLI)

    assert "CLI's default model" in spelled
