import docker.errors
import httpx
import pytest

from orchestrator.core.doctor import CHECK_IDS, CheckStatus
from orchestrator.core.doctor_probes import probe_worker_endpoint
from orchestrator.core.entrypoint_hash import LABEL_KEY


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


class _FakeImageWithLabel:
    """A docker-SDK image object carrying a real entrypoint-hash label.

    `_FakeImage` above carries no `Config` key at all, so its
    `image_labels` entry comes out `None` whether or not the label-reading
    code actually runs; a mutation that hardcodes `image_labels[tag] = None`
    passes every test using `_FakeImage` unnoticed. This fake is the one
    that would catch it.
    """

    def __init__(self, label_value: str) -> None:
        self.attrs = {"Config": {"Labels": {LABEL_KEY: label_value}}}


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


def _install_fake_worker_endpoint_unreachable(monkeypatch) -> None:
    """Stub the LM Studio HTTP probe as unreachable, no 5s timeout either."""
    from orchestrator.api import doctor as doctor_api

    async def _fake(url: str):
        return False, []

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

    The worker harness is pinned to a local-LLM one (`opencode`) so this
    stays a test of the malformed-body path specifically: the suite's
    default worker (`config/praxis.yaml`, `default_worker_harness: agy`)
    does not use an OpenAI endpoint at all, which would make the row green
    before the malformed body was ever inspected.
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
    monkeypatch.setattr(
        client.app.state.effective_settings,
        "auto_delegate_worker",
        lambda: {"harness": "opencode", "model": "qwen3.6-27b"},
    )

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

    `_entrypoint_hashes` only reads files today, but the contract this pins is
    that ANY exception out of ANY gathering unit degrades that unit's row and
    nothing else.
    """
    from orchestrator.api import doctor as doctor_api

    def _boom() -> dict:
        message = "the filesystem went away"
        raise RuntimeError(message)

    _install_fake_docker(monkeypatch, lambda _tag: _FakeImage())
    _install_fake_worker_endpoint(monkeypatch, ["qwen3.6-27b"])
    monkeypatch.setattr(doctor_api, "_entrypoint_hashes", _boom)

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


@pytest.mark.integration
async def test_a_non_local_llm_worker_is_never_probed_for_reachability(
    client, auth_headers, monkeypatch
):
    """The gathering half of the reachability gate, not just the model-name one.

    `test_a_non_local_llm_worker_is_never_compared_against_v1_models` above
    stubs the LM Studio probe as reachable, so it cannot tell whether
    `_build_probes` actually threads `endpoint_required` through to
    `probe_worker_endpoint`: a hardcoded `endpoint_required=True` at that call
    site would still read green there, because reachable=True short-circuits
    the model-name comparison regardless of the gate. Here the probe reports
    UNREACHABLE for an agy/Gemini worker (which never talks to LM Studio at
    all), so only a correctly wired `endpoint_required=False` keeps the row
    green; a hardcoded True turns it red.
    """
    _install_fake_docker(monkeypatch, lambda _tag: _FakeImage())
    _install_fake_worker_endpoint_unreachable(monkeypatch)
    monkeypatch.setattr(
        client.app.state.effective_settings,
        "auto_delegate_worker",
        lambda: {"harness": "agy", "model": "Gemini 3.6 Flash (High)"},
    )

    body = (await client.get("/api/doctor", headers=auth_headers)).json()

    row = _rows(body)["worker_endpoint"]
    assert row["status"] == "green"
    assert "not applicable" in row["detail"].lower()


def test_worker_endpoint_skipped_for_non_local_llm_harness() -> None:
    """agy talks to Google directly; an LM Studio probe is a category error.

    Reachability must be gated exactly like the model-name comparison
    already is, or the flagged default preset is permanently red.
    """
    result = probe_worker_endpoint(
        reachable=False,
        models=[],
        configured_model="",
        error="connection refused",
        endpoint_required=False,
    )
    assert result.status is CheckStatus.GREEN
    assert "not applicable" in result.detail.lower()


def test_worker_endpoint_still_red_for_local_llm_harness() -> None:
    result = probe_worker_endpoint(
        reachable=False,
        models=[],
        configured_model="qwen3.8-27b",
        error="connection refused",
        endpoint_required=True,
    )
    assert result.status is CheckStatus.RED


# --- _entrypoint_hashes: content hashing replaces mtime comparison ----------


def test_entrypoint_hashes_reads_every_registered_harness(tmp_path, monkeypatch):
    """Every harness in the registry contributes a source hash."""
    from orchestrator.api import doctor as doctor_api
    from orchestrator.core.harnesses import REGISTRY

    for harness in REGISTRY.values():
        d = tmp_path / f"{harness.id}-agent"
        d.mkdir(parents=True)
        (d / "entrypoint.sh").write_text("#!/bin/bash\necho x\n", encoding="utf-8")

    monkeypatch.setattr(doctor_api, "_ENTRYPOINT_ROOT", tmp_path)
    hashes = doctor_api._entrypoint_hashes()

    for harness in REGISTRY.values():
        assert hashes[harness.image] is not None


def test_entrypoint_hashes_missing_file_is_none(tmp_path, monkeypatch):
    from orchestrator.api import doctor as doctor_api
    from orchestrator.core.harnesses import REGISTRY

    monkeypatch.setattr(doctor_api, "_ENTRYPOINT_ROOT", tmp_path)
    hashes = doctor_api._entrypoint_hashes()

    for harness in REGISTRY.values():
        assert hashes[harness.image] is None


def test_gather_docker_facts_reads_the_entrypoint_hash_label(monkeypatch):
    """The label-reading half of the gathering layer, pinned directly.

    `_FakeImage` (used by every other test in this file) carries no
    `Config` key, so `image_labels` comes out `None` regardless of whether
    the code under test actually reads the label — a mutation that
    hardcodes `image_labels[tag] = None` passes the whole suite unnoticed.
    This test uses `_FakeImageWithLabel` specifically to close that gap.
    """
    from orchestrator.api import doctor as doctor_api

    _install_fake_docker(monkeypatch, lambda _tag: _FakeImageWithLabel("abc123"))

    facts = doctor_api._gather_docker_facts(resolve_port=False)

    assert facts.image_labels["opencode-agent:latest"] == "abc123"
    assert facts.image_labels["agy-agent:latest"] == "abc123"


# --- The three field-report rows, at the GATHERING layer --------------------
#
# The decision half of each lives in tests/test_doctor_field_report_rows.py.
# What is pinned here is the wiring that feeds them: which facts are read,
# from where, and -- for agy -- whether anything is spent at all.


class _FakeContainer:
    """An inspected container with only the attributes doctor reads."""

    def __init__(
        self,
        container_id: str,
        name: str,
        project: str | None = None,
        working_dir: str | None = None,
    ) -> None:
        self.id = container_id
        self.name = name
        labels: dict[str, str] = {}
        if project:
            labels["com.docker.compose.project"] = project
        if working_dir:
            labels["com.docker.compose.project.working_dir"] = working_dir
        self.attrs = {"Config": {"Labels": labels}}


class _FakeContainers:
    def __init__(self, by_name: dict[str, _FakeContainer], run=None) -> None:
        self._by_name = by_name
        self.run = run or _forbidden_run

    def get(self, name: str) -> _FakeContainer:
        try:
            return self._by_name[name]
        except KeyError as exc:
            message = f"no such container: {name}"
            raise docker.errors.NotFound(message) from exc


def _forbidden_run(*args, **kwargs):
    """A `containers.run` that must never be called from a doctor test."""
    message = "the doctor spawned a container in a test"
    raise AssertionError(message)


class _FakeDockerClientWithContainers(_FakeDockerClient):
    """`_FakeDockerClient` plus a container namespace to inspect."""

    def __init__(self, images_get, containers: dict[str, _FakeContainer]) -> None:
        super().__init__(images_get)
        self.containers = _FakeContainers(containers)


def _install_fake_docker_with_containers(
    monkeypatch, images_get, containers: dict[str, _FakeContainer]
) -> None:
    from orchestrator.api import doctor as doctor_api

    monkeypatch.setattr(doctor_api, "_in_container", lambda: False)
    monkeypatch.setattr(
        doctor_api.docker,
        "from_env",
        lambda: _FakeDockerClientWithContainers(images_get, containers),
    )


def _install_dotenv(monkeypatch, values: dict[str, str]) -> None:
    """Replace the mounted `.env` the compose-substitution vars are read from."""
    from orchestrator.api import doctor as doctor_api

    monkeypatch.setattr(doctor_api, "_dotenv_on_disk", lambda: dict(values))


async def _add_project(db, repo_url: str, name: str, harness: str = "opencode") -> None:
    await db.execute(
        """INSERT INTO projects (id, user_id, name, repo_url, default_branch, harness)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (name, "test-user", name, repo_url, "main", harness),
    )


