# Onboarding Blockers Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the five defects from the 2026-08-15/16 newcomer walkthrough that stop a new operator during the first ten minutes, so that a fresh clone reaches a green `praxis doctor` and the deployment's own default preset is completable from the product's own output.

**Architecture:** Five independent fixes across three seams. (1) `src/cli/init.py` gains a setup-hint lookup so an unmet preset requirement prints the actual recipe, and reorders its writes so declining a preset does not discard collected answers. (2) Agent image staleness moves from an mtime comparison to a content hash baked into each image as a Docker `LABEL`, read back through the Docker SDK — immune to the clone-time mtimes that make the current check red on every fresh clone. (3) The `worker_endpoint` doctor probe gains the same `supports_local_llm` gate its model-name half already has, finishing a half-applied fix. (4) A new doctor check compares the running container's `LM_STUDIO_URL` against the `.env` on disk, converting the silent `docker compose restart` trap into a red light.

**Tech Stack:** Python 3.11, Typer + rich (CLI), FastAPI (doctor endpoint), Docker SDK for Python, pytest with `asyncio_mode = "auto"`, ruff (line-length 88), mypy strict-ish via `--ignore-missing-imports`.

**Source of truth for the defects:** `docs/walkthrough-15min.md`, "Defects found, ranked", items 1-5.

---

## Context for the executing agent

**Read this section before Task 1. It assumes you have never seen this repo.**

### What Praxis is, in one paragraph

Praxis is a Docker-based AI software-engineering orchestrator. It splits work into
four roles (plan, implement, review, verify), each independently pointed at any
provider. A FastAPI monolith (`src/orchestrator/`) is the engine; a Typer CLI
(`src/cli/`), an MCP server (`src/mcp_server/`), and a no-build web dashboard
(`web/`) are thin clients of its REST API. Implementation runs in throwaway Docker
containers built from `docker/<harness>-agent/`. You are fixing onboarding defects
in the CLI and the diagnostic ("doctor"), not touching the orchestration loop.

### Where things live

```
src/cli/init.py                        the `praxis init` setup wizard (Tasks 3, 8, 9)
src/cli/doctor.py                      the `praxis doctor` CLI renderer
src/orchestrator/core/doctor.py        CheckResult/CheckStatus + check registry + hints
src/orchestrator/core/doctor_probes.py PURE probe logic: facts in, verdict out (Tasks 4, 6, 10)
src/orchestrator/api/doctor.py         LIVE fact gathering (Docker SDK, files, env) (Tasks 5, 6, 10)
src/orchestrator/core/harnesses.py     REGISTRY of harnesses (id, image, supports_local_llm, ...)
config/praxis.yaml                     global settings + worker_presets (Task 7)
docker/agy-agent/, docker/opencode-agent/   the two agent images (Task 2)
tests/                                 pytest suite, asyncio_mode = "auto"
```

### The doctor's three-layer split (important — do not collapse it)

This separation is deliberate and load-bearing:

1. **`core/doctor_probes.py` is PURE.** Every probe takes plain facts as arguments
   and returns a `CheckResult`. No Docker, no network, no filesystem. This is why
   they are trivially testable, and every probe test in this plan calls the probe
   directly with literal arguments.
2. **`api/doctor.py` GATHERS.** It talks to the Docker SDK, reads files, reads
   `os.environ`, then calls the pure probes. Gathering is guarded per unit by a
   `_safe(...)` helper so one failing probe never 500s the endpoint — it always
   answers 200 with a degraded row.
3. **`core/doctor.py` holds the vocabulary**: `CheckResult`, `CheckStatus`
   (`GREEN`/`AMBER`/`RED`), the check registry, and each check's fix hint. A
   hintless RED resolves its hint from that registry, so you never pass a hint at
   a construction site.

When a task says "add a probe", it means: pure function in `doctor_probes.py`,
registry entry in `doctor.py`, gathering + wiring in `api/doctor.py`, tests
against the pure function.

### Conventions you must follow

- Python 3.11+, PEP 8, **type annotations on every function signature**.
- Line length **88** (ruff default). `ruff format`, **not** `ruff fmt`.
- `X | Y` unions and built-in generics (`list[str]`, never `List[str]`).
- **Google-style docstrings** on every function you add. Look at the neighbours —
  the existing docstrings in `doctor_probes.py` explain *why*, not just *what*, and
  yours should match that register.
- `logging` module only, never `print()` in production code. The CLI uses
  `rich`'s `console.print`, which is correct **only** inside `src/cli/`.
- Catch specific exceptions; use `raise ... from` for chaining.
- pytest with `asyncio_mode = "auto"` — async test functions need no decorator.

### Commands

```bash
uv run pytest tests/test_doctor_probes.py -v          # one file
uv run pytest --cov=orchestrator --cov-report=term-missing -q   # full, 80%+ required
uv run ruff format src/ tests/
uv run ruff check --fix src/ tests/
uv run mypy src/orchestrator/ --ignore-missing-imports
```

Run tests from the **repo root**. The CLI lives at `src/cli/` (not top-level) and
resolves via `where = ["src"]` in `pyproject.toml`, so `from cli.init import ...`
and `from orchestrator.core... import ...` both work from the root.

### Traps specific to this plan

- **`.env` is read by pydantic-settings.** A test asserting a missing env var must
  pass `_env_file=None`, or the real `.env` supplies a fallback and the test fails
  with "DID NOT RAISE".
- **CRLF.** This is a Windows checkout. A bulk edit (especially `sed -i`) can flip
  a whole file from CRLF to LF and show every line as changed. After any bulk edit
  run `git diff --numstat` and check no file shows a whole-file rewrite. Task 11
  Step 3 makes this explicit.
- **Agent image changes need a rebuild.** Editing `docker/*/entrypoint.sh` or a
  `Dockerfile` does nothing until `docker compose --profile agents build` runs. The
  agent images are behind a compose **profile** and are not built by a plain
  `docker compose up`.
- **`config/praxis.yaml` is MOUNTED, not baked.** A YAML edit takes effect on
  `docker compose restart orchestrator`, never a rebuild. But note the defect this
  plan fixes: `.env` changes need `up -d`, not `restart`. Two different files, two
  different recoveries.
- **Never write the literal string `"config/praxis.yaml"` anywhere in `src/`.**
  `core/settings_file.config_file_path()` is the only place that path is decided,
  and `tests/test_config_path.py` greps for exactly that literal and will fail you.
- **Do not add a `src/cli/` import to `src/orchestrator/api/`** casually. Task 10
  Step 5 needs an `.env` parser; if importing `cli.init.parse_env` creates a layering
  problem, copy the minimal parser into the API layer instead. Prefer the copy.

### How to know a step really worked

Two rules, both learned the hard way in this repo:

1. **A gate must be proven to fail.** After a test goes green, break the thing it
   guards (delete the parameter, flip the comparison), confirm the test goes RED,
   then restore. A regex or assertion can be satisfied by a comment sitting next to
   the code rather than by the code. This matters most for Task 4 and Task 6, where
   the whole point is a comparison changing polarity.
2. **Shell and Dockerfile changes must be EXECUTED, not just read.** Task 2 Step 3
   and all of Task 12 exist because a syntax-checked entrypoint shipped a real bug
   here before. Actually build the image and actually read the label back.

### Scope discipline

