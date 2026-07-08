# Agent Container Security Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close two of the three open agent-container security findings from the 2026-07-02 OSS review: fail-closed callback auth (Finding 3) and bridge-network agent containers (Finding 1). Finding 2 (`GH_TOKEN` plain env var) is scoped as a documented follow-up, not implemented here.

**Architecture:** Two independent, self-contained changes. (A) `api/internal.py` currently *skips* callback-token verification when the secret is unset (fail-open, warn-only); flip it to reject with `503` (fail-closed), set the secret in the test `client` fixture to mirror production, and repoint the affected tests. (B) `agent_manager.py` currently runs agent containers with `network_mode="host"`, which shares the host network stack with untrusted worker-LLM code; switch to Docker's default bridge network plus `extra_hosts={"host.docker.internal": "host-gateway"}` (needed on Linux), and rewrite any `localhost`/`127.0.0.1` in the LM Studio URL to `host.docker.internal` so the containerized agent can still reach LM Studio and the callback endpoint on the host.

**Tech Stack:** Python 3.11, FastAPI, Docker SDK for Python, pytest (`asyncio_mode = "auto"`), ruff, mypy.

---

## Background & Context (read before starting)

This plan came out of the OSS-readiness review recorded in memory `oss-review-2026-07-02`. The three open findings were:

1. **Host-network agent containers** — `agent_manager.py` spawns worker containers with `network_mode="host"`. Worker LLM code (untrusted, model-generated) shares the host's entire network stack; the only isolation is filesystem. This is the biggest adopter-facing flag. **This plan fixes it** (Tasks 4-7).
2. **`GH_TOKEN` plain env var** — the GitHub token is passed as a container env var (visible via `docker inspect`, readable by the worker). The "serious" fix is short-lived GitHub App installation tokens; an interim mitigation is passing the token via a mounted file instead of env. **This plan does NOT fix it** — see "Follow-up: Finding 2" at the end for the scoped design.
3. **Fail-open callback auth** — `api/internal.py::_verify_callback_token` returns (allows the request) when `app.state.internal_callback_secret` is `None`, logging a warning only. In production the secret is always set (it derives from the required `auth_token`), so this is defense-in-depth, not an exploitable hole — but it should fail closed by construction. **This plan fixes it** (Tasks 1-3).

### Key files and their current state

**`src/orchestrator/api/internal.py`** — the callback endpoint. Current verification function (lines 30-51):

```python
def _verify_callback_token(request: Request) -> None:
    expected: str | None = getattr(request.app.state, "internal_callback_secret", None)
    if expected is None:
        logger.warning(
            "internal_callback_secret not configured; skipping callback auth "
            "(set INTERNAL_CALLBACK_SECRET or AUTH_TOKEN to enable)"
        )
        return
    provided = request.headers.get(_CALLBACK_TOKEN_HEADER, "")
    if not secrets.compare_digest(provided.encode(), expected.encode()):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing callback token",
        )
```

The header constant is `_CALLBACK_TOKEN_HEADER = "x-praxis-callback-token"` (line 18).

**`src/orchestrator/main.py`** (lines 77-81) — production always sets the secret in the lifespan:

```python
app.state.internal_callback_secret = (
    settings.internal_callback_secret or settings.auth_token
)
```

`settings.auth_token` is a **required** field (`config.py:23`, `auth_token: str`, no default), so `internal_callback_secret` is never `None` in a real deployment. The fail-open path is only reachable when the lifespan did not run — i.e. in-process tests.

**`src/orchestrator/core/agent_manager.py`** — `spawn_agent` builds the container env and runs it. Current `containers.run` call (lines 129-136):

```python
container = self._client.containers.run(
    image=spec.image,
    name=container_name,
    environment=environment,
    detach=True,
    auto_remove=False,
    network_mode="host",
)
```

The env dict (lines 94-105) sets `"OPENAI_API_BASE": f"{lm_studio_url}/v1"` where `lm_studio_url` comes from `await self._effective_settings.lm_studio_url()` (or `self._lm_studio_url` fallback). It also sets `"CALLBACK_URL": callback_url` (passed in by the caller; production value is `http://host.docker.internal:{port}/api/internal/agent-done` from `Settings.callback_url()`).

### Why bridge networking needs the two extra changes

