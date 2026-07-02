import json as _json
import time

import httpx
import pytest

from orchestrator.core.github_credentials import (
    CredentialError,
    GitHubAppCredentialProvider,
    PatCredentialProvider,
    repo_slug_from_url,
)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://github.com/owner/repo", "owner/repo"),
        ("https://github.com/owner/repo.git", "owner/repo"),
        ("git@github.com:owner/repo.git", "owner/repo"),
        ("owner/repo", "owner/repo"),
    ],
)
def test_repo_slug_from_url(url, expected):
    assert repo_slug_from_url(url) == expected


def test_repo_slug_from_url_rejects_garbage():
    with pytest.raises(CredentialError):
        repo_slug_from_url("not-a-repo")


async def test_pat_provider_returns_static_token_for_any_repo():
    provider = PatCredentialProvider("ghp_static")
    assert await provider.token_for_repo("https://github.com/a/b") == "ghp_static"
    assert await provider.token_for_repo("c/d") == "ghp_static"


class _FakeClock:
    def __init__(self, now: float) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def _mock_transport(handler):
    return httpx.MockTransport(handler)


async def test_app_provider_mints_repo_scoped_token(monkeypatch):
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path == "/repos/owner/repo/installation":
            return httpx.Response(200, json={"id": 42})
        if request.url.path == "/app/installations/42/access_tokens":
            return httpx.Response(
                201,
                json={"token": "ghs_minted", "expires_at": "2026-07-02T13:00:00Z"},
            )
        return httpx.Response(404)

    provider = GitHubAppCredentialProvider(
        app_id="123",
        private_key_pem="unused-in-test",
        clock=_FakeClock(1000.0),
        transport=_mock_transport(handler),
    )
    monkeypatch.setattr(provider, "_app_jwt", lambda: "fake-jwt")

    token = await provider.token_for_repo("https://github.com/owner/repo")
    assert token == "ghs_minted"
    mint = next(r for r in calls if r.url.path.endswith("/access_tokens"))
    body = _json.loads(mint.content)
    assert body["repositories"] == ["repo"]
    assert body["permissions"] == {"contents": "write", "pull_requests": "write"}


async def test_app_provider_caches_token_until_near_expiry(monkeypatch):
    mint_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal mint_count
        if request.url.path == "/repos/owner/repo/installation":
            return httpx.Response(200, json={"id": 7})
        if request.url.path.endswith("/access_tokens"):
            mint_count += 1
            return httpx.Response(
                201,
                json={
                    "token": f"ghs_{mint_count}",
                    "expires_at": "2026-07-02T13:00:00Z",
                },
            )
        return httpx.Response(404)

    clock = _FakeClock(
        time.mktime(time.strptime("2026-07-02T12:00:00Z", "%Y-%m-%dT%H:%M:%SZ"))
    )
    provider = GitHubAppCredentialProvider(
        app_id="123",
        private_key_pem="unused",
        clock=clock,
        transport=_mock_transport(handler),
    )
    monkeypatch.setattr(provider, "_app_jwt", lambda: "fake-jwt")

    first = await provider.token_for_repo("owner/repo")
    second = await provider.token_for_repo("owner/repo")
    assert first == second == "ghs_1"
    assert mint_count == 1

    clock.now += 56 * 60  # 12:56, expiry 13:00 -> 4 min left
    third = await provider.token_for_repo("owner/repo")
    assert third == "ghs_2"
    assert mint_count == 2


async def test_app_provider_raises_on_missing_installation(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "Not Found"})

    provider = GitHubAppCredentialProvider(
        app_id="123",
        private_key_pem="unused",
        clock=_FakeClock(1000.0),
        transport=_mock_transport(handler),
    )
    monkeypatch.setattr(provider, "_app_jwt", lambda: "fake-jwt")

    with pytest.raises(CredentialError):
        await provider.token_for_repo("owner/repo")