Fix defects 1-5 and nothing else. The walkthrough recorded eleven; items 6-11 are
listed in the Notes at the bottom and are explicitly **out of scope**. If you find
an unrelated bug, write it down in the Notes section rather than fixing it. Diff
every file you touch before committing.

---

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `config/praxis.yaml` | Preset menu data | Add `setup_doc` + `setup_hint` to `gemini-agy` |
| `src/cli/init.py` | Setup wizard | Print the setup hint on an unmet requirement; write `.env` before the preset challenge |
| `docker/agy-agent/Dockerfile` | agy image build | Add `ARG`/`LABEL` carrying the entrypoint hash |
| `docker/opencode-agent/Dockerfile` | opencode image build | Same |
| `src/orchestrator/core/entrypoint_hash.py` | **New.** Pure hashing helper | `hash_entrypoint()` + `LABEL_KEY` constant |
| `src/orchestrator/core/doctor.py` | Staleness decision | Replace `image_is_stale` with `image_content_differs` |
| `src/orchestrator/core/doctor_probes.py` | Pure probe verdicts | Rework `probe_agent_image_freshness`; gate `probe_worker_endpoint`; add `probe_env_drift` |
| `src/orchestrator/api/doctor.py` | Live fact gathering | Gather image labels + on-disk hashes + env drift facts |
| `src/orchestrator/core/doctor.py` (registry) | Check registry | Register `env_drift` check + hint |
| `tests/test_cli_init.py` | init tests | New cases for hint + write ordering |
| `tests/test_doctor_probes.py` | probe tests | New cases for all three probe changes |
| `tests/test_entrypoint_hash.py` | **New.** hashing tests | Hash stability + label key |
| `tests/test_api_doctor.py` | gathering tests | Env-drift + label gathering |

---

### Task 1: Add a hashing helper for entrypoint content

**Files:**
- Create: `src/orchestrator/core/entrypoint_hash.py`
- Test: `tests/test_entrypoint_hash.py`

**Depends on:** None

The current staleness check compares an image's build time against the entrypoint file's mtime. `git clone` stamps every file at clone time, so the entrypoint is always "newer" than any cached layer and the check is red on every fresh clone. This task builds the content-addressed replacement.

- [ ] **Step 1: Write the failing test**

Create `tests/test_entrypoint_hash.py`:

```python
"""Tests for the entrypoint content hash used by the staleness check."""

from pathlib import Path

from orchestrator.core.entrypoint_hash import LABEL_KEY, hash_entrypoint


def test_hash_is_stable_for_identical_content(tmp_path: Path) -> None:
    a = tmp_path / "a.sh"
    b = tmp_path / "b.sh"
    a.write_text("#!/bin/bash\necho hi\n", encoding="utf-8")
    b.write_text("#!/bin/bash\necho hi\n", encoding="utf-8")
    assert hash_entrypoint(a) == hash_entrypoint(b)


def test_hash_ignores_mtime(tmp_path: Path) -> None:
    """The whole point: a re-checkout changes mtime, not content."""
    script = tmp_path / "entrypoint.sh"
    script.write_text("#!/bin/bash\necho hi\n", encoding="utf-8")
    first = hash_entrypoint(script)
    import os
    os.utime(script, (0, 0))
    assert hash_entrypoint(script) == first


def test_hash_changes_when_content_changes(tmp_path: Path) -> None:
    script = tmp_path / "entrypoint.sh"
    script.write_text("#!/bin/bash\necho hi\n", encoding="utf-8")
    first = hash_entrypoint(script)
    script.write_text("#!/bin/bash\necho CHANGED\n", encoding="utf-8")
    assert hash_entrypoint(script) != first


def test_hash_normalizes_line_endings(tmp_path: Path) -> None:
    """A CRLF checkout on Windows must hash the same as LF in the image.

    Without this the check would be red on every Windows clone for the
    opposite reason it is red today.
    """
    lf = tmp_path / "lf.sh"
    crlf = tmp_path / "crlf.sh"
    lf.write_bytes(b"#!/bin/bash\necho hi\n")
    crlf.write_bytes(b"#!/bin/bash\r\necho hi\r\n")
    assert hash_entrypoint(lf) == hash_entrypoint(crlf)


def test_missing_file_returns_none(tmp_path: Path) -> None:
    assert hash_entrypoint(tmp_path / "nope.sh") is None


def test_label_key_is_namespaced() -> None:
    assert LABEL_KEY == "org.praxis.entrypoint-sha256"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_entrypoint_hash.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'orchestrator.core.entrypoint_hash'`

- [ ] **Step 3: Write minimal implementation**

Create `src/orchestrator/core/entrypoint_hash.py`:

```python
"""Content hash of a harness entrypoint, shared by the build and the doctor.

The staleness check used to compare an image's build timestamp against the
entrypoint's filesystem mtime.  ``git clone`` stamps every file at clone
time, so the source was always "newer" than any cached or pulled layer and
a correct fresh install reported a stale image.  Docker's own build cache
already keys on content, so this module makes the check agree with it.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

#: Docker label carrying the hash of the entrypoint baked into an image.
LABEL_KEY = "org.praxis.entrypoint-sha256"


def hash_entrypoint(path: Path) -> str | None:
    """Return the sha256 of an entrypoint's normalized content.

    Line endings are normalized to LF before hashing: a Windows checkout may
    hold CRLF while the image always holds LF, and that difference is not a
    staleness signal.

    Args:
        path: Path to the ``entrypoint.sh`` to hash.

    Returns:
        The hex digest, or ``None`` when the file cannot be read.  ``None``
        means "cannot be judged" and callers must not treat it as a mismatch.
    """
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    normalized = raw.replace(b"\r\n", b"\n")
    return hashlib.sha256(normalized).hexdigest()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_entrypoint_hash.py -v`
Expected: PASS, 6 passed

- [ ] **Step 5: Commit**

```bash
git add src/orchestrator/core/entrypoint_hash.py tests/test_entrypoint_hash.py
git commit -m "feat(doctor): add content hash helper for harness entrypoints"
```

---

### Task 2: Bake the entrypoint hash into both agent images

**Files:**
- Modify: `docker/agy-agent/Dockerfile`
- Modify: `docker/opencode-agent/Dockerfile`
- Test: manual build verification (no unit test — this is Dockerfile content)

**Depends on:** Task 1

The hash must be present on the image for the doctor to read it back. Docker cannot hash a file at build time on its own, so the build passes it in as a build arg.

- [ ] **Step 1: Add the ARG and LABEL to the agy Dockerfile**

In `docker/agy-agent/Dockerfile`, immediately **before** the final `ENTRYPOINT` line, add:

```dockerfile
# Content hash of entrypoint.sh, supplied by the build and read back by
# `praxis doctor` to decide whether this image is stale.  Compared against a
# hash of the file on disk, never against mtimes: a fresh `git clone` stamps
# every file at clone time, which made the old mtime check red on every
# correct install.
ARG PRAXIS_ENTRYPOINT_SHA256=""
LABEL org.praxis.entrypoint-sha256=$PRAXIS_ENTRYPOINT_SHA256
```

- [ ] **Step 2: Add the identical block to the opencode Dockerfile**

In `docker/opencode-agent/Dockerfile`, immediately before its final `ENTRYPOINT` line, add the same six lines verbatim (comment included).

- [ ] **Step 3: Verify the label lands on a built image**