- **`extra_hosts={"host.docker.internal": "host-gateway"}`** — on Docker Desktop (Windows/Mac) `host.docker.internal` resolves automatically, but on native Linux it does NOT unless you add this mapping. The callback URL already uses `host.docker.internal`, so without this the callback 404s/connection-refuses on Linux hosts.
- **localhost rewrite** — under `host` networking, `OPENAI_API_BASE=http://localhost:1234/v1` reaches LM Studio because the container shares the host loopback. Under bridge networking, `localhost` inside the container is the container itself, so LM Studio becomes unreachable. Any `localhost`/`127.0.0.1` in the LM Studio URL must be rewritten to `host.docker.internal` for the container env only (do not mutate the stored setting).

### Test harness facts

- `tests/conftest.py::client` builds `app.state` **manually** (lines 60-91) and does NOT set `internal_callback_secret`. So after the fail-closed flip, every test that POSTs `/api/internal/agent-done` without a secret configured would get `503` unless we set the secret in the fixture. Task 1 sets it in the fixture.
- Callbacks are POSTed in `tests/test_api_tasks.py` (lines 174, 200) and `tests/test_security.py` (lines 42, 66, 85, 112). After the flip these need the `X-Praxis-Callback-Token` header (Tasks 2-3).
- `test_settings.auth_token` is `"test-auth"` (the `auth_headers` fixture uses `Bearer test-auth`, conftest line 96). Use that same value as the callback secret in the fixture so it is consistent.
- Agent-manager spawn tests live in `tests/test_agent_manager.py`; they assert on `mock_client.containers.run.call_args.kwargs`. Task 4 adds assertions there.

---

## File Structure

- `src/orchestrator/api/internal.py` — MODIFY `_verify_callback_token` to fail closed (503 when secret unset).
- `src/orchestrator/core/agent_manager.py` — MODIFY `spawn_agent`: drop `network_mode="host"`, add `extra_hosts`, rewrite localhost in LM Studio URL for the container env. Add a small pure helper `_container_host_url`.
- `tests/conftest.py` — MODIFY `client` fixture to set `app.state.internal_callback_secret`.
- `tests/test_api_tasks.py` — MODIFY the two callback POSTs to send the header.
- `tests/test_security.py` — MODIFY the fail-open test into a fail-closed test.
- `tests/test_agent_manager.py` — ADD assertions for bridge network + localhost rewrite.
- `CLAUDE.md` — UPDATE the two relevant gotchas (callback auth, host networking) to reflect the new behavior.

---

### Task 1: Set callback secret in the test client fixture

**Files:**
- Modify: `tests/conftest.py:64-87` (the `client` fixture body, before the transport is built)

**Depends on:** None

This must land first: it makes the existing callback tests keep passing once Task 3 flips the endpoint to fail-closed. Setting it now (before the endpoint changes) is a no-op for current behavior (a configured secret with matching header already passes; the two `test_api_tasks` callbacks don't send a header yet, but the current fail-open logic ignores the secret anyway... actually with the secret SET and fail-open removed later they'd 401). To avoid an intermediate broken state, do Tasks 1→2→3 as one reviewed unit, but keep them as separate commits.

- [ ] **Step 1: Add the secret assignment to the fixture**

In `tests/conftest.py`, inside the `client` fixture, add this line right after `app.state.settings = test_settings` (line 65):

```python
    app.state.internal_callback_secret = test_settings.auth_token
```

- [ ] **Step 2: Run the callback tests to confirm still green (fail-open still active, secret now set + header sent by security tests only)**

Run: `uv run pytest tests/test_security.py tests/test_api_tasks.py -q`
Expected: PASS (the `test_api_tasks` callbacks still pass because the endpoint is still fail-open in this commit; `test_security` tests already set/reset their own secret).

- [ ] **Step 3: Commit**

```bash
git add tests/conftest.py
git commit -m "test: set internal_callback_secret in client fixture"
```

---

### Task 2: Send the callback token header in the task-callback tests

**Files:**
- Modify: `tests/test_api_tasks.py:174-182` and `tests/test_api_tasks.py:200-203`

**Depends on:** Task 1

- [ ] **Step 1: Add the header to `test_agent_done_callback`**

Change the POST at line 174 to include the header (the secret value matches `test_settings.auth_token` set in Task 1):

```python
    response = await client.post(
        "/api/internal/agent-done",
        json={
            "task_id": task_id,
            "run_id": run_id,
            "status": "completed",
            "pr_url": "https://github.com/u/a/pull/1",
        },
        headers={"X-Praxis-Callback-Token": "test-auth"},
    )
```

