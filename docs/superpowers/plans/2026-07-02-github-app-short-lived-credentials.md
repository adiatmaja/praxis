# Short-lived GitHub App Credentials Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the single long-lived GitHub PAT with short-lived, repo-scoped GitHub App installation tokens minted per operation, while keeping the existing PAT path working as an opt-out fallback.

**Architecture:** Introduce a `GitHubCredentialProvider` seam in `core/github_credentials.py` with two backends — `PatCredentialProvider` (returns the static PAT, legacy behavior) and `GitHubAppCredentialProvider` (JWT-signs with the App private key, resolves the installation, mints a repo-scoped ≤1h token, caches per repo). Every consumer that reads `settings.github_token` (`GitOps`, `AgentManager`, `BrainstormManager`, `ContextSync`) instead resolves a token from the provider using its repo context. The `GH_TOKEN` env contract inside containers and the git credential-helper plumbing do not change — only the token *value* becomes short-lived and repo-scoped.

**Tech Stack:** Python 3.11, FastAPI, `httpx` (already a dep), `PyJWT` + `cryptography` (new), pytest / pytest-asyncio (`asyncio_mode = "auto"`).

---

## Context for the implementer (read first)

You have zero prior context, so here is everything you need:

- **Where the PAT lives today:** `Settings.github_token` in `src/orchestrator/config.py` (sourced from the `GITHUB_TOKEN` env var). It is passed at startup in `src/orchestrator/main.py` lifespan to `GitOps`, `AgentManager`, `BrainstormManager`, and `ContextSync`, and injected into every agent container as `GH_TOKEN` by `AgentManager.spawn_agent` (`src/orchestrator/core/agent_manager.py`).
- **How the token authenticates git:** a credential helper reads the `GH_TOKEN` env var at call time and echoes `username=x-access-token` / `password=$GH_TOKEN`. This keeps the token out of `.git/config` and out of process argv. See `_CREDENTIAL_HELPER` / `_token_git_args()` in `src/orchestrator/core/git_ops.py`. Installation tokens use the same `x-access-token` username, so **no helper changes are needed** — only the value changes.
- **`gh` CLI auth:** the GitHub CLI reads the `GH_TOKEN` environment variable. `GitOps._run_command` sets it; we keep doing that but with a per-repo token.
- **Installation tokens are repo-scoped and ≤1h.** Every GitOps operation and the agent injection already has a repo in scope (a `repo_url`, a `pr_url`, an `owner/repo` slug, or a cloned workspace whose `origin` we can read), so per-repo resolution is always possible.
- **Test conventions:** tests live in `tests/`, use `asyncio_mode = "auto"` (async test functions run without a decorator), and mock subprocess / Docker / HTTP. Run a single test with `uv run pytest tests/<file>::<test> -v`. Run the suite with `uv run pytest --cov=orchestrator --cov-report=term-missing -v`.
- **Lint/type before each commit:** `uv run ruff format src/ tests/` then `uv run ruff check --fix src/ tests/` then `uv run mypy src/orchestrator/ --ignore-missing-imports`.
- **Style:** Python 3.11, `X | Y` unions, `list[str]` generics, Google-style docstrings, `logging` not `print`, catch specific exceptions, `raise ... from`.
- **No em dashes** in prose, docs, or commit messages. Use a comma, colon, or semicolon.

### GitHub App REST endpoints used

- Resolve installation for a repo: `GET https://api.github.com/repos/{owner}/{repo}/installation`, header `Authorization: Bearer <app-jwt>`, `Accept: application/vnd.github+json`. Response JSON has `id` (the installation id).
- Mint an installation token: `POST https://api.github.com/app/installations/{installation_id}/access_tokens`, header `Authorization: Bearer <app-jwt>`, body `{"repositories": ["<repo-name>"], "permissions": {"contents": "write", "pull_requests": "write"}}`. Response JSON has `token` (string) and `expires_at` (ISO-8601, e.g. `2026-07-02T13:00:00Z`).
- The App JWT is `jwt.encode({"iat": now-60, "exp": now+540, "iss": app_id}, private_key_pem, algorithm="RS256")`.

## File structure

- **Create** `src/orchestrator/core/github_credentials.py` — provider protocol, `repo_slug_from_url`, `CredentialError`, `PatCredentialProvider`, `GitHubAppCredentialProvider`, `build_credential_provider` factory. One responsibility: turn config + a repo reference into a usable GitHub token.
- **Create** `tests/test_github_credentials.py` — unit tests for slug parsing, both providers, cache/expiry, and factory precedence.
- **Modify** `src/orchestrator/config.py` — add `github_app_id`, `github_app_private_key`, `github_app_installation_id`; make `github_token` optional.
- **Modify** `src/orchestrator/core/git_ops.py` — `GitOps` accepts a provider (str still accepted for back-compat), resolves a per-repo token per operation.
- **Modify** `src/orchestrator/core/agent_manager.py` — `spawn_agent` mints a fresh token from the provider.
- **Modify** `src/orchestrator/core/brainstorm.py` and `src/orchestrator/core/context_sync.py` — accept a provider, resolve per-repo tokens.
- **Modify** `src/orchestrator/main.py` — build the provider once and thread it everywhere.
- **Modify** `pyproject.toml`, `.env.example`, `docs/deployment.md`.

---

### Task 1: Dependencies, config fields, and .env template

**Files:**
- Modify: `pyproject.toml` (dependencies list)
- Modify: `src/orchestrator/config.py`
- Modify: `.env.example`
- Test: `tests/test_config.py`

**Depends on:** None

- [ ] **Step 1: Add the JWT dependencies**

In `pyproject.toml`, add to the runtime `dependencies` array (keep the array alphabetically grouped with the existing entries):

```toml
    "pyjwt>=2.8",
    "cryptography>=42.0",
```

Then run: `uv sync --extra dev`
Expected: resolves and installs `pyjwt` and `cryptography`.

- [ ] **Step 2: Write failing tests for the new config fields**