Run from the repo root:

```bash
docker build \
  --build-arg PRAXIS_ENTRYPOINT_SHA256=testhash123 \
  -t praxis-label-probe:test \
  docker/agy-agent
docker inspect praxis-label-probe:test \
  --format '{{ index .Config.Labels "org.praxis.entrypoint-sha256" }}'
```

Expected output: `testhash123`

- [ ] **Step 4: Clean up the probe image**

```bash
docker image rm praxis-label-probe:test
```

- [ ] **Step 5: Commit**

```bash
git add docker/agy-agent/Dockerfile docker/opencode-agent/Dockerfile
git commit -m "feat(docker): label agent images with their entrypoint content hash"
```

---

### Task 3: Pass the hash at build time from init and compose

**Files:**
- Modify: `docker-compose.yml`
- Modify: `src/cli/init.py`
- Test: `tests/test_cli_init.py`

**Depends on:** Task 1, Task 2

A label that is never populated is worse than no label: it reads as "unknown" forever. The build must compute and pass the hash.

- [ ] **Step 1: Add the build arg to both agent services in docker-compose.yml**

In `docker-compose.yml`, under the `opencode-agent` service's `build:` block, add:

```yaml
      args:
        PRAXIS_ENTRYPOINT_SHA256: ${OPENCODE_ENTRYPOINT_SHA256:-}
```

And under the `agy-agent` service's `build:` block:

```yaml
      args:
        PRAXIS_ENTRYPOINT_SHA256: ${AGY_ENTRYPOINT_SHA256:-}
```

Note: `${VAR:-}` is correct here and does not repeat the `praxis.yaml` bare-pass-through trap — these are build args consumed by Docker, not `Settings` fields that a set-but-empty env var would suppress.

- [ ] **Step 2: Write the failing test for init computing the hashes**

Add to `tests/test_cli_init.py`:

```python
def test_build_env_includes_entrypoint_hashes(tmp_path, monkeypatch):
    """init must compute a hash per harness before invoking the build."""
    from orchestrator.core.entrypoint_hash import hash_entrypoint
    from cli.init import _entrypoint_build_env

    root = tmp_path
    for harness in ("agy", "opencode"):
        d = root / "docker" / f"{harness}-agent"
        d.mkdir(parents=True)
        (d / "entrypoint.sh").write_text(f"#!/bin/bash\necho {harness}\n", encoding="utf-8")

    env = _entrypoint_build_env(root)

    assert env["AGY_ENTRYPOINT_SHA256"] == hash_entrypoint(
        root / "docker" / "agy-agent" / "entrypoint.sh"
    )
    assert env["OPENCODE_ENTRYPOINT_SHA256"] == hash_entrypoint(
        root / "docker" / "opencode-agent" / "entrypoint.sh"
    )


def test_build_env_omits_unreadable_entrypoint(tmp_path):
    """A missing entrypoint yields no key, so the label stays empty."""
    from cli.init import _entrypoint_build_env

    env = _entrypoint_build_env(tmp_path)
    assert "AGY_ENTRYPOINT_SHA256" not in env
```

- [ ] **Step 3: Run to verify it fails**

Run: `uv run pytest tests/test_cli_init.py -k entrypoint_build_env -v`
Expected: FAIL with `ImportError: cannot import name '_entrypoint_build_env'`

- [ ] **Step 4: Implement `_entrypoint_build_env` in src/cli/init.py**

Add near the other module-level helpers in `src/cli/init.py`:

```python
def _entrypoint_build_env(root: Path) -> dict[str, str]:
    """Return build-arg env vars carrying each harness entrypoint's hash.

    The compose build args are read from the process environment, so these
    are merged into the env passed to the build subprocess.  A harness whose
    entrypoint cannot be read contributes no key: an absent build arg leaves
    the label empty, which the doctor reports as "cannot judge" rather than
    as a mismatch.

    Args:
        root: The repository root.

    Returns:
        ``{"<HARNESS>_ENTRYPOINT_SHA256": "<hex>"}`` for each readable file.
    """
    build_env: dict[str, str] = {}
    for harness in REGISTRY.values():
        path = root / "docker" / f"{harness.id}-agent" / "entrypoint.sh"
        digest = hash_entrypoint(path)
        if digest is not None:
            build_env[f"{harness.id.upper()}_ENTRYPOINT_SHA256"] = digest
    return build_env
```

Add the imports at the top of `src/cli/init.py`:

```python
from orchestrator.core.entrypoint_hash import hash_entrypoint
from orchestrator.core.harnesses import REGISTRY
```

- [ ] **Step 5: Run to verify it passes**

Run: `uv run pytest tests/test_cli_init.py -k entrypoint_build_env -v`
Expected: PASS, 2 passed

- [ ] **Step 6: Wire the build env into the image build subprocess**

Find the call in `src/cli/init.py` that runs the agent image build (the one printing `Building agent images (this takes a few minutes the first time)`). It invokes `docker compose ... build` via `subprocess`. Change that call to pass a merged environment:

```python
    build_env = {**os.environ, **_entrypoint_build_env(root)}
```

and add `env=build_env` to that `subprocess.run(...)` call. Ensure `import os` is present at the top of the file.

- [ ] **Step 7: Run the full init test suite**

Run: `uv run pytest tests/test_cli_init.py -v`
Expected: PASS, all existing tests still green

- [ ] **Step 8: Commit**

```bash
git add docker-compose.yml src/cli/init.py tests/test_cli_init.py
git commit -m "feat(init): pass entrypoint content hashes as image build args"
```

---

### Task 4: Replace the mtime staleness check with the content comparison

**Files:**
- Modify: `src/orchestrator/core/doctor.py:155-165`
- Modify: `src/orchestrator/core/doctor_probes.py:118-160`
- Test: `tests/test_doctor_probes.py`

**Depends on:** Task 1

This is the behavior change that turns the false red green. `image_is_stale` is deleted and replaced; `probe_agent_image_freshness` takes hashes instead of timestamps.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_doctor_probes.py`:

```python
from orchestrator.core.doctor import CheckStatus, image_content_differs
from orchestrator.core.doctor_probes import probe_agent_image_freshness


def test_image_content_differs_matching_hashes() -> None:
    assert image_content_differs("abc123", "abc123") is False


def test_image_content_differs_mismatched_hashes() -> None:
    assert image_content_differs("abc123", "def456") is True


def test_image_content_differs_unknown_image_label_is_not_a_mismatch() -> None:
    """An unlabeled image predates this check; it cannot be judged.

    This must NOT be treated as stale: every image built before this feature
    shipped has no label, and calling them all stale recreates the false red
    from the other direction.
    """
    assert image_content_differs(None, "abc123") is None
    assert image_content_differs("", "abc123") is None


def test_image_content_differs_unknown_source_is_not_a_mismatch() -> None:
    assert image_content_differs("abc123", None) is None


def test_freshness_green_when_hashes_match() -> None:
    result = probe_agent_image_freshness(
        image_labels={"agy-agent:latest": "abc123"},
        source_hashes={"agy-agent:latest": "abc123"},
    )
    assert result.status is CheckStatus.GREEN


def test_freshness_red_when_hashes_differ() -> None:
    result = probe_agent_image_freshness(
        image_labels={"agy-agent:latest": "OLD"},
        source_hashes={"agy-agent:latest": "NEW"},
    )
    assert result.status is CheckStatus.RED
    assert "agy-agent:latest" in result.detail


