import pytest


@pytest.mark.integration
async def test_presets_endpoint_requires_auth(client):
    response = await client.get("/api/settings/presets")
    assert response.status_code == 401


@pytest.mark.integration
async def test_presets_endpoint_returns_the_shipped_presets(client, auth_headers):
    response = await client.get("/api/settings/presets", headers=auth_headers)
    assert response.status_code == 200
    names = {p["name"] for p in response.json()["presets"]}
    # hosted-openweight was REMOVED on 2026-08-27: it named a model no endpoint
    # here serves, and the worker endpoint answers 200 for ANY model string, so
    # the preset could never be exercised or verified. Exact equality is
    # deliberate - a preset nobody can run must not creep back in unnoticed.
    assert names == {"local-lmstudio", "gemini-agy"}


@pytest.mark.integration
async def test_every_preset_exposes_the_three_fields(client, auth_headers):
    response = await client.get("/api/settings/presets", headers=auth_headers)
    for preset in response.json()["presets"]:
        assert preset["harness"]
        assert preset["model"]
        assert "endpoint" in preset
        assert isinstance(preset["requires"], list)