- [ ] **Step 2: Add the header to `test_agent_done_callback_without_run_id_uses_latest_run`**

Change the POST at line 200:

```python
    response = await client.post(
        "/api/internal/agent-done",
        json={"task_id": task_id, "status": "completed"},
        headers={"X-Praxis-Callback-Token": "test-auth"},
    )
```

- [ ] **Step 3: Run the tests to confirm still green**

Run: `uv run pytest tests/test_api_tasks.py -q`
Expected: PASS (sending a valid header is accepted by the still-fail-open endpoint).

- [ ] **Step 4: Commit**

```bash
git add tests/test_api_tasks.py
git commit -m "test: send callback token header in agent-done tests"
```

---

### Task 3: Fail closed when the callback secret is unconfigured

**Files:**
- Modify: `src/orchestrator/api/internal.py:30-51` (`_verify_callback_token`)
- Modify: `tests/test_security.py:94-116` (rewrite `test_callback_no_secret_configured_still_works`)

**Depends on:** Task 1, Task 2

- [ ] **Step 1: Rewrite the fail-open test into a fail-closed test (RED)**

Replace `test_callback_no_secret_configured_still_works` (lines 94-116) in `tests/test_security.py` with:

```python
@pytest.mark.integration
async def test_callback_no_secret_configured_fails_closed(
    client: AsyncClient,
    db: Database,
    auth_headers: dict[str, str],
) -> None:
    """When internal_callback_secret is unset, the callback is rejected (503)."""
    from tests.test_api_tasks import _setup_plan_with_task

    # Remove the secret the fixture set, to simulate a misconfigured deploy.
    if hasattr(client.app.state, "internal_callback_secret"):
        del client.app.state.internal_callback_secret  # type: ignore[attr-defined]

    _, task_id = await _setup_plan_with_task(client, db, auth_headers)
    queue = client.app.state.task_queue  # type: ignore[attr-defined]
    run_id = await queue.create_agent_run(task_id, "container-noauth")

    response = await client.post(
        "/api/internal/agent-done",
        json={"task_id": task_id, "run_id": run_id, "status": "completed"},
        headers={"X-Praxis-Callback-Token": "anything"},
    )
    assert response.status_code == 503
    # The fixture sets the secret; restore it so later tests are unaffected.
    client.app.state.internal_callback_secret = "test-auth"  # type: ignore[attr-defined]
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_security.py::test_callback_no_secret_configured_fails_closed -v`
Expected: FAIL — the current endpoint returns `200` (fail-open), not `503`.

- [ ] **Step 3: Flip the endpoint to fail closed**

In `src/orchestrator/api/internal.py`, replace the `if expected is None:` block inside `_verify_callback_token` so it raises instead of returning:

```python
def _verify_callback_token(request: Request) -> None:
    """Reject the request unless it carries the correct callback token.

    The expected secret is ``app.state.internal_callback_secret``, set during
    application startup (see ``main.py`` lifespan) from
    ``INTERNAL_CALLBACK_SECRET`` or the required ``AUTH_TOKEN``. If it is unset,
    the server is misconfigured and we fail CLOSED (503) rather than accepting
    unauthenticated callbacks. Tests that exercise the endpoint set the secret
    on ``app.state`` via the client fixture.
    """
    expected: str | None = getattr(request.app.state, "internal_callback_secret", None)
    if expected is None:
        logger.error(
            "internal_callback_secret not configured; rejecting callback "
            "(set INTERNAL_CALLBACK_SECRET or AUTH_TOKEN)"
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Callback authentication is not configured",
        )
    provided = request.headers.get(_CALLBACK_TOKEN_HEADER, "")
    if not secrets.compare_digest(provided.encode(), expected.encode()):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing callback token",
        )
```

- [ ] **Step 4: Run the security + task callback tests to verify all pass**

Run: `uv run pytest tests/test_security.py tests/test_api_tasks.py -q`
Expected: PASS (new fail-closed test passes; valid-header tests pass; missing/wrong-token tests still 401).

- [ ] **Step 5: Commit**

```bash
git add src/orchestrator/api/internal.py tests/test_security.py
git commit -m "fix: fail closed on agent-done callback when secret unconfigured"
```

---

### Task 4: Write the bridge-network + localhost-rewrite tests (RED)

**Files:**
- Test: `tests/test_agent_manager.py` (add two new tests near the existing `test_spawn_agent` tests)

