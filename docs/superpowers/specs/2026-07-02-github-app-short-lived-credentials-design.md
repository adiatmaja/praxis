---
title: Short-lived GitHub App credentials
date: 2026-07-02
status: design
---

# Short-lived GitHub App credentials

## Problem

Praxis authenticates to GitHub with a single long-lived Personal Access Token
(`github_token`, sourced from the `GITHUB_TOKEN` env var). That one PAT is used
for two very different things:

1. **Orchestrator-side git/API operations** — PR review/merge/comment via `gh`,
   repo clones, `ls-remote`, and REST calls with `Authorization: Bearer`
   (`core/git_ops.py`, `core/brainstorm.py`, `core/context_sync.py`,
   `core/backfill.py`).
2. **Worker credential** — the same token is injected verbatim into every agent
   container as `GH_TOKEN` (`core/agent_manager.py`), where each harness
   entrypoint wires it into a git credential helper to clone and push.

Path 2 is the open security finding: a broad, long-lived credential that carries
whatever the user's account can touch sits as a plain environment variable
inside an untrusted worker container. A leaked worker token is high-value and
long-lived.

## Goal

Replace the long-lived broad PAT with **short-lived, repo-scoped GitHub App
installation tokens** minted per dispatch. The orchestrator's durable secret
becomes the App private key, a signing key that never leaves the orchestrator
and is never placed in a container. Every actual git/API credential — the
orchestrator's own and each worker's — becomes a token that expires in <=1h and
is scoped to a single repository.

Existing PAT-based deployments must keep working unchanged; the GitHub App is
opt-in.

## Non-goals

- Automating GitHub App registration or installation (interactive, user-driven).
- Refreshing a worker token mid-run (see Known Limitations; deferred follow-up).
- Changing the `GH_TOKEN` env contract or credential-helper mechanics inside
  containers. Only the *value* of the token changes.

## Design

### 1. Credential-provider seam

Introduce `core/github_credentials.py` with a provider protocol:

```python
class GitHubCredentialProvider(Protocol):
    async def token_for_repo(self, repo_url: str) -> str: ...
```

Every consumer that today reads `settings.github_token` instead calls
`provider.token_for_repo(repo_url)`:

- `GitOps` — clone, commit/push, `clone_pr_head`, `ls-remote`, PR ops, REST
  (all already repo-aware per call).
- `AgentManager.spawn_agent` — mints a fresh token and injects it as `GH_TOKEN`.
- `brainstorm` / `context_sync` clones, `backfill`.

The `GH_TOKEN` env contract inside containers and the git credential-helper
plumbing do not change. Only the token value becomes short-lived and repo-scoped.

### 2. Two backends

**`PatCredentialProvider`** — wraps the existing `github_token`.
`token_for_repo` ignores the repo and returns the static PAT. This is the
unchanged legacy path, so existing deployments behave exactly as before.

**`GitHubAppCredentialProvider`** — holds `app_id` and the App private key. For a
given repo it:

1. Signs a short-lived JWT (10-minute expiry) with the PEM private key
   (`PyJWT` + `cryptography`).
2. Resolves the installation id via `GET /repos/{owner}/{repo}/installation`
   (authenticated with the JWT), cached per repo. An explicit
   `github_app_installation_id` config short-circuits this lookup.
3. Mints an installation token via
   `POST /app/installations/{id}/access_tokens`, scoped to the single repo with
   `contents: write` and `pull_requests: write` permissions.

Installation tokens are cached per repo and refreshed when fewer than 5 minutes
of TTL remain. The private key never leaves the orchestrator and is never placed
in a container.

### 3. Config and precedence (`config.py`)

Add:

- `github_app_id: str | None`
- `github_app_private_key: str | None` — PEM contents or a path to a PEM file.
- `github_app_installation_id: int | None` — optional; auto-resolved when unset.

`github_token` becomes optional (required only when no App is configured). A
factory selects the provider:

> If App config is present (`github_app_id` + `github_app_private_key`), use the
> App provider. Otherwise use the PAT provider from `github_token`.

New dependencies: `PyJWT`, `cryptography`.

### 4. Token lifecycle for agents

`spawn_agent` mints a **fresh** token at spawn time (maximizing remaining TTL)
and injects it as `GH_TOKEN`. The container clones immediately; the only
TTL-sensitive moment is the final push on a run approaching 1h.

### 5. Orchestrator Dockerfile helper

The baked-in global git credential helper that reads `$GITHUB_TOKEN`
(`docker/orchestrator/Dockerfile`) stays as a fallback for any incidental
in-container `git push` (e.g. brainstorm doc commits). The primary
orchestrator-side paths in `git_ops` already pass the token per call via the
`GH_TOKEN` env, so they use the minted token regardless of the baked-in helper.

### 6. Error handling

- **App configured but key invalid or installation missing** — fail fast at
  startup with a clear message. Do **not** silently fall back to the PAT; that
  would mask misconfiguration and quietly re-open the finding.
- **Token mint failure at dispatch** — the task fails with the GitHub API error
  surfaced in its feedback, the same as any other dispatch error.

### 7. Testing

- Unit-test both providers: mock the GitHub REST API and the JWT clock.
- Test factory precedence (App config present vs. PAT-only vs. neither).
- Test per-repo installation-id cache and token expiry/refresh (<5 min TTL
  triggers a re-mint).
- Existing `GitOps` / `AgentManager` tests inject the PAT provider so they stay
  green with no behavioral change.

## Known limitations

Installation tokens cap at **1 hour**. A worker run exceeding 1h could fail its
final push on an expired token. v1 documents this ceiling. The clean fix,
deferred as a follow-up, is a small authenticated `POST /api/internal/repo-token`
endpoint that a worker calls to refresh its token, reusing the existing
callback-token auth.

## Rollout

1. Ship the provider seam with the PAT backend wired in — pure refactor, no
   behavior change, all existing tests green.
2. Add the GitHub App backend, config, and factory precedence.
3. Document GitHub App setup (register App, install on target repos, supply
   `GITHUB_APP_ID` + private key) in deployment docs, alongside the retained PAT
   path.
