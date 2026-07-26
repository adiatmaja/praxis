"""Tests for registry / roles / capabilities settings endpoints."""

# ruff: noqa: S101, EM101, PT018

from __future__ import annotations

from httpx import AsyncClient


async def test_get_registry_returns_defaults(
    client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    resp = await client.get("/api/settings/registry", headers=auth_headers)
    assert resp.status_code == 200
    names = {m["name"] for m in resp.json()}
    assert "sonnet" in names


async def test_put_registry_roundtrips(
    client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    body = [
        {
            "name": "opus",
            "provider": "claude",
            "model": "claude-opus-4-8",
            "effort": "high",
        }
    ]
    resp = await client.put("/api/settings/registry", json=body, headers=auth_headers)
    assert resp.status_code == 200
    got_resp = await client.get("/api/settings/registry", headers=auth_headers)
    assert got_resp.json() == body


async def test_put_roles_rejects_unknown_model(
    client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    resp = await client.put(
        "/api/settings/roles",
        json={"chains": {"plan": ["ghost"]}},
        headers=auth_headers,
    )
    assert resp.status_code == 422


async def test_put_roles_rejects_empty_chain(
    client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    resp = await client.put(
        "/api/settings/roles",
        json={"chains": {"plan": []}},
        headers=auth_headers,
    )
    assert resp.status_code == 422


async def test_put_roles_accepts_valid_chain(
    client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    resp = await client.put(
        "/api/settings/roles",
        json={"chains": {"plan": ["sonnet", "opus"]}},
        headers=auth_headers,
    )
    assert resp.status_code == 200


async def test_capabilities_joins_registry(
    client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    resp = await client.get("/api/settings/capabilities", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert "models" in data and "as_of" in data