**Depends on:** None

- [ ] **Step 1: Add the bridge-network assertion test**

Append to `tests/test_agent_manager.py`:

```python
@pytest.mark.unit
@patch("orchestrator.core.agent_manager.docker")
async def test_spawn_agent_uses_bridge_network_not_host(
    mock_docker: MagicMock,
) -> None:
    """Agent containers must NOT use host networking; they use bridge + host-gateway."""
    mock_client = MagicMock()
    mock_docker.from_env.return_value = mock_client
    mock_client.containers.run.return_value = _mock_container()

    manager = AgentManager(
        lm_studio_url="http://host.docker.internal:1234", github_token="ghp_x"
    )
    await manager.spawn_agent(
        task_id="net-1",
        repo_url="https://github.com/u/r.git",
        branch="agent/x",
        base_branch="plan/x",
        task_prompt="p",
        model_name="m",
        callback_url="http://host.docker.internal:8080/api/internal/agent-done",
    )

    kwargs = mock_client.containers.run.call_args.kwargs
    assert kwargs.get("network_mode") != "host"
    assert kwargs["extra_hosts"] == {"host.docker.internal": "host-gateway"}


@pytest.mark.unit
@patch("orchestrator.core.agent_manager.docker")
async def test_spawn_agent_rewrites_localhost_lm_studio_url(
    mock_docker: MagicMock,
) -> None:
    """A localhost LM Studio URL is rewritten to host.docker.internal for the container."""
    mock_client = MagicMock()
    mock_docker.from_env.return_value = mock_client
    mock_client.containers.run.return_value = _mock_container()

    manager = AgentManager(
        lm_studio_url="http://localhost:1234", github_token="ghp_x"
    )
    await manager.spawn_agent(
        task_id="net-2",
        repo_url="https://github.com/u/r.git",
        branch="agent/x",
        base_branch="plan/x",
        task_prompt="p",
        model_name="m",
        callback_url="http://host.docker.internal:8080/api/internal/agent-done",
    )

    env = mock_client.containers.run.call_args.kwargs["environment"]
    assert env["OPENAI_API_BASE"] == "http://host.docker.internal:1234/v1"
```

Note: these tests rely on the `_no_real_context_probe` autouse fixture already in the file (it patches `detect_context_limit` to return `None`), so no LM Studio call happens.

- [ ] **Step 2: Run the new tests to verify they fail**

Run: `uv run pytest tests/test_agent_manager.py::test_spawn_agent_uses_bridge_network_not_host tests/test_agent_manager.py::test_spawn_agent_rewrites_localhost_lm_studio_url -v`
Expected: FAIL — `network_mode == "host"`, `extra_hosts` KeyError, and `OPENAI_API_BASE` still uses `localhost`.

- [ ] **Step 3: Commit the failing tests**

```bash
git add tests/test_agent_manager.py
git commit -m "test: assert bridge network + localhost rewrite for agent spawn"
```

---

### Task 5: Add the container-host URL rewrite helper

**Files:**
- Modify: `src/orchestrator/core/agent_manager.py` (add a module-level pure helper)
- Test: `tests/test_agent_manager.py` (add unit tests for the helper)

**Depends on:** Task 4

- [ ] **Step 1: Write the helper unit tests (RED)**

Append to `tests/test_agent_manager.py` (import the helper at the top of the file where other `agent_manager` symbols are imported):

```python
@pytest.mark.unit
def test_container_host_url_rewrites_localhost() -> None:
    from orchestrator.core.agent_manager import _container_host_url

    assert (
        _container_host_url("http://localhost:1234")
        == "http://host.docker.internal:1234"
    )


@pytest.mark.unit
def test_container_host_url_rewrites_127_0_0_1() -> None:
    from orchestrator.core.agent_manager import _container_host_url

    assert (
        _container_host_url("http://127.0.0.1:1234")
        == "http://host.docker.internal:1234"
    )


@pytest.mark.unit
def test_container_host_url_leaves_remote_untouched() -> None:
    from orchestrator.core.agent_manager import _container_host_url

    assert (
        _container_host_url("http://host.docker.internal:1234")
        == "http://host.docker.internal:1234"
    )
    assert _container_host_url("http://192.168.1.5:1234") == "http://192.168.1.5:1234"
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_agent_manager.py -k container_host_url -v`
Expected: FAIL with `ImportError` / `cannot import name '_container_host_url'`.