@pytest.mark.integration
async def test_the_suite_never_spawns_an_agy_probe_container(
    client, auth_headers, monkeypatch
):
    """The `no_live_agy_container` autouse fixture, pinned.

    `config/praxis.yaml` ships `default_worker_harness: agy`, so this row is
    in play on a stock install and the fixture is what stops a machine that
    has built the image from starting a real container and waiting on a real
    Gemini call. The worker is pinned here rather than inherited, because
    settings precedence puts `.env` above the YAML and a developer whose
    `.env` names opencode would silently turn this test into a no-op.
    """
    _install_fake_docker(monkeypatch, lambda _tag: _FakeImage())
    _install_fake_worker_endpoint(monkeypatch, ["qwen3.6-27b"])
    monkeypatch.setattr(
        client.app.state.effective_settings,
        "auto_delegate_worker",
        lambda: {"harness": "agy", "model": "Gemini 3.7 Flash (High)"},
    )

    body = (await client.get("/api/doctor", headers=auth_headers)).json()

    row = _rows(body)["agy_credentials"]
    assert row["status"] == "amber"
    assert "not probed" in row["detail"]
    assert "the test suite never spawns a probe container" in row["detail"]


@pytest.mark.integration
async def test_no_container_is_spawned_when_no_agy_harness_is_in_play(
    client, auth_headers, monkeypatch
):
    """The cost gate, in the direction that keeps `praxis doctor` fast.

    `probe_agy_models` is replaced with a tripwire rather than a stub: the
    assertion is that it is never REACHED, which a stub returning "not
    probed" could not tell apart from a probe that ran and found nothing.
    """
    from orchestrator.api import doctor as doctor_api

    async def _tripwire(image: str, volume: str):
        message = "the agy probe ran with no agy harness in play"
        raise AssertionError(message)

    _install_fake_docker(monkeypatch, lambda _tag: _FakeImage())
    _install_fake_worker_endpoint(monkeypatch, ["qwen3.6-27b"])
    monkeypatch.setattr(doctor_api, "probe_agy_models", _tripwire)
    monkeypatch.setattr(
        client.app.state.effective_settings,
        "auto_delegate_worker",
        lambda: {"harness": "opencode", "model": "qwen3.6-27b"},
    )

    body = (await client.get("/api/doctor", headers=auth_headers)).json()

    row = _rows(body)["agy_credentials"]
    assert row["status"] == "green"
    assert "not applicable" in row["detail"]
    assert "no project uses the agy harness" in row["detail"]