def test_freshness_amber_when_nothing_comparable() -> None:
    """Unlabeled images cannot be judged; amber, never green, never red."""
    result = probe_agent_image_freshness(
        image_labels={"agy-agent:latest": None},
        source_hashes={"agy-agent:latest": "NEW"},
    )
    assert result.status is CheckStatus.AMBER


def test_freshness_reports_only_the_mismatched_tag() -> None:
    result = probe_agent_image_freshness(
        image_labels={"agy-agent:latest": "SAME", "opencode-agent:latest": "OLD"},
        source_hashes={"agy-agent:latest": "SAME", "opencode-agent:latest": "NEW"},
    )
    assert result.status is CheckStatus.RED
    assert "opencode-agent:latest" in result.detail
    assert "agy-agent:latest" not in result.detail
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_doctor_probes.py -k "content_differs or freshness" -v`
Expected: FAIL with `ImportError: cannot import name 'image_content_differs'`

- [ ] **Step 3: Replace `image_is_stale` in src/orchestrator/core/doctor.py**

Delete `image_is_stale` (lines 155-165) and put this in its place:

```python
def image_content_differs(
    image_label: str | None, source_hash: str | None
) -> bool | None:
    """Whether an image's baked entrypoint differs from the source on disk.

    Deliberately tri-state.  The predecessor compared an image build time
    against the entrypoint's mtime and treated "unknown" as stale, which made
    a fresh ``git clone`` red on every correct install: clone stamps the
    source at clone time, so it always looked newer than a cached layer.

    ``None`` means "cannot be judged" and must not be rendered as either a
    pass or a failure.  An image built before this label existed carries no
    label, and calling those stale would reproduce the same false red from
    the other direction.

    Args:
        image_label: The ``org.praxis.entrypoint-sha256`` label read off the
            image, or ``None``/``""`` when the image carries none.
        source_hash: The hash of the entrypoint on disk, or ``None`` when it
            could not be read.

    Returns:
        ``True`` on a definite mismatch, ``False`` on a definite match,
        ``None`` when either side is unknown.
    """
    if not image_label or not source_hash:
        return None
    return image_label != source_hash
```

- [ ] **Step 4: Rewrite `probe_agent_image_freshness` in src/orchestrator/core/doctor_probes.py**

Replace the whole function (lines 118-160) with:

```python
def probe_agent_image_freshness(
    image_labels: dict[str, str | None],
    source_hashes: dict[str, str | None],
    errors: dict[str, str] | None = None,
) -> CheckResult:
    """Red when an agent image's baked entrypoint differs from the source.

    This converts the project's oldest silent failure into a red light: a
    stale agent image runs old entrypoint logic while the source looks
    current.  The comparison is on CONTENT, not timestamps, because a fresh
    checkout rewrites every mtime and the previous timestamp comparison was
    therefore red on every correct install.

    A tag whose verdict is unknown (no label on the image, or an unreadable
    source) is reported AMBER, never GREEN: a green this check has not
    earned is exactly the failure it exists to prevent.

    Args:
        image_labels: ``image_tag`` to its baked entrypoint hash.
        source_hashes: ``image_tag`` to the on-disk entrypoint hash.
        errors: ``image_tag`` to an inspection error, if any.

    Returns:
        The check verdict.
    """
    errors = errors or {}
    verdicts = {
        tag: image_content_differs(label, source_hashes.get(tag))
        for tag, label in image_labels.items()
    }
    stale = sorted(tag for tag, differs in verdicts.items() if differs is True)
    if stale:
        return CheckResult(
            check_id="agent_image_freshness",
            status=CheckStatus.RED,
            detail=f"stale image(s): {', '.join(stale)}",
        )
    unknown = sorted(
        {tag for tag, differs in verdicts.items() if differs is None} | set(errors)
    )
    if unknown:
        return CheckResult(
            check_id="agent_image_freshness",
            status=CheckStatus.AMBER,
            detail=(
                f"could not compare {', '.join(unknown)}: no entrypoint hash "
                "on the image (rebuild to populate it)"
            ),
        )
    return CheckResult(
        check_id="agent_image_freshness",
        status=CheckStatus.GREEN,
        detail="all agent images match their entrypoints",
    )
```

Update the import at `src/orchestrator/core/doctor_probes.py:10`:

```python
from orchestrator.core.doctor import (
    CheckResult,
    CheckStatus,
    image_content_differs,
)
```

- [ ] **Step 5: Run to verify it passes**

Run: `uv run pytest tests/test_doctor_probes.py -v`
Expected: PASS. Any pre-existing test referencing `image_is_stale` or passing `entrypoint_mtimes=` will fail — update those call sites to the new keyword arguments (`image_labels=`, `source_hashes=`) and the new tri-state semantics.

- [ ] **Step 6: Commit**

```bash
git add src/orchestrator/core/doctor.py src/orchestrator/core/doctor_probes.py tests/test_doctor_probes.py
git commit -m "fix(doctor): compare agent image entrypoints by content, not mtime"
```

---

### Task 5: Gather image labels and source hashes in the doctor endpoint

**Files:**
- Modify: `src/orchestrator/api/doctor.py:190-260, 340-430`
- Test: `tests/test_api_doctor.py`

**Depends on:** Task 1, Task 4

The pure probe now wants hashes. The gathering layer still supplies timestamps.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_api_doctor.py`:

```python
def test_entrypoint_hashes_reads_every_registered_harness(tmp_path, monkeypatch):
    """Every harness in the registry contributes a source hash."""
    from orchestrator.api import doctor as doctor_api
    from orchestrator.core.harnesses import REGISTRY

    for harness in REGISTRY.values():
        d = tmp_path / f"{harness.id}-agent"
        d.mkdir(parents=True)
        (d / "entrypoint.sh").write_text("#!/bin/bash\necho x\n", encoding="utf-8")

    monkeypatch.setattr(doctor_api, "_ENTRYPOINT_ROOT", tmp_path)
    hashes = doctor_api._entrypoint_hashes()

    for harness in REGISTRY.values():
        assert hashes[harness.image] is not None


def test_entrypoint_hashes_missing_file_is_none(tmp_path, monkeypatch):
    from orchestrator.api import doctor as doctor_api
    from orchestrator.core.harnesses import REGISTRY

    monkeypatch.setattr(doctor_api, "_ENTRYPOINT_ROOT", tmp_path)
    hashes = doctor_api._entrypoint_hashes()

    for harness in REGISTRY.values():
        assert hashes[harness.image] is None
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_api_doctor.py -k entrypoint_hashes -v`
Expected: FAIL with `AttributeError: module ... has no attribute '_entrypoint_hashes'`

- [ ] **Step 3: Replace `_entrypoint_mtimes` with `_entrypoint_hashes`**

In `src/orchestrator/api/doctor.py`, replace the whole `_entrypoint_mtimes` function (starting line 231) with:

```python
def _entrypoint_hashes() -> dict[str, str | None]:
    """Return {image_tag: source entrypoint hash} for every harness.

    ``docker/<harness>-agent/entrypoint.sh`` is not COPYed into the
    orchestrator image, so a container only sees it through the
    ``./docker:/app/docker:ro`` mount both compose files carry; a bare
    ``uv run uvicorn`` from the repo root sees it because its CWD IS the
    checkout.  Either way ``_ENTRYPOINT_ROOT`` is the one path to look at.

    A tag whose file cannot be read maps to ``None`` rather than being
    omitted, so the probe reports it as unjudgeable instead of silently
    excluding it and claiming a green it has not earned.
    """
    hashes: dict[str, str | None] = {}
    for harness in REGISTRY.values():
        entrypoint = _ENTRYPOINT_ROOT / f"{harness.id}-agent" / "entrypoint.sh"
        hashes[harness.image] = hash_entrypoint(entrypoint)
    return hashes
```

Add the import at the top of `src/orchestrator/api/doctor.py`:

```python
from orchestrator.core.entrypoint_hash import LABEL_KEY, hash_entrypoint
```

- [ ] **Step 4: Gather the image label instead of the build time**

In `_gather_docker_facts` (around line 202-210), replace the `image_created_at[tag] = _parse_created(...)` assignment with a label read. Change the `_DockerFacts` field `image_created_at: dict[str, float | None]` to `image_labels: dict[str, str | None]`, and inside the `try` block:

```python
            image = client.images.get(tag)
            image_present[tag] = True
            labels = image.attrs.get("Config", {}).get("Labels") or {}
            image_labels[tag] = labels.get(LABEL_KEY)
```

In the `ImageNotFound` branch, set `image_labels[tag] = None` in place of `image_created_at[tag] = None`. Rename the local `image_created_at` dict declaration to `image_labels` and update the `_DockerFacts(...)` construction accordingly.

- [ ] **Step 5: Update the probe call site**

At `src/orchestrator/api/doctor.py:349-350`, replace the `entrypoint_mtimes` gathering:

```python
    entrypoint_hashes, hashes_error = await _safe(
        "entrypoint_hashes", _entrypoint_hashes, {}
    )
```

And at line ~411-421, update the probe invocation:

```python
    result_map["agent_image_freshness"] = probes.probe_agent_image_freshness(
        image_labels=docker_facts.image_labels,
        source_hashes=entrypoint_hashes,
        errors=docker_facts.image_errors,
    )
```

- [ ] **Step 6: Run the doctor test suites**

Run: `uv run pytest tests/test_api_doctor.py tests/test_doctor.py tests/test_doctor_probes.py -v`
Expected: PASS. Update any test still constructing `_DockerFacts(image_created_at=...)`.

- [ ] **Step 7: Commit**

```bash
git add src/orchestrator/api/doctor.py tests/test_api_doctor.py
git commit -m "feat(doctor): gather entrypoint hashes and image labels for freshness"
```

---

### Task 6: Gate the worker-endpoint reachability probe on supports_local_llm

**Files:**
- Modify: `src/orchestrator/api/doctor.py:360-380`
- Test: `tests/test_api_doctor.py`

**Depends on:** None

Defect 4. The model-name half of this check is already gated with a comment naming the category error; the reachability half is not, and `if not reachable` fires first. So `gemini-agy` — the deployment's flagged default — can never produce a green doctor.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_api_doctor.py`:

```python
from orchestrator.core.doctor import CheckStatus
from orchestrator.core.doctor_probes import probe_worker_endpoint


def test_worker_endpoint_skipped_for_non_local_llm_harness() -> None:
    """agy talks to Google directly; an LM Studio probe is a category error.

    Reachability must be gated exactly like the model-name comparison
    already is, or the flagged default preset is permanently red.
    """
    result = probe_worker_endpoint(
        reachable=False,
        models=[],
        configured_model="",
        error="connection refused",
        endpoint_required=False,
    )
    assert result.status is CheckStatus.GREEN
    assert "not applicable" in result.detail.lower()


def test_worker_endpoint_still_red_for_local_llm_harness() -> None:
    result = probe_worker_endpoint(
        reachable=False,
        models=[],
        configured_model="qwen3.8-27b",
        error="connection refused",
        endpoint_required=True,
    )
    assert result.status is CheckStatus.RED
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_api_doctor.py -k worker_endpoint -v`
Expected: FAIL with `TypeError: probe_worker_endpoint() got an unexpected keyword argument 'endpoint_required'`

- [ ] **Step 3: Add the gate to the probe**

In `src/orchestrator/core/doctor_probes.py`, change the signature at line 247 and add the early return as the **first** statement in the body, before the `if not reachable` branch:

```python
def probe_worker_endpoint(
    reachable: bool,
    models: list[str],
    configured_model: str,
    error: str = "",
    endpoint_required: bool = True,
) -> CheckResult:
```

First statement of the body:

```python
    if not endpoint_required:
        # A harness that does not talk to an OpenAI-compatible endpoint (agy
        # calls Google directly) has nothing here to reach.  The model-name
        # comparison below was already gated for this reason; leaving the
        # reachability half ungated made the flagged default preset
        # permanently red on a correct install.
        return CheckResult(
            check_id="worker_endpoint",
            status=CheckStatus.GREEN,
            detail="not applicable: this harness does not use an OpenAI endpoint",
        )
```

- [ ] **Step 4: Pass the gate from the gathering layer**

In `src/orchestrator/api/doctor.py`, the block at ~line 368-379 already computes `worker_harness_spec`. Immediately after `configured_worker_model` is computed, add:

```python
    endpoint_required = worker_harness_spec.supports_local_llm
```

Then find the `probe_worker_endpoint(...)` invocation in the `result_map` assignments and add the argument:

```python
        endpoint_required=endpoint_required,
```

- [ ] **Step 5: Run to verify it passes**

Run: `uv run pytest tests/test_api_doctor.py tests/test_doctor_probes.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/orchestrator/core/doctor_probes.py src/orchestrator/api/doctor.py tests/test_api_doctor.py
git commit -m "fix(doctor): skip the worker endpoint check for non-local-LLM harnesses"
```

---

### Task 7: Add setup guidance to the preset menu data

**Files:**
- Modify: `config/praxis.yaml:97-104`
- Test: `tests/test_cli_init.py`

**Depends on:** None

Defect 1, data half. The recipe already exists in `docs/deployment.md`; the preset just has no field to point at it.

- [ ] **Step 1: Add the two fields to the gemini-agy preset**

In `config/praxis.yaml`, extend the `gemini-agy` entry so it reads:

```yaml
  - name: gemini-agy
    label: "Gemini via the agy harness"
    harness: agy
    model: "Gemini 3.7 Flash (High)"
    endpoint: ""
    requires: [interactive_login]
    default: true
    # Printed verbatim by `praxis init` when it challenges the unmet
    # requirement above.  Without this the operator was told WHAT was
    # missing and never HOW to supply it, which read as a dead end.
    setup_doc: "docs/deployment.md#agy-antigravity--gemini-harness--one-time-credential-setup"
    setup_hint: |
      Run these two commands once, then re-run `praxis init`:
        docker run --rm --user root -v praxis-gemini-creds:/home/agent/.gemini \
          --entrypoint bash agy-agent:latest -c 'chown -R agent:agent /home/agent/.gemini'
        docker run --rm -it -v praxis-gemini-creds:/home/agent/.gemini \
          --entrypoint bash agy-agent:latest -c 'agy login'
      The second is interactive: agy prints an OAuth URL to open in a browser.
```

- [ ] **Step 2: Add the same two fields to hosted-openweight**

