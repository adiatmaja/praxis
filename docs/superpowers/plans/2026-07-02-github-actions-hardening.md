# GitHub Actions Hardening Plan (CI additions for open-source readiness)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add the CI/CD workflows that matter for Praxis as an open-source project: Docker image build checks, Windows test coverage, dependency hygiene, and security scanning.

**Architecture:** All changes are config-only (workflow YAML + Dependabot config), except small code annotations that security scanners may require. Each task is one independent workflow; nothing here touches application logic. Everything extends the existing `.github/workflows/ci.yml` pattern.

**Tech Stack:** GitHub Actions, uv (Python package manager), Docker, pip-audit, bandit, gitleaks, CodeQL, Dependabot.

---

## Context for a fresh session (read this first)

You are working in `C:\working-space\praxis` (repo: https://github.com/adiatmaja/praxis.git, work on `main` or a feature branch and PR it). Praxis is a Python 3.11 FastAPI orchestrator that dispatches coding tasks to Docker agent containers. Read `CLAUDE.md` at the repo root first.

Facts this plan relies on (verified 2026-07-02):

- CI already exists at `.github/workflows/ci.yml` (added 2026-07-02, first run green). It runs on `ubuntu-latest`: `uv sync --extra dev`, `ruff format --check`, `ruff check`, `mypy src/orchestrator/ --ignore-missing-imports`, `pytest --cov=orchestrator --cov-fail-under=80 --timeout=120`. It sets placeholder env `AUTH_TOKEN: ci-test-token` and `GITHUB_TOKEN: placeholder` so `Settings()` resolves without a `.env` file. Follow its style (checkout@v4, astral-sh/setup-uv@v5 with `python-version: "3.11"`).
- The test suite is 507+ tests, ~35s on Ubuntu. Tests are designed to pass without Docker, LM Studio, or a `claude` CLI (external calls are mocked; `AgentManager` init failure is caught and set to `None`).
- **The primary developer works on Windows.** Several past bugs were Windows-only (command-line length limits, `.CMD` shim launching, SQLite WAL on bind mounts), so Windows CI coverage has real value here, not checkbox value.
- Four Dockerfiles exist and are NOT built by docker-compose or any CI: `docker/orchestrator/Dockerfile`, `docker/aider-agent/Dockerfile`, `docker/opencode-agent/Dockerfile`, `docker/openhands-agent/Dockerfile`. Each `docker/*-agent/` dir also has an `entrypoint.sh`. Broken images are this project's worst historical failure class (a stale/broken agent image silently wedges every task).
- Dependencies live in `pyproject.toml` with a `uv.lock` lockfile. There is no `requirements.txt` in the repo root.
- The repo is public on GitHub under `adiatmaja/praxis` (CodeQL and dependency-review are free for public repos).
- User conventions (from CLAUDE.md and global rules): conventional commit messages (`ci:`, `chore:`, `docs:`), no AI attribution lines in commits, no em dashes in prose, bandit is the user's standard Python security scanner.

Verification for any workflow change: after pushing, check the run with `gh run list --limit 3` and `gh run view <id> --log-failed` on failures. Workflows cannot be fully verified locally; lint them with actionlint (Task 6 adds it, but you can run `uvx --from actionlint-py actionlint` or download the binary at any point).

**Explicitly out of scope (decided 2026-07-02, do not add):** e2e workflows that need LM Studio or a Claude login (GitHub runners cannot reproduce the real loop; a mocked e2e tests mocks, not the product), stale-issue bots, PR labelers, greeting bots (noise on a low-traffic repo), Codecov upload (the `--cov-fail-under=80` gate already enforces the floor; revisit if PR coverage diffs are wanted later).

---

### Task 1: Docker image build checks (highest value)

**Files:**
- Create: `.github/workflows/docker.yml`

**Depends on:** None

**Why:** The harness images have zero CI coverage and stale/broken images are the #1 historical failure class. Path-filtered so it only runs when Docker files change.

- [ ] **Step 1: Create the workflow**

```yaml
name: Docker

on:
  push:
    branches: [main]
    paths:
      - "docker/**"
      - "docker-compose.yml"
      - ".github/workflows/docker.yml"
  pull_request:
    paths:
      - "docker/**"
      - "docker-compose.yml"
      - ".github/workflows/docker.yml"

jobs:
  entrypoint-syntax:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Shell-check all entrypoints
        run: |
          for f in docker/*/entrypoint.sh; do
            echo "== $f"
            bash -n "$f"
          done

  build:
    runs-on: ubuntu-latest
    strategy:
      fail-fast: false
      matrix:
        image: [orchestrator, aider-agent, opencode-agent, openhands-agent]
    steps:
      - uses: actions/checkout@v4
      - name: Build ${{ matrix.image }}
        run: |
          docker build -t praxis-ci/${{ matrix.image }} \
            -f docker/${{ matrix.image }}/Dockerfile docker/${{ matrix.image }}/
```

Note: the build context for each image is its own directory (matches the documented manual build commands in README/CLAUDE.md). If `docker/orchestrator/Dockerfile` actually needs the repo root as context (check its `COPY` lines first), special-case it: context `.` with `-f docker/orchestrator/Dockerfile`.

- [ ] **Step 2: Verify the context assumption before committing**

Run: `grep -n "^COPY\|^ADD" docker/*/Dockerfile`
Expected: agent images copy only files within their own directory (entrypoint.sh etc.); if the orchestrator image copies `src/` or `pyproject.toml`, adjust its matrix entry to build with repo-root context as described in Step 1.

- [ ] **Step 3: Commit, push, verify the run**

```bash
git add .github/workflows/docker.yml
git commit -m "ci: build all Docker images and shell-check entrypoints on docker/ changes"
git push
gh run list --limit 2
```
Expected: the Docker workflow triggers (the workflow file itself is in the path filter) and all 5 jobs pass. OpenHands and orchestrator images may take several minutes; that is normal.

---

### Task 2: Windows in the test matrix

**Files:**
- Modify: `.github/workflows/ci.yml`

**Depends on:** None

**Why:** Primary development happens on Windows; several past bugs were Windows-only. Lint/mypy stay Ubuntu-only (results are platform-independent); only pytest runs on both.

- [ ] **Step 1: Split the existing job into lint + test-matrix**

Restructure `.github/workflows/ci.yml` to:

```yaml
name: CI

on:
  push:
    branches: [main]
  pull_request:

jobs:
  lint:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Install uv
        uses: astral-sh/setup-uv@v5
        with:
          python-version: "3.11"
      - name: Install dependencies
        run: uv sync --extra dev
      - name: Format check
        run: uv run ruff format --check src/ tests/
      - name: Lint
        run: uv run ruff check src/ tests/
      - name: Type check
        run: uv run mypy src/orchestrator/ --ignore-missing-imports

  test:
    strategy:
      fail-fast: false
      matrix:
        os: [ubuntu-latest, windows-latest]
    runs-on: ${{ matrix.os }}
    env:
      AUTH_TOKEN: ci-test-token
      GITHUB_TOKEN: placeholder
    steps:
      - uses: actions/checkout@v4
      - name: Install uv
        uses: astral-sh/setup-uv@v5
        with:
          python-version: "3.11"
      - name: Install dependencies
        run: uv sync --extra dev
      - name: Tests
        run: uv run pytest --cov=orchestrator --cov-report=term-missing --cov-fail-under=80 --timeout=120
```

- [ ] **Step 2: Expect and triage Windows-only failures**

Push on a branch and open a PR first (do NOT push straight to main for this task; the Windows leg is unproven). Likely failure sources and their fixes:
- Tests assuming POSIX paths or `/tmp`: fix the test to use `tmp_path` / `tempfile`, not the workflow.
- Tests spawning subprocesses that rely on Unix tools: mark with `@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only tool")` ONLY if the code under test is genuinely POSIX-only (e.g. exercises a Linux-container path); otherwise fix the code.
- SQLite WAL/temp-dir quirks: prefer fixing the test's DB path handling.
Timeouts: the suite is ~35s on Ubuntu; Windows runners are slower, expect 2-4x. The per-test `--timeout=120` should still hold.

- [ ] **Step 3: Merge once both legs are green**

```bash
git add .github/workflows/ci.yml   # plus any test fixes, each in its own commit
git commit -m "ci: run tests on windows-latest in addition to ubuntu"
```

---

### Task 3: Dependabot + dependency review

**Files:**
- Create: `.github/dependabot.yml`
- Create: `.github/workflows/dependency-review.yml`

**Depends on:** None

- [ ] **Step 1: Create the Dependabot config**

```yaml
version: 2
updates:
  - package-ecosystem: "pip"
    directory: "/"
    schedule:
      interval: "weekly"
    groups:
      dev-dependencies:
        dependency-type: "development"
  - package-ecosystem: "github-actions"
    directory: "/"
    schedule:
      interval: "weekly"
  - package-ecosystem: "docker"
    directory: "/docker/orchestrator"
    schedule:
      interval: "weekly"
```

(Dependabot's `pip` ecosystem reads `pyproject.toml`/`uv.lock`. The docker entry keeps the orchestrator base image current; agent images pin tool versions deliberately, leave them manual.)

- [ ] **Step 2: Create the dependency-review workflow** (PR-only; blocks known-vulnerable additions)

```yaml
name: Dependency Review

on:
  pull_request:
    paths:
      - "pyproject.toml"
      - "uv.lock"

permissions:
  contents: read

jobs:
  dependency-review:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/dependency-review-action@v4
        with:
          fail-on-severity: high
```

- [ ] **Step 3: Commit and push**

```bash
git add .github/dependabot.yml .github/workflows/dependency-review.yml
git commit -m "ci: add dependabot and dependency-review gate"
git push
```

Verify: the repo's Insights → Dependency graph → Dependabot tab shows the config as active (may take a few minutes).

---

### Task 4: Security scanning (pip-audit + bandit + gitleaks)

**Files:**
- Create: `.github/workflows/security.yml`
- Possibly modify: `pyproject.toml` (bandit config section) and isolated `# nosec` annotations in `src/`

**Depends on:** None

- [ ] **Step 1: Create the workflow**

```yaml
name: Security

on:
  push:
    branches: [main]
  pull_request:
  schedule:
    - cron: "0 3 * * 1"   # weekly, catches newly published CVEs on a quiet repo

permissions:
  contents: read

jobs:
  pip-audit:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Install uv
        uses: astral-sh/setup-uv@v5
        with:
          python-version: "3.11"
      - name: Audit locked dependencies
        run: |
          uv export --format requirements-txt --no-emit-project > requirements-ci.txt
          uvx pip-audit -r requirements-ci.txt

  bandit:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Install uv
        uses: astral-sh/setup-uv@v5
        with:
          python-version: "3.11"
      - name: Static security scan
        run: uvx bandit -r src/ -c pyproject.toml

  gitleaks:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0
      - uses: gitleaks/gitleaks-action@v2
        env:
          GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
```

- [ ] **Step 2: Run bandit locally FIRST and triage findings**

Run: `uvx bandit -r src/`
Expected findings to triage (this codebase legitimately does things bandit flags):
- `B404`/`B603`/`B607` subprocess usage in `core/opus_bridge.py`, `core/llm_router.py`, `core/git_ops.py`, `core/brainstorm.py`: legitimate by design (the product shells out to provider CLIs and git). Suppress via config, not per-line noise.
- `B602` `create_subprocess_shell` in `core/verify_gate.py`: intentional and documented as trusted operator config; suppress with an inline `# nosec B602` plus the existing comment.
- `B108` hardcoded tmp paths, `B311` random: fix or suppress case-by-case; prefer fixing.

Add to `pyproject.toml`:

```toml
[tool.bandit]
exclude_dirs = ["tests"]
skips = ["B404", "B603", "B607"]
```

Keep the skip list minimal; every additional skip needs a justifying comment above the table. Do NOT skip B602 globally (one audited call site only).

- [ ] **Step 3: Run gitleaks locally against full history before enabling**

Run: `docker run --rm -v "$PWD:/repo" zricethezav/gitleaks:latest detect -s /repo --log-opts="--all"` (or download the gitleaks binary).
If it finds real secrets in history (a `ghp_` GitHub token was committed to `.env` context earlier in this project's life and was flagged for rotation): confirm the secret is ROTATED (revoked server-side). History rewrite is optional afterward; rotation is mandatory. Add a `.gitleaks.toml` allowlist only for confirmed-dead example values.

- [ ] **Step 4: Commit and push, verify all three jobs green**

```bash
git add .github/workflows/security.yml pyproject.toml
git commit -m "ci: add pip-audit, bandit, and gitleaks security scanning"
git push
gh run list --limit 3
```

---

### Task 5: CodeQL

**Files:**
- Create: `.github/workflows/codeql.yml`

**Depends on:** None

- [ ] **Step 1: Create the workflow**

```yaml
name: CodeQL

on:
  push:
    branches: [main]
  pull_request:
    branches: [main]
  schedule:
    - cron: "0 4 * * 1"

permissions:
  actions: read
  contents: read
  security-events: write

jobs:
  analyze:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: github/codeql-action/init@v3
        with:
          languages: python
          queries: security-and-quality
      - uses: github/codeql-action/analyze@v3
```

- [ ] **Step 2: Commit, push, review findings**

```bash
git add .github/workflows/codeql.yml
git commit -m "ci: add CodeQL analysis"
git push
```

Findings appear under the repo's Security → Code scanning tab, not in the run log. Triage them there; expected candidates are taint-flow warnings around subprocess argv construction (same legitimate-by-design areas as bandit; dismiss with the "used in trusted context" reason where accurate).

---

### Task 6: actionlint (lint the workflows themselves)

**Files:**
- Create: `.github/workflows/actionlint.yml`

**Depends on:** Tasks 1-5 (run it last so it validates all the new workflows in one pass; it will also catch mistakes made in them)

- [ ] **Step 1: Create the workflow**

```yaml
name: Actionlint

on:
  push:
    branches: [main]
    paths: [".github/workflows/**"]
  pull_request:
    paths: [".github/workflows/**"]

jobs:
  actionlint:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Run actionlint
        uses: raven-actions/actionlint@v2
```

- [ ] **Step 2: Commit, push, fix anything it flags in the other workflows**

```bash
git add .github/workflows/actionlint.yml
git commit -m "ci: lint workflow files with actionlint"
git push
```

---

### Task 7 (optional, later): GHCR image publishing + release automation

Not fleshed out as steps on purpose; do this when cutting the first tagged release, not before. Design notes so the future session starts warm:

- **GHCR publish:** on `push: tags: ["v*"]`, build and push `ghcr.io/adiatmaja/praxis` (orchestrator) and the three agent images with the tag + `latest`. Needs `permissions: packages: write` and `docker/login-action` against `ghcr.io` with `GITHUB_TOKEN`. Payoff: users `docker pull` instead of building locally, and pinned published agent-image tags kill the stale-local-image failure class for adopters. Update README Quick Start + docker-compose image references when this lands.
- **Release automation:** `googleapis/release-please-action` fits because the repo already uses conventional commits; it maintains a changelog PR and creates GitHub Releases on merge. Alternative for lighter weight: `softprops/action-gh-release` with `generate_release_notes: true` on tag push.

---

## Parallel Execution Map

- **Wave 1:** Task 1 (docker builds), Task 2 (windows matrix), Task 3 (dependabot), Task 4 (security scans), Task 5 (CodeQL) — independent files, run in parallel. Caution: Task 2 modifies `ci.yml`; nothing else touches it, but Tasks 4 and 2 both may touch `pyproject.toml`/test files if triage requires it, so review diffs before merging concurrent branches.
- **Wave 2:** Task 6 (actionlint, after all workflows exist)
- **Later, on first release:** Task 7

## Final check

All workflows green on `gh run list`; the repo's Security tab shows CodeQL + Dependabot active; README badges optional (a wall of badges is noise; the CI badge already exists, add at most CodeQL).