- [ ] **Step 3: Implement the helper**

In `src/orchestrator/core/agent_manager.py`, add this module-level function (near the top, after imports, before the `AgentManager` class):

```python
def _container_host_url(url: str) -> str:
    """Rewrite a host-loopback URL so it is reachable from inside a bridge container.

    Under host networking a container could reach the orchestrator's LM Studio on
    ``localhost``; under bridge networking ``localhost`` is the container itself, so
    loopback hosts must be swapped for ``host.docker.internal`` (mapped to the host
    gateway via ``extra_hosts``). Non-loopback hosts are returned unchanged.
    """
    for loopback in ("localhost", "127.0.0.1"):
        url = url.replace(f"//{loopback}:", "//host.docker.internal:").replace(
            f"//{loopback}/", "//host.docker.internal/"
        )
    return url
```

- [ ] **Step 4: Run to verify they pass**

Run: `uv run pytest tests/test_agent_manager.py -k container_host_url -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/orchestrator/core/agent_manager.py tests/test_agent_manager.py
git commit -m "feat: add container-host URL rewrite helper for bridge networking"
```

---

### Task 6: Switch agent containers to bridge networking

**Files:**
- Modify: `src/orchestrator/core/agent_manager.py` — the `OPENAI_API_BASE` env line (~line 99) and the `containers.run` call (lines 129-136)

**Depends on:** Task 4, Task 5

- [ ] **Step 1: Rewrite the LM Studio URL for the container env**

In `spawn_agent`, after `lm_studio_url` is resolved (the `if self._effective_settings is not None:` block, ~lines 90-93) and before the `environment = {...}` dict, add:

```python
        container_lm_url = _container_host_url(lm_studio_url)
```

Then change the env dict's `OPENAI_API_BASE` entry (line 99) from:

```python
            "OPENAI_API_BASE": f"{lm_studio_url}/v1",
```

to:

```python
            "OPENAI_API_BASE": f"{container_lm_url}/v1",
```

Leave `detect_context_limit(lm_studio_url, model_name)` (line 124) using the ORIGINAL `lm_studio_url` — that call runs orchestrator-side (host), not in the container, so it must keep using the host-reachable URL.

- [ ] **Step 2: Drop host networking, add the host-gateway mapping**

Change the `containers.run` call (lines 129-136) from `network_mode="host"` to bridge + `extra_hosts`:

```python
        container = self._client.containers.run(
            image=spec.image,
            name=container_name,
            environment=environment,
            detach=True,
            auto_remove=False,
            extra_hosts={"host.docker.internal": "host-gateway"},
        )
```