Add to `tests/test_config.py`:

```python
def test_github_app_fields_default_none():
    from orchestrator.config import Settings

    settings = Settings(
        auth_token="t",
        github_token="ghp_x",
        _env_file=None,
    )
    assert settings.github_app_id is None
    assert settings.github_app_private_key is None
    assert settings.github_app_installation_id is None


def test_github_token_optional_when_app_configured():
    from orchestrator.config import Settings

    settings = Settings(
        auth_token="t",
        github_app_id="12345",
        github_app_private_key="-----BEGIN PRIVATE KEY-----\nabc\n-----END PRIVATE KEY-----",
        _env_file=None,
    )
    assert settings.github_token is None
    assert settings.github_app_id == "12345"
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `uv run pytest tests/test_config.py::test_github_app_fields_default_none tests/test_config.py::test_github_token_optional_when_app_configured -v`
Expected: FAIL (`github_token` is currently required; new fields do not exist).

- [ ] **Step 4: Make `github_token` optional and add App fields**

In `src/orchestrator/config.py`, change the `github_token` line and add the three new fields directly beneath it:

```python
    github_token: str | None = None
    github_app_id: str | None = None
    # PEM contents OR a path to a PEM file holding the App private key.
    github_app_private_key: str | None = None
    github_app_installation_id: int | None = None
```

- [ ] **Step 5: Run the config tests to verify they pass**

Run: `uv run pytest tests/test_config.py -v`
Expected: PASS (all existing config tests plus the two new ones).

- [ ] **Step 6: Document the new env vars in `.env.example`**

Append to `.env.example`:

```bash
# --- GitHub authentication ---------------------------------------------------
# Option A (legacy, still supported): a Personal Access Token.
# GITHUB_TOKEN=ghp_your_token_here
#
# Option B (recommended): a GitHub App. When these are set, Praxis mints
# short-lived, repo-scoped installation tokens instead of using a broad PAT.
# GITHUB_APP_ID=123456
# GITHUB_APP_PRIVATE_KEY=/run/secrets/praxis-app.pem   # PEM contents or a file path
# GITHUB_APP_INSTALLATION_ID=  # optional; auto-resolved per repo when unset
```

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml src/orchestrator/config.py .env.example tests/test_config.py
git commit -m "feat: add GitHub App config fields and JWT deps"
```

---

### Task 2: Credential provider module (slug helper, protocol, PAT backend)

**Files:**
- Create: `src/orchestrator/core/github_credentials.py`
- Test: `tests/test_github_credentials.py`

**Depends on:** None

- [ ] **Step 1: Write the failing tests**

Create `tests/test_github_credentials.py`:

```python
import pytest

from orchestrator.core.github_credentials import (
    CredentialError,
    PatCredentialProvider,
    repo_slug_from_url,
)


@pytest.mark.parametrize(
    "url,expected",
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_github_credentials.py -v`
Expected: FAIL with `ModuleNotFoundError: orchestrator.core.github_credentials`.

- [ ] **Step 3: Create the module with the slug helper, protocol, and PAT backend**

Create `src/orchestrator/core/github_credentials.py`:

```python
"""GitHub credential providers.

Turns configuration plus a repository reference into a usable GitHub token.
Two backends are provided: a legacy static Personal Access Token, and a GitHub
App backend that mints short-lived, repo-scoped installation tokens.
"""

from __future__ import annotations

import re
from typing import Protocol


class CredentialError(RuntimeError):
    """Raised when a GitHub credential cannot be resolved or minted."""


def repo_slug_from_url(repo_url: str) -> str:
    """Extract an ``owner/repo`` slug from a GitHub URL, SSH URL, or bare slug.

    Args:
        repo_url: e.g. ``https://github.com/owner/repo(.git)``,
            ``git@github.com:owner/repo.git``, or ``owner/repo``.

    Returns:
        The ``owner/repo`` slug.

    Raises:
        CredentialError: If no ``owner/repo`` can be extracted.
    """
    text = repo_url.strip()
    if text.endswith(".git"):
        text = text[: -len(".git")]
    match = re.search(r"github\.com[/:]([^/]+/[^/]+?)(?:/|$)", text)
    if match:
        return match.group(1)
    if re.fullmatch(r"[^/\s]+/[^/\s]+", text):
        return text
    msg = f"cannot extract owner/repo from: {repo_url!r}"
    raise CredentialError(msg)


class GitHubCredentialProvider(Protocol):
    """Resolves a GitHub token usable for a given repository."""

    async def token_for_repo(self, repo_url: str) -> str:
        """Return a token authorized for ``repo_url``."""
        ...


class PatCredentialProvider:
    """Returns a single static Personal Access Token for every repo.

    This preserves the legacy behavior: the same broad token is used everywhere.
    """

    def __init__(self, token: str) -> None:
        self._token = token

    async def token_for_repo(self, repo_url: str) -> str:  # noqa: ARG002
        return self._token
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_github_credentials.py -v`
Expected: PASS (6 test cases).

- [ ] **Step 5: Lint, type-check, commit**

```bash
uv run ruff format src/ tests/ && uv run ruff check --fix src/ tests/ && uv run mypy src/orchestrator/ --ignore-missing-imports
git add src/orchestrator/core/github_credentials.py tests/test_github_credentials.py
git commit -m "feat: add credential provider seam with PAT backend"
```

---

### Task 3: GitHub App credential backend (JWT, installation resolve, mint, cache)

**Files:**
- Modify: `src/orchestrator/core/github_credentials.py`
- Test: `tests/test_github_credentials.py`

**Depends on:** Task 2

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_github_credentials.py` (add the imports shown at the top of the block to the existing import section):

```python
import time

import httpx

