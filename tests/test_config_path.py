"""The YAML path is resolvable, so a mount replaces an image rebuild.

Found live 2026-07-27: editing config/praxis.yaml had no effect until the
orchestrator image was rebuilt, because dev compose mounts src/, web/, .git/,
and data/ but not config/, and the YAML is baked in at build time.

The resolver alone does not retire that bug: without the compose mount the
process resolves a path that is still the image-baked copy, and every Python
test stays green. So the compose files are asserted here too, parsed as YAML
rather than via `docker compose config`, which keeps this runnable with no
Docker installed.
"""

from pathlib import Path

import pytest
import yaml

from orchestrator.core.settings_file import config_file_path, load_yaml_settings


REPO = Path(__file__).resolve().parents[1]
BASE_COMPOSE = yaml.safe_load((REPO / "docker-compose.yml").read_text(encoding="utf-8"))
DEV_COMPOSE = yaml.safe_load(
    (REPO / "docker-compose.local.yml").read_text(encoding="utf-8")
)

CONFIG_MOUNT_TARGET = "/app/config"

# The harness entrypoint sources, pinned here for the same reason as the config
# mount: `praxis doctor`'s agent_image_freshness check stats them to prove each
# agent image was built after its entrypoint last changed, the orchestrator
# image does not COPY docker/, and deleting the mount turns the check inert
# with every Python test still green.
DOCKER_MOUNT_TARGET = "/app/docker"


def _mounts(compose: dict) -> list[tuple[str, str, str]]:
    """Return the orchestrator's short-form volumes as (source, target, mode).

    ``mode`` is "" when the entry omits it.  Sources like
    ``${SSH_KEY_PATH:-~/.ssh}`` contain colons, so this splits from the right.
    """
    parsed: list[tuple[str, str, str]] = []
    for entry in compose["services"]["orchestrator"].get("volumes", []):
        head, _, tail = entry.rpartition(":")
        if tail in ("ro", "rw"):
            source, _, target = head.rpartition(":")
            parsed.append((source, target, tail))
        else:
            parsed.append((head, tail, ""))
    return parsed


def _merged_mounts() -> dict[str, tuple[str, str]]:
    """Volumes as `docker compose -f base -f local` resolves them.

    Compose merges volumes by TARGET with the later file winning, so the dev
    stack inherits the base mount even though the dev file also states it.
    """
    merged: dict[str, tuple[str, str]] = {}
    for compose in (BASE_COMPOSE, DEV_COMPOSE):
        for source, target, mode in _mounts(compose):
            merged[target] = (source, mode)
    return merged


def _environment(compose: dict) -> dict[str, str]:
    """Return the orchestrator's `KEY=value` environment list as a dict."""
    env = compose["services"]["orchestrator"].get("environment", [])
    if isinstance(env, dict):
        return {key: "" if value is None else str(value) for key, value in env.items()}
    return dict(entry.split("=", 1) for entry in env)


@pytest.mark.unit
def test_the_default_path_is_the_repo_relative_config(monkeypatch):
    """Exact, not a suffix: an absolute default would escape the repo silently.

    `.endswith("config/praxis.yaml")` also accepts `/etc/praxis/config/praxis.yaml`,
    which resolves outside both the repo and the container mount.
    """
    monkeypatch.delenv("PRAXIS_CONFIG_PATH", raising=False)
    assert config_file_path() == "config/praxis.yaml"


@pytest.mark.unit
def test_an_env_override_wins(monkeypatch, tmp_path):
    target = tmp_path / "elsewhere.yaml"
    monkeypatch.setenv("PRAXIS_CONFIG_PATH", str(target))
    assert config_file_path() == str(target)


@pytest.mark.unit
def test_settings_load_from_the_overridden_path(monkeypatch, tmp_path):
    target = tmp_path / "elsewhere.yaml"
    target.write_text("default_worker_model: from-the-mount\n", encoding="utf-8")
    monkeypatch.setenv("PRAXIS_CONFIG_PATH", str(target))
    assert load_yaml_settings(config_file_path())["default_worker_model"] == (
        "from-the-mount"
    )


@pytest.mark.unit
def test_a_missing_file_yields_empty_settings_not_a_crash(monkeypatch, tmp_path):
    """A fresh clone with no config file must still boot.

    `env={}` because the ambient environment is not this test's subject: CI
    exports `PRAXIS_BUILD_SHA=$GITHUB_SHA` (see .env.example), and any exported
    `PRAXIS_*` var lands in the overlay and breaks a bare `== {}`.
    """
    monkeypatch.setenv("PRAXIS_CONFIG_PATH", str(tmp_path / "absent.yaml"))
    assert load_yaml_settings(config_file_path(), env={}) == {}


@pytest.mark.unit
def test_only_the_exact_config_path_key_is_kept_out_of_the_overlay(tmp_path):
    """The pointer to the file is excluded; every other PRAXIS_* var is a setting.

    The exclusion must be an exact key match. `endswith("_PATH")` would also
    swallow `PRAXIS_MEMORY_MD_PATH`, and `memory_md_path` is a real `Settings`
    field; `startswith("PRAXIS_CONFIG")` would swallow any future
    `PRAXIS_CONFIG_*` setting. Both weakenings drop live configuration with no
    error, so this pins the exclusion AND its exactness.
    """
    settings = load_yaml_settings(
        str(tmp_path / "absent.yaml"),
        env={
            "PRAXIS_CONFIG_PATH": "/app/config/praxis.yaml",
            "PRAXIS_MEMORY_MD_PATH": "docs/OTHER.md",
            "PRAXIS_CONFIG_RELOAD": "true",
        },
    )
    assert settings == {"memory_md_path": "docs/OTHER.md", "config_reload": True}