@pytest.mark.integration
async def test_no_container_is_spawned_when_the_agy_image_is_not_built(
    client, auth_headers, monkeypatch
):
    """The second half of the gate, and the one that names its reason.

    An unbuilt image cannot answer, and trying anyway would turn one missing
    image into a Docker error the operator has to decode.
    """
    from orchestrator.api import doctor as doctor_api

    async def _tripwire(image: str, volume: str):
        message = "the agy probe ran against an image that is not built"
        raise AssertionError(message)

    def _no_agy_image(tag: str):
        if tag == "agy-agent:latest":
            raise docker.errors.ImageNotFound(tag)
        return _FakeImage()

    _install_fake_docker(monkeypatch, _no_agy_image)
    _install_fake_worker_endpoint(monkeypatch, ["qwen3.6-27b"])
    monkeypatch.setattr(doctor_api, "probe_agy_models", _tripwire)
    monkeypatch.setattr(
        client.app.state.effective_settings,
        "auto_delegate_worker",
        lambda: {"harness": "agy", "model": "Gemini 3.7 Flash (High)"},
    )

    body = (await client.get("/api/doctor", headers=auth_headers)).json()

    row = _rows(body)["agy_credentials"]
    assert row["status"] == "amber"
    assert "agy-agent:latest" in row["detail"]
    assert "not built" in row["detail"]


