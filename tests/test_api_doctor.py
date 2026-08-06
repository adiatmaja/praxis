import docker.errors
import httpx
import pytest

from orchestrator.core.doctor import CHECK_IDS


# --- Fakes for the gathering layer ------------------------------------------
#
# `api/doctor.py` is the fact-GATHERING half of doctor and runs entirely
# outside `run_checks`'s per-probe exception shield, so it needs its own tests.
# Each helper below replaces exactly one live-IO seam, leaving the rest of the
# real gathering code running.

_CREATED = "2026-01-01T00:00:00.000000Z"


class _FakeImage:
    """A docker-SDK image object with only the attribute doctor reads."""

    def __init__(self, created: str = _CREATED) -> None:
        self.attrs = {"Created": created}


class _FakeImages:
    def __init__(self, get) -> None:
        self.get = get


class _FakeDockerClient:
    """A daemon that answers `ping()` and delegates `images.get` to a stub."""

    def __init__(self, images_get) -> None:
        self.images = _FakeImages(images_get)

    def ping(self) -> bool:
        return True


def _install_fake_docker(monkeypatch, images_get) -> None:
    """Point the gathering layer at a reachable fake daemon.

    `_in_container` is pinned False as well: `_resolve_published_port` is only
    called inside a container, and this fake has no `containers` attribute.
    """
    from orchestrator.api import doctor as doctor_api

    monkeypatch.setattr(doctor_api, "_in_container", lambda: False)
    monkeypatch.setattr(
        doctor_api.docker, "from_env", lambda: _FakeDockerClient(images_get)
    )


def _install_fake_worker_endpoint(monkeypatch, models) -> None:
    """Stub the LM Studio HTTP probe so no test waits on a 5s timeout."""
    from orchestrator.api import doctor as doctor_api

    async def _fake(url: str):
        return True, list(models)

    monkeypatch.setattr(doctor_api, "_probe_lm_studio", _fake)


def _rows(body: dict) -> dict:
    return {check["check_id"]: check for check in body["checks"]}


def _assert_well_formed(body: dict) -> None:
    """Every registered check, in order, each row complete, every red hinted.

    Asserted alongside the HTTP 200 in each degradation test below: a guard
    that swallowed everything into a contentless response would still answer
    200, so totality alone is not enough to call the endpoint healthy.
    """
    assert tuple(check["check_id"] for check in body["checks"]) == CHECK_IDS
    assert body["status"] in {"green", "amber", "red"}
    for check in body["checks"]:
        assert set(check) == {"check_id", "label", "status", "detail", "hint"}
        assert check["status"] in {"green", "amber", "red"}
        assert check["label"]
        assert check["detail"]
        if check["status"] == "red":
            assert check["hint"]


@pytest.mark.integration
async def test_doctor_requires_auth(client):
    response = await client.get("/api/doctor")
    assert response.status_code == 401


@pytest.mark.integration
async def test_doctor_returns_every_check(client, auth_headers):
    from orchestrator.core.doctor import CHECK_IDS

    response = await client.get("/api/doctor", headers=auth_headers)
    assert {c["check_id"] for c in response.json()["checks"]} == set(CHECK_IDS)


@pytest.mark.integration
async def test_doctor_is_http_200_even_when_checks_are_red(client, auth_headers):
    """A diagnosis is a successful response, whatever it diagnoses."""
    response = await client.get("/api/doctor", headers=auth_headers)
    assert response.status_code == 200
    assert response.json()["status"] in {"green", "amber", "red"}


@pytest.mark.integration
async def test_every_red_check_in_the_response_carries_a_hint(client, auth_headers):
    body = (await client.get("/api/doctor", headers=auth_headers)).json()
    for check in body["checks"]:
        if check["status"] == "red":
            assert check["hint"]


@pytest.mark.integration
async def test_doctor_checks_are_in_registry_order(client, auth_headers):
    from orchestrator.core.doctor import CHECK_IDS

    body = (await client.get("/api/doctor", headers=auth_headers)).json()
    assert tuple(c["check_id"] for c in body["checks"]) == CHECK_IDS


# --- The gathering phase must never break the response ----------------------


@pytest.mark.integration
async def test_a_docker_api_error_degrades_one_row_not_the_response(
    client, auth_headers, monkeypatch
):
    """A daemon that pings then 500s mid-request is a diagnosable state.

    Gathering runs BEFORE `run_checks`'s per-probe shield, so an unguarded
    `docker.errors.APIError` out of `images.get` used to 500 the one endpoint
    whose job is answering on a broken machine.

    The daemon row staying GREEN is the load-bearing half of this assertion:
    it proves the failure was contained to the check it actually concerns
    instead of collapsing every Docker fact, which is what a guard placed at
    the outer boundary alone would do.
    """

    def _boom(tag: str):
        message = "500 Server Error: Internal Server Error"
        raise docker.errors.APIError(message)

    _install_fake_docker(monkeypatch, _boom)
    _install_fake_worker_endpoint(monkeypatch, ["qwen3.6-27b"])

    response = await client.get("/api/doctor", headers=auth_headers)

    assert response.status_code == 200
    body = response.json()
    _assert_well_formed(body)
    rows = _rows(body)
    assert rows["docker_daemon"]["status"] == "green"
    assert rows["agent_images"]["status"] == "amber"
    assert "APIError" in rows["agent_images"]["detail"]
    assert "opencode-agent:latest" in rows["agent_images"]["detail"]


