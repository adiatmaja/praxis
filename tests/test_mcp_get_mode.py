from __future__ import annotations

from typing import Any

import httpx
import pytest

from mcp_server.client import PraxisClient, PraxisClientError
from mcp_server.server import get_mode_impl


async def test_client_get_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_get(self: PraxisClient, path: str) -> Any:
        assert path == "/api/settings/auto-delegate"
        return {
            "enabled": True,
            "worker": {"harness": "agy", "model": "Gemini 3.6 Flash (High)"},
        }

    monkeypatch.setattr(PraxisClient, "get", fake_get, raising=False)
    client = PraxisClient(base_url="http://x", token="t")
    result = await client.get_mode()
    assert result["enabled"] is True


async def test_client_get_mode_http() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/settings/auto-delegate"
        return httpx.Response(200, json={"enabled": False, "worker": None})

    transport = httpx.MockTransport(handler)
    client = PraxisClient(base_url="http://test", token="tok", transport=transport)
    result = await client.get_mode()
    assert result == {"enabled": False, "worker": None}


async def test_get_mode_impl_success() -> None:
    class FakeClient:
        async def get_mode(self) -> dict[str, Any]:
            return {
                "enabled": True,
                "worker": {"harness": "agy", "model": "Gemini 3.6 Flash (High)"},
            }

    result = await get_mode_impl(FakeClient())
    assert result == {
        "enabled": True,
        "worker": {"harness": "agy", "model": "Gemini 3.6 Flash (High)"},
    }


async def test_get_mode_impl_error_handling() -> None:
    class ErrClient:
        async def get_mode(self) -> dict[str, Any]:
            raise PraxisClientError("connection_error", "unreachable")  # noqa: EM101

    result = await get_mode_impl(ErrClient())
    assert result == {"error": "connection_error", "message": "unreachable"}