@pytest.mark.integration
async def test_an_undescribable_agy_image_is_unknown_not_absent(
    client, auth_headers, monkeypatch
):
    """A daemon that pings then fails the image query tells us NOTHING.

    `_DockerFacts.image_errors` exists to keep "unknown" apart from "absent",
    and reporting unknown as "not built" sends the operator to rebuild an
    image that may be perfectly current. `image_present` has no entry for a
    tag that errored, so a bare `.get()` silently reads it as missing.
    """
    from orchestrator.api import doctor as doctor_api

    async def _tripwire(image: str, volume: str):
        message = "the agy probe ran against an image nobody could describe"
        raise AssertionError(message)

    def _undescribable(tag: str):
        if tag == "agy-agent:latest":
            message = "500 Server Error: Internal Server Error"
            raise docker.errors.APIError(message)
        return _FakeImage()

    _install_fake_docker(monkeypatch, _undescribable)
    _install_fake_worker_endpoint(monkeypatch, ["qwen3.6-27b"])
    monkeypatch.setattr(doctor_api, "probe_agy_models", _tripwire)
    monkeypatch.setattr(
        client.app.state.effective_settings,
        "auto_delegate_worker",
        lambda: {"harness": "agy", "model": "Gemini 3.7 Flash (High)"},
    )

    body = (await client.get("/api/doctor", headers=auth_headers)).json()

    row = _rows(body)["agy_credentials"]
    assert row["status"] == "amber"
    assert "could not describe" in row["detail"]
    assert "APIError" in row["detail"]
    assert "not built" not in row["detail"]


@pytest.mark.integration
async def test_the_agy_probe_is_reached_when_agy_is_configured_and_built(
    client, auth_headers, monkeypatch
):
    """The gate in the OTHER direction: a gate stuck closed probes nothing.

    Both tests above pass against a `probe_agy_models` that is never called
    under any circumstances, so without this one the whole row could be inert
    and the suite green.
    """
    from orchestrator.api import doctor as doctor_api

    async def _answered(image: str, volume: str):
        assert image == "agy-agent:latest"
        assert volume == "praxis-gemini-creds"
        # The REAL two-column output, not an invented one: the invented
        # fixtures are what hid a classifier that rejected the real thing.
        return doctor_api._AgyProbeResult(
            ran=True,
            output=(
                "Fetching available models...\n"
                "gemini-3.7-flash-high\tGemini 3.7 Flash (High)\n"
                "gemini-3.7-pro-high\tGemini 3.7 Pro (High)\n"
            ),
        )

    _install_fake_docker(monkeypatch, lambda _tag: _FakeImage())
    _install_fake_worker_endpoint(monkeypatch, ["qwen3.6-27b"])
    monkeypatch.setattr(doctor_api, "probe_agy_models", _answered)
    monkeypatch.setattr(
        client.app.state.effective_settings,
        "auto_delegate_worker",
        lambda: {"harness": "agy", "model": "Gemini 3.7 Pro (High)"},
    )

    body = (await client.get("/api/doctor", headers=auth_headers)).json()

    row = _rows(body)["agy_credentials"]
    assert row["status"] == "green"
    assert "2 model(s)" in row["detail"]
    assert "praxis-gemini-creds" in row["detail"]