from orchestrator.core.github_credentials import GitHubAppCredentialProvider


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
    # Body of the mint request scopes to the single repo with write perms.
    mint = next(r for r in calls if r.url.path.endswith("/access_tokens"))
    import json as _json

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
                json={"token": f"ghs_{mint_count}", "expires_at": "2026-07-02T13:00:00Z"},
            )
        return httpx.Response(404)

    clock = _FakeClock(time.mktime(time.strptime("2026-07-02T12:00:00Z", "%Y-%m-%dT%H:%M:%SZ")))
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

    # Advance to within 5 minutes of expiry: a fresh mint is required.
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_github_credentials.py -k app_provider -v`
Expected: FAIL with `ImportError: cannot import name 'GitHubAppCredentialProvider'`.

- [ ] **Step 3: Implement the App backend**

Add to `src/orchestrator/core/github_credentials.py` (extend the imports at the top, then append the class):

```python
import logging
import time
from collections.abc import Callable

import httpx
import jwt
```

```python
logger = logging.getLogger(__name__)

_API_BASE = "https://api.github.com"
# Refresh a cached token when fewer than this many seconds of life remain.
_REFRESH_MARGIN_S = 300
_ACCEPT = "application/vnd.github+json"


class GitHubAppCredentialProvider:
    """Mints short-lived, repo-scoped GitHub App installation tokens.

    The App private key never leaves this process. Per repo we sign a short JWT,
    resolve the installation id (cached), and mint a token scoped to that single
    repository with ``contents:write`` + ``pull_requests:write``. Minted tokens
    are cached per repo and refreshed shortly before they expire.
    """

    def __init__(
        self,
        app_id: str,
        private_key_pem: str,
        installation_id: int | None = None,
        *,
        clock: Callable[[], float] = time.time,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._app_id = app_id
        self._private_key_pem = private_key_pem
        self._fixed_installation_id = installation_id
        self._clock = clock
        self._transport = transport
        self._install_ids: dict[str, int] = {}
        # slug -> (token, expiry_epoch_seconds)
        self._token_cache: dict[str, tuple[str, float]] = {}

    def _app_jwt(self) -> str:
        now = int(self._clock())
        payload = {"iat": now - 60, "exp": now + 540, "iss": self._app_id}
        return jwt.encode(payload, self._private_key_pem, algorithm="RS256")

    def _client(self, token: str) -> httpx.AsyncClient:
        headers = {"Authorization": f"Bearer {token}", "Accept": _ACCEPT}
        return httpx.AsyncClient(
            base_url=_API_BASE,
            headers=headers,
            timeout=15,
            transport=self._transport,
        )

    async def _installation_id_for(self, slug: str) -> int:
        if self._fixed_installation_id is not None:
            return self._fixed_installation_id
        if slug in self._install_ids:
            return self._install_ids[slug]
        async with self._client(self._app_jwt()) as client:
            resp = await client.get(f"/repos/{slug}/installation")
        if resp.status_code != 200:
            msg = (
                f"cannot resolve GitHub App installation for {slug} "
                f"(status {resp.status_code}); is the App installed on that repo?"
            )
            raise CredentialError(msg)
        install_id = int(resp.json()["id"])
        self._install_ids[slug] = install_id
        return install_id

    @staticmethod
    def _parse_expiry(expires_at: str) -> float:
        return time.mktime(time.strptime(expires_at, "%Y-%m-%dT%H:%M:%SZ"))

    async def token_for_repo(self, repo_url: str) -> str:
        slug = repo_slug_from_url(repo_url)
        cached = self._token_cache.get(slug)
        if cached is not None:
            token, expiry = cached
            if expiry - self._clock() > _REFRESH_MARGIN_S:
                return token
        install_id = await self._installation_id_for(slug)
        repo_name = slug.split("/", 1)[1]
        body = {
            "repositories": [repo_name],
            "permissions": {"contents": "write", "pull_requests": "write"},
        }
        async with self._client(self._app_jwt()) as client:
            resp = await client.post(
                f"/app/installations/{install_id}/access_tokens", json=body
            )
        if resp.status_code not in (200, 201):
            msg = (
                f"failed to mint installation token for {slug} "
                f"(status {resp.status_code}): {resp.text}"
            )
            raise CredentialError(msg)
        data = resp.json()
        token = str(data["token"])
        expiry = self._parse_expiry(data["expires_at"])
        self._token_cache[slug] = (token, expiry)
        logger.info("Minted installation token for %s (expires %s)", slug, data["expires_at"])
        return token
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_github_credentials.py -v`
Expected: PASS (all cases including the three App tests).

- [ ] **Step 5: Lint, type-check, commit**

```bash
uv run ruff format src/ tests/ && uv run ruff check --fix src/ tests/ && uv run mypy src/orchestrator/ --ignore-missing-imports
git add src/orchestrator/core/github_credentials.py tests/test_github_credentials.py
git commit -m "feat: add GitHub App installation-token backend"
```

---

### Task 4: Provider factory with precedence and key loading

**Files:**
- Modify: `src/orchestrator/core/github_credentials.py`
- Test: `tests/test_github_credentials.py`

**Depends on:** Task 2, Task 3

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_github_credentials.py`:

```python
from orchestrator.core.github_credentials import build_credential_provider


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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_github_credentials.py -k factory -v`
Expected: FAIL with `ImportError: cannot import name 'build_credential_provider'`.

- [ ] **Step 3: Implement the factory and key loader**

Add to `src/orchestrator/core/github_credentials.py` (extend imports with `os` and `from pathlib import Path`, then append):

```python
def _load_private_key(value: str) -> str:
    """Return PEM contents, reading from a file path when ``value`` is one.

    A value containing a PEM header is used verbatim; otherwise, if it points at
    an existing file, that file is read.
    """
    if "BEGIN" in value:
        return value
    path = Path(value)
    if path.is_file():
        return path.read_text(encoding="utf-8")
    return value


def build_credential_provider(settings: object) -> GitHubCredentialProvider:
    """Choose a credential backend from settings.

    Precedence: GitHub App (when ``github_app_id`` and ``github_app_private_key``
    are both set) beats the legacy PAT. Raises when neither is configured.
    """
    app_id = getattr(settings, "github_app_id", None)
    private_key = getattr(settings, "github_app_private_key", None)
    if app_id and private_key:
        install_id = getattr(settings, "github_app_installation_id", None)
        return GitHubAppCredentialProvider(
            app_id=str(app_id),
            private_key_pem=_load_private_key(str(private_key)),
            installation_id=install_id,
        )
    token = getattr(settings, "github_token", None)
    if token:
        return PatCredentialProvider(str(token))
    msg = (
        "no GitHub credentials configured: set GITHUB_TOKEN, or "
        "GITHUB_APP_ID + GITHUB_APP_PRIVATE_KEY"
    )
    raise CredentialError(msg)
```

Note: `os` is not strictly needed by the code above; only add imports you actually use (`from pathlib import Path`). Remove the `os` import mention if `ruff` flags it as unused.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_github_credentials.py -v`
Expected: PASS (all cases).

- [ ] **Step 5: Lint, type-check, commit**

```bash
uv run ruff format src/ tests/ && uv run ruff check --fix src/ tests/ && uv run mypy src/orchestrator/ --ignore-missing-imports
git add src/orchestrator/core/github_credentials.py tests/test_github_credentials.py
git commit -m "feat: add credential provider factory with App-over-PAT precedence"
```

---

### Task 5: GitOps resolves a per-repo token from the provider

**Files:**
- Modify: `src/orchestrator/core/git_ops.py`
- Test: `tests/test_git_ops.py`

**Depends on:** Task 2

**Background:** `GitOps.__init__` currently stores a raw `github_token` string and `_run_command` always sets `GH_TOKEN` to it. We change the constructor to accept a `GitHubCredentialProvider` (still accepting a bare `str` for back-compat so existing tests pass), and resolve a per-repo token at each remote-touching method, threading it into `_run_command(cmd, cwd, token=...)`. Local-only git operations (checkout, diff, log) pass no token.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_git_ops.py`:

```python
from orchestrator.core.github_credentials import PatCredentialProvider


def test_gitops_accepts_str_token_for_backcompat():
    from orchestrator.core.git_ops import GitOps

    ops = GitOps("ghp_legacy")
    assert isinstance(ops._provider, PatCredentialProvider)


def test_gitops_accepts_provider():
    from orchestrator.core.git_ops import GitOps

    provider = PatCredentialProvider("ghp_x")
    ops = GitOps(provider)
    assert ops._provider is provider


async def test_remote_branch_exists_resolves_repo_token(monkeypatch):
    from orchestrator.core.git_ops import GitOps

    class _Recording(PatCredentialProvider):
        def __init__(self):
            super().__init__("ghs_scoped")
            self.seen: list[str] = []

        async def token_for_repo(self, repo_url):
            self.seen.append(repo_url)
            return await super().token_for_repo(repo_url)

    provider = _Recording()
    ops = GitOps(provider)

    captured = {}

    async def fake_run(cmd, cwd=None, token=None):
        captured["token"] = token
        return (0, "abc123\trefs/heads/main", "")

    monkeypatch.setattr(ops, "_run_command", fake_run)

    result = await ops.remote_branch_exists("https://github.com/o/r", "main")
    assert result is True
    assert provider.seen == ["https://github.com/o/r"]
    assert captured["token"] == "ghs_scoped"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_git_ops.py -k "backcompat or provider or resolves_repo_token" -v`
Expected: FAIL (`_provider` does not exist; `_run_command` does not accept `token`).

- [ ] **Step 3: Update the constructor and `_run_command`**

In `src/orchestrator/core/git_ops.py`, add the import near the top:

```python
from orchestrator.core.github_credentials import (
    GitHubCredentialProvider,
    PatCredentialProvider,
)
```

Replace the constructor (lines around 108-109) and `_run_command` (lines around 111-130):

```python
    def __init__(
        self, credentials: GitHubCredentialProvider | str
    ) -> None:
        # Accept a bare token for back-compat; wrap it in a PAT provider.
        if isinstance(credentials, str):
            credentials = PatCredentialProvider(credentials)
        self._provider: GitHubCredentialProvider = credentials

    async def _token_for_repo(self, repo_ref: str) -> str:
        return await self._provider.token_for_repo(repo_ref)

    async def _token_for_workspace(self, workspace: str) -> str:
        origin = await self._run_checked(
            ["git", "-C", workspace, "remote", "get-url", "origin"]
        )
        return await self._provider.token_for_repo(origin)

    async def _run_command(
        self,
        cmd: list[str],
        cwd: str | None = None,
        token: str | None = None,
    ) -> tuple[int, str, str]:
        env = os.environ.copy()
        if token is not None:
            env["GH_TOKEN"] = token
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        stdout, stderr = await proc.communicate()
        return (
            proc.returncode or 0,
            stdout.decode().strip(),
            stderr.decode().strip(),
        )

    async def _run_checked(
        self, cmd: list[str], cwd: str | None = None, token: str | None = None
    ) -> str:
        code, stdout, stderr = await self._run_command(cmd, cwd, token)
        if code != 0:
            message = f"Git command failed (exit {code}): {' '.join(cmd)}\n{stderr}"
            raise RuntimeError(message)
        return stdout
```

- [ ] **Step 4: Thread tokens into the remote-touching methods**

Replace the method bodies as follows.

`clone_repo` (adds credential helper + token):

```python
    async def clone_repo(self, repo_url: str, workspace: str) -> None:
        token = await self._token_for_repo(repo_url)
        await self._run_checked(
            ["git", *_token_git_args(), "clone", repo_url, workspace], token=token
        )
        logger.info("Cloned %s to %s", repo_url, workspace)
```

`create_branch` (the `git pull` is remote; resolve from the workspace origin):

```python
    async def create_branch(
        self,
        workspace: str,
        branch: str,
        base: str = "main",
    ) -> None:
        token = await self._token_for_workspace(workspace)
        await self._run_checked(["git", "checkout", base], cwd=workspace)
        await self._run_checked(
            ["git", *_token_git_args(), "-C", workspace, "pull", "origin", base],
            token=token,
        )
        await self._run_checked(["git", "checkout", "-b", branch], cwd=workspace)
        logger.info("Created branch %s from %s", branch, base)
```

`push_branch`:

```python
    async def push_branch(self, workspace: str, branch: str) -> None:
        token = await self._token_for_workspace(workspace)
        await self._run_checked(
            ["git", *_token_git_args(), "-C", workspace, "push", "-u", "origin", branch],
            token=token,
        )
        logger.info("Pushed branch %s", branch)
```

`create_pr` (gh reads GH_TOKEN):

```python
    async def create_pr(
        self,
        workspace: str,
        title: str,
        body: str,
        base: str,
        head: str,
    ) -> str:
        token = await self._token_for_workspace(workspace)
        stdout = await self._run_checked(
            [
                "gh", "pr", "create",
                "--title", title,
                "--body", body,
                "--base", base,
                "--head", head,
            ],
            cwd=workspace,
            token=token,
        )
        logger.info("Created PR: %s", stdout)
        return stdout.strip()
```

`merge_pr` (prefer the explicit repo slug when given, else workspace origin):

```python
    async def merge_pr(
        self, workspace: str, pr_number: int, repo: str | None = None
    ) -> None:
        token = (
            await self._token_for_repo(repo)
            if repo
            else await self._token_for_workspace(workspace)
        )
        await self._run_checked(
            [
                "gh", "pr", "merge", str(pr_number),
                "--squash", "--delete-branch",
                *(["--repo", repo] if repo else []),
            ],
            cwd=workspace,
            token=token,
        )
        logger.info("Merged PR #%d", pr_number)
```

`comment_on_pr`:

```python
    async def comment_on_pr(
        self,
        workspace: str,
        pr_number: int,
        comment: str,
        repo: str | None = None,
    ) -> None:
        token = (
            await self._token_for_repo(repo)
            if repo
            else await self._token_for_workspace(workspace)
        )
        await self._run_checked(
            [
                "gh", "pr", "comment", str(pr_number),
                "--body", comment,
                *(["--repo", repo] if repo else []),
            ],
            cwd=workspace,
            token=token,
        )
        logger.info("Commented on PR #%d", pr_number)
```

`get_pr_diff`:

```python
    async def get_pr_diff(
        self, workspace: str, pr_number: int, repo: str | None = None
    ) -> str:
        token = (
            await self._token_for_repo(repo)
            if repo
            else await self._token_for_workspace(workspace)
        )
        return await self._run_checked(
            ["gh", "pr", "diff", str(pr_number), *(["--repo", repo] if repo else [])],
            cwd=workspace,
            token=token,
        )
```

`clone_pr_head` (both the clone and the `gh pr checkout` need the token; resolve once from the repo slug):

```python
    async def clone_pr_head(self, pr_url: str, dest: str) -> str:
        repo = self.repo_slug(pr_url)
        if repo is None:
            msg = f"cannot extract repo slug from PR URL: {pr_url}"
            raise RuntimeError(msg)
        token = await self._token_for_repo(repo)
        pr_number = await self.extract_pr_number(pr_url)
        clone_url = f"https://github.com/{repo}.git"
        cmd_clone = [
            "git", *_token_git_args(),
            "clone", "--depth", "1", "--no-single-branch",
            clone_url, dest,
        ]
        code, _, stderr = await self._run_command(cmd_clone, token=token)
        if code != 0:
            msg = f"clone failed (exit {code}) for {clone_url}: {stderr}"
            raise RuntimeError(msg)
        await self._run_checked(
            ["gh", "pr", "checkout", str(pr_number), "--repo", repo],
            cwd=dest,
            token=token,
        )
        logger.info("Cloned PR #%d head into %s", pr_number, dest)
        return dest
```

`remote_branch_exists`:

```python
    async def remote_branch_exists(self, repo_url: str, branch: str) -> bool:
        token = await self._token_for_repo(repo_url)
        cmd = ["git", *_token_git_args(), "ls-remote", "--heads", repo_url, branch]
        code, stdout, stderr = await self._run_command(cmd, token=token)
        if code != 0:
            msg = f"git ls-remote failed (exit {code}): {stderr}"
            raise RuntimeError(msg)
        ref = f"refs/heads/{branch}"
        return any(ref == line.split("\t")[-1] for line in stdout.splitlines() if line)
```

`remote_file_exists` (REST call uses the resolved token in the Bearer header):

```python
    async def remote_file_exists(self, repo_slug: str, branch: str, path: str) -> bool:
        token = await self._token_for_repo(repo_slug)
        url = f"https://api.github.com/repos/{repo_slug}/contents/{path}"
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
        }
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(url, headers=headers, params={"ref": branch})
        except httpx.HTTPError as exc:
            msg = f"network error checking file on GitHub: {exc}"
            raise RuntimeError(msg) from exc
        if resp.status_code == 200:
            return True
        if resp.status_code == 404:
            return False
        msg = (
            f"unexpected GitHub API status {resp.status_code}"
            f" for {repo_slug}/{path}@{branch}"
        )
        raise RuntimeError(msg)
```

Leave purely local methods unchanged (`get_changed_files`, `branch_commit_log`, `extract_pr_number`, `repo_slug`). They do not need `GH_TOKEN`.

- [ ] **Step 5: Run the git_ops tests to verify they pass**

Run: `uv run pytest tests/test_git_ops.py -v`
Expected: PASS. If any existing test asserted `_run_command` set `GH_TOKEN` unconditionally, update it to pass/expect a `token` argument. If a test constructs `GitOps("token")` and calls a remote method with a mocked `_run_command`, it keeps working because the PAT provider returns that token.

- [ ] **Step 6: Run the full suite to catch regressions**

Run: `uv run pytest --cov=orchestrator -q`
Expected: PASS (fix any test that patched the old `_run_command` signature).

- [ ] **Step 7: Lint, type-check, commit**

```bash
uv run ruff format src/ tests/ && uv run ruff check --fix src/ tests/ && uv run mypy src/orchestrator/ --ignore-missing-imports
git add src/orchestrator/core/git_ops.py tests/test_git_ops.py
git commit -m "refactor: resolve per-repo GitHub tokens in GitOps"
```

---

### Task 6: AgentManager mints a fresh token per dispatch

**Files:**
- Modify: `src/orchestrator/core/agent_manager.py`
- Test: `tests/test_agent_manager.py`

**Depends on:** Task 2

**Background:** `AgentManager.__init__` takes `github_token: str` and injects `self._github_token` as `GH_TOKEN` (line ~118). We swap it for a provider and mint a fresh, repo-scoped token in `spawn_agent` using the dispatch's `repo_url`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_agent_manager.py` (follow the file's existing pattern for mocking `docker.from_env`; if the suite already has a fixture/helper that builds an `AgentManager` with a fake docker client, reuse it and just assert on the injected env):

