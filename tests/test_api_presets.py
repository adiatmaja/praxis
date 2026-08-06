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
    assert {"local-lmstudio", "hosted-openweight", "gemini-agy"} <= names


@pytest.mark.integration
async def test_every_preset_exposes_the_three_fields(client, auth_headers):
    response = await client.get("/api/settings/presets", headers=auth_headers)
    for preset in response.json()["presets"]:
        assert preset["harness"]
        assert preset["model"]
        assert "endpoint" in preset
        assert isinstance(preset["requires"], list)
