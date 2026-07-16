"""Harness catalog endpoint tests."""
# ruff: noqa: S101

from __future__ import annotations

import pytest
from httpx import AsyncClient


@pytest.mark.integration
async def test_list_harnesses_requires_auth(client: AsyncClient) -> None:
    resp = await client.get("/api/harnesses")
    assert resp.status_code in (401, 403)


@pytest.mark.integration
async def test_list_harnesses_returns_catalog(
    client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    resp = await client.get("/api/harnesses", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    ids = {h["id"] for h in data}
    assert ids == {"opencode", "agy"}


@pytest.mark.integration
async def test_recommended_harness_is_first(
    client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    resp = await client.get("/api/harnesses", headers=auth_headers)
    data = resp.json()
    assert data[0]["recommended"] is True


@pytest.mark.integration
async def test_each_harness_has_about_fields(
    client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    resp = await client.get("/api/harnesses", headers=auth_headers)
    for h in resp.json():
        for key in (
            "display_name",
            "description",
            "uniqueness",
            "when_to_pick",
            "pros",
            "cons",
        ):
            assert h[key]
