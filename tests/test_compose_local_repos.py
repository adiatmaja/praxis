"""The local-repo bind mount must satisfy two namespaces, not one.

`core/preflight._preflight_local` checks a project's local `repo_url` with
`Path.exists()` INSIDE the orchestrator container. `core/agent_manager.
local_repo_volume` then hands that same string to the Docker daemon as a
bind-mount SOURCE for a spawned worker, which the daemon resolves in the HOST
(or, on Docker Desktop, the Linux VM) namespace, not the orchestrator's own.
On Linux those namespaces coincide and nobody notices. On Docker Desktop for
Windows nothing satisfies both at once, so this needs two variables:
`LOCAL_REPOS_HOST_PATH` (the bind source) and `LOCAL_REPOS_PATH` (the mount
target, and the required `repo_url` prefix).

Compose has no conditional volumes, so the mount defaults both variables to a
harmless no-op (`./docker`, already read elsewhere in this file, onto an
unused container path) when neither is set. The acceptance test for that
degenerate form is `docker compose config` succeeding BOTH with the variables
unset and with them set -- exercised for real here (skipped when no docker
binary is on PATH), plus a YAML-only check of the authored defaults that runs
with no Docker installed, matching the pattern in `tests/test_config_path.py`.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml


REPO = Path(__file__).resolve().parents[1]
BASE = yaml.safe_load((REPO / "docker-compose.yml").read_text(encoding="utf-8"))

_DOCKER = shutil.which("docker")


def _split_top_level(entry: str, sep: str = ":") -> list[str]:
    """Split ``entry`` on ``sep``, but never inside a ``${...}`` block.

    ``tests/test_config_path.py``'s simple ``rpartition(":")`` helper is
    correct only when at most one side of a volume entry contains a
    ``${VAR:-default}`` substitution -- the ``:-`` inside that construct is
    itself a colon. This mount has ONE on each side
    (``${LOCAL_REPOS_HOST_PATH:-./docker}:${LOCAL_REPOS_PATH:-...}``), so a
    right-to-left split lands inside the second variable reference instead of
    at the source/target boundary. Tracking brace depth makes every colon
    inside a ``${...}`` block invisible to the split.
    """
    parts: list[str] = []
    buf: list[str] = []
    depth = 0
    for ch in entry:
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        if ch == sep and depth == 0:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    parts.append("".join(buf))
    return parts


def _mounts(compose: dict) -> list[tuple[str, str, str]]:
    """Return the orchestrator's short-form volumes as (source, target, mode)."""
    parsed: list[tuple[str, str, str]] = []
    for entry in compose["services"]["orchestrator"].get("volumes", []):
        pieces = _split_top_level(entry)
        if len(pieces) == 3:
            parsed.append((pieces[0], pieces[1], pieces[2]))
        else:
            parsed.append((pieces[0], pieces[1], ""))
    return parsed


@pytest.mark.unit
def test_container_name_uses_the_env_var_with_orchestrator_default():
    """The default must be UNCHANGED: every doc says `docker logs orchestrator`."""
    assert (
        BASE["services"]["orchestrator"]["container_name"]
        == "${PRAXIS_CONTAINER_NAME:-orchestrator}"
    )


@pytest.mark.unit
def test_local_repos_mount_defaults_to_a_harmless_noop():
    """Unset, this must mount something that exists and nothing reads/writes.

    ``./docker`` is already bind-mounted elsewhere in this file (read-only),
    so reusing it as the fallback SOURCE guarantees the directory exists in
    every checkout. The fallback TARGET is a path no code touches, so the
    mount is inert rather than merely harmless-looking.
    """
    mounts = _mounts(BASE)
    match = [
        (source, target, mode)
        for source, target, mode in mounts
        if source == "${LOCAL_REPOS_HOST_PATH:-./docker}"
    ]
    assert len(match) == 1, (
        f"expected exactly one LOCAL_REPOS_HOST_PATH volume entry, found "
        f"{len(match)} in {mounts}"
    )
    _source, target, mode = match[0]
    assert target == "${LOCAL_REPOS_PATH:-/app/.local-repos-unused}"
    # Read-write, never :ro: LocalGitBackend pushes to the bare repo directly
    # from inside the orchestrator, not only from a spawned worker.
    assert mode != "ro"


@pytest.mark.unit
def test_local_repos_mount_is_never_given_a_compose_default_for_a_yaml_key():
    """Neither variable is a `config/praxis.yaml` setting, so `${VAR:-x}` is
    fine here -- this is the opposite case from the worker-preset vars, and
    the test exists so the two are not conflated by a future edit.
    """
    from orchestrator.config import Settings

    fields = set(Settings.model_fields)
    assert "local_repos_host_path" not in fields
    assert "local_repos_path" not in fields


@pytest.mark.unit
@pytest.mark.parametrize(
    "var", ["LOCAL_REPOS_HOST_PATH", "LOCAL_REPOS_PATH", "PRAXIS_CONTAINER_NAME"]
)
def test_env_example_documents_the_new_variables(var):
    contents = (REPO / ".env.example").read_text(encoding="utf-8")
    assert var in contents


@pytest.mark.skipif(_DOCKER is None, reason="docker not on PATH")
def test_docker_compose_config_is_valid_with_the_new_vars_unset(monkeypatch):
    """The acceptance test for the degenerate mount form: it must not break a
    plain `docker compose up` for every existing user who has never heard of
    the new variables.
    """
    monkeypatch.delenv("LOCAL_REPOS_HOST_PATH", raising=False)
    monkeypatch.delenv("LOCAL_REPOS_PATH", raising=False)
    monkeypatch.delenv("PRAXIS_CONTAINER_NAME", raising=False)
    result = subprocess.run(  # noqa: S603
        [_DOCKER, "compose", "config", "--quiet"],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(_DOCKER is None, reason="docker not on PATH")
def test_docker_compose_config_is_valid_with_the_new_vars_set(monkeypatch):
    """Set to real Docker-Desktop-shaped values (a drive letter in the SOURCE,
    which itself contains a colon) to prove compose's volume-string parser
    does not misparse the extra colon as a mode separator.
    """
    monkeypatch.setenv("LOCAL_REPOS_HOST_PATH", "C:/Users/me/repos")
    monkeypatch.setenv("LOCAL_REPOS_PATH", "/run/desktop/mnt/host/c/Users/me/repos")
    monkeypatch.setenv("PRAXIS_CONTAINER_NAME", "praxis-test")
    result = subprocess.run(  # noqa: S603
        [_DOCKER, "compose", "config"],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr

    resolved = yaml.safe_load(result.stdout)
    orch = resolved["services"]["orchestrator"]
    assert orch["container_name"] == "praxis-test"
    bind_entries = [
        v
        for v in orch["volumes"]
        if isinstance(v, dict)
        and v.get("target") == "/run/desktop/mnt/host/c/Users/me/repos"
    ]
    assert len(bind_entries) == 1, bind_entries
    assert bind_entries[0]["source"] == "C:/Users/me/repos"
