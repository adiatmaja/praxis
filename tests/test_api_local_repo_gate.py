"""The local-repo opt-in, enforced at every endpoint that takes a ``repo_url``.

The schema layer cannot see runtime settings, so it admits a local filesystem
path and defers.  These tests pin that all three endpoints actually ask, and
that with the default (off) a local path is still refused with a 422, which is
the same status the schema used to return.

They also pin the reason for the whole change: with the opt-in ON, a prepared
BARE repo is accepted end to end, which is what bench Phase A's local git
backend needs and what the REST surface used to make impossible.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest
from httpx import AsyncClient

from tests.conftest import seed_user


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def bare_repo(tmp_path: Path) -> Path:
    """A real bare repo with a ``main`` branch, the shape preflight demands."""
    work, bare = tmp_path / "w", tmp_path / "r.git"
    work.mkdir()
    _git("init", "-b", "main", cwd=work)
    _git("config", "user.email", "t@e.com", cwd=work)
    _git("config", "user.name", "t", cwd=work)
    _git("config", "commit.gpgsign", "false", cwd=work)
    (work / "app.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    _git("add", ".", cwd=work)
    _git("commit", "-m", "base", cwd=work)
    _git("clone", "--bare", str(work), str(bare), cwd=tmp_path)
    return bare


def _payload(endpoint: str, repo_url: str) -> dict[str, Any]:
    if endpoint == "/api/projects":
        return {"name": "local", "repo_url": repo_url, "model_name": "m"}
    if endpoint == "/api/dispatch":
        return {"repo_url": repo_url, "instructions": "do it", "model": "m"}
    return {"repo_url": repo_url, "plan": "# plan\n\n- [ ] step", "model": "m"}


ENDPOINTS = ["/api/projects", "/api/dispatch", "/api/execute-plan"]


@pytest.mark.integration
@pytest.mark.parametrize("endpoint", ENDPOINTS)
async def test_a_local_path_is_refused_by_default(
    client: AsyncClient, auth_headers: dict[str, str], db: Any, endpoint: str
) -> None:
    await seed_user(db)
    resp = await client.post(
        endpoint, json=_payload(endpoint, "/srv/praxis/r.git"), headers=auth_headers
    )
    assert resp.status_code == 422, resp.text
    assert "allow_local_repo_paths" in resp.text


@pytest.mark.integration
@pytest.mark.parametrize("endpoint", ENDPOINTS)
async def test_the_opt_in_admits_a_prepared_bare_repo(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db: Any,
    bare_repo: Path,
    endpoint: str,
) -> None:
    """With the opt-in ON the request is accepted, no GitHub credential involved."""
    await seed_user(db)
    client.app.state.settings.allow_local_repo_paths = True  # type: ignore[attr-defined]
    resp = await client.post(
        endpoint, json=_payload(endpoint, str(bare_repo)), headers=auth_headers
    )
    assert resp.status_code == 201, resp.text


@pytest.mark.integration
async def test_a_non_bare_local_repo_is_still_rejected_by_preflight(
    client: AsyncClient, auth_headers: dict[str, str], db: Any, tmp_path: Path
) -> None:
    """The opt-in admits the FORM; preflight still enforces the shape."""
    await seed_user(db)
    client.app.state.settings.allow_local_repo_paths = True  # type: ignore[attr-defined]
    work = tmp_path / "notbare"
    work.mkdir()
    _git("init", "-b", "main", cwd=work)
    resp = await client.post(
        "/api/projects", json=_payload("/api/projects", str(work)), headers=auth_headers
    )
    assert resp.status_code == 422, resp.text
    assert "BARE" in resp.text


@pytest.mark.integration
@pytest.mark.parametrize("endpoint", ENDPOINTS)
async def test_a_blocked_transport_is_refused_even_with_the_opt_in_on(
    client: AsyncClient, auth_headers: dict[str, str], db: Any, endpoint: str
) -> None:
    """The opt-in widens LOCAL paths only; it never relaxes a transport."""
    await seed_user(db)
    client.app.state.settings.allow_local_repo_paths = True  # type: ignore[attr-defined]
    resp = await client.post(
        endpoint, json=_payload(endpoint, "ext::sh -c whoami"), headers=auth_headers
    )
    assert resp.status_code == 422, resp.text