```yaml
  - name: hosted-openweight
    label: "Hosted open-weight model (OpenAI-compatible endpoint)"
    harness: opencode
    model: "glm-4.7"
    endpoint: "https://api.z.ai/v1"
    requires: [api_key]
    setup_doc: "docs/deployment.md"
    setup_hint: |
      Set an API key for your OpenAI-compatible endpoint in .env after init:
        LM_STUDIO_API_KEY=<your key>
      Then re-run `praxis init` and pick this preset again.
```

- [ ] **Step 3: Verify the YAML still parses and presets still load**

Run:

```bash
uv run python -c "
from orchestrator.core.settings_file import load_yaml_settings
p = load_yaml_settings()['worker_presets']
for x in p:
    print(x['name'], '| setup_hint:', bool(x.get('setup_hint')))
"
```

Expected output:

```
gemini-agy | setup_hint: True
local-lmstudio | setup_hint: False
hosted-openweight | setup_hint: True
```

- [ ] **Step 4: Commit**

```bash
git add config/praxis.yaml
git commit -m "feat(config): add setup_doc and setup_hint to presets needing credentials"
```

---

### Task 8: Print the setup hint when init challenges an unmet requirement

**Files:**
- Modify: `src/cli/init.py:634-673`
- Test: `tests/test_cli_init.py`

**Depends on:** Task 7

Defect 1, behavior half. The stop is correct; its silence about the remedy is not.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_cli_init.py`:

```python
def test_unmet_requirement_prints_the_setup_hint(capsys, monkeypatch):
    """The stop must name the remedy, not only the requirement."""
    import typer
    from rich.prompt import Confirm
    from cli.init import _confirm_unmet_requirements

    monkeypatch.setattr(Confirm, "ask", lambda *a, **k: False)
    preset = {
        "label": "Gemini via the agy harness",
        "requires": ["interactive_login"],
        "default": True,
        "setup_doc": "docs/deployment.md#agy",
        "setup_hint": "docker run --rm -it ... 'agy login'",
    }

    try:
        _confirm_unmet_requirements(preset)
    except typer.Exit:
        pass

    out = capsys.readouterr().out
    assert "agy login" in out
    assert "docs/deployment.md#agy" in out


def test_unmet_requirement_without_hint_still_stops(capsys, monkeypatch):
    """A preset with no hint must not crash on the missing key."""
    import typer
    from rich.prompt import Confirm
    from cli.init import _confirm_unmet_requirements

    monkeypatch.setattr(Confirm, "ask", lambda *a, **k: False)
    preset = {"label": "Some preset", "requires": ["api_key"]}

    with pytest.raises(typer.Exit):
        _confirm_unmet_requirements(preset)
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_cli_init.py -k unmet_requirement -v`
Expected: FAIL — `assert "agy login" in out` fails, the hint is never printed

- [ ] **Step 3: Print the hint in `_confirm_unmet_requirements`**

In `src/cli/init.py`, inside `_confirm_unmet_requirements`, insert this block **after** the `if preset.get("default"):` block and **before** the `Confirm.ask` call:

```python
    setup_hint = preset.get("setup_hint") or ""
    setup_doc = preset.get("setup_doc") or ""
    if setup_hint:
        # Naming the requirement without naming the remedy is what made this
        # stop read as a dead end: the recipe exists in the docs and the
        # preset now carries it, so print it at the point of refusal.
        console.print("\n[bold]To satisfy it:[/bold]")
        console.print(setup_hint.rstrip())
    if setup_doc:
        console.print(f"[dim]Full instructions: {setup_doc}[/dim]")
```

- [ ] **Step 4: Change the decline message to point forward, not away**

Replace the decline message body so it no longer tells the operator to abandon the deployment default:

```python
    if not proceed:
        console.print(
            "[red]No worker preset chosen.[/red] Complete the setup above and "
            "re-run `praxis init`, or re-run it now and pick a preset that "
            "needs no credential."
        )
        raise typer.Exit(code=1)
```

- [ ] **Step 5: Run to verify it passes**

Run: `uv run pytest tests/test_cli_init.py -k unmet_requirement -v`
Expected: PASS, 2 passed

- [ ] **Step 6: Commit**

```bash
git add src/cli/init.py tests/test_cli_init.py
git commit -m "fix(init): print the setup recipe when a preset requirement is unmet"
```

---

### Task 9: Persist collected answers before the preset challenge can exit

**Files:**
- Modify: `src/cli/init.py:812-860`
- Test: `tests/test_cli_init.py`

**Depends on:** Task 8

Defect 2. `_choose_preset` raises `typer.Exit(1)` at line 837, before `.env` is written at line 840, so declining discards the auth token, port, and GitHub credentials the operator just supplied. A newcomer holding Enter through Quick Start ends with an empty directory.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_cli_init.py`:

```python
def test_declining_preset_still_writes_collected_answers(tmp_path, monkeypatch):
    """Declining a preset must not discard the token, port, and credentials."""
    import typer
    from cli import init as init_mod

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(init_mod, "_require_repo_root", lambda: tmp_path)
    monkeypatch.setattr(init_mod, "_resolve_auth_token", lambda cur: "TESTTOKEN")
    monkeypatch.setattr(init_mod, "_resolve_github_token", lambda cur: "GHTOKEN")
    monkeypatch.setattr(init_mod.IntPrompt, "ask", lambda *a, **k: 12323)
    monkeypatch.setattr(
        init_mod,
        "_fetch_presets_or_defaults",
        lambda: [{"name": "p", "label": "P", "harness": "agy", "model": "M",
                  "requires": ["interactive_login"], "default": True}],
    )
    monkeypatch.setattr(init_mod.Confirm, "ask", lambda *a, **k: False)

    with pytest.raises(typer.Exit):
        init_mod.init()

    env = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "AUTH_TOKEN=TESTTOKEN" in env
    assert "PORT=12323" in env
    assert "GITHUB_TOKEN=GHTOKEN" in env
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_cli_init.py -k declining_preset -v`
Expected: FAIL with `FileNotFoundError` — no `.env` is written at all

- [ ] **Step 3: Write the non-preset keys before choosing a preset**

In `src/cli/init.py`'s `init()`, replace the line

```python
    preset = _choose_preset(_fetch_presets_or_defaults())
```

with:

```python
    # Write everything collected so far BEFORE the preset can exit.  A
    # declined preset used to raise out of _choose_preset and discard the
    # token, port, and credentials the operator had just typed, leaving a
    # fresh clone with no .env at all and exit 1.
    partial = _managed_values(token=token, gh_token=gh_token, port=port, preset=None)
    partial_text = merge_env(existing, partial) if existing else build_env_file(partial)
    env_path.write_text(partial_text, encoding="utf-8")
    existing = partial_text

    preset = _choose_preset(_fetch_presets_or_defaults())
```

- [ ] **Step 4: Make `_managed_values` accept a None preset**

In `src/cli/init.py`, change `_managed_values`' signature so `preset` is `dict[str, Any] | None`, and make the two preset-derived keys resolve to `None` (meaning "no opinion", so `merge_env` leaves any existing line untouched) when it is `None`:

```python
    harness = preset.get("harness") if preset else None
    model = preset.get("model") if preset else None
```

Use those two locals wherever the function currently reads `preset["harness"]` and `preset["model"]` for `DEFAULT_WORKER_HARNESS` / `DEFAULT_WORKER_MODEL`.