def test_the_agy_probe_never_mounts_the_creds_volume_writable(monkeypatch):
    """The probe must not be able to corrupt the thing it diagnoses.

    Measured 2026-08-25: one `agy models` run with the volume mounted
    read-write at ~/.gemini (the way a real worker mounts it) creates
    `antigravity-cli/` containing conversation_summaries.db-wal, cli.log and
    installation_id -- on an EMPTY volume, with no authentication at all.
    `docker/agy-agent/entrypoint.sh` gates its "no credentials" warning on
    exactly that directory being non-empty, deliberately, so one doctor run
    silenced the worker's own warning permanently and restored the Go
    stack-trace failure mode that warning exists to prevent.

    So the real volume is mounted READ-ONLY at a side path and a tmpfs stands
    in at ~/.gemini. This asserts the mount SHAPE rather than the outcome,
    because the outcome needs a real daemon: mode "rw" on the volume, or a
    missing tmpfs, is the regression, and either is one keyword away.
    """
    from orchestrator.api import doctor as doctor_api

    captured: dict = {}

    class _Container:
        id = "probe123"

        def wait(self, timeout=None):
            return {"StatusCode": 0}

        def logs(self, **kwargs):
            return b"Fetching available models...\ngemini-x\tGemini X\n"

        def remove(self, force=False):
            captured["removed"] = True

    class _Containers:
        def run(self, image, **kwargs):
            captured["image"] = image
            captured.update(kwargs)
            return _Container()

    class _Client:
        containers = _Containers()

    monkeypatch.setattr(doctor_api.docker, "from_env", lambda: _Client())

    result = doctor_api._run_agy_models("agy-agent:latest", "praxis-gemini-creds")

    assert result.ran
    volumes = captured["volumes"]
    # The creds volume is mounted READ-ONLY, and NOT at the worker's path.
    assert volumes["praxis-gemini-creds"]["mode"] == "ro"
    assert volumes["praxis-gemini-creds"]["bind"] != "/home/agent/.gemini"
    # ~/.gemini is a throwaway tmpfs, so every write agy makes dies with the
    # container instead of landing in the operator's volume.
    assert "/home/agent/.gemini" in captured["tmpfs"]
    # And the copy actually happens, or the probe would answer about an empty
    # home directory and report every authenticated install as signed out.
    assert "cp -R" in captured["command"][1]
    assert captured["removed"] is True


@pytest.mark.integration
async def test_a_project_on_the_agy_harness_puts_the_row_in_play(
    client, auth_headers, monkeypatch, db
):
    """In play is not only "the default worker": a single project counts.

    Reading the default worker alone would leave an install whose default is
    opencode and whose one agy project fails every dispatch with no row about
    it at all.
    """
    from orchestrator.api import doctor as doctor_api
    from tests.conftest import seed_user

    await seed_user(db)
    await _add_project(db, "https://github.com/o/r", "p-agy", harness="agy")

    async def _answered(image: str, volume: str):
        return doctor_api._AgyProbeResult(
            ran=True, output="Please sign in to view available models"
        )

    _install_fake_docker(monkeypatch, lambda _tag: _FakeImage())
    _install_fake_worker_endpoint(monkeypatch, ["qwen3.6-27b"])
    monkeypatch.setattr(doctor_api, "probe_agy_models", _answered)
    monkeypatch.setattr(
        client.app.state.effective_settings,
        "auto_delegate_worker",
        lambda: {"harness": "opencode", "model": "qwen3.6-27b"},
    )

    body = (await client.get("/api/doctor", headers=auth_headers)).json()

    row = _rows(body)["agy_credentials"]
    assert row["status"] == "amber"
    assert (
        "at least one project is configured with the agy harness" not in row["detail"]
    )
    assert "sign-in" in row["detail"]
    assert "-c 'agy'" in row["hint"]


@pytest.mark.integration
async def test_a_local_project_whose_path_is_missing_reds_its_row(
    client, auth_headers, monkeypatch, db
):
    """The 422 that cost the reporter an hour, as a light instead."""
    from tests.conftest import seed_user

    await seed_user(db)
    await _add_project(db, "/repos/nowhere.git", "playground")

    _install_fake_docker(monkeypatch, lambda _tag: _FakeImage())
    _install_fake_worker_endpoint(monkeypatch, ["qwen3.6-27b"])
    _install_dotenv(monkeypatch, {"LOCAL_REPOS_PATH": "/repos"})

    body = (await client.get("/api/doctor", headers=auth_headers)).json()

    row = _rows(body)["local_repo_paths"]
    assert row["status"] == "red"
    assert "playground" in row["detail"]
    assert "/repos/nowhere.git" in row["detail"]
    assert row["hint"]


