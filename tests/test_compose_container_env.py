"""A remedy that dirties a tracked file is a remedy nobody follows twice.

`~/.claude` is mounted into the orchestrator, so the host's Claude Code hooks
run INSIDE the container. A hook whose detector assumes the host OS (a VPN
killswitch is the one that keeps happening) blocks every brain call, tunnel up
or down, and `praxis doctor` reports the planner RED carrying the hook's own
message. The opt-out variable's NAME differs per hook, so it cannot be
enumerated in compose, and the documented remedy was "add a literal to
docker-compose.yml", which leaves a permanent local diff in a fresh clone.

`.env.container` is a gitignored `env_file`. Unlike `.env`, which compose
reads on the HOST for substitution and never passes in on its own, an
`env_file` hands every key to the process. Three things have to hold together
or the remedy is inert again, and each is asserted separately below: the
declaration, the gitignore entry, and the dev stack inheriting it.

Parsed as YAML rather than through `docker compose config`, so this runs with
no Docker installed, the same choice `tests/test_config_path.py` makes.
"""
# ruff: noqa: S101

from pathlib import Path

import pytest
import yaml


REPO = Path(__file__).resolve().parents[1]
BASE = yaml.safe_load((REPO / "docker-compose.yml").read_text(encoding="utf-8"))
DEV = yaml.safe_load((REPO / "docker-compose.local.yml").read_text(encoding="utf-8"))

CONTAINER_ENV_FILE = "./.env.container"


def _env_file_entries(compose: dict, service: str) -> list[dict]:
    """Return a service's `env_file` entries in compose's long form.

    The short form is a bare string; the long form is a mapping with `path`
    and `required`. Normalized here so an assertion does not have to care
    which form the file happens to use.
    """
    raw = compose["services"][service].get("env_file") or []
    if isinstance(raw, str):
        raw = [raw]
    return [
        {"path": entry, "required": True} if isinstance(entry, str) else dict(entry)
        for entry in raw
    ]


@pytest.mark.unit
def test_the_orchestrator_reads_a_container_only_env_file() -> None:
    """Without this declaration the hook opt-out has nowhere to live."""
    paths = [entry["path"] for entry in _env_file_entries(BASE, "orchestrator")]
    assert CONTAINER_ENV_FILE in paths


@pytest.mark.unit
def test_the_container_env_file_is_optional() -> None:
    """An install that never needed one must still start.

    `required: true` (compose's default for a bare string entry) turns a
    missing file into a hard startup failure, which would make the fix worse
    than the problem it replaces: every fresh clone would refuse to boot.
    """
    entry = next(
        e
        for e in _env_file_entries(BASE, "orchestrator")
        if e["path"] == CONTAINER_ENV_FILE
    )
    assert entry.get("required") is False


@pytest.mark.unit
def test_the_dev_stack_inherits_it() -> None:
    """Compose merges service keys, so the dev overlay must not redeclare
    `env_file` in a way that drops the base entry.

    If it ever does, the remedy works under `docker compose up` and silently
    does nothing under the documented dev command, which is the stack an
    operator debugging a blocked planner is most likely to be running.
    """
    override = DEV["services"]["orchestrator"].get("env_file")
    if override is None:
        return
    paths = [entry["path"] for entry in _env_file_entries(DEV, "orchestrator")]
    assert CONTAINER_ENV_FILE in paths


@pytest.mark.unit
def test_the_container_env_file_is_gitignored() -> None:
    """The whole point: following the remedy must leave the tree clean."""
    ignored = (REPO / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert ".env.container" in [line.strip() for line in ignored]


@pytest.mark.unit
def test_the_doctor_names_the_file_compose_actually_reads() -> None:
    """The remedy and the mechanism have to be one change, not two.

    The doctor's hook remedy and this compose declaration are the two halves
    of one instruction, written in different files by different hands, and a
    remedy naming a file compose does not read is the same dead end as a
    remedy naming no file at all. Asserted from the compose file's own
    `env_file` entry rather than from a literal, so the two cannot drift.
    """
    from orchestrator.core.doctor_probes import _HOOK_REMEDY

    declared = Path(CONTAINER_ENV_FILE).name

    assert declared in _HOOK_REMEDY, (
        f"the doctor's hook remedy does not name {declared}, the file compose "
        f"declares as its env_file:\n{_HOOK_REMEDY}"
    )
    assert "docker-compose.yml" not in _HOOK_REMEDY, (
        "the remedy sends the operator to edit a TRACKED file, which leaves a "
        "permanent local diff in a fresh clone"
    )


@pytest.mark.unit
def test_compose_forwards_domain_to_caddy() -> None:
    """`docker/caddy/Caddyfile` is `{$DOMAIN:localhost}`, read from CADDY's
    own environment.

    The caddy service had no environment block at all, so the documented
    `DOMAIN=praxis.example.com docker compose --profile hosted up` produced a
    site bound to localhost. Auto-HTTPS then cannot be issued for the real
    hostname, and the only symptom is a certificate that never arrives.
    """
    environment = BASE["services"]["caddy"].get("environment") or []
    if isinstance(environment, dict):
        keys = set(environment)
    else:
        keys = {entry.split("=", 1)[0] for entry in environment}
    assert "DOMAIN" in keys


@pytest.mark.unit
def test_the_caddyfile_reads_the_variable_compose_forwards() -> None:
    """The forwarding and the Caddyfile's placeholder must name the same var."""
    caddyfile = (REPO / "docker" / "caddy" / "Caddyfile").read_text(encoding="utf-8")
    assert "{$DOMAIN" in caddyfile
