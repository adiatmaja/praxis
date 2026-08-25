"""The local-repo bind mount must satisfy two namespaces, not one.

`core/preflight._preflight_local` checks a project's local `repo_url` with
`Path.exists()` INSIDE the orchestrator container. `core/agent_manager.
local_repo_volume` then hands that same string to the Docker daemon as a
bind-mount SOURCE for a spawned worker, which the daemon resolves in the HOST
(or, on Docker Desktop, the Linux VM) namespace, not the orchestrator's own.
On Linux those namespaces coincide and nobody notices; on Docker Desktop for
Windows an IDENTITY mount still works, because `/run/desktop/mnt/host/
<drive>/...` is valid simultaneously as a daemon bind source and as a
container path (verified live against Docker 29.6.1 / Compose v5.3.0 -- an
earlier draft of this fix claimed otherwise and was wrong). So the compose
mount's `LOCAL_REPOS_HOST_PATH` variable defaults to `LOCAL_REPOS_PATH`'s
value via a nested compose default, making the common case a single
variable, with `LOCAL_REPOS_HOST_PATH` as an escape hatch for a genuinely
different bind source.

Compose has no conditional volumes, so unset, both variables fall through to
`praxis_local_repos_unused`, an EMPTY NAMED VOLUME -- never a host bind. An
earlier version of this mount defaulted to bind-mounting `./docker` (already
mounted read-only elsewhere in this file for the entrypoint-freshness check)
onto an unused container path with NO `read_only`, which made that host
directory writable from a second mount and, from inside the container, from
a process running as root. The regression test for that is
`test_no_mount_shares_a_source_with_a_read_only_mount` below, run for real
against `docker compose config` because the defect is only visible in the
RESOLVED config, not in the authored YAML text.

Every `docker compose config` invocation here is pinned to `docker-compose.yml`
explicitly and to an empty `--env-file`: without both, compose auto-merges
`./.env` AND the untracked `docker-compose.override.yml` that can exist in a
working tree (it does in this one), silently turning an intended "vars unset"
run into a "vars set from someplace else" run.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from tests.test_config_path import _mounts


REPO = Path(__file__).resolve().parents[1]
BASE = yaml.safe_load((REPO / "docker-compose.yml").read_text(encoding="utf-8"))

_DOCKER = shutil.which("docker")

#: The three variables this task adds, all optional and all absent from a
#: default install's environment.
_NEW_VARS = ("LOCAL_REPOS_HOST_PATH", "LOCAL_REPOS_PATH", "PRAXIS_CONTAINER_NAME")


def _compose_config(
    tmp_path: Path, env_overrides: dict[str, str], *, quiet: bool = False
) -> subprocess.CompletedProcess[str]:
    """Run `docker compose config` pinned to the shipped file, isolated from
    both the real `.env` and any `docker-compose.override.yml` on disk.

    Passing `-f docker-compose.yml` explicitly is what excludes the override
    file: compose only auto-appends `docker-compose.override.yml` when NO
    `-f` is given at all. `--env-file` at an empty, test-owned path is what
    excludes the real `./.env`, which compose otherwise reads unconditionally
    regardless of the *process* environment `monkeypatch`/`env=` controls.
    """
    empty_env_file = tmp_path / "empty.env"
    empty_env_file.write_text("", encoding="utf-8")
    env = dict(os.environ)
    for var in _NEW_VARS:
        env.pop(var, None)
    env.update(env_overrides)
    args = [
        _DOCKER,
        "compose",
        "-f",
        "docker-compose.yml",
        "--env-file",
        str(empty_env_file),
    ]
    args += ["config", "--quiet"] if quiet else ["config"]
    return subprocess.run(  # noqa: S603
        args, cwd=REPO, capture_output=True, text=True, timeout=60, env=env
    )


@pytest.mark.unit
def test_container_name_uses_the_env_var_with_orchestrator_default():
    """The default must be UNCHANGED: every doc says `docker logs orchestrator`."""
    assert (
        BASE["services"]["orchestrator"]["container_name"]
        == "${PRAXIS_CONTAINER_NAME:-orchestrator}"
    )


@pytest.mark.unit
def test_local_repos_mount_default_source_is_a_declared_named_volume():
    """Unset, the SOURCE must resolve to a named volume, never a host bind.

    A bind default here exposes whatever host directory it names, read-write,
    to every operator who never heard of these variables -- which is exactly
    what the previous draft of this mount did (defaulted to `./docker`, a
    directory already mounted read-only elsewhere in this file for a
    different purpose). Checking the literal default token AND that it is
    declared under the top-level `volumes:` key is what pins it to a volume:
    an undeclared name would still look like a host path to compose.
    """
    mounts = _mounts(BASE)
    match = [
        (source, target, mode)
        for source, target, mode in mounts
        if "LOCAL_REPOS_HOST_PATH" in source
    ]
    assert len(match) == 1, (
        f"expected exactly one LOCAL_REPOS_HOST_PATH volume entry, found "
        f"{len(match)} in {mounts}"
    )
    source, target, mode = match[0]
    assert source == (
        "${LOCAL_REPOS_HOST_PATH:-${LOCAL_REPOS_PATH:-praxis_local_repos_unused}}"
    )
    assert target == "${LOCAL_REPOS_PATH:-/app/.local-repos-unused}"
    assert "praxis_local_repos_unused" in BASE["volumes"], (
        "the mount's default source names a volume that isn't declared under "
        "the top-level `volumes:` key, so compose would treat it as a bind "
        "path instead of a named volume"
    )
    # Read-write when a real path IS given: LocalGitBackend pushes to the
    # bare repo directly from inside the orchestrator, not only from a
    # spawned worker.
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
@pytest.mark.parametrize("var", _NEW_VARS)
def test_env_example_documents_the_new_variables(var):
    contents = (REPO / ".env.example").read_text(encoding="utf-8")
    assert var in contents


@pytest.mark.skipif(_DOCKER is None, reason="docker not on PATH")
def test_docker_compose_config_is_valid_with_the_new_vars_unset(tmp_path):
    """The acceptance test for the degenerate mount form: it must not break a
    plain `docker compose up` for every existing user who has never heard of
    the new variables.
    """
    result = _compose_config(tmp_path, {}, quiet=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(_DOCKER is None, reason="docker not on PATH")
def test_docker_compose_config_is_valid_with_the_new_vars_set(tmp_path):
    """Set to real Docker-Desktop-shaped values (a drive letter in the SOURCE,
    which itself contains a colon) to prove compose's volume-string parser
    does not misparse the extra colon as a mode separator, and that setting
    BOTH variables to genuinely different values still resolves as
    (host path) -> (VM share path), the escape-hatch case.
    """
    result = _compose_config(
        tmp_path,
        {
            "LOCAL_REPOS_HOST_PATH": "C:/Users/me/repos",
            "LOCAL_REPOS_PATH": "/run/desktop/mnt/host/c/Users/me/repos",
            "PRAXIS_CONTAINER_NAME": "praxis-test",
        },
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


@pytest.mark.skipif(_DOCKER is None, reason="docker not on PATH")
def test_docker_compose_config_resolves_the_identity_case_with_one_variable(
    tmp_path,
):
    """The design point of finding 2: setting ONLY `LOCAL_REPOS_PATH` (the
    normal case, including on Docker Desktop) must resolve source == target,
    an identity bind mount, because `LOCAL_REPOS_HOST_PATH`'s nested default
    falls through to `LOCAL_REPOS_PATH`'s value. If the nested default were
    ever flattened back to a plain no-op default, this is what would break:
    the single-variable case would silently mount the unused named volume
    instead of the real repo.
    """
    result = _compose_config(
        tmp_path, {"LOCAL_REPOS_PATH": "/run/desktop/mnt/host/c/Users/me/repos"}
    )
    assert result.returncode == 0, result.stderr

    resolved = yaml.safe_load(result.stdout)
    orch = resolved["services"]["orchestrator"]
    bind_entries = [
        v
        for v in orch["volumes"]
        if isinstance(v, dict)
        and v.get("target") == "/run/desktop/mnt/host/c/Users/me/repos"
    ]
    assert len(bind_entries) == 1, bind_entries
    assert bind_entries[0]["source"] == "/run/desktop/mnt/host/c/Users/me/repos"


@pytest.mark.skipif(_DOCKER is None, reason="docker not on PATH")
def test_no_mount_shares_a_source_with_a_read_only_mount(tmp_path):
    """The real invariant, checked against the RESOLVED config, not authored
    YAML text: no volume's source may be mounted read-only in one entry and
    read-write (or without `read_only`) in another.

    This is only visible after compose resolves variable defaults -- the
    authored YAML never repeats a literal source string, so a check against
    the raw text is a tautology that cannot fail. Before this mount's fix, an
    unset `LOCAL_REPOS_HOST_PATH` resolved to `./docker`, the exact same host
    directory the entrypoint-freshness mount above binds `:ro`, so this test
    is the regression guard for that.
    """
    result = _compose_config(tmp_path, {})
    assert result.returncode == 0, result.stderr

    resolved = yaml.safe_load(result.stdout)
    volumes = resolved["services"]["orchestrator"]["volumes"]
    ro_sources = {v["source"] for v in volumes if v.get("read_only")}
    offenders = [
        v for v in volumes if v["source"] in ro_sources and not v.get("read_only")
    ]
    assert offenders == [], (
        f"these mounts share a source with a :ro mount but are not "
        f"themselves read-only: {offenders}"
    )