@pytest.mark.integration
async def test_a_remote_project_never_reaches_the_local_repo_row(
    client, auth_headers, monkeypatch, db
):
    """A GitHub project has no path to resolve, in either namespace."""
    from tests.conftest import seed_user

    await seed_user(db)
    await _add_project(db, "https://github.com/o/r", "remote-only")

    _install_fake_docker(monkeypatch, lambda _tag: _FakeImage())
    _install_fake_worker_endpoint(monkeypatch, ["qwen3.6-27b"])
    _install_dotenv(monkeypatch, {})

    body = (await client.get("/api/doctor", headers=auth_headers)).json()

    row = _rows(body)["local_repo_paths"]
    assert row["status"] == "green"
    assert "not applicable" in row["detail"]


@pytest.mark.integration
async def test_the_half_set_local_repos_config_is_read_from_the_mounted_env(
    client, auth_headers, monkeypatch
):
    """LOCAL_REPOS_* reach this process ONLY through the mounted `.env`.

    compose reads them on the host to build a volume mapping and forwards
    neither into the container, so a gathering layer that consulted
    `os.environ` alone would report every install as having them unset and
    this row could never fire.
    """
    _install_fake_docker(monkeypatch, lambda _tag: _FakeImage())
    _install_fake_worker_endpoint(monkeypatch, ["qwen3.6-27b"])
    _install_dotenv(monkeypatch, {"LOCAL_REPOS_HOST_PATH": "C:/Users/me/repos"})

    body = (await client.get("/api/doctor", headers=auth_headers)).json()

    row = _rows(body)["local_repo_paths"]
    assert row["status"] == "red"
    assert "C:/Users/me/repos" in row["detail"]
    assert ".local-repos-unused" in row["detail"]


@pytest.mark.integration
async def test_container_identity_names_the_owner_of_the_configured_name(
    client, auth_headers, monkeypatch
):
    """A bare process beside a container that owns the name.

    `_in_container` is False in the suite, so this is the state an operator
    running `uv run uvicorn` in one checkout while another checkout's stack is
    up actually sees.
    """
    _install_fake_docker_with_containers(
        monkeypatch,
        lambda _tag: _FakeImage(),
        {
            "orchestrator": _FakeContainer(
                "def456", "orchestrator", "praxis", "/c/working-space/praxis"
            )
        },
    )
    _install_fake_worker_endpoint(monkeypatch, ["qwen3.6-27b"])
    _install_dotenv(monkeypatch, {})

    body = (await client.get("/api/doctor", headers=auth_headers)).json()

    row = _rows(body)["container_identity"]
    assert row["status"] == "amber"
    assert "/c/working-space/praxis" in row["detail"]
    assert "'praxis'" in row["detail"]


@pytest.mark.integration
async def test_the_container_name_is_read_from_the_mounted_env_too(
    client, auth_headers, monkeypatch
):
    """PRAXIS_CONTAINER_NAME is a compose substitution variable, same as above.

    With the default `orchestrator` hardcoded here, a checkout that set its
    own name would have this row answer about a container it deliberately does
    not own, which is the opposite of the fact it exists to establish.
    """
    _install_fake_docker_with_containers(
        monkeypatch,
        lambda _tag: _FakeImage(),
        {
            "orchestrator": _FakeContainer(
                "def456", "orchestrator", "praxis", "/c/working-space/praxis"
            )
        },
    )
    _install_fake_worker_endpoint(monkeypatch, ["qwen3.6-27b"])
    _install_dotenv(monkeypatch, {"PRAXIS_CONTAINER_NAME": "orchestrator-b"})

    body = (await client.get("/api/doctor", headers=auth_headers)).json()

    row = _rows(body)["container_identity"]
    # Nothing holds `orchestrator-b`, and the container named `orchestrator`
    # is somebody else's business now.
    assert row["status"] == "green"
    assert "orchestrator-b" in row["detail"]
    assert "not applicable" in row["detail"]


