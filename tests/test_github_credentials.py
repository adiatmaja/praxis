import json as _json
from datetime import UTC, datetime

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
        # HTTPS cases
        ("https://github.com/owner/repo", "owner/repo"),
        ("https://github.com/owner/repo.git", "owner/repo"),
        ("http://github.com/owner-name/repo-name", "owner-name/repo-name"),
        ("https://github.com/owner.name/repo.name", "owner.name/repo.name"),
        ("https://github.com/owner/repo/sub/path", "owner/repo"),
        # SSH cases
        ("git@github.com:owner/repo.git", "owner/repo"),
        ("git@github.com:owner/repo", "owner/repo"),
        ("ssh://git@github.com/owner/repo.git", "owner/repo"),
        ("git@github.com:owner-name/repo.git", "owner-name/repo"),
        # Bare-slug cases
        ("owner/repo", "owner/repo"),
        ("owner-name/repo.name", "owner-name/repo.name"),
        ("owner/repo.git", "owner/repo"),
    ],
)
def test_repo_slug_from_url(url, expected):
    assert repo_slug_from_url(url) == expected


@pytest.mark.parametrize(
    "invalid_url",
    [
        "not-a-repo",
        "owner",
        "owner/",
        "/repo",
        "https://github.com/owner",
        "https://github.com/owner/",
        "ssh://git@github.com:owner/repo.git",
        "https://not-github.com/owner/repo",
        "owner/repo space",
        "owner/repo/../path",
    ],
)
def test_repo_slug_from_url_rejects_garbage(invalid_url):
    with pytest.raises(CredentialError, match="cannot extract owner/repo"):
        repo_slug_from_url(invalid_url)




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

    # Seed the fake clock with a real UTC epoch (independent of the provider's
    # own parser) so the test would fail if expiry were parsed as local time.
    clock = _FakeClock(datetime(2026, 7, 2, 12, 0, 0, tzinfo=UTC).timestamp())
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


from orchestrator.core.github_credentials import build_credential_provider  # noqa: E402


class _Cfg:
    def __init__(self, **kw):
        self.github_token = kw.get("github_token")
        self.github_app_id = kw.get("github_app_id")
        self.github_app_private_key = kw.get("github_app_private_key")
        self.github_app_installation_id = kw.get("github_app_installation_id")


def test_factory_prefers_app_when_configured():
    cfg = _Cfg(
        github_token="ghp_x",
        github_app_id="123",
        github_app_private_key="-----BEGIN KEY-----\nx\n-----END KEY-----",
    )
    provider = build_credential_provider(cfg)
    assert isinstance(provider, GitHubAppCredentialProvider)


def test_factory_falls_back_to_pat():
    cfg = _Cfg(github_token="ghp_x")
    provider = build_credential_provider(cfg)
    assert isinstance(provider, PatCredentialProvider)


def test_factory_raises_when_nothing_configured():
    with pytest.raises(CredentialError):
        build_credential_provider(_Cfg())


def test_factory_reads_private_key_from_file(tmp_path):
    pem = tmp_path / "app.pem"
    pem.write_text("-----BEGIN PRIVATE KEY-----\nfromfile\n-----END PRIVATE KEY-----")
    cfg = _Cfg(github_app_id="123", github_app_private_key=str(pem))
    provider = build_credential_provider(cfg)
    assert isinstance(provider, GitHubAppCredentialProvider)
    assert "fromfile" in provider._private_key_pem


def test_repo_slug_validation_invalid():
    with pytest.raises(CredentialError, match="cannot extract owner/repo"):
        repo_slug_from_url("owner/repo/sub/path")

    with pytest.raises(CredentialError, match="cannot extract owner/repo"):
        repo_slug_from_url("owner/repo/../path")


@pytest.mark.asyncio
async def test_app_provider_installation_id_validation_raises():
    provider = GitHubAppCredentialProvider(
        app_id="123",
        private_key_pem="-----BEGIN PRIVATE KEY-----\nx\n-----END PRIVATE KEY-----",
    )
    with pytest.raises(CredentialError, match="Invalid repository slug"):
        await provider._installation_id_for("invalid/slug/path")


