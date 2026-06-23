"""Unit tests for PraxisClient using httpx MockTransport."""

from __future__ import annotations

import httpx
import pytest

from mcp_server.client import PraxisClient, PraxisClientError


def _client(handler: object, token: str = "tok") -> PraxisClient:
    transport = httpx.MockTransport(handler)  # type: ignore[arg-type]
    return PraxisClient(
        base_url="http://praxis.test",
        token=token,
        transport=transport,
    )


async def test_get_attaches_bearer_and_returns_json() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization", "")
        return httpx.Response(200, json={"ok": True})

    client = _client(handler)
    data = await client.get("/api/status")
    assert data == {"ok": True}
    assert seen["auth"] == "Bearer tok"


async def test_auth_error_maps_401() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"detail": "bad token"})

    client = _client(handler)
    with pytest.raises(PraxisClientError) as exc:
        await client.get("/api/status")
    assert exc.value.code == "auth_error"


async def test_not_found_maps_404() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "nope"})

    client = _client(handler)
    with pytest.raises(PraxisClientError) as exc:
        await client.get("/api/tasks/x")
    assert exc.value.code == "not_found"


async def test_validation_error_maps_422() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"detail": "bad"})

    client = _client(handler)
    with pytest.raises(PraxisClientError) as exc:
        await client.post("/api/dispatch", {"x": 1})
    assert exc.value.code == "validation_error"


async def test_connection_error_maps() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")  # noqa: EM101

    client = _client(handler)
    with pytest.raises(PraxisClientError) as exc:
        await client.get("/api/status")
    assert exc.value.code == "connection_error"


async def test_html_response_maps_wrong_service() -> None:
    """An HTML body (e.g. another app on the same port) is flagged, not parsed."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text="<!DOCTYPE html><html><body>SearXNG</body></html>",
            headers={"content-type": "text/html; charset=utf-8"},
        )

    client = _client(handler)
    with pytest.raises(PraxisClientError) as exc:
        await client.get("/api/status")
    assert exc.value.code == "wrong_service"


async def test_html_error_page_maps_wrong_service() -> None:
    """A 404 HTML page from a foreign service reports wrong_service, not not_found."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            404,
            text="<html><body>not here</body></html>",
            headers={"content-type": "text/html"},
        )

    client = _client(handler)
    with pytest.raises(PraxisClientError) as exc:
        await client.get("/api/status")
    assert exc.value.code == "wrong_service"


def test_from_env_default_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PRAXIS_BASE_URL", raising=False)
    monkeypatch.setenv("PRAXIS_AUTH_TOKEN", "secret")
    client = PraxisClient.from_env()
    assert client.base_url == "http://localhost:12323"


def test_from_env_reads_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRAXIS_BASE_URL", "http://h:9000")
    monkeypatch.setenv("PRAXIS_AUTH_TOKEN", "secret")
    client = PraxisClient.from_env()
    assert client.base_url == "http://h:9000"
    assert client.token == "secret"


def test_from_env_missing_token_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PRAXIS_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("PRAXIS_BASE_URL", "http://h:9000")
    with pytest.raises(PraxisClientError) as exc:
        PraxisClient.from_env()
    assert exc.value.code == "config_error"
