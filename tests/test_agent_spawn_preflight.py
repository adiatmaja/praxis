"""The spawn preflight's three namespace seams, and what it is blind to.

Every guard here answers a question of the form "which frame of reference was
this value computed in, and which one is it consumed in".  All three failures
covered below are silent: a bind source resolved in the wrong namespace is
CREATED by Docker as an empty directory rather than refused, a disk figure
measured in the wrong namespace passes a threshold the real filesystem would
fail, and a container count taken daemon-wide is spent against one stack's cap.
"""

# ruff: noqa: S101

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from orchestrator.core import agent_manager as am
from orchestrator.core.agent_manager import (
    LOCAL_REPO_MOUNT,
    STACK_LABEL,
    AgentManager,
    SpawnConfigurationError,
    host_bind_source,
    local_repo_volume,
)


@pytest.fixture(autouse=True)
def _no_real_context_probe():
    """Keep every spawn in this file off the network."""
    with patch(
        "orchestrator.core.agent_manager.detect_context_limit",
        new_callable=AsyncMock,
        return_value=None,
    ):
        yield


def _fake_container(name: str, labels: dict[str, str] | None = None) -> MagicMock:
    container = MagicMock()
    container.id = "abc123def456"
    container.name = name
    container.labels = labels or {}
    container.status = "running"
    container.attrs = {"State": {"ExitCode": 0}}
    return container


def _fake_daemon(*containers: MagicMock) -> Any:
    """A ``containers.list`` that actually APPLIES the filters it is handed.

    A bare ``MagicMock`` returns the same list for every call, so a guard
    written against one still passes when the ``filters`` argument is deleted
    outright.  This double answers the question the cap actually asks -- WHICH
    containers are being counted -- so removing a filter changes the answer.
    """

    def _list(**kwargs: Any) -> list[MagicMock]:
        filters = kwargs.get("filters") or {}
        name = filters.get("name")
        raw_labels = filters.get("label") or []
        wanted = [raw_labels] if isinstance(raw_labels, str) else list(raw_labels)
        matched = []
        for container in containers:
            if name is not None and name not in container.name:
                continue
            pairs = (item.split("=", 1) for item in wanted)
            if any(container.labels.get(key) != value for key, value in pairs):
                continue
            matched.append(container)
        return matched

    return _list


def _manager(client: MagicMock, **kwargs: Any) -> AgentManager:
    with patch("orchestrator.core.agent_manager.docker.from_env", return_value=client):
        return AgentManager(lm_studio_url="http://localhost:1234", **kwargs)


def _plenty_of_disk(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(total=0, used=0, free=500 * 1024**3),
    )


async def _spawn(manager: AgentManager, **overrides: Any) -> str:
    kwargs: dict[str, Any] = {
        "task_id": "task-1234abcd",
        "repo_url": "https://github.com/u/r",
        "branch": "agent/x",
        "base_branch": "main",
        "task_prompt": "do it",
        "model_name": "m",
        "callback_url": "http://cb",
        "harness": "opencode",
    }
    kwargs.update(overrides)
    return await manager.spawn_agent(**kwargs)


# ---------------------------------------------------------------------------
# Defect 1: the bind source is resolved in the container's namespace and
# consumed by the daemon in the host's
# ---------------------------------------------------------------------------

_VM_PREFIX = "/run/desktop/mnt/host/c/Users/me/repos"
_HOST_PREFIX = "C:/Users/me/repos"


@pytest.mark.unit
def test_bind_source_is_translated_into_the_host_namespace() -> None:
    """The daemon resolves a bind SOURCE on the host, never in this container."""
    volumes = local_repo_volume(
        f"{_VM_PREFIX}/a.git", repos_path=_VM_PREFIX, host_path=_HOST_PREFIX
    )
    assert list(volumes) == [f"{_HOST_PREFIX}/a.git"]
    assert volumes[f"{_HOST_PREFIX}/a.git"]["bind"] == LOCAL_REPO_MOUNT
    assert volumes[f"{_HOST_PREFIX}/a.git"]["mode"] == "rw"