```python
async def test_spawn_agent_injects_freshly_minted_token(monkeypatch):
    import orchestrator.core.agent_manager as am
    from orchestrator.core.github_credentials import PatCredentialProvider

    # Fake docker client capturing the run() kwargs.
    captured = {}

    class _FakeContainers:
        def run(self, **kwargs):
            captured.update(kwargs)

            class _C:
                id = "deadbeefcafe"

            return _C()

        def get(self, name):
            raise am.docker.errors.NotFound("none")

    class _FakeClient:
        containers = _FakeContainers()

    monkeypatch.setattr(am.docker, "from_env", lambda: _FakeClient())
    # Avoid a real LM Studio round-trip for context detection.
    monkeypatch.setattr(am, "detect_context_limit", _async_return(None))

    manager = am.AgentManager(
        lm_studio_url="http://host.docker.internal:1234",
        credentials=PatCredentialProvider("ghs_fresh"),
    )
    await manager.spawn_agent(
        task_id="task1234abcd",
        repo_url="https://github.com/o/r",
        branch="agent/x",
        base_branch="main",
        task_prompt="do the thing",
        model_name="qwen3",
        callback_url="http://host.docker.internal:8080/api/internal/agent-done",
    )
    assert captured["environment"]["GH_TOKEN"] == "ghs_fresh"


def _async_return(value):
    async def _inner(*args, **kwargs):
        return value

    return _inner
```