@pytest.mark.integration
async def test_doctor_reds_planner_cli_when_the_round_trip_is_refused(
    client, auth_headers, monkeypatch
):
    """The check must go red when prompts are blocked, not stay green.

    Walkthrough #4: this row printed OK while every brain call in the container
    was refused, and the operator found out mid-plan instead.
    """
    from orchestrator.api import doctor as doctor_api
    from orchestrator.api.system import RoundTripResult

    async def fake_provider(name: str) -> dict:
        return {"cli_available": True, "authenticated": True}

    async def fake_roundtrip(name: str) -> RoundTripResult:
        return RoundTripResult(ok=False, error="Blocked by policy hook")

    monkeypatch.setattr(doctor_api, "_probe_provider", fake_provider)
    monkeypatch.setattr(doctor_api, "probe_provider_roundtrip", fake_roundtrip)

    response = await client.get("/api/doctor", headers=auth_headers)

    assert response.status_code == 200
    check = next(c for c in response.json()["checks"] if c["check_id"] == "planner_cli")
    assert check["status"] == "red"
    assert check["hint"]
    # The gathering layer must carry the CLI's own words through to the row.
    assert "Blocked by policy hook" in check["detail"]


@pytest.mark.integration
async def test_doctor_ambers_planner_cli_when_the_subscription_is_rate_limited(
    client, auth_headers, monkeypatch
):
    """A throttled subscription must not fail the install.

    `src/cli/init.py` ends with `raise typer.Exit(code=_run_doctor(...))`, so a
    red here means a newcomer who happens to be rate limited gets a failed
    setup plus a hint pointing at a hook that is not there.
    """
    from orchestrator.api import doctor as doctor_api
    from orchestrator.api.system import RoundTripResult

    async def fake_provider(name: str) -> dict:
        return {"cli_available": True, "authenticated": True}

    async def fake_roundtrip(name: str) -> RoundTripResult:
        return RoundTripResult(
            ok=None, rate_limited=True, error="Claude usage limit reached"
        )

    monkeypatch.setattr(doctor_api, "_probe_provider", fake_provider)
    monkeypatch.setattr(doctor_api, "probe_provider_roundtrip", fake_roundtrip)

    response = await client.get("/api/doctor", headers=auth_headers)

    check = next(c for c in response.json()["checks"] if c["check_id"] == "planner_cli")
    assert check["status"] == "amber"
    assert "rate limited" in check["detail"].lower()


@pytest.mark.integration
async def test_the_suite_never_spends_a_live_planner_round_trip(
    client, auth_headers, monkeypatch
):
    """The `no_live_planner_round_trip` autouse fixture, pinned.

    Nothing else in the suite asserts a non-red `planner_cli`, so without this
    test the fixture could return the wrong value, or be deleted outright, and
    every check would still pass while the suite quietly spent a real
    subscription call on every doctor request.

    The DETAIL is asserted, not just the status: "no test prompt was made" is
    the pre-round-trip verdict and is reachable only from `prompt_ok=None`.
    `True` would say "answering prompts" and `False` would go red, so this one
    assertion pins the fixture to exactly "not probed".

    The row now also names the planner it resolved, which is the whole point of
    the configured-planner probe, so the assertion is a suffix match rather than
    an equality: pinning the resolved model string here would re-pin the
    settings YAML's role chain in a test about the fixture.
    `tests/test_doctor_probes_configured_planner.py` is where the naming itself
    is pinned.

    UPDATED: this pinned `status == "green"` and the words "installed and
    authenticated". Both were claims nothing measured: no round trip was made,
    and `claude` has no auth command, so the row is now amber and says what it
    actually established. The fixture pin itself is unchanged, and the suffix
    asserted below is reachable ONLY from `prompt_ok=None` with no rate limit.
    """
    from orchestrator.api import doctor as doctor_api

    async def fake_provider(name: str) -> dict:
        return {"cli_available": True, "authenticated": True}

    monkeypatch.setattr(doctor_api, "_probe_provider", fake_provider)

    response = await client.get("/api/doctor", headers=auth_headers)

    check = next(c for c in response.json()["checks"] if c["check_id"] == "planner_cli")
    assert check["status"] == "amber"
    assert check["detail"].endswith(
        "no test prompt was made, so nothing here established that it can answer one"
    )
    assert check["detail"].startswith("planner claude/")