@pytest.mark.integration
async def test_a_non_dict_models_body_degrades_one_row_not_the_response(
    client, auth_headers, monkeypatch
):
    """A reachable endpoint answering a JSON list, not an object.

    `data.get("data", [])` then raises `AttributeError`, which the probe's
    narrow `(httpx.HTTPError, ValueError, KeyError)` never caught. A proxy or
    a gateway login page in front of LM Studio produces exactly this.
    """
    from orchestrator.api import doctor as doctor_api

    class _Response:
        def raise_for_status(self) -> None:
            return None

        def json(self):
            return ["qwen3.6-27b"]

    class _AsyncClient:
        def __init__(self, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc) -> bool:
            return False

        async def get(self, url: str):
            return _Response()

    class _Httpx:
        """Stand-in for the `httpx` name inside api/doctor only."""

        AsyncClient = _AsyncClient
        HTTPError = httpx.HTTPError

    _install_fake_docker(monkeypatch, lambda _tag: _FakeImage())
    monkeypatch.setattr(doctor_api, "httpx", _Httpx)

    response = await client.get("/api/doctor", headers=auth_headers)

    assert response.status_code == 200
    body = response.json()
    _assert_well_formed(body)
    row = _rows(body)["worker_endpoint"]
    assert row["status"] == "red"
    assert "AttributeError" in row["detail"]


@pytest.mark.integration
async def test_a_gathering_helper_raising_a_bare_runtime_error_still_answers(
    client, auth_headers, monkeypatch
):
    """Not every failure mode is enumerable, so the guard is per unit.

    `_entrypoint_mtimes` only stats files today, but the contract this pins is
    that ANY exception out of ANY gathering unit degrades that unit's row and
    nothing else.
    """
    from orchestrator.api import doctor as doctor_api

    def _boom() -> dict:
        message = "the filesystem went away"
        raise RuntimeError(message)

    _install_fake_docker(monkeypatch, lambda _tag: _FakeImage())
    _install_fake_worker_endpoint(monkeypatch, ["qwen3.6-27b"])
    monkeypatch.setattr(doctor_api, "_entrypoint_mtimes", _boom)

    response = await client.get("/api/doctor", headers=auth_headers)

    assert response.status_code == 200
    body = response.json()
    _assert_well_formed(body)
    rows = _rows(body)
    assert rows["agent_images"]["status"] == "green"
    assert rows["agent_image_freshness"]["status"] == "amber"
    assert "RuntimeError" in rows["agent_image_freshness"]["detail"]


@pytest.mark.integration
async def test_a_wholesale_gathering_failure_still_answers_with_a_diagnosis(
    client, auth_headers, monkeypatch
):
    """The backstop: even the code BETWEEN gathering units cannot 500 this.

    `orchestrator_health` carries the exception because the orchestrator's own
    diagnosis code is what failed; every other row is an honest "not checked"
    amber rather than a fabricated verdict, mirroring the CLI's unreachable
    table.
    """
    from orchestrator.api import doctor as doctor_api

    async def _boom(request):
        message = "app.state went away"
        raise RuntimeError(message)

    monkeypatch.setattr(doctor_api, "_build_probes", _boom)

    response = await client.get("/api/doctor", headers=auth_headers)

    assert response.status_code == 200
    body = response.json()
    _assert_well_formed(body)
    rows = _rows(body)
    assert rows["orchestrator_health"]["status"] == "red"
    assert "RuntimeError" in rows["orchestrator_health"]["detail"]
    assert rows["docker_daemon"]["status"] == "amber"
    assert body["status"] == "red"


@pytest.mark.integration
async def test_a_non_local_llm_worker_is_never_compared_against_v1_models(
    client, auth_headers, monkeypatch
):
    """The gathering half of the `supports_local_llm` gate.

    agy/Gemini names a provider model that LM Studio's `/v1/models` will never
    list, so comparing the two is a category error and a permanent false RED on
    a correctly installed agy worker. Removing the gate in `_build_probes`
    leaves every other test in the suite green, so it is pinned here.
    """
    _install_fake_docker(monkeypatch, lambda _tag: _FakeImage())
    _install_fake_worker_endpoint(monkeypatch, ["qwen3.6-27b"])
    monkeypatch.setattr(
        client.app.state.effective_settings,
        "auto_delegate_worker",
        lambda: {"harness": "agy", "model": "Gemini 3.6 Flash (High)"},
    )

    body = (await client.get("/api/doctor", headers=auth_headers)).json()

    row = _rows(body)["worker_endpoint"]
    assert row["status"] == "green"
    assert "Gemini" not in row["detail"]
