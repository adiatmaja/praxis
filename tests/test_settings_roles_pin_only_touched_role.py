"""Writing one role chain must not pin every other role in the database.

``GET /api/settings/roles`` answers with the EFFECTIVE map, which falls back to
the mounted ``config/praxis.yaml`` when no override exists.  Every writer of
that endpoint -- ``praxis config set-role``, the dashboard's Settings -> Models
panel, and plain curl -- reads that map, changes ONE key, and PUTs the whole
thing back.  When the endpoint stored the body wholesale under a single
``models.roles`` key, one ``set-role`` froze EVERY role in the DB, and editing
``models.roles`` in the mounted settings file plus ``docker compose restart``
-- the documented way to change chains, and the reason the file is mounted
rather than baked -- silently did nothing for any role.

The tests below are written against BEHAVIOUR, never against the DB key shape:
each one edits the settings file after the write and asks the API what is now
in effect, which is exactly the operator gesture that used to be swallowed.
"""

# ruff: noqa: S101, EM101

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from httpx import AsyncClient

from orchestrator.database import Database


FILE_CHAINS: dict[str, list[str]] = {
    "plan": ["sonnet", "opus"],
    "review": ["sonnet", "haiku"],
    "implement": ["local"],
}

REGISTRY: list[dict[str, Any]] = [
    {"name": "sonnet", "provider": "claude", "model": "claude-sonnet-4-6"},
    {"name": "opus", "provider": "claude", "model": "claude-opus-4-8"},
    {"name": "haiku", "provider": "claude", "model": "claude-haiku-4-5"},
    {"name": "local", "provider": "local", "model": ""},
]


def _write_settings_file(path: Path, chains: dict[str, list[str]]) -> None:
    """Write a settings file declaring the registry and the given role chains."""
    path.write_text(
        yaml.safe_dump({"models": {"registry": REGISTRY, "roles": chains}}),
        encoding="utf-8",
    )