@pytest.mark.unit
def test_bind_source_is_untouched_when_one_string_serves_both_namespaces() -> None:
    """The documented normal case: compose defaults the host var to the other."""
    volumes = local_repo_volume(
        f"{_VM_PREFIX}/a.git", repos_path=_VM_PREFIX, host_path=_VM_PREFIX
    )
    assert list(volumes) == [f"{_VM_PREFIX}/a.git"]


@pytest.mark.unit
def test_bind_source_is_untouched_when_no_namespaces_are_configured() -> None:
    """Both unset is a bare uvicorn or a Linux identity mount: nothing to translate."""
    assert list(local_repo_volume("/srv/bench/a.git")) == ["/srv/bench/a.git"]


@pytest.mark.unit
def test_bind_source_uses_the_host_prefix_separator() -> None:
    """A Windows bind source must not come back with a POSIX suffix glued on."""
    assert (
        host_bind_source(
            f"{_VM_PREFIX}/nested/a.git", _VM_PREFIX, "C:\\Users\\me\\repos"
        )
        == "C:\\Users\\me\\repos\\nested\\a.git"
    )


@pytest.mark.unit
def test_bind_source_refuses_a_repo_outside_the_translatable_prefix() -> None:
    """Refuse over a mount that silently succeeds and produces an empty directory."""
    with pytest.raises(SpawnConfigurationError) as excinfo:
        local_repo_volume(
            "/elsewhere/a.git", repos_path=_VM_PREFIX, host_path=_HOST_PREFIX
        )
    message = str(excinfo.value)
    assert "LOCAL_REPOS_PATH" in message
    assert "LOCAL_REPOS_HOST_PATH" in message
    assert "/elsewhere/a.git" in message


@pytest.mark.unit
def test_a_prefix_that_only_matches_as_a_string_is_not_inside_it() -> None:
    """``/repos-scratch`` starts with ``/repos`` and is not under it."""
    with pytest.raises(SpawnConfigurationError):
        host_bind_source("/repos-scratch/a.git", "/repos", "C:/repos")