- [ ] **Step 5: Run to verify it passes**

Run: `uv run pytest tests/test_cli_init.py -k declining_preset -v`
Expected: PASS

- [ ] **Step 6: Run the whole init suite for regressions**

Run: `uv run pytest tests/test_cli_init.py -v`
Expected: PASS, all green. The differential `.env`-parser test must still pass.

- [ ] **Step 7: Commit**

```bash
git add src/cli/init.py tests/test_cli_init.py
git commit -m "fix(init): persist collected answers before the preset challenge exits"
```

---

### Task 10: Add an env-drift doctor check

**Files:**
- Modify: `src/orchestrator/core/doctor_probes.py`
- Modify: `src/orchestrator/core/doctor.py` (check registry + hint)
- Modify: `src/orchestrator/api/doctor.py`
- Test: `tests/test_doctor_probes.py`

**Depends on:** None

Defect 5. Editing `.env` then `docker compose restart` silently keeps the old value; the docs say `restart` five times (correctly, about the mounted YAML) and Quick Start tells you to edit `.env`, so the pattern teaches the wrong recovery. This cost real time in both walkthrough runs. The fix is to detect it rather than rely on the operator knowing.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_doctor_probes.py`:

```python
from orchestrator.core.doctor_probes import probe_env_drift


def test_env_drift_green_when_values_match() -> None:
    result = probe_env_drift(
        running={"LM_STUDIO_URL": "http://a:1234"},
        on_disk={"LM_STUDIO_URL": "http://a:1234"},
    )
    assert result.status is CheckStatus.GREEN


def test_env_drift_red_when_container_is_stale() -> None:
    """The exact trap: .env edited, container restarted, old value retained."""
    result = probe_env_drift(
        running={"LM_STUDIO_URL": "http://old:1234"},
        on_disk={"LM_STUDIO_URL": "http://new:1234"},
    )
    assert result.status is CheckStatus.RED
    assert "LM_STUDIO_URL" in result.detail


def test_env_drift_ignores_keys_absent_from_disk() -> None:
    """A key the file does not set is not drift."""
    result = probe_env_drift(
        running={"LM_STUDIO_URL": "http://a:1234", "OTHER": "x"},
        on_disk={"LM_STUDIO_URL": "http://a:1234"},
    )
    assert result.status is CheckStatus.GREEN


def test_env_drift_amber_when_nothing_could_be_read() -> None:
    result = probe_env_drift(running={}, on_disk={})
    assert result.status is CheckStatus.AMBER
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_doctor_probes.py -k env_drift -v`
Expected: FAIL with `ImportError: cannot import name 'probe_env_drift'`

- [ ] **Step 3: Implement the probe**

Add to `src/orchestrator/core/doctor_probes.py`:

```python
def probe_env_drift(
    running: dict[str, str], on_disk: dict[str, str]
) -> CheckResult:
    """Red when the running container's env disagrees with ``.env`` on disk.

    ``docker compose restart`` does NOT re-read ``.env``; only ``up -d``
    recreates the container with new values.  The docs correctly say
    ``restart`` for the MOUNTED yaml, and Quick Start says to edit ``.env``,
    so the repeated pattern teaches the wrong recovery for the wrong file.
    Detecting the drift is cheaper than expecting every operator to know it.

    Only keys the file actually sets are compared: a variable present in the
    container and absent from ``.env`` came from compose or the image and is
    not drift.

    Args:
        running: Environment as seen inside the running container.
        on_disk: Environment parsed from ``.env``.

    Returns:
        The check verdict.
    """
    if not running or not on_disk:
        return CheckResult(
            check_id="env_drift",
            status=CheckStatus.AMBER,
            detail="could not read the container or .env to compare",
        )
    drifted = sorted(
        key
        for key, value in on_disk.items()
        if key in running and running[key] != value
    )
    if drifted:
        return CheckResult(
            check_id="env_drift",
            status=CheckStatus.RED,
            detail=f"container env is stale for: {', '.join(drifted)}",
        )
    return CheckResult(
        check_id="env_drift",
        status=CheckStatus.GREEN,
        detail="container env matches .env",
    )
```

- [ ] **Step 4: Register the check and its hint**

In `src/orchestrator/core/doctor.py`, add to the check registry alongside the existing entries:

```python
    "env_drift": (
        "Container env matches .env",
        "run `docker compose up -d` (not `restart`) to recreate the container "
        "with the new .env values",
    ),
```

Match the exact tuple/dataclass shape the neighbouring registry entries use.

- [ ] **Step 5: Gather the facts in the API layer**

In `src/orchestrator/api/doctor.py`, add a gatherer and wire it in:

```python
def _env_drift_facts() -> tuple[dict[str, str], dict[str, str]]:
    """Return (running container env, .env on disk) for the drift check.

    The orchestrator process IS the container, so ``os.environ`` is the
    running env.  ``.env`` sits at the repo root next to the compose files.
    """
    env_path = _ENTRYPOINT_ROOT.parent / ".env"
    try:
        on_disk = parse_env(env_path.read_text(encoding="utf-8"))
    except OSError:
        return dict(os.environ), {}
    watched = {k: v for k, v in on_disk.items() if k in os.environ}
    return {k: os.environ[k] for k in watched}, watched
```

Then, alongside the other `_safe` gatherings:

```python
    (running_env, disk_env), env_drift_error = await _safe(
        "env_drift", _env_drift_facts, ({}, {})
    )
    result_map["env_drift"] = probes.probe_env_drift(
        running=running_env, on_disk=disk_env
    )
```

Add `import os` and an import of the `.env` parser (reuse `cli.init.parse_env` if importable from here; otherwise copy the minimal parser rather than adding a CLI dependency to the API layer).

- [ ] **Step 6: Run the doctor suites**

Run: `uv run pytest tests/test_doctor_probes.py tests/test_api_doctor.py tests/test_doctor.py tests/test_cli_doctor.py -v`
Expected: PASS. `tests/test_cli_doctor.py` may assert a check count; update it to include the new check.

- [ ] **Step 7: Commit**

```bash
git add src/orchestrator/core/doctor_probes.py src/orchestrator/core/doctor.py src/orchestrator/api/doctor.py tests/test_doctor_probes.py tests/test_api_doctor.py tests/test_cli_doctor.py
git commit -m "feat(doctor): detect a container env that has drifted from .env"
```

---

### Task 11: Full gate and documentation update

**Files:**
- Modify: `CLAUDE.md`
- Modify: `docs/gotchas.md`
- Modify: `docs/walkthrough-15min.md`

**Depends on:** Task 3, Task 5, Task 6, Task 9, Task 10

- [ ] **Step 1: Run the full suite with coverage**

Run: `uv run pytest --cov=orchestrator --cov-report=term-missing -q`
Expected: PASS, coverage at or above 80%

- [ ] **Step 2: Run lint, format, and types**

```bash
uv run ruff format src/ tests/
uv run ruff check --fix src/ tests/
uv run mypy src/orchestrator/ --ignore-missing-imports
```

Expected: all clean.

- [ ] **Step 3: Verify the CRLF trap did not fire**

Run: `git diff --numstat`
Expected: no file shows a whole-file rewrite. If `CLAUDE.md` or any doc shows every line changed, the line endings flipped — convert back to CRLF before committing.

- [ ] **Step 4: Add the gotchas**

Append to `docs/gotchas.md`:

```markdown
- **Agent image staleness is judged by CONTENT, never mtime**: `core/entrypoint_hash.py`
  hashes `entrypoint.sh` with LF-normalized line endings, the build bakes it into
  each image as the `org.praxis.entrypoint-sha256` LABEL, and the doctor compares
  the two. The predecessor compared image build time against the file's mtime, and
  since `git clone` stamps every file at clone time, a correct fresh install always
  reported a stale image. `image_content_differs` is deliberately TRI-STATE: an
  image built before the label existed carries none, and calling those stale would
  reproduce the same false red from the other direction, so unknown is AMBER.