@pytest.fixture
def settings_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the orchestrator at a settings file this test owns and can rewrite."""
    path = tmp_path / "praxis.yaml"
    _write_settings_file(path, FILE_CHAINS)
    monkeypatch.setenv("PRAXIS_CONFIG_PATH", str(path))
    return path


async def _set_one_role(
    client: AsyncClient,
    auth_headers: dict[str, str],
    role: str,
    chain: list[str],
) -> None:
    """Change one role exactly the way every shipped writer of the endpoint does.

    Read the effective map, replace one key, PUT the whole map back.  This
    round trip is the defect's carrier, so the test must perform it rather
    than PUT a one-key body no real caller sends.
    """
    current = (await client.get("/api/settings/roles", headers=auth_headers)).json()
    current[role] = chain
    resp = await client.put(
        "/api/settings/roles", json={"chains": current}, headers=auth_headers
    )
    assert resp.status_code == 200, resp.text


async def test_setting_one_role_leaves_the_others_on_the_settings_file(
    client: AsyncClient, auth_headers: dict[str, str], settings_file: Path
) -> None:
    """A later settings-file edit must still reach a role nobody wrote.

    This is the load-bearing guard.  It touches ``plan`` and then asks about
    ``review``: a test that asserted only on the role it changed would pass
    whether the endpoint stored one key or all of them.
    """
    await _set_one_role(client, auth_headers, "plan", ["opus"])

    # The operator edits the mounted settings file and restarts.
    _write_settings_file(settings_file, {**FILE_CHAINS, "review": ["haiku"]})

    effective = (await client.get("/api/settings/roles", headers=auth_headers)).json()
    assert effective["review"] == ["haiku"]
    assert effective["implement"] == ["local"]


async def test_the_role_that_was_written_is_pinned_against_a_file_edit(
    client: AsyncClient, auth_headers: dict[str, str], settings_file: Path
) -> None:
    """The other half: the role the operator DID set outranks the file."""
    await _set_one_role(client, auth_headers, "plan", ["opus"])

    _write_settings_file(settings_file, {**FILE_CHAINS, "plan": ["haiku"]})

    effective = (await client.get("/api/settings/roles", headers=auth_headers)).json()
    assert effective["plan"] == ["opus"]


async def test_a_chain_equal_to_the_settings_file_is_not_pinned(
    client: AsyncClient, auth_headers: dict[str, str], settings_file: Path
) -> None:
    """Re-submitting the file's own chain leaves the file in charge.

    ``praxis config set-role`` and the dashboard both PUT every role on every
    write, so without this the first write of ANY role would pin all of them
    again by the back door.
    """
    await _set_one_role(client, auth_headers, "plan", ["opus"])

    _write_settings_file(settings_file, {**FILE_CHAINS, "review": ["haiku"]})

    effective = (await client.get("/api/settings/roles", headers=auth_headers)).json()
    assert effective["review"] == ["haiku"]


async def test_legacy_wholesale_override_is_not_stranded_by_a_later_write(
    client: AsyncClient,
    auth_headers: dict[str, str],
    settings_file: Path,
    db: Database,
) -> None:
    """An install that already ran ``set-role`` keeps every value it had.

    Its DB carries one wholesale ``models.roles`` row.  The next write must
    re-express it, not drop the roles it pinned to something the settings file
    does not say.
    """
    await db.execute(
        "INSERT INTO settings_overrides (key, value) VALUES (?, ?)",
        ("models.roles", json.dumps({**FILE_CHAINS, "review": ["opus", "haiku"]})),
    )

    # The wholesale row is still what the API answers with before any write.
    before = (await client.get("/api/settings/roles", headers=auth_headers)).json()
    assert before["review"] == ["opus", "haiku"]

    await _set_one_role(client, auth_headers, "plan", ["haiku"])

    after = (await client.get("/api/settings/roles", headers=auth_headers)).json()
    assert after["review"] == ["opus", "haiku"]
    assert after["plan"] == ["haiku"]

    # And it is now held per role, so the file reaches the roles it did not pin.
    _write_settings_file(settings_file, {**FILE_CHAINS, "implement": ["opus"]})
    later = (await client.get("/api/settings/roles", headers=auth_headers)).json()
    assert later["implement"] == ["opus"]


async def test_reset_clears_a_pinned_role(
    client: AsyncClient, auth_headers: dict[str, str], settings_file: Path
) -> None:
    """The documented way back to the settings file must still reach a pin.

    ``POST /api/settings/models/reset`` with no call site is the only verb
    that undoes one, and it deletes by key prefix, so a per-role key that
    fell outside that prefix would be unremovable through the API.
    """
    await _set_one_role(client, auth_headers, "plan", ["opus"])

    resp = await client.post(
        "/api/settings/models/reset", json={}, headers=auth_headers
    )
    assert resp.status_code == 200

    effective = (await client.get("/api/settings/roles", headers=auth_headers)).json()
    assert effective["plan"] == FILE_CHAINS["plan"]


async def test_put_registry_equal_to_the_settings_file_is_not_pinned(
    client: AsyncClient, auth_headers: dict[str, str], settings_file: Path
) -> None:
    """The registry follows the same rule at the only granularity a list has."""
    current = (await client.get("/api/settings/registry", headers=auth_headers)).json()
    resp = await client.put(
        "/api/settings/registry", json=current, headers=auth_headers
    )
    assert resp.status_code == 200

    extended = [*REGISTRY, {"name": "extra", "provider": "local", "model": "x"}]
    _write_settings_file(settings_file, FILE_CHAINS)
    settings_file.write_text(
        yaml.safe_dump({"models": {"registry": extended, "roles": FILE_CHAINS}}),
        encoding="utf-8",
    )

    names = {
        m["name"]
        for m in (
            await client.get("/api/settings/registry", headers=auth_headers)
        ).json()
    }
    assert "extra" in names


async def test_put_registry_differing_from_the_file_is_still_stored(
    client: AsyncClient, auth_headers: dict[str, str], settings_file: Path
) -> None:
    """The registry is REPLACED wholesale when it differs; that is deliberate."""
    body = [{"name": "only", "provider": "local", "model": "m", "effort": None}]
    resp = await client.put("/api/settings/registry", json=body, headers=auth_headers)
    assert resp.status_code == 200

    settings_file.write_text(
        yaml.safe_dump({"models": {"registry": REGISTRY, "roles": FILE_CHAINS}}),
        encoding="utf-8",
    )
    got = (await client.get("/api/settings/registry", headers=auth_headers)).json()
    assert [m["name"] for m in got] == ["only"]
