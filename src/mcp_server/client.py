"""HTTP client wrapper that forwards MCP tool calls to the Praxis REST API.

Translates transport and HTTP-status failures into a single structured
``PraxisClientError`` so MCP tools can return actionable messages to the brain
instead of opaque stack traces.
"""

from __future__ import annotations

import os
from typing import Any

import httpx


class PraxisClientError(Exception):
    """A structured client error carrying a machine-readable code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


_STATUS_CODES: dict[int, str] = {
    401: "auth_error",
    403: "auth_error",
    404: "not_found",
    422: "validation_error",
}


class PraxisClient:
    """Thin async HTTP client for the Praxis REST API with Bearer auth."""

    def __init__(
        self,
        base_url: str,
        token: str,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self._transport = transport

    @classmethod
    def from_env(cls) -> PraxisClient:
        """Build a client from PRAXIS_BASE_URL + PRAXIS_AUTH_TOKEN env vars."""
        # Default matches the docker-compose host port (PORT in .env, 12323).
        base_url = os.environ.get("PRAXIS_BASE_URL", "http://localhost:12323")
        token = os.environ.get("PRAXIS_AUTH_TOKEN")
        if not token:
            msg = "PRAXIS_AUTH_TOKEN is not set; the MCP server cannot authenticate."
            raise PraxisClientError("config_error", msg)  # noqa: EM101
        return cls(base_url=base_url, token=token)

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    async def _request(
        self, method: str, path: str, json: dict[str, Any] | None = None
    ) -> Any:
        url = f"{self.base_url}{path}"
        try:
            async with httpx.AsyncClient(
                transport=self._transport, timeout=30.0
            ) as client:
                response = await client.request(
                    method, url, headers=self._headers(), json=json
                )
        except httpx.ConnectError as exc:
            msg = f"Cannot reach Praxis at {self.base_url}. Is the server running?"
            raise PraxisClientError("connection_error", msg) from exc  # noqa: EM101
        except httpx.HTTPError as exc:
            msg = f"HTTP transport error: {exc}"
            raise PraxisClientError("connection_error", msg) from exc  # noqa: EM101

        # Guard against PRAXIS_BASE_URL pointing at a different service (e.g. a
        # dashboard/search UI that happens to share the port). Praxis always
        # answers JSON; an HTML body means we are talking to the wrong server,
        # which is far more actionable than a bare "404" from someone else's app.
        content_type = response.headers.get("content-type", "")
        if "text/html" in content_type:
            msg = (
                f"{self.base_url} responded with HTML, not Praxis JSON. "
                f"PRAXIS_BASE_URL is almost certainly pointing at the wrong "
                f"service or port (check it matches Praxis's PORT)."
            )
            raise PraxisClientError("wrong_service", msg)  # noqa: EM101

        if response.status_code >= 400:
            code = _STATUS_CODES.get(response.status_code, "request_error")
            detail = _safe_detail(response)
            raise PraxisClientError(
                code, f"Praxis returned {response.status_code}: {detail}"
            )
        if not response.content:
            return None
        return response.json()

    async def get(self, path: str) -> Any:
        return await self._request("GET", path)

    async def post(self, path: str, json: dict[str, Any] | None = None) -> Any:
        return await self._request("POST", path, json=json)


def _safe_detail(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text[:200]
    if isinstance(body, dict) and "detail" in body:
        return str(body["detail"])
    return str(body)[:200]