- **The worker-endpoint check is gated on `supports_local_llm` on BOTH halves**: the
  model-name comparison was already gated (agy names a provider model, not an LM
  Studio one) but the reachability probe was not, and `if not reachable` fires
  first, so the flagged default preset `gemini-agy` could never go green. Gate both
  or neither.
- **`docker compose restart` does NOT re-read `.env`; only `up -d` does**: the docs
  say `restart` correctly and repeatedly about the MOUNTED `config/praxis.yaml`, and
  Quick Start says to edit `.env`, so the pattern teaches the wrong recovery for the
  wrong file. The `env_drift` doctor check now detects it instead of relying on the
  operator knowing.
- **An unmet preset requirement must print the remedy, not just the requirement**:
  `praxis init` names what is missing AND how to supply it, from the preset's
  `setup_hint` / `setup_doc` in `config/praxis.yaml`. It also writes the collected
  token, port, and credentials BEFORE the preset challenge can exit, because
  `_choose_preset` raises `typer.Exit(1)` and used to discard them all.
```

- [ ] **Step 5: Update the CLAUDE.md gotchas index**

Add four matching one-line entries to the condensed index in `CLAUDE.md`, in the same style as its neighbours.

- [ ] **Step 6: Record the outcome in the walkthrough doc**

In `docs/walkthrough-15min.md`, under "Defects found, ranked", mark items 1-5 with a `**FIXED 2026-08-16**` prefix and add a line under "What to fix, ranked" noting that items 1-5 are closed by `docs/superpowers/plans/2026-08-16-onboarding-blockers.md`. Leave items 6-11 untouched — they are explicitly out of this plan's scope.

- [ ] **Step 7: Commit**

```bash
git add CLAUDE.md docs/gotchas.md docs/walkthrough-15min.md
git commit -m "docs: record the onboarding-blocker fixes and their gotchas"
```

---

### Task 12: Live verification against a fresh clone

**Files:** None (verification only)

**Depends on:** Task 11

Every fix in this plan was found by running the product, not by reading it. None of them is proven by unit tests alone: the staleness fix in particular depends on a real Docker build populating a real label.

- [ ] **Step 1: Rebuild the agent images so the labels exist**

```bash
docker compose --profile agents build
docker inspect agy-agent:latest \
  --format '{{ index .Config.Labels "org.praxis.entrypoint-sha256" }}'
```

Expected: a 64-character hex digest, not empty.

- [ ] **Step 2: Confirm the label matches the source**

```bash
uv run python -c "
from pathlib import Path
from orchestrator.core.entrypoint_hash import hash_entrypoint
print(hash_entrypoint(Path('docker/agy-agent/entrypoint.sh')))
"
```

Expected: identical to the digest from Step 1.

- [ ] **Step 3: Clone fresh and confirm the staleness red is gone**

```bash
rm -rf /c/working-space/praxis-verify
git clone https://github.com/adiatmaja/praxis.git /c/working-space/praxis-verify
cd /c/working-space/praxis-verify && uv venv && uv sync --extra dev
uv run praxis doctor
```

Expected: `Agent images newer than their entrypoints` is **not** red. This is the exact condition that failed on both arms of the walkthrough: a fresh clone rewrites every mtime.

- [ ] **Step 4: Confirm the Enter-only path now prints the recipe and keeps the .env**

```bash
cd /c/working-space/praxis-verify
printf '\n\n\n\n\n\n' | uv run praxis init
ls -la .env
```

Expected: the output contains `agy login` and a `docs/deployment.md` pointer, and `.env` **exists** with `AUTH_TOKEN` and `PORT` set despite the exit code being 1.

- [ ] **Step 5: Confirm the agy preset can reach a green doctor**

With the `praxis-gemini-creds` volume seeded, run init choosing preset 1 and confirming `y`, then:

```bash
uv run praxis doctor
```

Expected: the `Worker endpoint` row is green with a "not applicable" detail rather than red. Combined with Step 3, a correct `gemini-agy` install should now show **zero** FAIL rows.

- [ ] **Step 6: Confirm the env-drift check fires**

```bash
echo 'LM_STUDIO_URL=http://drift-probe:9999' >> .env
docker compose restart orchestrator && sleep 8
uv run praxis doctor
```

Expected: `Container env matches .env` is RED naming `LM_STUDIO_URL`. Then:

```bash
docker compose up -d && sleep 8
uv run praxis doctor
```

Expected: that row returns to green. Remove the probe line from `.env` afterwards.

- [ ] **Step 7: Clean up**

```bash
cd /c/working-space/praxis-verify && docker compose down
docker volume rm praxis-verify_praxis_data
rm -rf /c/working-space/praxis-verify
```

- [ ] **Step 8: Record the live results**

Append a short "Live verification 2026-08-16" section to `docs/walkthrough-15min.md` recording what each step actually printed. If any step failed, record the failure verbatim rather than the intent, and open the gap as a follow-up.

```bash
git add docs/walkthrough-15min.md
git commit -m "docs: record live verification of the onboarding-blocker fixes"
```

---

## Parallel Execution Map

- **Wave 1:** Task 1 (hash helper), Task 6 (worker endpoint gate), Task 7 (preset data), Task 10 (env drift) — no dependencies, run in parallel
- **Wave 2:** Task 2 (Dockerfile labels, depends on Task 1), Task 4 (staleness probe, depends on Task 1), Task 8 (init hint, depends on Task 7)
- **Wave 3:** Task 3 (build args, depends on Task 2), Task 5 (doctor gathering, depends on Task 4), Task 9 (init write ordering, depends on Task 8)
- **Wave 4:** Task 11 (full gate + docs, depends on Tasks 3, 5, 6, 9, 10)
- **Wave 5:** Task 12 (live verification, depends on Task 11)

Note that Tasks 4 and 5 both touch the doctor seam and Tasks 8 and 9 both touch `src/cli/init.py`. They are in different waves precisely so they never run concurrently against the same file — two agents in one non-isolated tree destroy each other's uncommitted work.

---

## Notes

**Out of scope, deliberately.** Defects 6-11 from the walkthrough (`verify_cmd` unreachable from every client, `dashboard_url` wrong port, `add-project` requiring a model, no CLI dispatch command, README's stale 3.6/3.7 reference config, `/health` reporting `"commit":"dev"`) are real and recorded, but they belong to the CLI/API surface and the docs, not to the first-ten-minutes path this plan closes. They are worth a second plan.

**Still unverified after this plan.** Whether OpenCode sends `reasoning_effort` in its own LM Studio requests. The walkthrough saw no pathology, but on a single easy leaf and with no token accounting from opencode at all, so the question is open rather than answered. Not in scope here.