@pytest.mark.unit
async def test_spawn_agent_hands_the_daemon_the_host_side_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The seam: a correct translator the spawn path never calls is inert."""
    _plenty_of_disk(monkeypatch)
    client = MagicMock()
    client.containers.list = _fake_daemon()
    client.containers.run.return_value = _fake_container("praxis-agent-task-123")
    manager = _manager(
        client,
        local_repos_path=_VM_PREFIX,
        local_repos_host_path=_HOST_PREFIX,
    )

    await _spawn(manager, repo_url=f"{_VM_PREFIX}/a.git")

    volumes = client.containers.run.call_args.kwargs["volumes"]
    assert f"{_HOST_PREFIX}/a.git" in volumes
    assert f"{_VM_PREFIX}/a.git" not in volumes


@pytest.mark.unit
async def test_spawn_agent_reads_the_namespaces_from_the_mounted_dotenv(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Compose forwards NEITHER variable into the container's environment.

    A translator that consulted ``os.environ`` alone would resolve both to ""
    on every containerized install, take the identity branch, and be inert
    exactly where it is deployed.  The mounted ``./.env:/app/.env`` copy is the
    only way the two values reach this process.
    """
    _plenty_of_disk(monkeypatch)
    monkeypatch.delenv("LOCAL_REPOS_PATH", raising=False)
    monkeypatch.delenv("LOCAL_REPOS_HOST_PATH", raising=False)
    (tmp_path / ".env").write_text(
        f"LOCAL_REPOS_PATH={_VM_PREFIX}\nLOCAL_REPOS_HOST_PATH={_HOST_PREFIX}\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    client = MagicMock()
    client.containers.list = _fake_daemon()
    client.containers.run.return_value = _fake_container("praxis-agent-task-123")
    manager = _manager(client)

    await _spawn(manager, repo_url=f"{_VM_PREFIX}/a.git")

    volumes = client.containers.run.call_args.kwargs["volumes"]
    assert f"{_HOST_PREFIX}/a.git" in volumes


@pytest.mark.unit
async def test_spawn_agent_refuses_an_untranslatable_repo_before_any_container(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _plenty_of_disk(monkeypatch)
    client = MagicMock()
    client.containers.list = _fake_daemon()
    manager = _manager(
        client,
        local_repos_path=_VM_PREFIX,
        local_repos_host_path=_HOST_PREFIX,
    )

    with pytest.raises(SpawnConfigurationError):
        await _spawn(manager, repo_url="/elsewhere/a.git")
    client.containers.run.assert_not_called()


# ---------------------------------------------------------------------------
# Defect 3: the disk refusal must name the filesystem it measured, and say
# which one it cannot see
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_disk_refusal_names_the_filesystem_it_actually_measured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A refusal naming a filesystem it did not measure is worse than none.

    Monkeypatching ``shutil.disk_usage`` makes any filesystem answer whatever
    the test wants, so the only thing worth asserting is the correspondence:
    whatever path the guard MEASURED is the path its refusal NAMES.
    """
    measured: list[str] = []

    def _record(path: str) -> SimpleNamespace:
        measured.append(path)
        return SimpleNamespace(total=100 * 1024**3, used=99 * 1024**3, free=1024**3)

    monkeypatch.setattr(shutil, "disk_usage", _record)
    client = MagicMock()
    client.containers.list = _fake_daemon()
    manager = _manager(client, min_free_disk_bytes=10 * 1024**3)

    with pytest.raises(RuntimeError) as excinfo:
        await _spawn(manager)

    message = str(excinfo.value)
    assert measured, "the guard refused without measuring anything"
    assert measured[0] in message, (
        f"refusal names no filesystem it measured ({measured[0]!r}): {message!r}"
    )
    assert "host disk space" not in message


@pytest.mark.unit
async def test_disk_refusal_inside_a_container_states_what_it_cannot_see(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Docker host's backing store is not observable from in here."""
    monkeypatch.setattr(
        shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(total=0, used=0, free=1024**3),
    )
    monkeypatch.setattr(am, "_in_container", lambda: True)
    client = MagicMock()
    client.containers.list = _fake_daemon()
    manager = _manager(client, min_free_disk_bytes=10 * 1024**3)

    with pytest.raises(RuntimeError) as excinfo:
        await _spawn(manager)

    message = str(excinfo.value)
    assert "not observable" in message
    assert "HOST disk" in message


@pytest.mark.unit
async def test_disk_refusal_outside_a_container_claims_no_blindness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bare uvicorn shares the daemon's filesystem; the caveat would be a lie."""
    monkeypatch.setattr(
        shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(total=0, used=0, free=1024**3),
    )
    monkeypatch.setattr(am, "_in_container", lambda: False)
    client = MagicMock()
    client.containers.list = _fake_daemon()
    manager = _manager(client, min_free_disk_bytes=10 * 1024**3)

    with pytest.raises(RuntimeError) as excinfo:
        await _spawn(manager)

    assert "not observable" not in str(excinfo.value)


@pytest.mark.unit
async def test_a_containerized_spawn_says_once_that_the_guard_is_half_blind(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The guard is INERT in the containerized deployment, so it must say so.

    The threshold is measured against the Docker VM's overlay, which reads
    hundreds of GiB free while the host disk backing it has single digits.  The
    refusal message never fires there, so the refusal message alone cannot
    carry this: only a line emitted on the passing path can.  Once per process,
    not once per spawn -- it describes the deployment, not the task.
    """
    _plenty_of_disk(monkeypatch)
    monkeypatch.setattr(am, "_in_container", lambda: True)
    monkeypatch.setattr(am, "_LOGGED_HOST_DISK_BLIND", False)
    client = MagicMock()
    client.containers.list = _fake_daemon()
    client.containers.run.return_value = _fake_container("praxis-agent-task-123")
    manager = _manager(client)

    with caplog.at_level(logging.INFO, logger=am.__name__):
        await _spawn(manager, task_id="task-aaaa1111")
        await _spawn(manager, task_id="task-bbbb2222")

    notices = [
        record
        for record in caplog.records
        if record.levelname == "INFO" and "not observable" in record.getMessage()
    ]
    assert len(notices) == 1, f"expected exactly one notice, got {len(notices)}"
    assert am.measure_disk_headroom().path in notices[0].getMessage()


@pytest.mark.unit
async def test_no_blindness_notice_outside_a_container(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _plenty_of_disk(monkeypatch)
    monkeypatch.setattr(am, "_in_container", lambda: False)
    monkeypatch.setattr(am, "_LOGGED_HOST_DISK_BLIND", False)
    client = MagicMock()
    client.containers.list = _fake_daemon()
    client.containers.run.return_value = _fake_container("praxis-agent-task-123")
    manager = _manager(client)

    with caplog.at_level(logging.INFO, logger=am.__name__):
        await _spawn(manager)

    assert not [
        record for record in caplog.records if "not observable" in record.getMessage()
    ]


# ---------------------------------------------------------------------------
# Defect 4: the concurrency cap counted every stack's agents on the daemon
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_the_cap_ignores_another_checkouts_agents(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two checkouts share one daemon; agent names carry no stack identity."""
    _plenty_of_disk(monkeypatch)
    others = [
        _fake_container(f"praxis-agent-other{n}", {STACK_LABEL: "other-checkout"})
        for n in range(3)
    ]
    client = MagicMock()
    client.containers.list = _fake_daemon(*others)
    client.containers.run.return_value = _fake_container("praxis-agent-task-123")
    manager = _manager(client, stack_id="orchestrator", max_agent_concurrency=3)

    await _spawn(manager)

    client.containers.run.assert_called_once()


@pytest.mark.unit
async def test_the_cap_still_fires_on_this_stacks_agents(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _plenty_of_disk(monkeypatch)
    mine = [
        _fake_container(f"praxis-agent-mine{n}", {STACK_LABEL: "orchestrator"})
        for n in range(3)
    ]
    client = MagicMock()
    client.containers.list = _fake_daemon(*mine)
    manager = _manager(client, stack_id="orchestrator", max_agent_concurrency=3)

    with pytest.raises(RuntimeError, match="Concurrent agent cap reached"):
        await _spawn(manager)


@pytest.mark.unit
async def test_an_unlabelled_container_is_not_counted_against_this_stack(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nothing but the label answers "whose agent is this"."""
    _plenty_of_disk(monkeypatch)
    strays = [_fake_container(f"praxis-agent-stray{n}") for n in range(3)]
    client = MagicMock()
    client.containers.list = _fake_daemon(*strays)
    client.containers.run.return_value = _fake_container("praxis-agent-task-123")
    manager = _manager(client, stack_id="orchestrator", max_agent_concurrency=3)

    await _spawn(manager)

    client.containers.run.assert_called_once()


@pytest.mark.unit
async def test_a_spawned_container_carries_its_stack_label(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cap's filter is only as good as what spawn actually stamps."""
    _plenty_of_disk(monkeypatch)
    client = MagicMock()
    client.containers.list = _fake_daemon()
    client.containers.run.return_value = _fake_container("praxis-agent-task-123")
    manager = _manager(client, stack_id="second-checkout")

    await _spawn(manager)

    labels = client.containers.run.call_args.kwargs["labels"]
    assert labels[STACK_LABEL] == "second-checkout"


@pytest.mark.unit
def test_the_stack_id_comes_from_the_mounted_dotenv(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``PRAXIS_CONTAINER_NAME`` is a compose substitution variable too."""
    monkeypatch.delenv("PRAXIS_CONTAINER_NAME", raising=False)
    (tmp_path / ".env").write_text(
        "PRAXIS_CONTAINER_NAME=second-checkout\n", encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)

    client = MagicMock()
    client.containers.list = _fake_daemon()
    manager = _manager(client)

    assert manager._stack_id == "second-checkout"


@pytest.mark.unit
def test_the_stack_id_falls_back_to_the_compose_container_name_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Two checkouts that both left it unset fight over the container name too."""
    monkeypatch.delenv("PRAXIS_CONTAINER_NAME", raising=False)
    monkeypatch.chdir(tmp_path)

    client = MagicMock()
    client.containers.list = _fake_daemon()
    manager = _manager(client)

    assert manager._stack_id == "orchestrator"