If `detect_context_limit` is imported differently in the module, patch it at the name the module actually uses. Check the top of `agent_manager.py` for the exact import.

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_agent_manager.py -k freshly_minted -v`
Expected: FAIL (`AgentManager` has no `credentials` parameter).

- [ ] **Step 3: Swap the constructor and mint at spawn**

In `src/orchestrator/core/agent_manager.py`, add the import:

```python
from orchestrator.core.github_credentials import (
    GitHubCredentialProvider,
    PatCredentialProvider,
)
```

Change the constructor (lines ~75-84):

```python
    def __init__(
        self,
        lm_studio_url: str,
        credentials: GitHubCredentialProvider | str,
        effective_settings: EffectiveSettings | None = None,
    ) -> None:
        self._lm_studio_url = lm_studio_url
        if isinstance(credentials, str):
            credentials = PatCredentialProvider(credentials)
        self._provider: GitHubCredentialProvider = credentials
        self._effective_settings = effective_settings
        self._client = docker.from_env()  # type: ignore[attr-defined]
```

In `spawn_agent`, replace the `"GH_TOKEN": self._github_token,` line with a freshly minted token. Just before building the `environment` dict, add:

```python
        gh_token = await self._provider.token_for_repo(repo_url)
```

and change the dict entry to:

```python
            "GH_TOKEN": gh_token,
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/test_agent_manager.py -k freshly_minted -v`
Expected: PASS.

- [ ] **Step 5: Run the agent_manager suite**

Run: `uv run pytest tests/test_agent_manager.py -v`
Expected: PASS. Update any existing test that constructed `AgentManager(..., github_token="x")` to use `credentials="x"` (the keyword changed) or pass a `PatCredentialProvider`.

- [ ] **Step 6: Lint, type-check, commit**

```bash
uv run ruff format src/ tests/ && uv run ruff check --fix src/ tests/ && uv run mypy src/orchestrator/ --ignore-missing-imports
git add src/orchestrator/core/agent_manager.py tests/test_agent_manager.py
git commit -m "feat: mint fresh repo-scoped token per agent dispatch"
```

---

### Task 7: BrainstormManager and ContextSync use the provider

**Files:**
- Modify: `src/orchestrator/core/brainstorm.py`
- Modify: `src/orchestrator/core/context_sync.py`
- Test: `tests/test_brainstorm.py`, `tests/test_context_sync.py`

**Depends on:** Task 2

**Background:** Both classes store `self._token` and call `clone_with_token(repo_url, dest, self._token, ...)` and `commit_and_push(workspace, self._token, ...)`. Both have the `repo_url` in scope when they clone and push. We store a provider and resolve `token_for_repo(repo_url)` at each call. `clone_with_token` and `commit_and_push` (module functions in `git_ops.py`) keep their explicit `token` parameter and do not change.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_brainstorm.py`:

```python
async def test_brainstorm_resolves_token_from_provider(monkeypatch):
    import orchestrator.core.brainstorm as bs
    from orchestrator.core.github_credentials import PatCredentialProvider

    seen = {}

    def fake_clone(repo_url, dest, token, depth=50):
        seen["clone_token"] = token

    monkeypatch.setattr(bs, "clone_with_token", fake_clone)

    mgr = bs.BrainstormManager(
        workspace_base="/tmp/x",
        event_bus=None,
        credentials=PatCredentialProvider("ghs_scoped"),
    )
    dest = await mgr._clone("https://github.com/o/r", "read-1")  # noqa: SLF001
    assert seen["clone_token"] == "ghs_scoped"
```

Note: match `_clone`'s real signature. Open `brainstorm.py` around line 149 and mirror the actual method name and arguments used to call `clone_with_token`. If `_clone` is not a separate method, write the test against whichever public method performs the clone, asserting the token passed to the patched `clone_with_token`.

Add the analogous test to `tests/test_context_sync.py`:

```python
async def test_context_sync_resolves_token_from_provider(monkeypatch):
    import orchestrator.core.context_sync as cs
    from orchestrator.core.github_credentials import PatCredentialProvider

    seen = {}

    def fake_clone(repo_url, dest, token, depth=20):
        seen["clone_token"] = token

    monkeypatch.setattr(cs, "clone_with_token", fake_clone)

    sync = cs.ContextSync(
        workspace_base="/tmp/x",
        credentials=PatCredentialProvider("ghs_scoped"),
        memory_md_path="docs/MEMORY.md",
    )
    # Call whichever method performs the clone; mirror its real signature.
    await sync._clone("https://github.com/o/r")  # noqa: SLF001
    assert seen["clone_token"] == "ghs_scoped"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_brainstorm.py -k resolves_token tests/test_context_sync.py -k resolves_token -v`