@pytest.mark.unit
def test_settings_resolves_the_config_path_at_call_time(monkeypatch, tmp_path):
    """A Settings() built after the env var is set must read the new file.

    Resolving the path in a keyword-argument DEFAULT would freeze it at import
    time, so the container would keep reading the baked-in copy no matter what
    ``PRAXIS_CONFIG_PATH`` said.
    """
    from orchestrator.config import Settings

    target = tmp_path / "elsewhere.yaml"
    target.write_text("default_worker_model: from-the-mount\n", encoding="utf-8")
    monkeypatch.setenv("PRAXIS_CONFIG_PATH", str(target))
    monkeypatch.delenv("DEFAULT_WORKER_MODEL", raising=False)

    assert Settings(auth_token="t", _env_file=None).default_worker_model == (
        "from-the-mount"
    )


@pytest.mark.unit
async def test_effective_settings_reads_the_overridden_path(db, monkeypatch, tmp_path):
    """No caller may hardcode 'config/praxis.yaml' any more."""
    target = tmp_path / "elsewhere.yaml"
    target.write_text("escalation:\n  policy: paid_fallback\n", encoding="utf-8")
    monkeypatch.setenv("PRAXIS_CONFIG_PATH", str(target))

    from orchestrator.config import Settings
    from orchestrator.core.effective_settings import EffectiveSettings

    settings = EffectiveSettings(Settings(auth_token="t", _env_file=None), db)
    assert await settings.escalation_policy(None) == "paid_fallback"


@pytest.mark.unit
def test_the_base_compose_bind_mounts_the_config_directory_read_only():
    """Without this line the container reads the image-baked copy again.

    Deleting it is invisible to every other test in the suite while silently
    restoring the 2026-07-27 bug, which is why it is pinned here.  Read-only
    because the orchestrator must never write an operator's settings file.
    """
    assert ("./config", CONFIG_MOUNT_TARGET, "ro") in _mounts(BASE_COMPOSE)


@pytest.mark.unit
def test_the_base_compose_points_the_resolver_inside_that_mount():
    """The env var and the mount target must agree, or the mount is dead weight."""
    value = _environment(BASE_COMPOSE)["PRAXIS_CONFIG_PATH"]
    assert value.startswith(f"{CONFIG_MOUNT_TARGET}/")
    assert value.rsplit("/", 1)[-1] == "praxis.yaml"


@pytest.mark.unit
def test_the_dev_stack_gets_the_config_mount_too():
    """Asserted on the MERGED volumes, which is what compose actually runs.

    The dev file states the mount itself, but compose merges by target so the
    base entry already covers the dev stack; either source satisfies this.
    """
    assert _merged_mounts()[CONFIG_MOUNT_TARGET] == ("./config", "ro")


@pytest.mark.unit
def test_the_dev_stack_resolves_into_that_mount():
    """Compose merges `environment` key-wise, so dev inherits the base value."""
    base = _environment(BASE_COMPOSE)
    effective = _environment(DEV_COMPOSE).get(
        "PRAXIS_CONFIG_PATH", base["PRAXIS_CONFIG_PATH"]
    )
    assert effective.startswith(f"{CONFIG_MOUNT_TARGET}/")


@pytest.mark.unit
def test_the_base_compose_bind_mounts_the_entrypoint_sources_read_only():
    """Without this line agent_image_freshness compares nothing, forever.

    The orchestrator Dockerfile copies src/, web/ and config/ but not docker/,
    so in a container `_entrypoint_mtimes()` can only see the entrypoints
    through this mount. Read-only because doctor never does more than stat
    them.
    """
    assert ("./docker", DOCKER_MOUNT_TARGET, "ro") in _mounts(BASE_COMPOSE)


@pytest.mark.unit
def test_the_dev_stack_gets_the_entrypoint_mount_too():
    """Asserted on the MERGED volumes, which is what compose actually runs."""
    assert _merged_mounts()[DOCKER_MOUNT_TARGET] == ("./docker", "ro")


@pytest.mark.unit
def test_the_entrypoint_mount_lands_where_the_gathering_code_looks():
    """The mount target and the path doctor stats must agree.

    The orchestrator image sets `WORKDIR /app`, so the CWD-relative path in
    `api/doctor._ENTRYPOINT_ROOT` resolves to the mount target above inside a
    container and to the checkout for a bare `uv run uvicorn` from the repo
    root. Either one drifting makes the mount dead weight.
    """
    from orchestrator.api.doctor import _ENTRYPOINT_ROOT

    assert f"/app/{_ENTRYPOINT_ROOT.as_posix()}" == DOCKER_MOUNT_TARGET


@pytest.mark.unit
def test_no_module_hardcodes_the_config_path():
    """Grep guard: one resolver, no scattered literals."""
    from pathlib import Path

    src = Path(__file__).resolve().parents[1] / "src"
    offenders = [
        path
        for path in src.rglob("*.py")
        if path.name != "settings_file.py"
        and "config/praxis.yaml" in path.read_text(encoding="utf-8")
    ]
    assert offenders == [], f"hardcoded config path in: {offenders}"