(Omitting `network_mode` uses Docker's default bridge network.)

- [ ] **Step 3: Run the full agent-manager suite**

Run: `uv run pytest tests/test_agent_manager.py -q`
Expected: PASS — including the two Task 4 tests and the three Task 5 helper tests. The pre-existing `test_spawn_agent_sets_correct_env` uses `http://localhost:9999`, so verify it does not assert on `OPENAI_API_BASE` (it does not — it checks `REPO_URL`, `BRANCH`, `BASE_BRANCH`, `MODEL`, `HARNESS`). If any pre-existing test asserts `OPENAI_API_BASE` with a localhost value, update it to expect the rewritten `host.docker.internal` form.

- [ ] **Step 4: Commit**

```bash
git add src/orchestrator/core/agent_manager.py
git commit -m "fix: run agent containers on bridge network instead of host"
```

---

### Task 7: Update CLAUDE.md gotchas + run full verification

**Files:**
- Modify: `CLAUDE.md` (the callback-auth and host-networking notes)

**Depends on:** Task 3, Task 6

- [ ] **Step 1: Update the callback-auth gotcha**

In `CLAUDE.md`, find the callback-auth description (the gotcha mentioning "Agent callbacks retry with backoff" / `internal_callback_secret`) and add a sentence stating the endpoint now **fails closed** (503) when `internal_callback_secret` is unconfigured, and that production always has it set because it derives from the required `AUTH_TOKEN`.

- [ ] **Step 2: Update the agent-isolation / networking note**

Find any statement that agent containers use `network_mode="host"` (in `CLAUDE.md` and, if present, the "Security Model" text) and replace it with: agent containers run on Docker's default **bridge** network with `extra_hosts={"host.docker.internal": "host-gateway"}`; the LM Studio URL is rewritten via `_container_host_url` so a `localhost` orchestrator setting stays reachable from inside the container. Note the remaining isolation caveat: the worker can still reach `host.docker.internal` (needed for LM Studio + callback), so this reduces but does not eliminate host network exposure.

- [ ] **Step 3: Run the full suite + lint + type check**

Run:
```bash
uv run pytest --cov=orchestrator --cov-fail-under=80 -q
uv run ruff format --check src/ tests/
uv run ruff check src/ tests/
uv run mypy src/orchestrator/ --ignore-missing-imports
```
Expected: all green, coverage >= 80%.

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: reflect fail-closed callback auth + bridge-network agents"
```

---

## Parallel Execution Map

- **Wave 1:** Task 1 (no deps), Task 4 (no deps) — run in parallel.
- **Wave 2:** Task 2 (depends on Task 1), Task 5 (depends on Task 4) — run in parallel.
- **Wave 3:** Task 3 (depends on Task 1, Task 2), Task 6 (depends on Task 4, Task 5) — run in parallel (different files: `internal.py`+`test_security.py` vs `agent_manager.py`).
- **Wave 4:** Task 7 (depends on Task 3, Task 6).

Note: Tasks 1-3 (Finding 3) and Tasks 4-6 (Finding 1) are fully independent tracks that only converge at the Task 7 docs/verification step. If dispatching to isolated worktrees, the two tracks can run concurrently; merge before Task 7.

---

## Manual Verification (cannot be unit-tested)

The bridge-network change alters real Docker networking, which the mocked tests cannot exercise. Before considering Finding 1 truly closed, run a **live dispatch** on a real project (see memory `e2e-blind-plan-walkthrough-2026-07-01` and `capability-continuity-e2e-test` for the e2e procedure) and confirm:

1. The agent container reaches LM Studio (implement step produces a diff, not a connection error in the agent log).
2. The `agent-done` callback reaches the orchestrator (task advances to `reviewing`, not stuck in `in_progress` until reconcile).
3. Test on the target host OS. On native Linux, `extra_hosts` host-gateway is what makes `host.docker.internal` resolve; on Docker Desktop it works either way. The primary dev host here is Windows (Docker Desktop), so also spot-check a Linux runner if adopters are Linux.

If the callback 404s or LM Studio is unreachable after this change, the most likely cause is the LM Studio URL not being rewritten (check `OPENAI_API_BASE` in `docker inspect <container>`) or `extra_hosts` not taking effect (older Docker Engine < 20.10 does not support `host-gateway`).

---

## Follow-up: Finding 2 (`GH_TOKEN` plain env var) — NOT implemented here

Deliberately out of scope for this plan; documented so a future session can pick it up.

**Current state:** `agent_manager.py:102` sets `"GH_TOKEN": self._github_token` in the container env. All three entrypoints (`docker/{aider,opencode,openhands}-agent/entrypoint.sh`) read it: a guard `: "${GH_TOKEN:?GH_TOKEN is required}"` (line 10) and a git credential helper (line ~76) `git config --global credential.helper '!f() { echo "username=x-access-token"; echo "password=${GH_TOKEN}"; }; f'`.

**Two paths:**

1. **Interim mitigation (closes the `docker inspect` vector only):** pass the token via a mounted read-only file instead of an env var. In `spawn_agent`, write the token to a per-task temp file and mount it (`volumes={tmpfile: {"bind": "/run/secrets/gh_token", "mode": "ro"}}`), set `GH_TOKEN_FILE=/run/secrets/gh_token` instead of `GH_TOKEN`, and update all three entrypoints to `GH_TOKEN="$(cat "$GH_TOKEN_FILE")"`. **Caveat:** the worker process can still `cat` the file (it needs the token to push), so this does NOT stop worker exfiltration — it only removes the token from `docker inspect` output. Given that limited benefit, decide whether it is worth the cross-cutting entrypoint churn before implementing.

2. **Serious fix (closes worker exfil too):** mint short-lived GitHub App **installation tokens** (1-hour TTL, scoped to the single repo) per dispatch instead of using a long-lived PAT. Requires: registering a GitHub App, storing its private key as an orchestrator secret, JWT-signing + calling the installation-token API at dispatch time, and threading the short-lived token through `spawn_agent`. This is a standalone epic and should get its own spec + plan (brainstorm first).

Recommendation: skip the interim mitigation (low benefit, real churn) and schedule the GitHub App path as a separate epic.