Expected: FAIL (`credentials` parameter does not exist).

- [ ] **Step 3: Update `BrainstormManager`**

In `src/orchestrator/core/brainstorm.py`, add the import:

```python
from orchestrator.core.github_credentials import (
    GitHubCredentialProvider,
    PatCredentialProvider,
)
```

Change the constructor (around line 140):

```python
    def __init__(
        self,
        workspace_base: str,
        event_bus: object,
        credentials: GitHubCredentialProvider | str,
    ) -> None:
        if isinstance(credentials, str):
            credentials = PatCredentialProvider(credentials)
        self._provider: GitHubCredentialProvider = credentials
        # ... keep the rest of the existing constructor body unchanged ...
```

Wherever the class calls `clone_with_token(repo_url, dest, self._token, depth=50)` (around line 149), resolve first:

```python
        token = await self._provider.token_for_repo(repo_url)
        clone_with_token(repo_url, dest, token, depth=50)
```

Wherever it calls `commit_and_push(workspace, self._token, ...)` (around line 238), resolve from the same `repo_url` in scope for that operation:

```python
        token = await self._provider.token_for_repo(repo_url)
        commit_and_push(workspace, token, "docs: update spec", paths=[path])
```

If `repo_url` is not already a local variable at the push site, derive it from the same value used to clone earlier in that method (it is passed into the method or stored alongside the workspace). Do not read it from `.git/config`.

- [ ] **Step 4: Update `ContextSync`**

In `src/orchestrator/core/context_sync.py`, apply the same pattern: add the import, change the constructor parameter from `github_token: str` to `credentials: GitHubCredentialProvider | str` (wrapping a `str`), store `self._provider`, and at each `clone_with_token(...)` / `commit_and_push(...)` call site resolve `token = await self._provider.token_for_repo(repo_url)` first, passing `token` in place of `self._token`.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/test_brainstorm.py tests/test_context_sync.py -v`
Expected: PASS. Update any existing test constructing these classes with `github_token=` to use `credentials=`.

- [ ] **Step 6: Lint, type-check, commit**

```bash
uv run ruff format src/ tests/ && uv run ruff check --fix src/ tests/ && uv run mypy src/orchestrator/ --ignore-missing-imports
git add src/orchestrator/core/brainstorm.py src/orchestrator/core/context_sync.py tests/test_brainstorm.py tests/test_context_sync.py
git commit -m "feat: resolve per-repo tokens in brainstorm and context sync"
```

---

### Task 8: Wire the provider through startup (`main.py`)

**Files:**
- Modify: `src/orchestrator/main.py`
- Test: `tests/test_main_lifespan.py` (or the existing lifespan/startup test file; search for one that imports `lifespan` or builds the app)

**Depends on:** Task 4, Task 5, Task 6, Task 7

**Background:** The lifespan currently passes `settings.github_token` to five places. Build one provider via the factory and pass it everywhere. Keep the `_before_drop_cb` gate working: it must run when *any* GitHub credential is configured, not only a PAT.

- [ ] **Step 1: Write the failing test**

Add to the startup test file (adapt the app-construction helper to the one already used in that file):

```python
async def test_lifespan_builds_app_credential_provider(monkeypatch):
    import orchestrator.main as main
    from orchestrator.core.github_credentials import GitHubAppCredentialProvider

    built = {}
    real = main.build_credential_provider

    def spy(settings):
        provider = real(settings)
        built["type"] = type(provider).__name__
        return provider

    monkeypatch.setattr(main, "build_credential_provider", spy)
    monkeypatch.setenv("AUTH_TOKEN", "t")
    monkeypatch.setenv("GITHUB_APP_ID", "123")
    monkeypatch.setenv(
        "GITHUB_APP_PRIVATE_KEY",
        "-----BEGIN PRIVATE KEY-----\nx\n-----END PRIVATE KEY-----",
    )
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    # Build the app / run the lifespan the same way the file's other tests do,
    # then assert the factory chose the App backend.
    async with main.lifespan(main.app):
        pass
    assert built["type"] == GitHubAppCredentialProvider.__name__
```

If constructing the app requires Docker or network that the test environment lacks, mirror how the existing lifespan tests neutralize those (they typically monkeypatch `AgentManager` or let it fail into `None`). The only new assertion is that `build_credential_provider` was used.

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_main_lifespan.py -k app_credential_provider -v`
Expected: FAIL (`build_credential_provider` is not referenced in `main`).

- [ ] **Step 3: Build the provider once and thread it through**

In `src/orchestrator/main.py`, add the import near the other core imports:

```python
from orchestrator.core.github_credentials import build_credential_provider
```

Near the top of the lifespan, after `settings` is available and before `BrainstormManager` is constructed, add:

```python
    credential_provider = build_credential_provider(settings)
```

Then update the five construction sites:

- `_brainstorm_pre = BrainstormManager(... github_token=settings.github_token)` becomes `credentials=credential_provider`.
- Replace the `_before_drop_cb` gate so it fires whenever any credential exists:

```python
    _has_git_creds = bool(
        settings.github_token
        or (settings.github_app_id and settings.github_app_private_key)
    )
    _before_drop_cb = _before_drop if _has_git_creds else None
```

- `git_ops = GitOps(settings.github_token)` becomes `git_ops = GitOps(credential_provider)`.
- `AgentManager(... github_token=settings.github_token ...)` becomes `credentials=credential_provider`.
- `BrainstormManager(... github_token=settings.github_token)` (the second, post-EventBus one) becomes `credentials=credential_provider`.
- `ContextSync(... github_token=settings.github_token ...)` becomes `credentials=credential_provider`.

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/test_main_lifespan.py -k app_credential_provider -v`
Expected: PASS.

- [ ] **Step 5: Run the full suite**

Run: `uv run pytest --cov=orchestrator --cov-report=term-missing -q`
Expected: PASS, coverage still >=80%. Fix any remaining call sites that used the old keyword arguments.

- [ ] **Step 6: Lint, type-check, commit**

```bash
uv run ruff format src/ tests/ && uv run ruff check --fix src/ tests/ && uv run mypy src/orchestrator/ --ignore-missing-imports
git add src/orchestrator/main.py tests/test_main_lifespan.py
git commit -m "feat: build one credential provider at startup and thread it through"
```

---

### Task 9: Documentation and known-limitation note

**Files:**
- Modify: `docs/deployment.md`
- Modify: `CLAUDE.md` (Gotchas section)

**Depends on:** Task 8

- [ ] **Step 1: Document GitHub App setup in `docs/deployment.md`**

Add a "GitHub authentication" section. Include both paths and the 1h ceiling. Write it in prose without em dashes:

```markdown
## GitHub authentication

Praxis needs GitHub credentials to clone repos, push agent branches, and manage
PRs. Two options:

### Option A: Personal Access Token (legacy)

Set `GITHUB_TOKEN` to a PAT with `repo` scope. Simple, but the token is
long-lived and broadly scoped, and it is injected into every agent container.

### Option B: GitHub App (recommended)

Praxis mints short-lived (<=1 hour), repo-scoped installation tokens per
operation. The App private key stays on the orchestrator and is never placed in
a worker container.

1. Create a GitHub App (Settings > Developer settings > GitHub Apps). Grant
   repository permissions: Contents (Read and write) and Pull requests (Read and
   write).
2. Generate a private key (PEM) and store it as an orchestrator secret.
3. Install the App on the repositories Praxis will operate on.
4. Configure the orchestrator:
   - `GITHUB_APP_ID` = the App's numeric id
   - `GITHUB_APP_PRIVATE_KEY` = the PEM contents or a path to the PEM file
   - `GITHUB_APP_INSTALLATION_ID` = optional; auto-resolved per repo when unset

When App variables are present they take precedence over `GITHUB_TOKEN`.

**Known limitation:** installation tokens expire after 1 hour. An agent run that
exceeds an hour may fail its final push on an expired token. A future refresh
endpoint will let a worker renew its token mid-run.
```

- [ ] **Step 2: Add a Gotcha to `CLAUDE.md`**

Add under the Gotchas section:

```markdown
- **GitHub credentials go through a provider seam** — `core/github_credentials.py`
  resolves a token per repo. With `GITHUB_APP_ID` + `GITHUB_APP_PRIVATE_KEY` set,
  Praxis mints short-lived, repo-scoped installation tokens (App private key never
  enters a container); otherwise it falls back to the static `GITHUB_TOKEN` PAT.
  `GitOps`, `AgentManager`, `BrainstormManager`, and `ContextSync` all take a
  `credentials` provider (a bare token string is still accepted and wrapped in a
  `PatCredentialProvider`). Installation tokens cap at 1h, so a >1h agent run can
  fail its final push (refresh endpoint is a planned follow-up).
```

- [ ] **Step 3: Commit**

```bash
git add docs/deployment.md CLAUDE.md
git commit -m "docs: document GitHub App credential setup"
```

---

## Parallel Execution Map

- **Wave 1:** Task 1 (config/deps), Task 2 (provider seam + PAT) — no dependencies, run in parallel.
- **Wave 2:** Task 3 (App backend), Task 5 (GitOps), Task 6 (AgentManager), Task 7 (brainstorm/context_sync) — each depends only on Task 2.
- **Wave 3:** Task 4 (factory) — depends on Task 2 and Task 3.
- **Wave 4:** Task 8 (main.py wiring) — depends on Task 4, Task 5, Task 6, Task 7.
- **Wave 5:** Task 9 (docs) — depends on Task 8.

Note on parallelism: Tasks 5, 6, 7 all touch different source files but each adds tests and may adjust shared test helpers. If run concurrently in the same worktree, commit each before starting the next to avoid clobbering. Task 4 can run in parallel with 5/6/7 since it only touches `github_credentials.py` and its test file.

---

## Final verification (after all tasks)

- [ ] Run the full suite: `uv run pytest --cov=orchestrator --cov-report=term-missing -v` — expect all green, coverage >=80%.
- [ ] Type-check: `uv run mypy src/orchestrator/ --ignore-missing-imports` — expect clean.
- [ ] Lint: `uv run ruff check src/ tests/` and `uv run ruff format --check src/ tests/` — expect clean.
- [ ] Sanity: `PatCredentialProvider` path is unchanged behavior. Grep for any remaining `settings.github_token` producers that were missed: `rg "github_token" src/orchestrator` should only show `config.py` (the field) and the factory reading it.

## Notes for a live end-to-end test (not part of the coded plan)

Real GitHub App verification needs a registered App and an installation, so it is
out of scope for the unit-tested plan. When ready to verify live: register a test
App, install it on a throwaway repo, set the three env vars, and run a single
dispatch through `POST /api/dispatch`. Confirm the agent clones and pushes, the PR
is created, and `docker inspect <agent-container>` shows a `ghs_`-prefixed
installation token in `GH_TOKEN` (not a `ghp_` PAT).