@pytest.mark.unit
def test_github_app_jwt_minting() -> None:
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives import serialization
    import jwt

    # Generate a real RSA private key
    private_key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=2048,
    )
    pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption()
    ).decode("utf-8")

    provider = GitHubAppCredentialProvider(
        app_id="987",
        private_key_pem=pem,
        clock=_FakeClock(5000.0),
    )

    jwt_token = provider._app_jwt()

    # Decode and verify payload
    payload = jwt.decode(jwt_token, options={"verify_signature": False})
    assert payload["iss"] == "987"
    assert payload["iat"] == 5000 - 60
    assert payload["exp"] == 5000 + 540

    # Verify signature using public key
    public_key = private_key.public_key()
    decoded = jwt.decode(jwt_token, public_key, algorithms=["RS256"], options={"verify_exp": False})
    assert decoded["iss"] == "987"
    assert decoded["iat"] == 5000 - 60


@pytest.mark.asyncio
async def test_app_provider_token_expiry_caching_robust() -> None:
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives import serialization
    import jwt

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption()
    ).decode("utf-8")

    installation_requests = []
    token_requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        # Verify JWT in Authorization header
        auth_header = request.headers.get("Authorization", "")
        assert auth_header.startswith("Bearer ")
        token_str = auth_header.split("Bearer ")[1]
        decoded = jwt.decode(token_str, private_key.public_key(), algorithms=["RS256"], options={"verify_exp": False})
        assert decoded["iss"] == "123"

        if "/repos/owner/repo/installation" in request.url.path:
            installation_requests.append(request)
            return httpx.Response(200, json={"id": 4242})
        if "/app/installations/4242/access_tokens" in request.url.path:
            token_requests.append(request)
            # Expiry is 1 hour in the future (3600 seconds)
            # e.g., iat is at clock, expires_at is 3600 seconds later
            expires_at = datetime.fromtimestamp(decoded["iat"] + 3600, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
            return httpx.Response(
                201,
                json={
                    "token": f"ghs_token_{len(token_requests)}",
                    "expires_at": expires_at,
                },
            )
        return httpx.Response(404)

    clock = _FakeClock(10000.0)
    provider = GitHubAppCredentialProvider(
        app_id="123",
        private_key_pem=pem,
        clock=clock,
        transport=httpx.MockTransport(handler),
    )

    # 1. First fetch
    token = await provider.token_for_repo("owner/repo")
    assert token == "ghs_token_1"
    assert len(installation_requests) == 1
    assert len(token_requests) == 1

    # Verify request payload
    mint_req = token_requests[0]
    body = _json.loads(mint_req.content)
    assert body["repositories"] == ["repo"]
    assert body["permissions"] == {"contents": "write", "pull_requests": "write"}

    # 2. Second fetch within cache period
    clock.now += 1800.0  # 30 mins elapsed, still 30 mins left
    token2 = await provider.token_for_repo("owner/repo")
    assert token2 == "ghs_token_1"
    assert len(installation_requests) == 1
    assert len(token_requests) == 1

    # 3. Third fetch past refresh margin (expires in 4 minutes, less than 5 minutes refresh margin)
    clock.now += 1500.0  # 55 mins total elapsed, 5 mins left. Since margin is 300s, this should refresh
    token3 = await provider.token_for_repo("owner/repo")
    assert token3 == "ghs_token_2"
    assert len(installation_requests) == 1  # Installation ID should STILL be cached!
    assert len(token_requests) == 2

    # 4. Fetch with fixed installation ID should not request installation endpoint at all
    provider_fixed = GitHubAppCredentialProvider(
        app_id="123",
        private_key_pem=pem,
        installation_id=8888,
        clock=clock,
        transport=httpx.MockTransport(handler),
    )
    # Define a clean mock transport handler for fixed provider
    fixed_token_requests = []
    def handler_fixed(request: httpx.Request) -> httpx.Response:
        assert "/repos/" not in request.url.path
        assert "/app/installations/8888/access_tokens" in request.url.path
        fixed_token_requests.append(request)
        expires_at = datetime.fromtimestamp(clock() + 3600, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        return httpx.Response(
            201,
            json={
                "token": "ghs_fixed_token",
                "expires_at": expires_at,
            },
        )
    provider_fixed._transport = httpx.MockTransport(handler_fixed)
    token_fixed = await provider_fixed.token_for_repo("owner/repo")
    assert token_fixed == "ghs_fixed_token"
    assert len(fixed_token_requests) == 1

