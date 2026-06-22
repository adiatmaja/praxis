# Security Policy

## Supported Versions

Praxis is pre-1.0 software under active development. Security fixes are applied to
the `main` branch only. Pin to a commit if you need stability.

## Reporting a Vulnerability

**Please do not open public GitHub issues for security vulnerabilities.**

Report privately via one of:

- GitHub's [private vulnerability reporting](https://github.com/adiatmaja/praxis/security/advisories/new)
- Email: **adi@pambudi.com**

Include where possible:

- A description of the issue and its impact
- Steps to reproduce (proof-of-concept if available)
- Affected component (orchestrator, agent container, CLI, web UI)

You can expect an initial acknowledgement within a few days. Please allow
reasonable time for a fix before any public disclosure.

## Security Model & Operator Responsibilities

Praxis orchestrates code-writing agents and holds privileged credentials. Operators
must treat a Praxis instance as a high-trust system.

- **Secrets live in `.env`** (gitignored, never committed). Never commit real
  `AUTH_TOKEN` or `GITHUB_TOKEN` values. The `.env.example` ships placeholders only.
- **`GITHUB_TOKEN` is a GitHub PAT with `repo` scope.** It can push branches and merge
  PRs on any repo it can reach. Scope it to the minimum set of repositories and rotate
  it if exposed.
- **`AUTH_TOKEN` is a single static bearer token** guarding the entire API and dashboard.
  Use a long random value and serve only over HTTPS (the hosted Caddy profile provides
  auto-HTTPS). Do not expose an instance to the public internet without it.
- **Agents run arbitrary model-generated code** inside Docker containers as a non-root
  user. Run Praxis on hardware you control and review merged output — autonomous merge
  is gated behind review, but you remain responsible for what lands in your repos.
- **LM Studio / model endpoints** are trusted upstreams. Point `LM_STUDIO_URL` only at
  endpoints you control.

If you find a credential committed to history, rotate it immediately — rotation, not
deletion, is the fix, since git history and forks may retain the value.
