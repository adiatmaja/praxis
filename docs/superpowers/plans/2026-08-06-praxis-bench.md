---
type: plan
spec_path: docs/superpowers/specs/2026-08-06-usable-praxis-spec.md
---

# Praxis Bench Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prove or disprove the capability-aware decomposition claim with a reproducible, stratified SWE-bench evaluation that isolates decomposition from verification, and ship the local git backend the benchmark needs, which doubles as "evaluate Praxis with zero GitHub credentials".

**Architecture:** Three phases. Phase A introduces a `GitBackend` protocol in `core/git_ops.py` with two implementations: the existing `gh`-CLI-backed `GitHubBackend` (behavior unchanged) and a new `LocalGitBackend` where a project's `repo_url` is a `file://` path to a bare repo, "open PR" becomes a recorded branch-and-base pair, review reads `git diff base...branch` from a fresh clone, and merge is a real `git merge --squash` pushed back. The merge gate, verify gates, and review flow are identical; only the PR plumbing differs. Phase B adds a dev-only `bench/` package that prepares SWE-bench instances as local bare repos, runs conditions A (monolithic) and B (Praxis decomposition) over a stratified sample, and grades with the OFFICIAL SWE-bench Docker harness. Phase C adds conditions C (decomposition without the verify gate) and D (decomposition plus adaptive split), the analysis math, and the published report.

**Tech Stack:** Python 3.11, git CLI, Docker (the SWE-bench grader runs in its own containers), pytest, no new runtime dependencies in the orchestrator image.

**Spec:** `docs/superpowers/specs/2026-08-06-usable-praxis-spec.md` (workstream B: sections 3.1 through 3.3; plan rows P4, P5, P6).

---

## Execution order across the three Usable-Praxis plans

Full order is documented in `2026-08-06-decomposition-standard-v2.md`. This plan's
place in it:

- **Phase A** (P4, local git backend) runs THIRD, after the engine plan's Phase A
  and the product plan's Phase A. It has no dependency on either; it is placed
  there so the benchmark's prerequisite lands before the engine work that the
  benchmark measures.
- **Phase B** (P5, pilot) runs SIXTH, after the engine plan's Phases B and C, so
  the pilot exercises the engine the report will describe.
- **Phase C** (P6, full run and report) runs SEVENTH.

---

## Dispatch guide: agent type per task

Read this before dispatching anything.

Model AND reasoning effort both come from the agent DEFINITION, not from the
Agent tool call. The tool call accepts `subagent_type`, `model`, `isolation`,
`run_in_background`, `description`, and `prompt`; it has no `effort` parameter.
The `effort` frontmatter field in `.claude/agents/*.md` **overrides the session
effort level** for the duration of that subagent (options `low`, `medium`,
`high`, `xhigh`, `max`; the available set depends on the model). So the clean
way to run this plan is to define the agent types below once, then dispatch each
task by `subagent_type` and change nothing else.

Set the ORCHESTRATING session to `high` (or `xhigh` for the phases marked
below). That governs your own planning, the between-task review, and the diff
audit; each subagent overrides it with its own effort.

Higher effort usually costs LESS in total here, not more: every task is a test,
run, implement, run, mutate, run, commit loop, so turn count dominates and
under-setting effort adds turns.

### Agent types to define once

Create these in `.claude/agents/` before starting. They are reused by all three
Usable Praxis plans.

```yaml
# .claude/agents/praxis-impl-light.md
---
name: praxis-impl-light
description: Mechanical Praxis plan tasks - doc index edits, single registry entries, small additive models.
model: haiku
effort: medium
---
Execute exactly one task from a Praxis implementation plan, following its steps
verbatim. Write the failing test first and confirm it fails for the stated
reason before implementing. Run every mutation check the task specifies. Do not
touch a file the task does not name.
```

```yaml
# .claude/agents/praxis-impl-standard.md
---
name: praxis-impl-standard
description: Standard Praxis plan implementation tasks - well-specified modules, validators, endpoints, and their tests.
model: sonnet
effort: high
---
Execute exactly one task from a Praxis implementation plan, following its steps
verbatim. Write the failing test first and confirm it fails for the stated
reason before implementing. Run every mutation check the task specifies; if a
mutation check does not fail, the test is vacuous - fix the test and redo the
check. Do not touch a file the task does not name. Report every file you
changed.
```

```yaml
# .claude/agents/praxis-impl-critical.md
---
name: praxis-impl-critical
description: High-risk Praxis plan tasks - load-bearing invariants, the review path, architecture seams, shell entrypoints.
model: opus
effort: xhigh
---
Execute exactly one task from a Praxis implementation plan. This task was marked
critical because a subtle error in it fails silently rather than loudly.

Before implementing, state in one paragraph what the load-bearing invariant is
and how the task's tests pin it. Write the failing test first and confirm it
fails for the stated reason. Run every mutation check the task specifies and
report its output; a mutation check that does not fail means the test is
vacuous, so fix the test and redo the check. Do not touch a file the task does
not name. Report every file you changed and every assumption you made.
```

```yaml
# .claude/agents/praxis-impl-max.md
---
name: praxis-impl-max
description: The single highest-stakes Praxis plan task, where an undetected error invalidates downstream work.
model: opus
effort: max
---
Execute exactly one task from a Praxis implementation plan. Correctness here
matters more than cost: an undetected error invalidates every result that
depends on this task, and the failure is not visible until much later.

Before implementing, state what would have to be true for this task's output to
be silently wrong, and how the task's tests would catch it. Write the failing
test first. Run every mutation check and report its full output. Verify the
result independently of the tests where the task tells you how. Do not touch a
file the task does not name.
```

```yaml
# .claude/agents/praxis-review-first.md
---
name: praxis-review-first
description: First-pass review of a completed Praxis plan task against the plan text.
model: sonnet
effort: high
tools: Read, Grep, Glob, Bash
---
Review one completed task against its plan text. Check: every step was done,
the tests match what the plan specified, the mutation checks actually ran, no
file outside the task's declared list was touched, and no em dash was
introduced. Report findings only; do not fix anything.
```

```yaml
# .claude/agents/praxis-review-adversarial.md
---
name: praxis-review-adversarial
description: Adversarial second-pass review of a critical Praxis plan task.
model: opus
effort: xhigh
tools: Read, Grep, Glob, Bash
---
Try to break this task's implementation. Assume the tests are wrong until you
have checked them. For each test, ask what mutation would leave it passing, and
say so if one exists. Check the load-bearing invariant the task names is
actually enforced, not merely asserted. Report findings only; do not fix
anything.
```

```yaml
# .claude/agents/praxis-review-recheck.md
---
name: praxis-review-recheck
description: Cheap re-review of a Praxis plan task after review fixes were applied.
model: haiku
effort: medium
tools: Read, Grep, Glob, Bash
---
Confirm that each previously reported finding was addressed and that nothing
else changed. Report findings only; do not fix anything.
```

If a model rejects an effort level (the available set depends on the model),
drop the `effort` line from that definition and let it inherit the session.

**Review pairing:** `praxis-review-first` after every task; then
`praxis-review-adversarial` for any task dispatched to `praxis-impl-critical` or
`praxis-impl-max`, and `praxis-review-recheck` for everything else.
`praxis-review-recheck` also handles every re-review after fixes.

**Never take a subagent's report at face value.** Diff every file it touched
before accepting the task. Subagents on this repo have historically done
unrequested work and under-reported it.

### Session baseline

| Phase | Tasks | Orchestrating-session effort |
|-------|-------|------------------------------|
| A | 1 to 6 | `xhigh` |
| B | 7 to 12 | `xhigh` |
| C | 13 to 17 | `high` |

### Per task

| Task | `subagent_type` | Effective model / effort | Why this tier |
|------|-----------------|--------------------------|---------------|
| 1 `GitBackend` seam | `praxis-impl-critical` | opus / xhigh | An architecture seam every later task builds on. Getting the protocol shape wrong is expensive to undo |
| 2 Route review through the backend | `praxis-impl-critical` | opus / xhigh | Touches `review_task`, `approve_task_merge`, and `reject_task_merge`. GitHub behavior must stay byte-identical |
| 3 Local preflight | `praxis-impl-standard` | sonnet / high | Additive branch in an established pattern |
| 4 Bind-mount the bare repo | `praxis-impl-standard` | sonnet / high | Env and volume wiring |
| 5 Both entrypoints | `praxis-impl-critical` | opus / xhigh | Shell plus an image rebuild, in the exact area where a printf bug once shipped past `bash -n`. The task requires EXECUTING the guards, not syntax-checking them |
| 6 End-to-end and docs | `praxis-impl-standard` | sonnet / high | Integration test against real git |
| 7 Bench skeleton | `praxis-impl-standard` | sonnet / high | Config data plus packaging exclusions |
| 8 Prepare instances | `praxis-impl-max` | opus / max | The highest-stakes task across the three plans. If the gold patch stays reachable, every number the full matrix produces is invalid and you will not find out until the whole run is spent. Its mutation check is the single most important verification in the three plans |
| 9 Stratified sampler | `praxis-impl-standard` | sonnet / high | Pure function plus a committed draw |
| 10 Metrics schema | `praxis-impl-standard` | sonnet / high | A dataclass and two file helpers |
| 11 Runner and grader | `praxis-impl-critical` | opus / xhigh | Owns the confounded-design guard and the mapping from the official harness's fields to the outcome flags. A wrong mapping publishes a wrong number |
| 12 Run the pilot | **You, not an agent** | n/a | Judging the three exit criteria is a human call, and step 6 is a hand-check by definition |
| 13 Bench-mode double gate | `praxis-impl-standard` | sonnet / high | Small and heavily tested, but read the literal-"1" rationale before touching it |
| 14 Conditions C and D | `praxis-impl-standard` | sonnet / high | Two pure translators |
| 15 Analysis math | `praxis-impl-critical` | opus / xhigh | Wilson and McNemar go straight into a published report. Known-answer fixtures pin them, but a mis-derived interval that happens to pass the fixture is the failure mode |
| 16 Report renderer | `praxis-impl-standard` | sonnet / high | Template filling, with the caveats structural |
| 17 Full run and publish | **You, not an agent** | n/a | Running the matrix and hand-classifying ten failures |

---

## Standing constraints (read before Task 1)

- **`gh pr` calls need `--repo <owner/name>`** or they resolve against the
  orchestrator's own cwd. Every call being moved behind the backend seam already
  passes it; preserve that.
- **Agent images are standalone and NOT in compose.** Phase A Task 5 changes
  `docker/opencode-agent/entrypoint.sh` and `docker/agy-agent/entrypoint.sh`.
  Both images MUST be rebuilt afterwards or a stale image silently runs the old
  logic. Read a baked file back with `docker cp`, never
  `docker run --entrypoint cat` (buildkit multi-manifest images resolve the
  attestation manifest and return nothing, a false negative).
- **Agent containers run non-root** as `agent`, workspace `/home/agent/workspace`.
  A bind-mounted bare repo must be readable AND writable by that user.
- **Remote preflight is GitHub-only today** (`core/preflight.py` step 1 rejects a
  non-GitHub URL with 422). Phase A Task 3 makes that a backend decision.
- **The bench package is dev-only.** It must be excluded from the orchestrator
  image and from the 80 percent coverage gate. Its report math still gets unit
  tests with known-answer fixtures.
- **`PRAXIS_BENCH_*` flags must be refusable outside bench mode.** Condition C
  disables the verify gate; that switch can never be reachable in normal
  operation.
- **No em dashes** in any prose, doc, code comment, or commit message.

### Test-harness facts (verified against the repo on 2026-08-06)

Do not re-derive these; they were checked while this plan was written and every
test in it assumes them.

| Fact | Detail |
|------|--------|
| Database fixture | **`db`** (not `test_db`), an async fixture yielding an initialized `Database` |
| API client fixture | **`client` is an httpx `AsyncClient`**, not a sync `TestClient`. Every API test is `async def` and every call is awaited |
| Auth fixture | `auth_headers` returns `{"Authorization": "Bearer test-auth"}` |
| Event bus | **`EventBus` has no callback API.** It is `subscribe() -> asyncio.Queue`, `unsubscribe(queue)`, `publish(event)`. Anything collecting events drains a queue |
| Foreign keys | `PRAGMA foreign_keys=ON`. A `projects` row needs its `users` row inserted first |
| Plan creation | `TaskQueue.create_plan(project_id, summary=None, source="user", ...) -> plan_id` |
| Agent run completion | `TaskQueue.complete_agent_run(run_id, status, logs)`. There is no `finish_agent_run` |
| Dead branches | `branch_sweeper.dead_branches(branches, *, open_pr_branches, terminal_failed, merged_plan)`, all keyword-only |
| Dispatch contract | `DispatchRequest` is keyed on **`repo_url`** plus `instructions`, `model`, `harness`, `branch`. There is no `project_id` field; the endpoint reuses an existing project matching `repo_url` |
| Execute-plan contract | `ExecutePlanRequest` is keyed on **`repo_url`** plus `plan`, `model`, `harness`, `branch` |
| Async mode | `asyncio_mode = "auto"`, so async tests need no decorator |
| Markers | `unit`, `integration`, `slow` are registered in `pyproject.toml` |


---

## File Structure

### Phase A (P4): local git backend

| File | Responsibility |
|------|----------------|
| Create `src/orchestrator/core/git_backend.py` | `GitBackend` protocol, `PullRequestRef`, `GitHubBackend`, `LocalGitBackend`, `resolve_backend` |
| Modify `src/orchestrator/core/git_ops.py` | Nothing removed; `GitOps` becomes the GitHub backend's implementation detail |
| Modify `src/orchestrator/core/preflight.py` | Backend-aware checks; local mode validates a bare repo path |
| Modify `src/orchestrator/core/orchestrator_review.py` | Route PR diff, comment, and merge through the backend |
| Modify `src/orchestrator/core/agent_manager.py` | Bind-mount the bare repo and omit `GH_TOKEN` in local mode |
| Modify `docker/opencode-agent/entrypoint.sh` | Skip credential-helper setup and `gh pr create` in local mode |
| Modify `docker/agy-agent/entrypoint.sh` | Same |
| Modify `docs/gotchas.md`, `CLAUDE.md` | The local-mode gotchas |

### Phase B (P5): pilot

| File | Responsibility |
|------|----------------|
| Create `bench/README.md` | How to run it, what it costs, what it proves |
| Create `bench/config.py` | Paths, strata, conditions, worker matrix |
| Create `bench/prepare.py` | SWE-bench instance to local bare repo at the buggy base commit |
| Create `bench/sample.py` | Stratified sampler with a published seed |
| Create `bench/runner.py` | Drive one instance through one condition via the REST API |
| Create `bench/grade.py` | Extract the patch and invoke the official SWE-bench grader |
| Create `bench/metrics.py` | The JSONL row schema and writer |
| Create `bench/samples/lite-pilot-30.json` | The committed pilot sample list |
| Create `tests/bench/test_sample.py`, `tests/bench/test_metrics.py` | Determinism and schema tests |
| Modify `pyproject.toml` | Exclude `bench` from packaging and coverage |

### Phase C (P6): full run and report

| File | Responsibility |
|------|----------------|
| Create `src/orchestrator/core/bench_mode.py` | The double-gated bench flag reader |
| Modify `src/orchestrator/core/orchestrator_review.py` | Honour the verify-gate disable in bench mode only |
| Create `bench/stats.py` | Wilson intervals, paired McNemar |
| Create `bench/report.py` | Per-stratum tables, ablation, honesty sections |
| Create `bench/templates/report.md.tmpl` | The report skeleton with the mandatory caveat sections |
| Create `tests/bench/test_stats.py` | Known-answer fixtures for Wilson and McNemar |
| Create `docs/bench/<date>-report.md` and `docs/bench/raw/*.jsonl` | The published artifact |

---

## Phase A: the local git backend

### Task 1: The `GitBackend` protocol and the GitHub implementation

**Files:**
- Create: `src/orchestrator/core/git_backend.py`
- Test: `tests/test_git_backend.py`

**Depends on:** None

- [ ] **Step 1: Write the failing test**

Create `tests/test_git_backend.py`:

```python
"""The backend seam: GitHub behavior is unchanged, local is new.

Everything above this seam (merge gate, verify gates, review flow) must be
identical for both backends; only the PR plumbing differs.
"""

from unittest.mock import AsyncMock

import pytest

from orchestrator.core.git_backend import (
    GitHubBackend,
    PullRequestRef,
    is_local_repo_url,
    resolve_backend,
)


@pytest.mark.unit
def test_a_file_url_is_a_local_repo():
    assert is_local_repo_url("file:///srv/bench/astropy.git") is True


@pytest.mark.unit
def test_an_absolute_path_is_a_local_repo():
    assert is_local_repo_url("/srv/bench/astropy.git") is True


@pytest.mark.unit
def test_a_windows_path_is_a_local_repo():
    assert is_local_repo_url(r"C:\bench\astropy.git") is True


@pytest.mark.unit
def test_an_https_github_url_is_not_local():
    assert is_local_repo_url("https://github.com/o/r") is False


@pytest.mark.unit
def test_an_ssh_github_url_is_not_local():
    assert is_local_repo_url("git@github.com:o/r.git") is False


@pytest.mark.unit
def test_resolve_backend_picks_github_for_a_github_url():
    backend = resolve_backend("https://github.com/o/r", git_ops=AsyncMock())
    assert isinstance(backend, GitHubBackend)
    assert backend.name == "github"


@pytest.mark.unit
def test_resolve_backend_picks_local_for_a_file_url():
    backend = resolve_backend("file:///srv/bench/a.git", git_ops=AsyncMock())
    assert backend.name == "local"


@pytest.mark.unit
def test_pull_request_ref_round_trips_through_its_url_form():
    ref = PullRequestRef(backend="local", branch="agent/x", base="main", number=None)
    assert PullRequestRef.from_url(ref.to_url()) == ref


@pytest.mark.unit
def test_pull_request_ref_parses_a_real_github_pr_url():
    ref = PullRequestRef.from_url("https://github.com/o/r/pull/42")
    assert ref.backend == "github"
    assert ref.number == 42


@pytest.mark.unit
async def test_github_backend_get_diff_delegates_to_git_ops_with_repo():
    git = AsyncMock()
    git.extract_pr_number.return_value = 42
    git.repo_slug.return_value = "o/r"
    git.get_pr_diff.return_value = "diff --git a/x b/x"
    backend = GitHubBackend(git)
    ref = PullRequestRef.from_url("https://github.com/o/r/pull/42")

    diff = await backend.get_diff(ref)

    assert diff == "diff --git a/x b/x"
    git.get_pr_diff.assert_awaited_once()
    assert git.get_pr_diff.await_args.kwargs["repo"] == "o/r"


@pytest.mark.unit
async def test_github_backend_merge_delegates_to_merge_pr():
    git = AsyncMock()
    git.extract_pr_number.return_value = 42
    git.repo_slug.return_value = "o/r"
    backend = GitHubBackend(git)
    await backend.merge(PullRequestRef.from_url("https://github.com/o/r/pull/42"))
    git.merge_pr.assert_awaited_once()


@pytest.mark.unit
async def test_github_backend_comment_delegates_to_comment_on_pr():
    git = AsyncMock()
    git.extract_pr_number.return_value = 42
    git.repo_slug.return_value = "o/r"
    backend = GitHubBackend(git)
    await backend.comment(
        PullRequestRef.from_url("https://github.com/o/r/pull/42"), "feedback"
    )
    git.comment_on_pr.assert_awaited_once()
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_git_backend.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'orchestrator.core.git_backend'`.

- [ ] **Step 3: Write the module**

Create `src/orchestrator/core/git_backend.py`:

```python
"""Git hosting seam: GitHub PRs or a local bare repo.

The whole loop above this module (merge gate, verify gates, review flow,
outcome recording) is identical for both backends.  What differs is only the
plumbing that GitHub calls a pull request:

- ``github``: a real PR object, created and merged with the ``gh`` CLI.
- ``local``: the project's ``repo_url`` is a filesystem path to a BARE repo.
  There is no PR object, so a "PR" is just the (branch, base) pair recorded on
  the task; the diff is ``git diff base...branch`` from a fresh clone and the
  merge is a real ``git merge --squash`` executed in a clone and pushed.

Local mode exists because the benchmark needs to run 100+ instances without
rate-limiting a GitHub account, and because "evaluate Praxis without giving it
a GitHub credential" removes the single largest setup cliff.  GitHub stays the
default and the recommendation for real work: an inspectable PR is the unit of
trust.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote, unquote


logger = logging.getLogger(__name__)

# A local "PR" is encoded as a URL so it can live in the existing
# ``tasks.pr_url`` TEXT column with no schema change and stay greppable.
_LOCAL_PR_RE = re.compile(r"^praxis-local://pr\?branch=([^&]+)&base=([^&]+)$")

_WINDOWS_PATH_RE = re.compile(r"^[A-Za-z]:[\\/]")


def is_local_repo_url(repo_url: str) -> bool:
    """True when ``repo_url`` names a local bare repo rather than a remote host.

    Recognized: a ``file://`` URL, a POSIX absolute path, and a Windows drive
    path.  Everything else (https, ssh, scp-style) is remote.
    """
    value = (repo_url or "").strip()
    if not value:
        return False
    if value.startswith("file://"):
        return True
    if _WINDOWS_PATH_RE.match(value):
        return True
    return value.startswith("/")


def local_repo_path(repo_url: str) -> str:
    """Return the filesystem path for a local repo URL."""
    value = repo_url.strip()
    if value.startswith("file://"):
        value = unquote(value[len("file://") :])
        # file:///c:/x on Windows leaves a leading slash before the drive.
        if _WINDOWS_PATH_RE.match(value.lstrip("/")):
            value = value.lstrip("/")
    return value


@dataclass(frozen=True)
class PullRequestRef:
    """A reviewable change, in whichever form the backend expresses one."""

    backend: str
    branch: str
    base: str
    number: int | None = None
    repo: str | None = None

    def to_url(self) -> str:
        """Render the ref for storage in ``tasks.pr_url``."""
        if self.backend == "github":
            return f"https://github.com/{self.repo}/pull/{self.number}"
        return (
            "praxis-local://pr"
            f"?branch={quote(self.branch, safe='')}"
            f"&base={quote(self.base, safe='')}"
        )

    @classmethod
    def from_url(cls, url: str) -> PullRequestRef:
        """Parse a stored ``pr_url`` back into a ref.

        Raises:
            ValueError: If the URL is neither a GitHub PR URL nor a local ref.
        """
        match = _LOCAL_PR_RE.match(url)
        if match:
            return cls(
                backend="local",
                branch=unquote(match.group(1)),
                base=unquote(match.group(2)),
            )
        github = re.match(
            r"^https://github\.com/([^/]+/[^/]+)/pull/(\d+)/?$", url.strip()
        )
        if github:
            return cls(
                backend="github",
                branch="",
                base="",
                number=int(github.group(2)),
                repo=github.group(1),
            )
        message = f"unrecognized pull-request reference: {url!r}"
        raise ValueError(message)


class GitBackend(Protocol):
    """What the review loop needs from a git host."""

    name: str

    async def get_diff(self, ref: PullRequestRef) -> str:
        """Return the unified diff of the change."""
        ...

    async def checkout(self, ref: PullRequestRef, dest: str) -> str:
        """Clone and check out the change's head into ``dest``; return ``dest``."""
        ...

    async def comment(self, ref: PullRequestRef, body: str) -> None:
        """Record review feedback against the change."""
        ...

    async def merge(self, ref: PullRequestRef) -> None:
        """Squash-merge the change into its base."""
        ...


class GitHubBackend:
    """The existing ``gh``-CLI behavior, unchanged, behind the protocol."""

    name = "github"

    def __init__(self, git_ops: Any) -> None:
        self._git = git_ops

    def _repo(self, ref: PullRequestRef) -> str | None:
        return ref.repo

    async def get_diff(self, ref: PullRequestRef) -> str:
        return await self._git.get_pr_diff(".", ref.number, repo=self._repo(ref))

    async def checkout(self, ref: PullRequestRef, dest: str) -> str:
        return await self._git.clone_pr_head(ref.to_url(), dest)

    async def comment(self, ref: PullRequestRef, body: str) -> None:
        await self._git.comment_on_pr(".", ref.number, body, repo=self._repo(ref))

    async def merge(self, ref: PullRequestRef) -> None:
        await self._git.merge_pr(".", ref.number, repo=self._repo(ref))


class LocalGitBackend:
    """A bare repo on disk. No PR objects, same review and merge semantics."""

    name = "local"

    def __init__(self, repo_url: str) -> None:
        self._path = local_repo_path(repo_url)

    async def _run(self, cmd: list[str], cwd: str | None = None) -> tuple[int, str, str]:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
        out, err = await proc.communicate()
        return proc.returncode or 0, out.decode(errors="replace"), err.decode(
            errors="replace"
        )

    async def _run_checked(self, cmd: list[str], cwd: str | None = None) -> str:
        code, out, err = await self._run(cmd, cwd=cwd)
        if code != 0:
            message = f"git command failed (exit {code}): {' '.join(cmd)}\n{err}"
            raise RuntimeError(message)
        return out.strip()

    async def get_diff(self, ref: PullRequestRef) -> str:
        """Diff the branch against its merge base, straight from the bare repo."""
        merge_base = await self._run_checked(
            ["git", "-C", self._path, "merge-base", ref.base, ref.branch]
        )
        return await self._run_checked(
            ["git", "-C", self._path, "diff", f"{merge_base}..{ref.branch}"]
        )

    async def checkout(self, ref: PullRequestRef, dest: str) -> str:
        """Clone the bare repo and check the branch out into ``dest``."""
        await self._run_checked(
            ["git", "clone", "--no-single-branch", self._path, dest]
        )
        await self._run_checked(["git", "checkout", ref.branch], cwd=dest)
        return dest

    async def comment(self, ref: PullRequestRef, body: str) -> None:
        """No PR object exists, so feedback lives only on the task row.

        The orchestrator already persists ``review_feedback`` before calling
        this, so a no-op here loses nothing.  Logged so a bench run's feedback
        is still greppable.
        """
        logger.info("local review feedback on %s: %s", ref.branch, body)

    async def merge(self, ref: PullRequestRef) -> None:
        """Squash-merge in a throwaway clone and push the base back.

        A bare repo cannot merge in place, so this clones, merges, pushes, and
        deletes the source branch, matching ``gh pr merge --squash
        --delete-branch``.
        """
        workdir = tempfile.mkdtemp(prefix="praxis-local-merge-")
        try:
            await self._run_checked(
                ["git", "clone", "--no-single-branch", self._path, workdir]
            )
            await self._run_checked(["git", "checkout", ref.base], cwd=workdir)
            await self._run_checked(
                ["git", "merge", "--squash", f"origin/{ref.branch}"], cwd=workdir
            )
            await self._run_checked(
                [
                    "git",
                    "-c",
                    "user.name=praxis",
                    "-c",
                    "user.email=praxis@localhost",
                    "commit",
                    "-m",
                    f"Merge {ref.branch} into {ref.base} (squash)",
                ],
                cwd=workdir,
            )
            await self._run_checked(["git", "push", "origin", ref.base], cwd=workdir)
            await self._run_checked(
                ["git", "push", "origin", "--delete", ref.branch], cwd=workdir
            )
        finally:
            shutil.rmtree(workdir, ignore_errors=True)


def resolve_backend(repo_url: str, git_ops: Any) -> GitBackend:
    """Return the backend for a project's ``repo_url``.

    GitHub is the default; a filesystem path selects local mode.
    """
    if is_local_repo_url(repo_url):
        return LocalGitBackend(repo_url)
    return GitHubBackend(git_ops)
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/test_git_backend.py -v`
Expected: PASS (12 tests).

- [ ] **Step 5: Add a real-git integration test for the local backend**

Create `tests/test_local_git_backend.py`:

```python
"""LocalGitBackend against a real bare repo. No mocks: git is the contract."""

import subprocess

import pytest

from orchestrator.core.git_backend import LocalGitBackend, PullRequestRef


def _git(*args: str, cwd) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.fixture
def bare_repo(tmp_path):
    """A bare repo with a `main` commit and an `agent/x` branch on top."""
    work = tmp_path / "work"
    bare = tmp_path / "repo.git"
    work.mkdir()
    _git("init", "-b", "main", cwd=work)
    _git("config", "user.email", "t@example.com", cwd=work)
    _git("config", "user.name", "t", cwd=work)
    (work / "a.txt").write_text("one\n", encoding="utf-8")
    _git("add", "a.txt", cwd=work)
    _git("commit", "-m", "base", cwd=work)
    _git("clone", "--bare", str(work), str(bare), cwd=tmp_path)
    _git("remote", "add", "origin", str(bare), cwd=work)
    _git("checkout", "-b", "agent/x", cwd=work)
    (work / "a.txt").write_text("one\ntwo\n", encoding="utf-8")
    _git("commit", "-am", "add two", cwd=work)
    _git("push", "origin", "agent/x", cwd=work)
    _git("push", "origin", "main", cwd=work)
    return bare


@pytest.fixture
def ref():
    return PullRequestRef(backend="local", branch="agent/x", base="main")


@pytest.mark.integration
async def test_get_diff_returns_the_branch_changes(bare_repo, ref):
    diff = await LocalGitBackend(str(bare_repo)).get_diff(ref)
    assert "+two" in diff
    assert "a.txt" in diff


@pytest.mark.integration
async def test_checkout_produces_a_working_tree_at_the_branch(bare_repo, ref, tmp_path):
    dest = tmp_path / "checkout"
    await LocalGitBackend(str(bare_repo)).checkout(ref, str(dest))
    assert (dest / "a.txt").read_text(encoding="utf-8") == "one\ntwo\n"


@pytest.mark.integration
async def test_merge_squashes_into_base_and_deletes_the_branch(bare_repo, ref, tmp_path):
    await LocalGitBackend(str(bare_repo)).merge(ref)
    heads = _git("branch", "--list", cwd=bare_repo)
    assert "agent/x" not in heads
    dest = tmp_path / "after"
    _git("clone", str(bare_repo), str(dest), cwd=tmp_path)
    assert (dest / "a.txt").read_text(encoding="utf-8") == "one\ntwo\n"


@pytest.mark.integration
async def test_merge_produces_exactly_one_new_commit_on_base(bare_repo, ref):
    before = int(_git("rev-list", "--count", "main", cwd=bare_repo))
    await LocalGitBackend(str(bare_repo)).merge(ref)
    after = int(_git("rev-list", "--count", "main", cwd=bare_repo))
    assert after == before + 1


@pytest.mark.integration
async def test_a_file_url_and_a_plain_path_behave_identically(bare_repo, ref):
    from_path = await LocalGitBackend(str(bare_repo)).get_diff(ref)
    from_url = await LocalGitBackend(
        "file:///" + str(bare_repo).replace("\\", "/").lstrip("/")
    ).get_diff(ref)
    assert from_path == from_url
```

Run: `uv run pytest tests/test_local_git_backend.py -v`
Expected: PASS (5 tests). These are marked `integration` because they shell out
to real git; that marker is already registered in `pyproject.toml`.

- [ ] **Step 6: Mutation-check the squash semantics**

Temporarily change `["git", "merge", "--squash", ...]` to
`["git", "merge", "--no-ff", ...]` and drop the follow-up `commit`.
Run: `uv run pytest tests/test_local_git_backend.py::test_merge_produces_exactly_one_new_commit_on_base -v`
Expected: FAIL (a no-ff merge of a one-commit branch adds two commits). Restore
and re-run to confirm PASS.

- [ ] **Step 7: Commit**

```bash
git add src/orchestrator/core/git_backend.py tests/test_git_backend.py tests/test_local_git_backend.py
git commit -m "feat(git-backend): add the GitBackend seam with a local implementation

GitHub behavior is unchanged behind the protocol; local mode treats a bare
repo path as the remote, encodes a PR as a (branch, base) pair in the
existing pr_url column, and squash-merges in a throwaway clone."
```

---

### Task 2: Route the review loop through the backend

**Files:**
- Modify: `src/orchestrator/core/orchestrator_review.py`
- Modify: `src/orchestrator/core/orchestrator.py` (constructor)
- Test: `tests/test_review_backend_routing.py`

**Depends on:** Task 1

- [ ] **Step 1: Write the failing test**

Create `tests/test_review_backend_routing.py`:

```python
"""Review is backend-agnostic: identical flow, different plumbing."""

from unittest.mock import AsyncMock

import pytest

from orchestrator.models.schemas import TaskStatus


@pytest.mark.unit
async def test_a_github_project_still_uses_gh(orchestrator_fixture):
    orch, task_id, project = orchestrator_fixture
    orch._opus.review_diff.return_value = {"verdict": "fail", "feedback": "nope"}
    await orch.review_task(task_id, project)
    orch._git.get_pr_diff.assert_awaited()
    orch._git.comment_on_pr.assert_awaited()


@pytest.mark.unit
async def test_a_local_project_never_touches_gh(orchestrator_fixture, tmp_path):
    orch, task_id, project = orchestrator_fixture
    local = dict(project)
    local["repo_url"] = str(tmp_path / "repo.git")
    await orch._tq.set_task_pr_url(
        task_id, "praxis-local://pr?branch=agent%2Fa&base=main"
    )
    await orch._tq.update_task_status(task_id, TaskStatus.REVIEWING)

    backend = AsyncMock()
    backend.name = "local"
    backend.get_diff.return_value = "diff --git a/x b/x\n+y\n"
    backend.checkout.side_effect = RuntimeError("no checkout in this test")
    orch._resolve_backend = lambda repo_url: backend

    orch._opus.review_diff.return_value = {"verdict": "fail", "feedback": "nope"}
    await orch.review_task(task_id, local)

    backend.get_diff.assert_awaited_once()
    backend.comment.assert_awaited_once()
    orch._git.get_pr_diff.assert_not_awaited()
    orch._git.comment_on_pr.assert_not_awaited()


@pytest.mark.unit
async def test_a_local_project_merges_through_the_backend(orchestrator_fixture, tmp_path):
    orch, task_id, project = orchestrator_fixture
    local = dict(project)
    local["repo_url"] = str(tmp_path / "repo.git")
    local["auto_merge"] = True
    await orch._tq.set_task_pr_url(
        task_id, "praxis-local://pr?branch=agent%2Fa&base=feature"
    )
    await orch._tq.update_task_status(task_id, TaskStatus.REVIEWING)

    backend = AsyncMock()
    backend.name = "local"
    backend.get_diff.return_value = "diff --git a/x b/x\n+y\n"
    backend.checkout.side_effect = RuntimeError("no checkout in this test")
    orch._resolve_backend = lambda repo_url: backend

    orch._opus.review_diff.return_value = {"verdict": "pass", "feedback": "ok"}
    await orch.review_task(task_id, local)

    backend.merge.assert_awaited_once()
    orch._git.merge_pr.assert_not_awaited()
    task = await orch._tq.get_task(task_id)
    assert task["status"] == TaskStatus.MERGED


@pytest.mark.unit
async def test_the_merge_gate_still_parks_a_local_pass_without_auto_merge(
    orchestrator_fixture, tmp_path
):
    """The merge gate is backend-independent: local mode does not bypass it."""
    orch, task_id, project = orchestrator_fixture
    local = dict(project)
    local["repo_url"] = str(tmp_path / "repo.git")
    local["auto_merge"] = False
    await orch._tq.set_task_pr_url(
        task_id, "praxis-local://pr?branch=agent%2Fa&base=main"
    )
    await orch._tq.update_task_status(task_id, TaskStatus.REVIEWING)

    backend = AsyncMock()
    backend.name = "local"
    backend.get_diff.return_value = "diff --git a/x b/x\n+y\n"
    backend.checkout.side_effect = RuntimeError("no checkout in this test")
    orch._resolve_backend = lambda repo_url: backend

    orch._opus.review_diff.return_value = {"verdict": "pass", "feedback": "ok"}
    await orch.review_task(task_id, local)

    backend.merge.assert_not_awaited()
    task = await orch._tq.get_task(task_id)
    assert task["status"] == TaskStatus.PASSED
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_review_backend_routing.py -v`
Expected: FAIL. The local tests fail because `review_task` calls
`self._git.extract_pr_number` on a `praxis-local://` URL.

- [ ] **Step 3: Add the backend resolver to the orchestrator**

In `src/orchestrator/core/orchestrator.py`, add a method to the `Orchestrator`
class (not a mixin, since all mixins use it):

```python
    def _resolve_backend(self, repo_url: str) -> Any:
        """Return the git backend for a project. Overridable in tests."""
        from orchestrator.core.git_backend import resolve_backend

        return resolve_backend(repo_url, self._git)
```

- [ ] **Step 4: Route `review_task` through the backend**

In `src/orchestrator/core/orchestrator_review.py`, add the import:

```python
from orchestrator.core.git_backend import PullRequestRef
```

Replace the PR-resolution block near the top of `review_task`:

```python
        pr_number = await self._git.extract_pr_number(task["pr_url"])
        repo = self._git.repo_slug(task["pr_url"]) or self._git.repo_slug(
            project["repo_url"]
        )
```

with:

```python
        backend = self._resolve_backend(project["repo_url"])
        try:
            ref = PullRequestRef.from_url(task["pr_url"])
        except ValueError:
            logger.warning(
                "Task %s has an unparseable pr_url %r; skipping review",
                task_id,
                task["pr_url"],
            )
            return
        if ref.backend == "github" and ref.repo is None:
            # Target the PR's own repo explicitly; otherwise gh resolves the PR
            # against the orchestrator's own cwd and reviews the wrong diff.
            ref = replace(ref, repo=self._git.repo_slug(project["repo_url"]))
```

Add `from dataclasses import dataclass, replace` to the existing dataclass import.

Then replace each remaining direct `gh` call in `review_task`:

- `await self._git.clone_pr_head(task["pr_url"], _checkout_dir)` becomes
  `await backend.checkout(ref, _checkout_dir)`
- `diff = await self._git.get_pr_diff(".", pr_number, repo=repo)` becomes
  `diff = await backend.get_diff(ref)`
- `await self._git.merge_pr(".", pr_number, repo=repo)` becomes
  `await backend.merge(ref)`
- `await self._git.comment_on_pr(".", pr_number, feedback, repo=repo)` becomes
  `await backend.comment(ref, feedback)`

Apply the same four substitutions in `approve_task_merge` and
`reject_task_merge`, resolving the backend and ref the same way at the top of
each.

- [ ] **Step 5: Run the test to verify it passes**

Run: `uv run pytest tests/test_review_backend_routing.py tests/test_orchestrator.py -v`
Expected: PASS. Existing orchestrator review tests assert on `self._git`
call counts through the `GitHubBackend`, which delegates to the same methods
with the same arguments, so they should be unaffected. Where a test asserted
`get_pr_diff(".", 42, repo="o/r")` positionally, adjust it to match the
backend's call shape rather than changing the backend.

- [ ] **Step 6: Mutation-check the backend routing**

Temporarily change `_resolve_backend` to always return `GitHubBackend(self._git)`.
Run: `uv run pytest tests/test_review_backend_routing.py -v -k local`
Expected: FAIL. Restore and re-run to confirm PASS.

- [ ] **Step 7: Commit**

```bash
git add src/orchestrator/core/orchestrator.py src/orchestrator/core/orchestrator_review.py tests/test_review_backend_routing.py
git commit -m "refactor(review): route PR diff, checkout, comment, and merge through the backend

The merge gate, verify gates, and review flow are unchanged and shared;
only the plumbing under them differs by backend."
```

---

### Task 3: Backend-aware preflight

**Files:**
- Modify: `src/orchestrator/core/preflight.py`
- Test: `tests/test_preflight_local.py`

**Depends on:** Task 1

- [ ] **Step 1: Write the failing test**

Create `tests/test_preflight_local.py`:

```python
"""Local mode has its own cheap preflight: real bare repo, real branch."""

import subprocess

import pytest

from orchestrator.core.preflight import PreflightError, PreflightKind, preflight_remote


@pytest.fixture
def bare_repo(tmp_path):
    work = tmp_path / "w"
    bare = tmp_path / "r.git"
    work.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=work, check=True)
    subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=work, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=work, check=True)
    (work / "a.txt").write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "add", "a.txt"], cwd=work, check=True)
    subprocess.run(["git", "commit", "-m", "base"], cwd=work, check=True)
    subprocess.run(["git", "clone", "--bare", str(work), str(bare)], check=True)
    return bare


@pytest.mark.integration
async def test_a_valid_local_repo_passes_with_no_warnings(bare_repo):
    warnings = await preflight_remote(
        git=None, repo_url=str(bare_repo), base="main", credential_configured=False
    )
    assert warnings == []


@pytest.mark.integration
async def test_a_missing_local_path_is_422(tmp_path):
    with pytest.raises(PreflightError) as exc:
        await preflight_remote(
            git=None,
            repo_url=str(tmp_path / "nope.git"),
            base="main",
            credential_configured=False,
        )
    assert exc.value.kind is PreflightKind.MISSING_REPO


@pytest.mark.integration
async def test_a_non_bare_directory_is_422(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(PreflightError) as exc:
        await preflight_remote(
            git=None, repo_url=str(plain), base="main", credential_configured=False
        )
    assert exc.value.kind is PreflightKind.NOT_A_REPO


@pytest.mark.integration
async def test_a_missing_base_branch_is_422(bare_repo):
    with pytest.raises(PreflightError) as exc:
        await preflight_remote(
            git=None,
            repo_url=str(bare_repo),
            base="does-not-exist",
            credential_configured=False,
        )
    assert exc.value.kind is PreflightKind.MISSING_BRANCH


@pytest.mark.integration
async def test_local_mode_needs_no_github_credential(bare_repo):
    """The whole point: evaluate Praxis with zero GitHub credentials."""
    warnings = await preflight_remote(
        git=None, repo_url=str(bare_repo), base="main", credential_configured=False
    )
    assert not any("credential" in w for w in warnings)


@pytest.mark.unit
def test_the_two_new_kinds_map_to_422():
    from orchestrator.core.preflight import status_and_detail

    for kind in (PreflightKind.MISSING_REPO, PreflightKind.NOT_A_REPO):
        status, _ = status_and_detail(PreflightError(kind, "x"))
        assert status == 422
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_preflight_local.py -v`
Expected: FAIL with `AttributeError: MISSING_REPO`, and the GitHub-only step 1
rejecting the path with `NOT_GITHUB`.

- [ ] **Step 3: Add the local branch to preflight**

In `src/orchestrator/core/preflight.py`, add two kinds to `PreflightKind`:

```python
    MISSING_REPO = "missing_repo"
    NOT_A_REPO = "not_a_repo"
```

and to `_STATUS_FOR_KIND`:

```python
    PreflightKind.MISSING_REPO: 422,
    PreflightKind.NOT_A_REPO: 422,
```

Add the local check function above `preflight_remote`:

```python
async def _preflight_local(
    repo_url: str, base: str, branch: str | None
) -> list[str]:
    """Validate a local bare repo: it exists, it is bare, the branch is there.

    Runs no network calls and needs no credential.  This is the "evaluate
    Praxis with zero GitHub credentials" path.
    """
    import asyncio
    from pathlib import Path

    from orchestrator.core.git_backend import local_repo_path

    path = Path(local_repo_path(repo_url))
    if not path.exists():
        raise PreflightError(
            PreflightKind.MISSING_REPO,
            f"local repository path does not exist: {path}",
        )

    async def _git(*args: str) -> tuple[int, str]:
        proc = await asyncio.create_subprocess_exec(
            "git",
            "-C",
            str(path),
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, _ = await proc.communicate()
        return proc.returncode or 0, out.decode(errors="replace").strip()

    code, is_bare = await _git("rev-parse", "--is-bare-repository")
    if code != 0:
        raise PreflightError(
            PreflightKind.NOT_A_REPO,
            f"not a git repository: {path}",
        )
    if is_bare != "true":
        raise PreflightError(
            PreflightKind.NOT_A_REPO,
            f"local repository must be BARE (workers push to it): {path}",
        )

    for name in filter(None, (base, branch)):
        code, _ = await _git("rev-parse", "--verify", f"refs/heads/{name}")
        if code != 0:
            raise PreflightError(
                PreflightKind.MISSING_BRANCH,
                f"branch not found in local repository: {name}",
            )
    return []
```

At the very top of `preflight_remote`, before the GitHub-only step 1, add:

```python
    from orchestrator.core.git_backend import is_local_repo_url

    # Local mode: a bare repo on disk. No remote calls, no credential.
    if is_local_repo_url(repo_url):
        return await _preflight_local(repo_url, base, branch)
```

and update the docstring's step list to name step 0.

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/test_preflight_local.py tests/test_preflight.py -v`
Expected: PASS. Existing GitHub preflight tests are untouched because the new
branch only fires for a filesystem path.

- [ ] **Step 5: Mutation-check the bare requirement**

Temporarily remove the `if is_bare != "true":` block.
Run: `uv run pytest tests/test_preflight_local.py::test_a_non_bare_directory_is_422 -v`
Expected: FAIL. Restore and re-run to confirm PASS. The bare requirement is
load-bearing: pushing to a checked-out branch of a non-bare repo is refused by
git and would fail deep inside the worker instead of at preflight.

- [ ] **Step 6: Commit**

```bash
git add src/orchestrator/core/preflight.py tests/test_preflight_local.py
git commit -m "feat(preflight): validate a local bare repo with no credential

Local mode short-circuits the GitHub-only path: the repo exists, it is
bare, and the base and named branches are present. Two new 422 kinds."
```

---

### Task 4: Mount the bare repo into the agent container

**Files:**
- Modify: `src/orchestrator/core/agent_manager.py`
- Test: `tests/test_agent_manager_local_repo.py`

**Depends on:** Task 1

- [ ] **Step 1: Write the failing test**

Create `tests/test_agent_manager_local_repo.py`:

```python
"""In local mode the bare repo is bind-mounted and GH_TOKEN is not required."""

import pytest

from orchestrator.core.agent_manager import LOCAL_REPO_MOUNT, build_spawn_env


def _env(repo_url: str, **overrides) -> dict[str, str]:
    base = {
        "repo_url": repo_url,
        "branch": "agent/x",
        "base_branch": "main",
        "task_prompt": "do it",
        "container_lm_url": "http://host.docker.internal:1234",
        "model_name": "m",
        "harness_id": "opencode",
        "gh_token": "",
        "callback_url": "http://host.docker.internal:8080/cb",
        "task_id": "t1",
        "git_author_name": "praxis",
        "git_author_email": "praxis@example.com",
    }
    base.update(overrides)
    return build_spawn_env(**base)


@pytest.mark.unit
def test_local_mode_sets_the_backend_flag():
    env = _env("/srv/bench/a.git")
    assert env["GIT_BACKEND"] == "local"


@pytest.mark.unit
def test_github_mode_sets_the_backend_flag():
    env = _env("https://github.com/o/r", gh_token="ghp_x")
    assert env["GIT_BACKEND"] == "github"


@pytest.mark.unit
def test_local_mode_rewrites_repo_url_to_the_container_mount_path():
    env = _env("/srv/bench/a.git")
    assert env["REPO_URL"] == LOCAL_REPO_MOUNT


@pytest.mark.unit
def test_local_mode_supplies_a_placeholder_gh_token():
    """The entrypoint hard-requires GH_TOKEN; local mode must not trip it."""
    env = _env("/srv/bench/a.git")
    assert env["GH_TOKEN"]


@pytest.mark.unit
def test_github_mode_repo_url_is_untouched():
    env = _env("https://github.com/o/r", gh_token="ghp_x")
    assert env["REPO_URL"] == "https://github.com/o/r"


@pytest.mark.unit
def test_local_repo_volume_is_read_write():
    from orchestrator.core.agent_manager import local_repo_volume

    volumes = local_repo_volume("/srv/bench/a.git")
    assert volumes["/srv/bench/a.git"]["bind"] == LOCAL_REPO_MOUNT
    assert volumes["/srv/bench/a.git"]["mode"] == "rw"


@pytest.mark.unit
def test_a_github_url_produces_no_volume():
    from orchestrator.core.agent_manager import local_repo_volume

    assert local_repo_volume("https://github.com/o/r") == {}
```

Read `src/orchestrator/core/agent_manager.py` `build_spawn_env` first and match
the test's keyword names to its real signature.

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_agent_manager_local_repo.py -v`
Expected: FAIL with `ImportError: cannot import name 'LOCAL_REPO_MOUNT'`.

- [ ] **Step 3: Implement the mount**

In `src/orchestrator/core/agent_manager.py`, add near the top:

```python
# Where a local bare repo is bind-mounted inside the agent container. Fixed so
# the entrypoint can clone from a stable path regardless of the host layout.
LOCAL_REPO_MOUNT = "/srv/praxis-repo.git"

# The entrypoint hard-requires GH_TOKEN (`: "${GH_TOKEN:?...}"`). Local mode has
# no credential, so a placeholder satisfies the guard; the entrypoint skips
# every credential-helper and gh call when GIT_BACKEND=local.
_LOCAL_GH_TOKEN_PLACEHOLDER = "local-mode-no-token"  # nosec B105 - not a secret


def local_repo_volume(repo_url: str) -> dict[str, dict[str, str]]:
    """Return the Docker volume mapping for a local bare repo, or {} for remote."""
    from orchestrator.core.git_backend import is_local_repo_url, local_repo_path

    if not is_local_repo_url(repo_url):
        return {}
    return {local_repo_path(repo_url): {"bind": LOCAL_REPO_MOUNT, "mode": "rw"}}
```

In `build_spawn_env`, at the point where `REPO_URL` and `GH_TOKEN` are set,
replace them with:

```python
    from orchestrator.core.git_backend import is_local_repo_url

    local_mode = is_local_repo_url(repo_url)
    environment["GIT_BACKEND"] = "local" if local_mode else "github"
    environment["REPO_URL"] = LOCAL_REPO_MOUNT if local_mode else repo_url
    environment["GH_TOKEN"] = (
        _LOCAL_GH_TOKEN_PLACEHOLDER if local_mode else (gh_token or "")
    )
```

In `spawn_agent`, merge the local volume into the `volumes` dict built for the
agy credentials and the OpenCode session store:

```python
        volumes.update(local_repo_volume(repo_url))
```

and skip the `token_for_repo` call entirely in local mode:

```python
        from orchestrator.core.git_backend import is_local_repo_url

        gh_token = (
            "" if is_local_repo_url(repo_url)
            else await self._provider.token_for_repo(repo_url)
        )
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/test_agent_manager_local_repo.py tests/test_agent_manager.py -v`
Expected: PASS.

- [ ] **Step 5: Mutation-check the credential bypass**

Temporarily remove the `is_local_repo_url` guard around `token_for_repo` and add
a test asserting `spawn_agent` with a local repo URL never awaits
`self._provider.token_for_repo`. Confirm it fails with the mutation and passes
without it.

- [ ] **Step 6: Commit**

```bash
git add src/orchestrator/core/agent_manager.py tests/test_agent_manager_local_repo.py
git commit -m "feat(agent-manager): bind-mount a local bare repo and skip GitHub creds

REPO_URL is rewritten to a fixed container path, GIT_BACKEND tells the
entrypoint which plumbing to use, and no credential provider is consulted."
```

---

### Task 5: Teach both entrypoints local mode

**Files:**
- Modify: `docker/opencode-agent/entrypoint.sh`
- Modify: `docker/agy-agent/entrypoint.sh`
- Test: `tests/test_entrypoint_local_backend.py`

**Depends on:** Task 4

- [ ] **Step 1: Write the failing test**

Create `tests/test_entrypoint_local_backend.py`:

```python
"""The entrypoints must skip gh and credential setup when GIT_BACKEND=local.

These are static assertions over the shell source: an entrypoint change needs
an agent IMAGE REBUILD, so a source-level test is the cheap early signal.
"""

from pathlib import Path

import pytest


ENTRYPOINTS = [
    Path(__file__).resolve().parents[1] / "docker" / h / "entrypoint.sh"
    for h in ("opencode-agent", "agy-agent")
]


@pytest.mark.unit
@pytest.mark.parametrize("path", ENTRYPOINTS, ids=lambda p: p.parent.name)
def test_entrypoint_reads_git_backend(path):
    assert "GIT_BACKEND" in path.read_text(encoding="utf-8")


@pytest.mark.unit
@pytest.mark.parametrize("path", ENTRYPOINTS, ids=lambda p: p.parent.name)
def test_credential_helper_is_guarded_by_the_backend(path):
    text = path.read_text(encoding="utf-8")
    helper_line = next(
        (i for i, line in enumerate(text.splitlines()) if "credential.helper" in line),
        None,
    )
    assert helper_line is not None
    window = "\n".join(text.splitlines()[max(helper_line - 6, 0) : helper_line])
    assert "GIT_BACKEND" in window, (
        "the credential helper must be inside a github-only guard"
    )


@pytest.mark.unit
@pytest.mark.parametrize("path", ENTRYPOINTS, ids=lambda p: p.parent.name)
def test_pr_creation_is_guarded_by_the_backend(path):
    text = path.read_text(encoding="utf-8")
    pr_line = next(
        (i for i, line in enumerate(text.splitlines()) if "gh pr create" in line),
        None,
    )
    assert pr_line is not None
    window = "\n".join(text.splitlines()[max(pr_line - 25, 0) : pr_line])
    assert "GIT_BACKEND" in window, (
        "gh pr create must be inside a github-only guard"
    )


@pytest.mark.unit
@pytest.mark.parametrize("path", ENTRYPOINTS, ids=lambda p: p.parent.name)
def test_local_mode_reports_a_praxis_local_pr_url(path):
    assert "praxis-local://pr" in path.read_text(encoding="utf-8")


@pytest.mark.unit
@pytest.mark.parametrize("path", ENTRYPOINTS, ids=lambda p: p.parent.name)
def test_entrypoint_is_valid_shell(path):
    import subprocess

    result = subprocess.run(
        ["bash", "-n", str(path)], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_entrypoint_local_backend.py -v`
Expected: FAIL. `GIT_BACKEND` appears in neither entrypoint.

- [ ] **Step 3: Edit `docker/opencode-agent/entrypoint.sh`**

Three edits, all guarded so GitHub behavior is byte-identical.

First, near the required-variable block at the top, add after the `GH_TOKEN`
line:

```sh
# Which git plumbing to use: "github" (PRs via gh) or "local" (a bind-mounted
# bare repo, no credential, no PR object). Defaults to github so an older
# orchestrator that does not set it behaves exactly as before.
GIT_BACKEND="${GIT_BACKEND:-github}"
```

Second, wrap the credential-helper line (currently line 115) in a guard:

```sh
if [ "${GIT_BACKEND}" = "github" ]; then
  git config --global credential.helper '!f() { echo "username=x-access-token"; echo "password=${GH_TOKEN}"; }; f'
fi
```

Third, replace the `gh pr create` block. Find the block that assigns `PR_URL`
and wrap it:

```sh
if [ "${GIT_BACKEND}" = "local" ]; then
  # No PR objects exist in local mode; the orchestrator reviews the branch
  # against its base directly. Report the same (branch, base) pair it will
  # parse back out of tasks.pr_url.
  PR_URL="praxis-local://pr?branch=$(url_encode "${BRANCH}")&base=$(url_encode "${BASE_BRANCH}")"
  echo "Local backend: reporting ${PR_URL}"
else
  PR_URL=$(gh pr create \
    ... existing arguments unchanged ...
  )
fi
```

Add a small encoder helper near the top of the file (before first use):

```sh
# Percent-encode a branch name so a slash survives the praxis-local:// query
# string. Only the characters that actually appear in branch names are handled.
url_encode() {
  printf '%s' "$1" | sed -e 's|/|%2F|g' -e 's| |%20|g'
}
```

- [ ] **Step 4: Apply the identical three edits to `docker/agy-agent/entrypoint.sh`**

The agy entrypoint has the same required-variable block, the same
credential-helper line, and the same `gh pr create` block. Make the same three
edits with the same guard names. Do not change the agy `--output-format json`
invocation or the `Status:` grep.

- [ ] **Step 5: EXECUTE both scripts' guards, do not just syntax-check them**

`bash -n` catches syntax only. A prior shipped bug (a printf leading-dash issue)
passed `bash -n` and failed at runtime. Exercise the new code paths for real:

```bash
bash -c 'url_encode() { printf "%s" "$1" | sed -e "s|/|%2F|g" -e "s| |%20|g"; }; url_encode "agent/my-leaf"'
```

Expected output: `agent%2Fmy-leaf`

Then dry-run the guard with both backend values:

```bash
GIT_BACKEND=local bash -c 'GIT_BACKEND="${GIT_BACKEND:-github}"; if [ "${GIT_BACKEND}" = "github" ]; then echo CRED-SETUP; else echo SKIP-CRED; fi'
GIT_BACKEND=github bash -c 'GIT_BACKEND="${GIT_BACKEND:-github}"; if [ "${GIT_BACKEND}" = "github" ]; then echo CRED-SETUP; else echo SKIP-CRED; fi'
bash -c 'GIT_BACKEND="${GIT_BACKEND:-github}"; if [ "${GIT_BACKEND}" = "github" ]; then echo CRED-SETUP; else echo SKIP-CRED; fi'
```

Expected: `SKIP-CRED`, `CRED-SETUP`, `CRED-SETUP` (the unset case must default
to github).

- [ ] **Step 6: Run the test to verify it passes**

Run: `uv run pytest tests/test_entrypoint_local_backend.py -v`
Expected: PASS (10 parametrized tests).

- [ ] **Step 7: Rebuild both agent images**

An entrypoint change needs an image rebuild or a stale image silently runs the
old logic:

```bash
docker build -t opencode-agent:latest -f docker/opencode-agent/Dockerfile docker/opencode-agent/
docker build -t agy-agent:latest -f docker/agy-agent/Dockerfile docker/agy-agent/
```

Verify the baked file, using `docker cp` (never `docker run --entrypoint cat`,
which resolves the attestation manifest on buildkit images and returns nothing):

```bash
cid=$(docker create opencode-agent:latest)
docker cp "$cid":/usr/local/bin/entrypoint.sh /tmp/baked-entrypoint.sh
docker rm "$cid"
grep -c GIT_BACKEND /tmp/baked-entrypoint.sh
```

Expected: a count of at least 3. If it is 0, the build did not pick up the edit.
Adjust the container path to wherever the Dockerfile actually places the
entrypoint; read the Dockerfile first.

- [ ] **Step 8: Commit**

```bash
git add docker/opencode-agent/entrypoint.sh docker/agy-agent/entrypoint.sh tests/test_entrypoint_local_backend.py
git commit -m "feat(entrypoints): support GIT_BACKEND=local in both harnesses

Credential-helper setup and gh pr create are now inside a github-only
guard; local mode reports a praxis-local:// (branch, base) ref instead.
Unset GIT_BACKEND defaults to github, so behavior is unchanged.
Requires an agent IMAGE REBUILD for both harnesses."
```

---

### Task 6: End-to-end local-mode smoke test and docs

**Files:**
- Create: `tests/test_local_mode_e2e.py`
- Modify: `docs/gotchas.md`
- Modify: `CLAUDE.md`

**Depends on:** Task 2, Task 3, Task 4, Task 5

- [ ] **Step 1: Write the end-to-end test**

Create `tests/test_local_mode_e2e.py`:

```python
"""One real bare repo through the whole review-and-merge path.

No containers: the worker's output is simulated by pushing a branch, which is
exactly what the entrypoint does. Everything after that is the real loop.
"""

import subprocess

import pytest

from orchestrator.core.git_backend import PullRequestRef, resolve_backend
from orchestrator.core.preflight import preflight_remote


def _git(*args, cwd):
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def seeded(tmp_path):
    work, bare = tmp_path / "w", tmp_path / "r.git"
    work.mkdir()
    _git("init", "-b", "main", cwd=work)
    _git("config", "user.email", "t@e.com", cwd=work)
    _git("config", "user.name", "t", cwd=work)
    (work / "app.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    _git("add", ".", cwd=work)
    _git("commit", "-m", "base", cwd=work)
    _git("clone", "--bare", str(work), str(bare), cwd=tmp_path)
    _git("remote", "add", "origin", str(bare), cwd=work)
    _git("push", "origin", "main", cwd=work)
    # Simulate the worker: a branch with a fix.
    _git("checkout", "-b", "agent/fix", cwd=work)
    (work / "app.py").write_text("def f():\n    return 2\n", encoding="utf-8")
    _git("commit", "-am", "fix the return", cwd=work)
    _git("push", "origin", "agent/fix", cwd=work)
    return bare


@pytest.mark.integration
async def test_the_full_local_path_preflight_diff_merge(seeded):
    repo_url = str(seeded)

    # 1. Preflight passes with no credential at all.
    assert await preflight_remote(
        git=None,
        repo_url=repo_url,
        base="main",
        branch="agent/fix",
        credential_configured=False,
    ) == []

    # 2. The backend resolves to local and produces a reviewable diff.
    backend = resolve_backend(repo_url, git_ops=None)
    assert backend.name == "local"
    ref = PullRequestRef(backend="local", branch="agent/fix", base="main")
    diff = await backend.get_diff(ref)
    assert "return 2" in diff
    assert "return 1" in diff

    # 3. Merge lands the change on main and removes the branch.
    await backend.merge(ref)
    assert "agent/fix" not in _git("branch", "--list", cwd=seeded)
    blob = _git("show", "main:app.py", cwd=seeded)
    assert "return 2" in blob


@pytest.mark.integration
async def test_a_pr_url_survives_a_round_trip_through_storage(seeded):
    ref = PullRequestRef(backend="local", branch="agent/fix", base="main")
    stored = ref.to_url()
    assert PullRequestRef.from_url(stored) == ref
    backend = resolve_backend(str(seeded), git_ops=None)
    assert "return 2" in await backend.get_diff(PullRequestRef.from_url(stored))
```

Run: `uv run pytest tests/test_local_mode_e2e.py -v`
Expected: PASS (2 tests).

- [ ] **Step 2: Add the gotchas**

Append to `docs/gotchas.md`:

```markdown
- **Local git mode is a backend, not a special case** 
  `core/git_backend.resolve_backend` picks `LocalGitBackend` when a project's
  `repo_url` is a filesystem path or a `file://` URL, and `GitHubBackend`
  otherwise. Everything above the seam is shared and unchanged: the merge gate
  still parks a PASS at `PASSED` unless `auto_merge` is set, the verify gates
  still run, outcomes are still recorded. Only the plumbing differs. A local
  "PR" is a `praxis-local://pr?branch=...&base=...` string stored in the
  existing `tasks.pr_url` column, so there is no schema change; parse it with
  `PullRequestRef.from_url`, never with string slicing.
- **The local repo MUST be bare**: workers push to it, and git refuses a push
  to a checked-out branch. `core/preflight._preflight_local` enforces this with
  a `rev-parse --is-bare-repository` check and returns 422 (`NOT_A_REPO`) so
  the failure surfaces before a container spawns rather than deep inside the
  worker. Local mode also needs NO GitHub credential: preflight skips every
  remote call and `AgentManager` never consults the credential provider.
- **Local mode bind-mounts the bare repo read-write at a fixed container path** 
  `agent_manager.LOCAL_REPO_MOUNT` (`/srv/praxis-repo.git`). `REPO_URL` inside
  the container is rewritten to that path, and `GIT_BACKEND=local` tells both
  entrypoints to skip credential-helper setup and `gh pr create`. `GH_TOKEN`
  still gets a placeholder because the entrypoints hard-require it
  (`: "${GH_TOKEN:?...}"`). An unset `GIT_BACKEND` defaults to `github`, so an
  older orchestrator against a new image behaves exactly as before. Changing
  either entrypoint needs an agent IMAGE REBUILD.
```

- [ ] **Step 3: Add three CLAUDE.md index lines**

Add one-line entries mirroring the three gotchas, in the existing terse style.

- [ ] **Step 4: Verify the gate**

```bash
uv run ruff format src/ tests/
uv run ruff check --fix src/ tests/
uv run mypy src/ --ignore-missing-imports
uv run pytest --cov=orchestrator --cov-fail-under=80 -q
```

Expected: all clean.

- [ ] **Step 5: Commit**

```bash
git add tests/test_local_mode_e2e.py docs/gotchas.md CLAUDE.md
git commit -m "test(local-mode): end-to-end preflight, diff, and merge on a real bare repo

Plus the three local-mode gotchas."
```

### Phase A execution record (2026-08-06)

Executed 2026-08-06, tasks 1 to 6 complete, all committed on local `main` and NOT
pushed. Gate at the end: ruff format and check clean, mypy clean on 80 files, 1464
tests passing at 90.79 percent coverage.

Task commits in order: `5ff0bdb`, `9879b9a`, `290fc99`, `698775e`, `07b620d`,
`2651c12`. Commit order does not match task order: tasks 2, 3, and 4 were dispatched
concurrently per the Parallel Execution Map, so 3 and 4 landed before 2. Three
further commits came out of review findings: `4a882cd`, `5b5b085`, `95181c8`.
Seven more cleared the deferred backlog (see below).

**Concurrency.** Four parallel dispatches ran: tasks 2+3+4, then 2+5, then task 6
plus the backlog, then three fix agents. Zero file overlap held and the full suite
was run at every join point. One hazard surfaced that earlier phases had not: **the
session scratchpad is SHARED across concurrently running agents.** A mutation
harness written to `scratchpad/mutate.py` was overwritten by another agent mid-run,
silently turning that mutation into a no-op and producing a false green. It was
caught only because the agent sha-verified the file before and after. Any agent
running mutations in parallel must use an isolated scratchpad subdirectory and prove
the mutation actually changed the file.

**Defects in this plan's own verbatim code, corrected during execution.** Fix them
here before anyone re-runs these tasks.

1. **Task 5's `url_encode` is wrong and would have shipped a silent data loss.** The
   plan's encoder is `sed -e 's|/|%2F|g' -e 's| |%20|g'`. It encodes neither `&` nor
   `%`. A branch named `feat/a&b` yields
   `praxis-local://pr?branch=feat%2Fa&b&base=main`, which `PullRequestRef.from_url`'s
   `([^&]+)` capture cannot parse, and the failure is silent: the worker still
   reports `status=completed`, the orchestrator stores an unparseable `pr_url`, and
   the reviewable change vanishes with no error anywhere. Shipped as
   `-e 's|%|%25|g' -e 's|/|%2F|g' -e 's| |%20|g' -e 's|&|%26|g'`, with `%` FIRST or
   the escapes get re-escaped.
2. **Task 5's Step 7 verification path is wrong.** It reads
   `/usr/local/bin/entrypoint.sh`. Both Dockerfiles do
   `COPY entrypoint.sh /home/agent/entrypoint.sh`, so the plan's command returns
   nothing, which reads as a failed build.
3. **Task 5 guards only `gh pr create`.** A `gh pr view` reuse call sits above it and
   is equally a `gh` call, so it would fail in local mode with no credential. The
   guard must wrap the whole block.
4. **Task 2's tests use an `orchestrator_fixture` that does not exist** anywhere in
   the repo, and the plan never defines it. Written from the `_make_reviewing_orch`
   pattern in `tests/test_fixes35.py`.
5. **Task 2's Step 6 mutation check is vacuous as written.** Every local test assigns
   `orch._resolve_backend = lambda ...`, so mutating the real `_resolve_backend`
   cannot fail them, and its `-k local` selector is wrong besides. Only a new test
   calling the un-stubbed resolver catches it.
6. **Task 3's `test_a_non_bare_directory_is_422` is vacuous as written.**
   `plain.mkdir()` with no `git init` exercises the "not a git repository" branch and
   never reaches the bare check, so the plan's own Step 5 mutation left it green.
   Fixed by making the fixture a real non-bare repo.
7. **Task 1's module imports `pathlib.Path` and never uses it** (ruff F401), and its
   two value-returning `GitHubBackend` methods fail mypy under this repo's
   `warn_return_any = true` without a `cast`.
8. Both Task 3 and Task 4 put their `git_backend` imports inside function bodies for
   no reason. `git_backend` imports nothing from `orchestrator`, so there is no
   circular-import risk; hoisted.
9. **Task 5's `test_entrypoint_is_valid_shell` is a permanent false failure on
   Windows.** `subprocess.run(["bash", ...])` finds the WSL launcher before Git Bash
   and fails regardless of the script. CI has a `windows-latest` leg. Resolved with
   `shutil.which` and an absolute path.
10. The plan's test-harness table claims the `slow` marker is registered in
    `pyproject.toml`. Only `unit` and `integration` are.

**Vacuous tests exposed by mutation, beyond the two plan defects above.** Task 1's
`GitHubBackend` tests survived arbitrary argument corruption: dropping `repo=` from
`merge_pr`, rewriting `comment_on_pr` to `("/nonexistent", 999, "WRONG")`, and
swapping `clone_pr_head`'s two arguments all left the suite green, and `checkout` had
no test at all. Task 1's percent-encoding and its merge-base logic could each be
deleted outright with every test still passing. Task 2's routing tests used bare
`assert_awaited_once()` throughout, so rewriting the merge target to `main` was
invisible. Task 5's guard tests are substring greps over a line window: inverting
either guard, closing the guard early so every `gh` call runs unconditionally, and
moving the `GIT_BACKEND` default below its first use ALL left the suite green, and
`test_local_mode_reports_a_praxis_local_pr_url` could not fail at all, because the
string it greps for also appears in a comment.

**Design defects found by review, not present in the plan text.**

- `LocalGitBackend.merge` was non-atomic and self-poisoning. The base push and the
  branch delete are separate operations, so a failed delete left base already
  advanced, propagated out of `approve_task_merge` so the task was never MERGED, and
  every retry then hit `git commit` with nothing to commit, forever, with a BLANK
  message because `_run_checked` interpolated only stderr while git writes "nothing
  to commit" and "CONFLICT" to stdout.
- `shutil.rmtree(..., ignore_errors=True)` over a git clone leaks on Windows, one
  full clone per merge, measured. Over a 100-plus instance bench run that is 100-plus
  silently leaked clones.
- The fail-closed `--repo` guard keyed on `ref.backend`, so a `local` ref handed to
  `GitHubBackend` sailed past it and reached `gh` with `--repo` omitted, which is the
  exact wrong-repo gotcha the guard exists to prevent. `checkout` never consulted the
  guard on any path. Now keyed on `ref.repo is None` alone.
- Routing an unparseable `pr_url` to a silent `return`, as the plan specifies, WEDGES
  the plan forever: the loop re-enters `review_task` every tick, REVIEWING counts as
  active so the plan never completes, and `plan_stalled` requires `not active` so it
  never fires either. A regression, since the pre-routing code raised. Now routed
  through the existing fail-and-retry path.
- The `ref.repo is None` backfill block the plan mandates is provably dead, and
  reachable it would substitute the PROJECT's slug for the PR's, silently targeting
  the wrong repo for a fork PR. Deleted.
- `is_local_repo_url` misrouted UNC (`\\server\share`) and `~` paths to GitHub.
- `to_url()` treated any `backend` string except exactly `"github"` as local, so a
  capitalization typo silently discarded `repo` and `number`.
- `from_url(None)` raised `AttributeError` against a docstring promising `ValueError`,
  and `tasks.pr_url` is nullable.
- The two entrypoint guards tested opposite sides of DIFFERENT values (`= "github"`
  for the credential helper, `= "local"` for the PR block), so a third value got no
  credential helper AND the full `gh` path, the worst combination. Both harnesses now
  derive one `IS_LOCAL_BACKEND` boolean once, mirroring the file's own
  `REUSING_BRANCH` precedent, and the guards are tested by EXECUTING the real sliced
  regions against `gh` and `git` spies rather than by grepping near them.

**Deferred backlog cleared here** (product plan Phase A items 1, 2, 3, and 6), in
commits `effe842`, `706c0e5`, `bab45c0`, `c080703`, `b8f46ad`, `d140685`, `2737254`.

- **Item 1** (compose does not forward the worker preset): confirmed exactly. Fixed
  with the BARE pass-through form (`- DEFAULT_WORKER_HARNESS`), NOT `${VAR:-default}`
  and NOT `- VAR=${VAR}`. Verified independently against real `docker compose config`:
  bare resolves from the project `.env` (where `init` writes), preserves spaces in the
  model name, and yields `null` when unset so the mounted YAML stays authoritative.
  `- VAR=${VAR}` sets it to an EMPTY string when unset, and `Settings.__init__` drops a
  YAML key whenever the name is in `os.environ`, so both expansion forms silently
  suppress the YAML. `_print_next_steps` told the operator the container reads worker
  defaults "not from .env", which the fix made false; corrected, and its decline path
  no longer claims a preset it did not write.
- **Item 6** (`init` writes a secret-bearing `.env` outside the repo root): confirmed,
  with one correction to the original note. It does NOT report success, `_compose`
  eventually fails. But the `.env` holding a live `AUTH_TOKEN` is already on disk by
  then and no message names the directory or the secret, so the security-relevant half
  stands. The guard now also accepts a renamed fork that still ships the `praxis`
  console script, since locking a fork out permanently while telling it to `cd` where
  it already is was the worse failure.
- **Item 2** (`init()` has 0 percent coverage): confirmed. Now 91 percent.
- **Item 3** (`load_yaml_settings` silent on a missing path): confirmed, and the
  hot-path concern is real, `EffectiveSettings._get_yaml` has no memoization at all.
  Warns once per distinct absolute path. Its docstring claimed to cover a dropped
  container mount; it does not, because the Dockerfile bakes `config/` so a dropped
  mount leaves a stale file PRESENT. That case belongs to the doctor's `config_mount`
  probe, and the docstring now says so.

**Still open, raised at the phase gate rather than fixed.**

1. **The merge gate is evaluated against a different branch than the merge acts on.**
   `auto_merge_eligible(project, plan_branch_name)` decides, but the merge targets
   `ref.base`. In auto-delegate single-branch mode `dispatch_pending_tasks` sets
   `branch = plan_branch_name` and `base_branch = project default branch`, so the two
   differ and the protected-branch carve-out never sees `main`. Review demonstrated an
   auto-merge into `main` past an "eligible" verdict. PRE-EXISTING, not introduced
   here: the old `gh pr merge N` merged into the PR's own base too. Fixing it changes
   auto-merge semantics repo-wide, so it needs its own decision.
2. **`LM_STUDIO_URL` still uses the `${VAR:-default}` form** that item 1's fix
   condemns, in both compose files, and it is a `MANAGED_KEYS` entry. Picking the
   `gemini-agy` preset makes `merge_env` deliberately REMOVE the `.env` line and
   compose silently re-supplies it. Benign today because agy ignores the endpoint, but
   the invariant is broken. Not fixed because that compose default is the only source
   of `host.docker.internal` for containerized deployments; changing it needs a real
   decision about the field default. Six other `Settings` fields have the same shape.
3. **An empty value suppresses the YAML.** `DEFAULT_WORKER_HARNESS=` in `.env` reaches
   the container as set-but-empty, and `Settings.__init__` tests membership in
   `os.environ` rather than truthiness, so the YAML key is dropped and `agent_manager`
   substitutes `opencode`. Only reachable by hand-edit, but a hand-edit is the natural
   way to "unset the preset and go back to the YAML" and it does the opposite.
4. **Three other git operations in `orchestrator_review.py` still assume GitHub for a
   local project.** `_verify_plan_branch` returns `"skipped"` without a credential, so
   the whole-plan verify backstop silently no-ops for every local project.
   `compare_url` emits `https://github.com//srv/bench/astropy/compare/...` into the
   `plan_integration_ready` event. `_sync_plan_checkbox` runs `clone_with_token` and
   `commit_and_push` against the local bare repo. The first matters most for Phase B,
   since the bench measures exactly that gate.
5. **The dashboard cannot render a local PR.** `web/app.js` builds the "View PR" link
   without the `safeHref` guard it uses elsewhere, so a `praxis-local://` task shows a
   dead link. The human merge gate in local mode is approve-blind: there is no
   inspectable artifact behind the ref.
6. Routing dropped the two-source `repo_slug` fallback, so three URL shapes that used
   to work now raise: `http://`, `www.github.com`, and a `.git` suffix in the path.
   Low likelihood, since both entrypoints emit the canonical form.
7. `_compose` and `_wait_for_health` stay stubbed in the init tests by choice; a real
   `docker compose` in a unit test is worse. Their internals are the uncovered 9
   percent of `cli/init.py`.

**Not verified: no local-mode task has ever run end to end through a real container.**
Everything above is proven by unit and integration tests, by executing the real sliced
shell against spies, and by executing the baked entrypoints inside both rebuilt
images. The full loop (orchestrator spawns a container against a bind-mounted bare
repo, worker pushes, review reads the diff, merge lands) has not been run. Phase B
Task 8 is the first thing that will exercise it.

**Phase A is complete.** Per the cross-plan execution order, the next work is
the engine plan's Phase B and Phase C, before returning here for Phase B.

---

## Phase B: the bench harness and the 30-task pilot

The pilot exists to debug the harness, NOT to conclude anything. Its exit
criteria are in Task 12.

### Task 7: The bench package skeleton and its exclusions

**Files:**
- Create: `bench/__init__.py`, `bench/README.md`, `bench/config.py`
- Modify: `pyproject.toml`
- Modify: `docker/orchestrator/Dockerfile`
- Modify: `.dockerignore` (create if absent)
- Test: `tests/bench/__init__.py`, `tests/bench/test_config.py`

**Depends on:** Task 6

- [ ] **Step 1: Write the failing test**

Create `tests/bench/__init__.py` (empty) and `tests/bench/test_config.py`:

```python
"""Bench config is data, and the strata must partition the space exactly once."""

import pytest

from bench.config import (
    CONDITIONS,
    PATCH_SIZE_STRATA,
    REPO_SIZE_STRATA,
    WORKERS,
    stratum_for,
)


@pytest.mark.unit
def test_the_four_conditions_are_declared():
    assert [c.key for c in CONDITIONS] == ["A", "B", "C", "D"]


@pytest.mark.unit
def test_condition_a_and_c_both_run_without_a_verify_gate():
    """A and C must be a matched pair or the ablation is confounded."""
    by_key = {c.key: c for c in CONDITIONS}
    assert by_key["A"].verify_gate is False
    assert by_key["C"].verify_gate is False
    assert by_key["B"].verify_gate is True
    assert by_key["D"].verify_gate is True


@pytest.mark.unit
def test_only_condition_a_is_monolithic():
    by_key = {c.key: c for c in CONDITIONS}
    assert by_key["A"].decompose is False
    assert all(by_key[k].decompose for k in ("B", "C", "D"))


@pytest.mark.unit
def test_only_condition_d_enables_adaptive_split():
    by_key = {c.key: c for c in CONDITIONS}
    assert by_key["D"].adaptive_split is True
    assert not any(by_key[k].adaptive_split for k in ("A", "B", "C"))


@pytest.mark.unit
@pytest.mark.parametrize(
    "files,loc,expected",
    [
        (1, 3, "small"),
        (1, 4, "small"),
        (1, 5, "medium"),
        (2, 10, "medium"),
        (2, 100, "medium"),
        (2, 101, "large"),
        (3, 5, "large"),
        (7, 400, "large"),
    ],
)
def test_patch_size_strata_partition_the_space(files, loc, expected):
    assert stratum_for(files, loc, repo_files=50)[0] == expected


@pytest.mark.unit
@pytest.mark.parametrize(
    "repo_files,expected",
    [(1, "tiny"), (99, "tiny"), (100, "mid"), (500, "big"), (5000, "big")],
)
def test_repo_size_strata_partition_the_space(repo_files, expected):
    assert stratum_for(1, 3, repo_files=repo_files)[1] == expected


@pytest.mark.unit
def test_every_stratum_name_is_declared():
    names = {s for s, _ in [stratum_for(f, l, 50) for f, l in [(1, 3), (2, 50), (5, 200)]]}
    assert names <= set(PATCH_SIZE_STRATA)


@pytest.mark.unit
def test_two_workers_are_declared_for_a_comparative_claim():
    assert len(WORKERS) == 2
    assert {w.harness for w in WORKERS} == {"opencode", "agy"}
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/bench -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'bench'`.

- [ ] **Step 3: Write the config**

Create `bench/__init__.py` (empty), then `bench/config.py`:

```python
"""Static configuration for the Praxis decomposition benchmark.

Dev-only: this package is excluded from the orchestrator image and from the
coverage gate.  Its numbers are the experiment's design, so they live in code
and get committed, not in a shell script someone retypes.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


# Where prepared bare repos and run artifacts live. Overridable per machine via
# the PRAXIS_BENCH_ROOT environment variable, read in bench/prepare.py.
DEFAULT_BENCH_ROOT = Path("bench/.work")

# Gold-patch size buckets, per SWE-bench Goes Live! (arXiv 2505.23419).
PATCH_SIZE_STRATA: tuple[str, ...] = ("small", "medium", "large")

# Repository size buckets, by tracked file count.
REPO_SIZE_STRATA: tuple[str, ...] = ("tiny", "mid", "big")


@dataclass(frozen=True)
class Condition:
    """One arm of the within-subject design.

    Same tasks, same worker, same brain across arms; only these three switches
    move.  ``A`` and ``C`` are a MATCHED PAIR: both run without a verify gate,
    so the A-versus-B and B-versus-C comparisons isolate different things
    without confounding each other.
    """

    key: str
    label: str
    decompose: bool
    verify_gate: bool
    adaptive_split: bool


CONDITIONS: tuple[Condition, ...] = (
    Condition("A", "monolithic baseline", False, False, False),
    Condition("B", "praxis decomposition", True, True, False),
    Condition("C", "decomposition, no verify gate", True, False, False),
    Condition("D", "decomposition plus adaptive split", True, True, True),
)


@dataclass(frozen=True)
class Worker:
    """One implementer configuration."""

    key: str
    harness: str
    model: str


# Two workers, so the capability claim is comparative rather than anecdotal:
# the reference local open-weight model and a cheap hosted mid-tier.
WORKERS: tuple[Worker, ...] = (
    Worker("local-openweight", "opencode", "qwen3.6-27b"),
    Worker("hosted-flash", "agy", "Gemini 3.6 Flash (High)"),
)

# Runs with temperature above zero get two seeds; both are reported.
SEEDS: tuple[int, ...] = (1, 2)

# Fixed sample seed, published so the draw is reproducible.
SAMPLE_SEED = 20260806

# Per-stratum sample sizes.
PILOT_PER_STRATUM = 4      # 30-task Lite pilot, conditions A and B, one worker
FULL_PER_STRATUM = 16      # about 144 tasks across the 9 strata cells


def stratum_for(files: int, loc: int, repo_files: int) -> tuple[str, str]:
    """Return ``(patch_size_stratum, repo_size_stratum)`` for one instance.

    Boundaries follow arXiv 2505.23419: a single-file patch under 5 lines
    resolves about 48 percent of the time; 3 or more files or 100 or more LOC
    drops under 10 percent.  The buckets are chosen to straddle those cliffs so
    the expected effect concentrates in the middle cell.
    """
    if files >= 3 or loc > 100:
        size = "large"
    elif files == 1 and loc < 5:
        size = "small"
    else:
        size = "medium"

    if repo_files < 100:
        repo = "tiny"
    elif repo_files < 500:
        repo = "mid"
    else:
        repo = "big"
    return size, repo
```

- [ ] **Step 4: Write the bench README**

Create `bench/README.md`:

````markdown
# praxis-bench

A reproducible, stratified SWE-bench evaluation of Praxis's capability-aware
decomposition, with an ablation that isolates decomposition from verification.

This package is **development-only**. It is excluded from the orchestrator
Docker image and from the 80 percent coverage gate. It runs on an operator's
machine because it needs a GPU or a subscription CLI, neither of which exists on
a CI runner.

## What it answers

Does decomposing a task to fit the implementing model's capability actually raise
the resolve rate, and if so, is it the decomposition or the per-leaf verification
doing the work?

## Design

Within-subject: the same instances, the same worker, and the same brain across
four conditions.

| Condition | What runs | Isolates |
|-----------|-----------|----------|
| A | monolithic: the whole issue as one task via `dispatch_task` | baseline |
| B | Praxis decomposition via `execute_plan` | decomposition |
| C | condition B with the verify gate disabled | is it decomposition or verification |
| D | condition B plus adaptive split-on-failure | the adaptive policy delta |

A and C are a **matched pair**: both run without a verify gate. The runner
asserts this before it starts; without it the A-versus-B comparison is
confounded by verification.

## Stratification

Pre-stratified on published per-instance metadata: gold-patch size
(1 file under 5 lines / 2 files or 5 to 100 lines / 3+ files or over 100 lines)
crossed with repo size (under 100 / 100 to 500 / 500+ tracked files). Fixed
sample per cell, published seed (`bench/config.SAMPLE_SEED`), and the drawn
instance list committed under `bench/samples/`.

## Grading

The OFFICIAL SWE-bench evaluation harness, run against the patch extracted from
the final branch (`git diff base...result`). Praxis never grades itself.

## Running it

```bash
# One-time: prepare instances as local bare repos at the buggy base commit
uv run python -m bench.prepare --sample bench/samples/lite-pilot-30.json

# Pilot: 30 Lite tasks, conditions A and B, one worker
uv run python -m bench.runner --sample bench/samples/lite-pilot-30.json \
    --conditions A,B --worker local-openweight

# Grade and report
uv run python -m bench.grade --run bench/.work/runs/<run-id>
uv run python -m bench.report --run bench/.work/runs/<run-id>
```

## Cost

The pilot measures cost per task so the full run can be budgeted before it is
started. Expect the pilot to take several hours of wall clock on one machine.

## Honesty

Every report carries, by template and not by choice:

- a **contamination note** naming the worker model's training cutoff and linking
  SWE-rebench as the decontaminated alternative;
- the **correlational-anchor caveat** on the stratum boundaries;
- a hand-inspected sample of 10 failures classified plan-shaped versus
  execution-shaped (arXiv 2603.14248: decomposition only fixes the former), and
  a statement of which class dominates.

A null or negative result is published unchanged. The engineering plus the rigor
is the artifact; the number is whatever it is.
````

- [ ] **Step 5: Exclude bench from packaging, the image, and coverage**

In `pyproject.toml`:

```toml
[tool.setuptools.packages.find]
where = ["src"]
```

already excludes `bench/` from the wheel because it lives outside `src/`. Add
the coverage exclusion under the pytest config:

```toml
[tool.coverage.run]
omit = ["bench/*", "tests/*"]
```

and register the bench test path so `tests/bench` is collected:

```toml
[tool.pytest.ini_options]
testpaths = ["tests"]
```

(already correct; `tests/bench` is inside it).

Create or extend `.dockerignore` at the repo root:

```
bench/
bench/.work/
docs/bench/
```

Confirm `docker/orchestrator/Dockerfile` copies only `src/`, `web/`,
`config/`, and `pyproject.toml`; if it uses a bare `COPY . .`, tighten it to the
explicit list rather than relying on `.dockerignore` alone.

- [ ] **Step 6: Run the test to verify it passes**

Run: `uv run pytest tests/bench -v`
Expected: PASS.

- [ ] **Step 7: Verify bench really is out of the image**

```bash
PRAXIS_BUILD_SHA=$(git rev-parse --short HEAD) docker compose build orchestrator
cid=$(docker create orchestrator-orchestrator:latest 2>/dev/null || docker create $(docker compose config --images | head -1))
docker cp "$cid":/app /tmp/app-contents >/dev/null 2>&1 && ls /tmp/app-contents
docker rm "$cid"
```

Expected: no `bench` directory in the listing. Adjust the image name to whatever
`docker compose config --images` reports.

- [ ] **Step 8: Commit**

```bash
git add bench/ tests/bench/ pyproject.toml .dockerignore
git commit -m "feat(bench): add the dev-only bench package skeleton and design config

Four conditions with A and C as a matched no-verify-gate pair, nine
stratum cells with literature-grounded boundaries, two workers, a
published sample seed. Excluded from the image and the coverage gate."
```

---

### Phase B execution record (2026-08-07)

**Status: Tasks 7 to 11 COMPLETE. Task 12 is human-gated AND BLOCKED; see
"What blocks Task 12" below. It was not attempted and must not be simulated.**

Committed on local `main`, NOT pushed. Gate at close: `ruff format --check` and
`ruff check` clean across `src/`, `tests/`, `bench/`; `mypy src/ bench/` clean;
**1766 tests passing**, 91 percent coverage.

Commits in order: `ff9f593` (Task 7), `6a701f1` (the prerequisite), `169f115`
(CI lint widening) from the prior session, then `3365395` (Task 8), `ce1bab8`
(Task 10), `2162910` (Task 9), `b97bdd2` (the strata finding), `ae9df26`
(Task 11), `8e442be` (sample writer LF fix), `c94e98e` (Task 11 test gaps),
`5898973` and `e48b8c8` (Task 8 hardening).

#### Defects in the plan's own code, every one proven by execution

**Task 8 (six proven before dispatch, all fixed).** `git checkout --orphan main`
dies because `clone --no-checkout` already made `main`. The module re-rooted
history while its own tests and its idempotency guard both required
`rev-parse main == base_commit`, so the guard could never be true and
preparation re-cloned every call. `.praxis-bench-base` contaminated the tree.
A default local bare clone HARDLINKS the object store, so the gold commit stayed
readable by sha; measured under mutation, `test_no_later_commit_is_reachable`
(`rev-list --count --all == 1`) PASSED while `git cat-file -e <gold>` returned 0.
Tags leak the answer and the plan's fixture had none. `shutil.rmtree` raises
`WinError 5` over packed git files, and Python here is 3.11 so the handler kwarg
is `onerror`.

Resolution: `refs/heads/main` is the TRUE base sha with real ancestry, every
other ref is swept, remotes are dropped, and the object store is pruned. An
independent check was added that the plan never used: the prepared tree sha at
`main` EQUALS upstream's tree sha at `base_commit`.

**Task 9.** The plan requires `repo_files` but never says where it comes from,
and it is not in the SWE-bench metadata. Resolved with
`gh api repos/{repo}/git/trees/{base_commit}?recursive=1`, per instance.
Reproducibility depended on a `sorted()` call the plan never tested: without it
the draw follows pool arrival order, so re-fetching in a different order yields
a different sample from the same seed. `Path.write_text` with no explicit
`newline` wrote the COMMITTED sample as CRLF; `main()` had no test at all.

**Task 10.** `path.open("a", encoding="utf-8")` writes CRLF JSONL on Windows into
a published artifact, and the plan's own `test_every_row_is_a_single_line_of_json`
uses `splitlines()`, which normalizes it away. Confirmed by contrast under
mutation: the raw-bytes test goes red while the plan's test stays green.

**Task 11 (four proven before dispatch, plus one found during).**
`write_predictions` derived each patch's base from `bare / "praxis-bench-base"`.
A bare repo has no working tree, so that file can never exist; the base fell back
to `"main"` and `git diff main...main` is 0 bytes. **Every prediction would have
carried an empty patch and every condition would have reported 0 percent.**
`_issue_prompt` read `instance['problem_statement']`, absent from the sample, so
every attempt would `KeyError` into a 100 percent error matrix.
`verify_cmd` resolved to `None` for every condition, which combined with the
known `skipped`-equals-`passed` hole would have made **condition B silently
become condition C**. `superseded` was missing from the terminal status set. And
the plan's `merged_repo` fixture calls its own `_git` helper without the
keyword-only `cwd`, so its Step 8 ("Expected: PASS (6 tests)") was never run.

#### Vacuous tests exposed by mutation

- Task 8's `test_no_later_commit_is_reachable` is blind to the dominant leak
  vector; it is kept deliberately and
  `test_the_gold_commit_object_is_gone_from_the_object_store` is what catches it.
- Task 11's two `extract_patch` tests both PASS under the exact mutation that
  empties every prediction, because they pass `base` explicitly and never
  exercise the derivation. Re-run by the orchestrator against the committed file.
- Both of `bench/runner.py`'s timeout clauses could be replaced with
  `error = None` with **all 30 tests still passing**, despite the module's own
  docstring promising that nothing is dropped.
- Four decompose-branch metrics (`leaf_count`, `leaf_retries`, `clarifications`,
  `human_gate_touches`) were computed and published but asserted nowhere.
- `select_report`'s tiebreak test proved order-independence but not correctness;
  a polarity reversal survived it.
- The Task 11 implementer self-reported one of its own: a client stub that fell
  back to `"merged"` once its scripted states drained made a poll test pass under
  the mutation it existed to catch.

#### Design defects review found

The adversarial review of Task 8 found, and the orchestrator independently
confirmed, that **a failed preparation was laundered into "already prepared" by
the very next run**. `_already_prepared` ran the verifier WITHOUT a canary, so a
swept-but-unpruned repo passed every remaining check: measured, a repo holding
the gold commit as an unreachable object showed `refs/heads/main` alone,
`rev-list --count --all` of 1, `_verify_prepared` passing, `_already_prepared`
returning True, and `prepare_instance` handing back the leaking repo. Nothing
deleted `bare` on failure, and `main()` has no per-instance recovery, so one
failure aborts the sample and the operator's natural re-run poisons the instance.

Fixed by replacing the canary with a stronger canary-free invariant: **the object
store must EQUAL the reachable closure exactly**. It needs no clone-time state so
it works inside the idempotency guard, and it catches missing objects (a partial
clone) as well as extra ones. Validated on a real instance, `pydata__xarray-3364`:
reachable/present 20947/20947, prepared in 11.5 s, 234 tracked files matching the
sample, later commits unreadable. Poisoned, the same repo reads 20947/20953, is
rejected in 0.30 s, and is rebuilt in 11.1 s.

The review also found: a `pack-*.keep` file hardlinked from the upstream defeats
`gc --prune=now` entirely while every other signal reads clean; `_leak_canary`
built one 40-char sha per ref into a single argv and died at about 790 targets
with `FileNotFoundError: [WinError 206]`, which its `except subprocess.CalledProcessError`
could not catch (the same trap `CLAUDE.md` already documents for brain prompts);
the fixture's single already-named-`main` branch hid both the branch half of the
sweep and the `symbolic-ref HEAD` line; and the per-ref delete loop costs 50 ms
per ref, about 35 minutes of pure process spawn across a 100-instance run.

#### Cleared from the deferred backlog

Nothing. No item on the standing backlog was assigned to this phase.

#### Still open

1. **What `verify_cmd` actually runs is undecided, and it blocks a meaningful
   condition B.** The mechanism is built and its absence is now fatal rather than
   silent, but no command has been chosen. The 16 pilot instances span django,
   xarray, pytest, sympy, seaborn, pylint, and scikit-learn, each with a
   different test runner and setup cost, so a single run-wide command is unlikely
   to be right. A meaningful SWE-bench verification needs the official
   per-instance Docker environment, which `verify_cmd` cannot shell out to today.
2. **`skipped` versus `passed` is still invisible to both verify-gate callers.**
   The runner's refusal stops the bench from tripping it, but does not fix it.
3. **SWE-bench Lite populates only 4 of the 9 stratum cells.** Verified over all
   300 instances: every gold patch touches exactly 1 file, patch size is 1 to 76
   lines with a median of 6, and the smallest repo is `psf/requests` at 121 files.
   The `large` patch bucket and the `tiny` repo bucket are structurally empty, so
   the pilot draws 16 rather than 30 and a full run would draw 64 rather than 144.
   The boundaries cite arXiv 2505.23419, which describes full SWE-bench.
   Recorded in `bench/README.md` and `bench/config.py` (`b97bdd2`).
4. `main()` in `bench/prepare.py` has no per-instance recovery, so one bad
   instance still aborts the rest of the sample. The re-run is now safe, but the
   run is not resilient.
5. `_discard_failed` swallows `OSError`, so a Windows file lock can defeat the
   cleanup. The "leaves nothing on disk" guarantee is best-effort; the real
   protection is the closure invariant rejecting leftovers on the next pass.
6. Partial/promisor clones and non-ASCII ref names are reasoned about but
   untested. Both should fail loudly rather than silently.
7. A worker container with internet access can clone the upstream itself.
   Preparation cannot prevent that; it is a runner and sandbox concern.

#### What blocks Task 12

**The REST API rejects local repository paths, so bench Phase A's entire local
git backend is unreachable and the pilot cannot run.** Verified:
`ProjectCreate.validate_repo_url` (`schemas.py:208`) and
`DispatchRequest.validate_repo_url` (`schemas.py:545`) reject every form
`core/git_backend.is_local_repo_url` accepts (`file://`, POSIX absolute, Windows
drive, UNC). `register_project` and `dispatch` both 422 on a prepared bare repo
path.

A related inconsistency surfaced alongside it: the three sibling request schemas
have three different policies. `ExecutePlanRequest` has NO `repo_url` validator
at all and accepts `ext::sh -c whoami`, `git://evil/repo.git`, and an embedded
`--upload-pack=`; `DispatchRequest` accepts the `--upload-pack=` form because its
check is prefix-only. This is defense in depth rather than a live RCE: git 2.52
refuses `ext::` itself (`fatal: transport 'ext' not allowed`, its default policy
is `never`), and the option-injection form is passed as a single argv element so
curl rejects it as a malformed URL. Worth closing, not urgent.

Recommended shape, NOT implemented because it changes the product's security
posture and that is the user's call: keep rejecting `ext::`, `git://`, and the
option-injection fragments in all three schemas, share one validator
implementation, and admit local filesystem paths only behind an explicit
opt-in setting (default off), gated at the endpoint or preflight layer where
runtime settings are reachable. `core/preflight._preflight_local` already
verifies the target is a bare repo and is the natural choke point.

---

### Task 8: Prepare SWE-bench instances as local bare repos

**Files:**
- Create: `bench/prepare.py`
- Test: `tests/bench/test_prepare.py`

**Depends on:** Task 7

- [ ] **Step 1: Write the failing test**

Create `tests/bench/test_prepare.py`:

```python
"""Instance preparation is the thing the whole run rests on.

A repo at the wrong commit, or not bare, silently produces a run that measures
nothing. These tests use a real local git repo as the upstream, so no network.
"""

import subprocess

import pytest

from bench.prepare import InstanceSpec, prepare_instance, tracked_file_count


def _git(*args, cwd):
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def upstream(tmp_path):
    """A repo with two commits, so 'the buggy base' is a real earlier commit."""
    repo = tmp_path / "upstream"
    repo.mkdir()
    _git("init", "-b", "main", cwd=repo)
    _git("config", "user.email", "t@e.com", cwd=repo)
    _git("config", "user.name", "t", cwd=repo)
    (repo / "app.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    (repo / "util.py").write_text("X = 1\n", encoding="utf-8")
    _git("add", ".", cwd=repo)
    _git("commit", "-m", "buggy state", cwd=repo)
    base = _git("rev-parse", "HEAD", cwd=repo)
    (repo / "app.py").write_text("def f():\n    return 2\n", encoding="utf-8")
    _git("commit", "-am", "the gold fix", cwd=repo)
    return repo, base


@pytest.mark.integration
def test_prepared_repo_is_bare(upstream, tmp_path):
    repo, base = upstream
    spec = InstanceSpec(
        instance_id="test__repo-1", upstream=str(repo), base_commit=base
    )
    bare = prepare_instance(spec, tmp_path / "work")
    assert _git("rev-parse", "--is-bare-repository", cwd=bare) == "true"


@pytest.mark.integration
def test_prepared_repo_head_is_the_buggy_base_commit(upstream, tmp_path):
    repo, base = upstream
    spec = InstanceSpec(
        instance_id="test__repo-1", upstream=str(repo), base_commit=base
    )
    bare = prepare_instance(spec, tmp_path / "work")
    assert _git("rev-parse", "main", cwd=bare) == base


@pytest.mark.integration
def test_the_gold_fix_is_not_present_in_the_prepared_repo(upstream, tmp_path):
    """The single most important property: the answer is not in the repo."""
    repo, base = upstream
    spec = InstanceSpec(
        instance_id="test__repo-1", upstream=str(repo), base_commit=base
    )
    bare = prepare_instance(spec, tmp_path / "work")
    assert "return 1" in _git("show", "main:app.py", cwd=bare)
    assert "return 2" not in _git("show", "main:app.py", cwd=bare)


@pytest.mark.integration
def test_no_later_commit_is_reachable(upstream, tmp_path):
    """A worker must not be able to `git log` its way to the gold patch."""
    repo, base = upstream
    spec = InstanceSpec(
        instance_id="test__repo-1", upstream=str(repo), base_commit=base
    )
    bare = prepare_instance(spec, tmp_path / "work")
    count = int(_git("rev-list", "--count", "--all", cwd=bare))
    assert count == 1


@pytest.mark.integration
def test_preparation_is_idempotent(upstream, tmp_path):
    repo, base = upstream
    spec = InstanceSpec(
        instance_id="test__repo-1", upstream=str(repo), base_commit=base
    )
    first = prepare_instance(spec, tmp_path / "work")
    second = prepare_instance(spec, tmp_path / "work")
    assert first == second
    assert _git("rev-parse", "main", cwd=second) == base


@pytest.mark.integration
def test_tracked_file_count_matches_git(upstream, tmp_path):
    repo, base = upstream
    spec = InstanceSpec(
        instance_id="test__repo-1", upstream=str(repo), base_commit=base
    )
    bare = prepare_instance(spec, tmp_path / "work")
    assert tracked_file_count(bare, "main") == 2
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/bench/test_prepare.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'bench.prepare'`.

- [ ] **Step 3: Write the module**

Create `bench/prepare.py`:

```python
"""Turn a SWE-bench instance into a local bare repo at its buggy base commit.

The critical property is negative: the gold patch, and every commit after the
base, must be UNREACHABLE from the prepared repo.  A worker that can
``git log`` its way to the answer measures nothing.  This module therefore
builds an orphan-free single-commit repo rather than a shallow clone with
dangling refs.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import subprocess  # nosec B404 - git CLI is the interface
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class InstanceSpec:
    """One SWE-bench instance, reduced to what preparation needs."""

    instance_id: str
    upstream: str
    base_commit: str


def _git(*args: str, cwd: Path | str | None = None) -> str:
    result = subprocess.run(  # nosec B603 - fixed argv, no shell
        ["git", *args],
        cwd=str(cwd) if cwd else None,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def prepare_instance(spec: InstanceSpec, root: Path) -> Path:
    """Materialize ``spec`` as a bare repo whose ``main`` is the buggy base.

    Idempotent: an already-prepared instance whose ``main`` is at the expected
    commit is returned untouched, so a re-run after a partial failure is cheap.

    Args:
        spec: The instance to prepare.
        root: Directory that holds every prepared repo.

    Returns:
        The path to the bare repo.
    """
    root.mkdir(parents=True, exist_ok=True)
    bare = root / f"{spec.instance_id}.git"

    if bare.exists():
        try:
            if _git("rev-parse", "main", cwd=bare) == spec.base_commit:
                logger.info("instance %s already prepared", spec.instance_id)
                return bare
        except subprocess.CalledProcessError:
            pass
        shutil.rmtree(bare)

    staging = Path(tempfile.mkdtemp(prefix=f"bench-{spec.instance_id}-"))
    try:
        work = staging / "work"
        _git("clone", "--no-checkout", spec.upstream, str(work))
        _git("checkout", "--detach", spec.base_commit, cwd=work)
        # Re-root at the base commit so nothing after it is reachable. A
        # shallow clone still leaves grafted parents and can be deepened;
        # rewriting to a fresh single-commit history cannot.
        _git("checkout", "--orphan", "main", cwd=work)
        _git("add", "-A", cwd=work)
        _git(
            "-c",
            "user.name=praxis-bench",
            "-c",
            "user.email=bench@localhost",
            "commit",
            "-m",
            f"{spec.instance_id} at {spec.base_commit}",
            cwd=work,
        )
        # Preserve the true base sha as a note so the grader can diff against
        # upstream even though the local history is re-rooted.
        (work / ".praxis-bench-base").write_text(
            spec.base_commit + "\n", encoding="utf-8"
        )
        _git("add", ".praxis-bench-base", cwd=work)
        _git(
            "-c",
            "user.name=praxis-bench",
            "-c",
            "user.email=bench@localhost",
            "commit",
            "--amend",
            "--no-edit",
            cwd=work,
        )
        _git("clone", "--bare", str(work), str(bare))
        _git("symbolic-ref", "HEAD", "refs/heads/main", cwd=bare)
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    logger.info("prepared %s at %s", spec.instance_id, bare)
    return bare


def tracked_file_count(bare: Path, ref: str = "main") -> int:
    """Return the number of tracked files at ``ref``, for repo-size strata."""
    listing = _git("ls-tree", "-r", "--name-only", ref, cwd=bare)
    return len([line for line in listing.splitlines() if line])


def main(argv: list[str] | None = None) -> int:
    """CLI: prepare every instance named in a committed sample file."""
    parser = argparse.ArgumentParser(description="Prepare bench instances")
    parser.add_argument("--sample", required=True, help="path to a sample JSON file")
    parser.add_argument(
        "--root",
        default=os.environ.get("PRAXIS_BENCH_ROOT", "bench/.work/repos"),
        help="where to write prepared bare repos",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    sample = json.loads(Path(args.sample).read_text(encoding="utf-8"))
    root = Path(args.root)
    for entry in sample["instances"]:
        prepare_instance(
            InstanceSpec(
                instance_id=entry["instance_id"],
                upstream=entry["upstream"],
                base_commit=entry["base_commit"],
            ),
            root,
        )
    logger.info("prepared %d instances into %s", len(sample["instances"]), root)
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/bench/test_prepare.py -v`
Expected: PASS (6 tests).

- [ ] **Step 5: Mutation-check the unreachability guarantee**

This is the single most important property in the benchmark. Temporarily replace
the orphan re-root (`checkout --orphan` through the amend) with a plain
`git checkout -b main`.
Run: `uv run pytest tests/bench/test_prepare.py -v -k "gold_fix or later_commit"`
Expected: `test_no_later_commit_is_reachable` FAILS (the full history comes
along). Restore and re-run to confirm PASS.

- [ ] **Step 6: Commit**

```bash
git add bench/prepare.py tests/bench/test_prepare.py
git commit -m "feat(bench): prepare instances as re-rooted bare repos

The gold patch and every later commit are unreachable by construction, not
by a shallow-clone depth that a worker could deepen."
```

---

### Task 9: The stratified sampler and the committed pilot sample

**Files:**
- Create: `bench/sample.py`
- Create: `bench/samples/lite-pilot-30.json`
- Test: `tests/bench/test_sample.py`

**Depends on:** Task 7

- [ ] **Step 1: Write the failing test**

Create `tests/bench/test_sample.py`:

```python
"""The draw must be reproducible and balanced, or the strata mean nothing."""

import json
from pathlib import Path

import pytest

from bench.sample import draw_stratified


def _pool(n: int = 400) -> list[dict]:
    """A synthetic instance pool spanning every stratum cell."""
    pool = []
    for i in range(n):
        pool.append(
            {
                "instance_id": f"repo__proj-{i}",
                "upstream": f"https://example.invalid/{i}.git",
                "base_commit": f"{i:040d}",
                "patch_files": (i % 3) + 1,
                "patch_loc": [3, 50, 400][i % 3],
                "repo_files": [40, 250, 900][i % 3],
            }
        )
    return pool


@pytest.mark.unit
def test_the_same_seed_draws_the_same_sample():
    a = draw_stratified(_pool(), per_stratum=2, seed=42)
    b = draw_stratified(_pool(), per_stratum=2, seed=42)
    assert [i["instance_id"] for i in a] == [i["instance_id"] for i in b]


@pytest.mark.unit
def test_a_different_seed_draws_a_different_sample():
    a = draw_stratified(_pool(), per_stratum=2, seed=42)
    b = draw_stratified(_pool(), per_stratum=2, seed=43)
    assert [i["instance_id"] for i in a] != [i["instance_id"] for i in b]


@pytest.mark.unit
def test_every_populated_cell_gets_the_requested_count():
    sample = draw_stratified(_pool(), per_stratum=2, seed=42)
    from collections import Counter

    counts = Counter((i["stratum_patch"], i["stratum_repo"]) for i in sample)
    assert all(c == 2 for c in counts.values())


@pytest.mark.unit
def test_each_instance_is_drawn_at_most_once():
    sample = draw_stratified(_pool(), per_stratum=3, seed=42)
    ids = [i["instance_id"] for i in sample]
    assert len(ids) == len(set(ids))


@pytest.mark.unit
def test_every_drawn_instance_carries_its_stratum_labels():
    for entry in draw_stratified(_pool(), per_stratum=1, seed=42):
        assert entry["stratum_patch"] in {"small", "medium", "large"}
        assert entry["stratum_repo"] in {"tiny", "mid", "big"}


@pytest.mark.unit
def test_a_thin_cell_takes_everything_it_has_without_raising():
    thin = _pool(4)
    sample = draw_stratified(thin, per_stratum=99, seed=42)
    assert len(sample) == len(thin)


@pytest.mark.unit
def test_the_committed_pilot_sample_is_valid_and_thirty_instances():
    path = Path("bench/samples/lite-pilot-30.json")
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["seed"] == 20260806
    assert data["corpus"] == "swe-bench-lite"
    assert len(data["instances"]) == 30
    for entry in data["instances"]:
        assert entry["instance_id"]
        assert entry["upstream"]
        assert len(entry["base_commit"]) >= 7
        assert entry["stratum_patch"] in {"small", "medium", "large"}
        assert entry["stratum_repo"] in {"tiny", "mid", "big"}
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/bench/test_sample.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'bench.sample'`.

- [ ] **Step 3: Write the sampler**

Create `bench/sample.py`:

```python
"""Stratified sampling from a SWE-bench instance pool.

Stratification is pre-registered: the buckets, the per-cell counts, and the
seed are all fixed in ``bench/config.py`` and the drawn list is committed.
Nobody gets to look at the results and then re-draw.
"""

from __future__ import annotations

import argparse
import json
import logging
import random
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

from bench.config import PILOT_PER_STRATUM, SAMPLE_SEED, stratum_for


logger = logging.getLogger(__name__)


def draw_stratified(
    pool: list[dict[str, Any]], per_stratum: int, seed: int
) -> list[dict[str, Any]]:
    """Draw ``per_stratum`` instances from every populated stratum cell.

    Args:
        pool: Instance dicts carrying ``patch_files``, ``patch_loc``, and
            ``repo_files`` from the published SWE-bench metadata.
        per_stratum: Target count per cell.  A thinner cell contributes
            everything it has rather than raising.
        seed: Reproducibility seed; publish it with the sample.

    Returns:
        The drawn instances, each annotated with ``stratum_patch`` and
        ``stratum_repo``, sorted by instance id so the output is stable.
    """
    cells: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for entry in pool:
        patch, repo = stratum_for(
            int(entry["patch_files"]),
            int(entry["patch_loc"]),
            int(entry["repo_files"]),
        )
        annotated = {**entry, "stratum_patch": patch, "stratum_repo": repo}
        cells[(patch, repo)].append(annotated)

    rng = random.Random(seed)  # nosec B311 - reproducible sampling, not crypto
    drawn: list[dict[str, Any]] = []
    for cell in sorted(cells):
        candidates = sorted(cells[cell], key=lambda e: e["instance_id"])
        take = min(per_stratum, len(candidates))
        if take < per_stratum:
            logger.warning(
                "stratum %s has only %d instances, requested %d",
                cell,
                len(candidates),
                per_stratum,
            )
        drawn.extend(rng.sample(candidates, take))
    return sorted(drawn, key=lambda e: e["instance_id"])


def main(argv: list[str] | None = None) -> int:
    """CLI: draw a sample from a pool file and write a committed sample file."""
    parser = argparse.ArgumentParser(description="Draw a stratified bench sample")
    parser.add_argument("--pool", required=True, help="instance metadata JSON")
    parser.add_argument("--out", required=True, help="where to write the sample")
    parser.add_argument("--per-stratum", type=int, default=PILOT_PER_STRATUM)
    parser.add_argument("--seed", type=int, default=SAMPLE_SEED)
    parser.add_argument("--corpus", default="swe-bench-lite")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    pool = json.loads(Path(args.pool).read_text(encoding="utf-8"))
    instances = draw_stratified(pool, args.per_stratum, args.seed)
    Path(args.out).write_text(
        json.dumps(
            {
                "corpus": args.corpus,
                "seed": args.seed,
                "per_stratum": args.per_stratum,
                "instances": instances,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    logger.info("wrote %d instances to %s", len(instances), args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Draw and commit the pilot sample**

Obtain the SWE-bench Lite instance metadata (instance id, repo, base commit,
gold-patch file count, gold-patch LOC) and write it to
`bench/.work/pool-lite.json` as a JSON list with the keys the sampler expects.
Then draw:

```bash
mkdir -p bench/samples
uv run python -m bench.sample \
  --pool bench/.work/pool-lite.json \
  --out bench/samples/lite-pilot-30.json \
  --per-stratum 4 \
  --corpus swe-bench-lite
```

If the draw comes to fewer or more than 30 because some cells are thin, adjust
`--per-stratum` and record the actual per-cell counts in the sample file. Then
update the assertion in `test_the_committed_pilot_sample_is_valid_and_thirty_instances`
to the real count and rename the file to match.

- [ ] **Step 5: Run the test to verify it passes**

Run: `uv run pytest tests/bench/test_sample.py -v`
Expected: PASS (7 tests).

- [ ] **Step 6: Mutation-check reproducibility**

Temporarily change `rng = random.Random(seed)` to `rng = random.Random()`.
Run: `uv run pytest tests/bench/test_sample.py::test_the_same_seed_draws_the_same_sample -v`
Expected: FAIL. Restore and re-run to confirm PASS.

- [ ] **Step 7: Commit**

```bash
git add bench/sample.py bench/samples/ tests/bench/test_sample.py
git commit -m "feat(bench): add the stratified sampler and commit the pilot sample

Buckets, per-cell counts, and seed are pre-registered; the drawn list is
committed so the run is reproducible and nobody re-draws after seeing
results."
```

---

### Task 10: The metrics schema and writer

**Files:**
- Create: `bench/metrics.py`
- Test: `tests/bench/test_metrics.py`

**Depends on:** Task 7

- [ ] **Step 1: Write the failing test**

Create `tests/bench/test_metrics.py`:

```python
"""One JSONL row per task-attempt. The schema is the experiment's record."""

import json

import pytest

from bench.metrics import AttemptRecord, append_record, read_records


def _record(**overrides) -> AttemptRecord:
    base = {
        "run_id": "run-1",
        "instance_id": "astropy__astropy-12907",
        "condition": "B",
        "worker": "local-openweight",
        "seed": 1,
        "stratum_patch": "medium",
        "stratum_repo": "big",
        "resolved": True,
        "plausible": True,
        "leaf_count": 3,
        "leaf_retries": 1,
        "whole_task_retries": 0,
        "clarifications": 0,
        "human_gate_touches": 1,
        "brain_tokens": 12000,
        "worker_tokens": 48000,
        "wall_clock_s": 940.5,
        "error": None,
    }
    base.update(overrides)
    return AttemptRecord(**base)


@pytest.mark.unit
def test_a_record_round_trips_through_jsonl(tmp_path):
    path = tmp_path / "run.jsonl"
    append_record(path, _record())
    rows = read_records(path)
    assert len(rows) == 1
    assert rows[0].instance_id == "astropy__astropy-12907"
    assert rows[0].resolved is True


@pytest.mark.unit
def test_append_does_not_truncate_prior_rows(tmp_path):
    path = tmp_path / "run.jsonl"
    append_record(path, _record(condition="A"))
    append_record(path, _record(condition="B"))
    assert [r.condition for r in read_records(path)] == ["A", "B"]


@pytest.mark.unit
def test_every_row_is_a_single_line_of_json(tmp_path):
    path = tmp_path / "run.jsonl"
    append_record(path, _record())
    append_record(path, _record(condition="A"))
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    for line in lines:
        json.loads(line)


@pytest.mark.unit
def test_plausible_but_wrong_is_representable():
    """AutoCodeRover found 35 percent of plausible patches wrong; we measure it."""
    record = _record(resolved=False, plausible=True)
    assert record.plausible_but_wrong is True


@pytest.mark.unit
def test_a_resolved_patch_is_never_counted_as_plausible_but_wrong():
    assert _record(resolved=True, plausible=True).plausible_but_wrong is False


@pytest.mark.unit
def test_an_unapplied_patch_is_not_plausible_but_wrong():
    assert _record(resolved=False, plausible=False).plausible_but_wrong is False


@pytest.mark.unit
def test_leaf_scoped_and_whole_task_retries_are_separate_fields():
    """The distinction is the point: static decomposition raises whole-task retries."""
    record = _record(leaf_retries=3, whole_task_retries=1)
    assert record.leaf_retries == 3
    assert record.whole_task_retries == 1
    assert record.total_retries == 4


@pytest.mark.unit
def test_an_errored_attempt_is_recorded_not_dropped(tmp_path):
    """Silently dropping a crashed attempt inflates the resolve rate."""
    path = tmp_path / "run.jsonl"
    append_record(path, _record(resolved=False, error="worker endpoint unreachable"))
    assert read_records(path)[0].error == "worker endpoint unreachable"


@pytest.mark.unit
def test_reading_a_missing_file_returns_no_rows(tmp_path):
    assert read_records(tmp_path / "absent.jsonl") == []
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/bench/test_metrics.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'bench.metrics'`.

- [ ] **Step 3: Write the module**

Create `bench/metrics.py`:

```python
"""The benchmark's record: one JSONL row per task-attempt.

Append-only, one line of JSON per row, committed with the report.  Every
attempt is recorded including crashes: silently dropping a failed attempt
inflates the resolve rate, which is the easiest way to publish a wrong number
without lying on purpose.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, fields
from pathlib import Path


@dataclass(frozen=True)
class AttemptRecord:
    """One instance, run once, under one condition, by one worker."""

    run_id: str
    instance_id: str
    condition: str
    worker: str
    seed: int
    stratum_patch: str
    stratum_repo: str

    # Primary outcome, from the OFFICIAL SWE-bench grader. Never self-graded.
    resolved: bool
    # The patch applied and the project built, whether or not it is correct.
    plausible: bool

    leaf_count: int
    leaf_retries: int
    whole_task_retries: int
    clarifications: int
    human_gate_touches: int

    brain_tokens: int
    worker_tokens: int
    wall_clock_s: float

    error: str | None = None

    @property
    def plausible_but_wrong(self) -> bool:
        """A patch that applies and builds but does not resolve the issue.

        AutoCodeRover (arXiv 2404.05427) found 35 percent of plausible patches
        wrong; a worker gaming surface checks shows up exactly here.
        """
        return self.plausible and not self.resolved

    @property
    def total_retries(self) -> int:
        """Leaf-scoped plus whole-task-scoped retries."""
        return self.leaf_retries + self.whole_task_retries

    @property
    def total_tokens(self) -> int:
        """Brain plus worker tokens, for cost per RESOLVED task."""
        return self.brain_tokens + self.worker_tokens


def append_record(path: Path, record: AttemptRecord) -> None:
    """Append one row. Creates the file and its parent if needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(asdict(record), sort_keys=True) + "\n")


def read_records(path: Path) -> list[AttemptRecord]:
    """Read every row. A missing file is an empty run, not an error."""
    if not Path(path).is_file():
        return []
    known = {f.name for f in fields(AttemptRecord)}
    rows: list[AttemptRecord] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        data = json.loads(line)
        rows.append(AttemptRecord(**{k: v for k, v in data.items() if k in known}))
    return rows
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/bench/test_metrics.py -v`
Expected: PASS (9 tests).

- [ ] **Step 5: Mutation-check the plausible-but-wrong definition**

Temporarily change `return self.plausible and not self.resolved` to
`return self.plausible`.
Run: `uv run pytest tests/bench/test_metrics.py::test_a_resolved_patch_is_never_counted_as_plausible_but_wrong -v`
Expected: FAIL. Restore and re-run to confirm PASS.

- [ ] **Step 6: Commit**

```bash
git add bench/metrics.py tests/bench/test_metrics.py
git commit -m "feat(bench): add the append-only attempt record schema

Leaf-scoped and whole-task retries are separate fields, plausible-but-wrong
is derived, and errored attempts are recorded rather than dropped."
```

---

### Task 11: The runner and the official grader

**Files:**
- Create: `bench/runner.py`
- Create: `bench/grade.py`
- Test: `tests/bench/test_runner.py`, `tests/bench/test_grade.py`

**Depends on:** Task 8, Task 9, Task 10

- [ ] **Step 1: Write the failing runner test**

Create `tests/bench/test_runner.py`:

```python
"""The runner's job is to be boring and to refuse a confounded design."""

import pytest

from bench.config import CONDITIONS
from bench.runner import ConfoundedDesignError, assert_matched_pair, plan_attempts


@pytest.mark.unit
def test_a_and_c_together_are_a_valid_matched_pair():
    assert_matched_pair(["A", "B", "C"])  # must not raise


@pytest.mark.unit
def test_c_without_a_is_refused():
    """C alone cannot be compared to anything; A is its verify-gate twin."""
    with pytest.raises(ConfoundedDesignError, match="A"):
        assert_matched_pair(["B", "C"])


@pytest.mark.unit
def test_a_and_b_alone_is_a_valid_pilot_design():
    assert_matched_pair(["A", "B"])


@pytest.mark.unit
def test_an_unknown_condition_is_refused():
    with pytest.raises(ConfoundedDesignError):
        assert_matched_pair(["A", "Z"])


@pytest.mark.unit
def test_plan_attempts_is_the_full_cross_product():
    instances = [{"instance_id": "i1"}, {"instance_id": "i2"}]
    attempts = plan_attempts(instances, ["A", "B"], ["local-openweight"], seeds=[1])
    assert len(attempts) == 4
    assert {(a.instance["instance_id"], a.condition.key) for a in attempts} == {
        ("i1", "A"),
        ("i1", "B"),
        ("i2", "A"),
        ("i2", "B"),
    }


@pytest.mark.unit
def test_plan_attempts_multiplies_by_seed():
    instances = [{"instance_id": "i1"}]
    attempts = plan_attempts(instances, ["B"], ["local-openweight"], seeds=[1, 2])
    assert sorted(a.seed for a in attempts) == [1, 2]


@pytest.mark.unit
def test_plan_attempts_multiplies_by_worker():
    instances = [{"instance_id": "i1"}]
    attempts = plan_attempts(
        instances, ["B"], ["local-openweight", "hosted-flash"], seeds=[1]
    )
    assert {a.worker.key for a in attempts} == {"local-openweight", "hosted-flash"}


@pytest.mark.unit
def test_plan_attempts_is_deterministically_ordered():
    instances = [{"instance_id": "i2"}, {"instance_id": "i1"}]
    a = plan_attempts(instances, ["B", "A"], ["local-openweight"], seeds=[1])
    b = plan_attempts(instances, ["B", "A"], ["local-openweight"], seeds=[1])
    assert [(x.instance["instance_id"], x.condition.key) for x in a] == [
        (y.instance["instance_id"], y.condition.key) for y in b
    ]


@pytest.mark.unit
def test_every_declared_condition_is_plannable():
    instances = [{"instance_id": "i1"}]
    attempts = plan_attempts(
        instances, [c.key for c in CONDITIONS], ["local-openweight"], seeds=[1]
    )
    assert len(attempts) == len(CONDITIONS)
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/bench/test_runner.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'bench.runner'`.

- [ ] **Step 3: Write the runner**

Create `bench/runner.py`:

```python
"""Drive bench instances through Praxis via the REST API.

The runner owns no engine logic.  It registers a project pointed at a prepared
bare repo, submits the issue either monolithically (condition A) or as a plan to
decompose (conditions B, C, D), polls to a terminal state, and writes one
``AttemptRecord``.  Grading happens separately, in ``bench/grade.py``, with the
official harness.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from bench.config import CONDITIONS, SEEDS, WORKERS, Condition, Worker
from bench.metrics import AttemptRecord, append_record


logger = logging.getLogger(__name__)

# Poll cadence and ceiling for one attempt. An instance that has not reached a
# terminal state by the ceiling is recorded as an error, never dropped.
_POLL_INTERVAL_S = 15
_ATTEMPT_TIMEOUT_S = 60 * 60


class ConfoundedDesignError(Exception):
    """Raised when a requested condition set cannot support its comparisons."""


def assert_matched_pair(condition_keys: list[str]) -> None:
    """Refuse a condition set that would produce a confounded comparison.

    Condition C is condition B with the verify gate disabled.  Its only valid
    reading is "how much of B's effect is verification", which requires the
    no-gate baseline A in the same run.  Running C without A yields a number
    with no interpretation, so the runner refuses rather than producing one.
    """
    known = {c.key for c in CONDITIONS}
    unknown = set(condition_keys) - known
    if unknown:
        message = f"unknown conditions: {sorted(unknown)}; known are {sorted(known)}"
        raise ConfoundedDesignError(message)
    if "C" in condition_keys and "A" not in condition_keys:
        message = (
            "condition C (decomposition without the verify gate) requires "
            "condition A (monolithic without a verify gate) in the same run: "
            "they are a matched no-gate pair and the ablation is confounded "
            "without both"
        )
        raise ConfoundedDesignError(message)


@dataclass(frozen=True)
class Attempt:
    """One cell of the run matrix."""

    instance: dict[str, Any]
    condition: Condition
    worker: Worker
    seed: int


def plan_attempts(
    instances: list[dict[str, Any]],
    condition_keys: list[str],
    worker_keys: list[str],
    seeds: list[int],
) -> list[Attempt]:
    """Return the full, deterministically ordered cross product."""
    assert_matched_pair(condition_keys)
    by_condition = {c.key: c for c in CONDITIONS}
    by_worker = {w.key: w for w in WORKERS}
    attempts: list[Attempt] = []
    for instance in sorted(instances, key=lambda i: i["instance_id"]):
        for condition_key in sorted(condition_keys):
            for worker_key in sorted(worker_keys):
                for seed in sorted(seeds):
                    attempts.append(
                        Attempt(
                            instance=instance,
                            condition=by_condition[condition_key],
                            worker=by_worker[worker_key],
                            seed=seed,
                        )
                    )
    return attempts


class BenchClient:
    """Thin REST client. Mirrors src/mcp_server/client.PraxisClient deliberately."""

    def __init__(self, base_url: str, token: str) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=120,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def register_project(
        self,
        name: str,
        repo_url: str,
        worker: Worker,
        verify_cmd: str | None,
        adaptive_split: bool,
    ) -> str:
        """Create the project row that carries this condition's switches.

        ``/api/dispatch`` and ``/api/execute-plan`` are keyed on ``repo_url``
        and reuse an existing project for it (``SELECT * FROM projects WHERE
        repo_url = ?``), so registering first is how per-condition settings
        (``verify_cmd``, ``max_retries``, ``auto_merge``) reach the run.
        """
        response = await self._client.post(
            "/api/projects",
            json={
                "name": name,
                "repo_url": repo_url,
                "model_name": worker.model,
                "harness": worker.harness,
                "default_branch": "main",
                "verify_cmd": verify_cmd,
                # Only condition D reaches the second worker-attributable
                # failure that triggers adaptive triage; the others cap at one
                # attempt so triage can never fire. See bench/README.md.
                "max_retries": 3 if adaptive_split else 1,
                # The bench measures the engine, not the human gate.
                "auto_merge": True,
                "approval_gate": False,
            },
        )
        response.raise_for_status()
        return str(response.json()["id"])

    async def dispatch(self, repo_url: str, instructions: str, worker: Worker) -> str:
        """Monolithic condition A. DispatchRequest is keyed on repo_url."""
        response = await self._client.post(
            "/api/dispatch",
            json={
                "repo_url": repo_url,
                "instructions": instructions,
                "model": worker.model,
                "harness": worker.harness,
                "branch": "main",
            },
        )
        response.raise_for_status()
        return str(response.json()["task_id"])

    async def execute_plan(self, repo_url: str, plan: str, worker: Worker) -> str:
        """Decomposed conditions B, C, D. ExecutePlanRequest is keyed on repo_url."""
        response = await self._client.post(
            "/api/execute-plan",
            json={
                "repo_url": repo_url,
                "plan": plan,
                "model": worker.model,
                "harness": worker.harness,
                "branch": "main",
            },
        )
        response.raise_for_status()
        return str(response.json()["plan_id"])

    async def poll_plan(self, plan_id: str) -> dict[str, Any]:
        response = await self._client.get(f"/api/plans/{plan_id}")
        response.raise_for_status()
        return dict(response.json())

    async def plan_tasks(self, plan_id: str) -> list[dict[str, Any]]:
        response = await self._client.get(f"/api/plans/{plan_id}/tasks")
        response.raise_for_status()
        return list(response.json())


def _issue_prompt(instance: dict[str, Any]) -> str:
    """Render the instance's problem statement as a worker-facing task."""
    return (
        f"{instance['problem_statement']}\n\n"
        "Fix this in the repository. Add or update tests only where the issue "
        "describes behavior that should be covered."
    )


async def run_attempt(
    attempt: Attempt,
    client: BenchClient,
    run_id: str,
    repo_root: Path,
    out_path: Path,
) -> AttemptRecord:
    """Run one cell to a terminal state and record it.

    Every failure mode, including a timeout or an orchestrator error, produces a
    record with ``error`` set.  Nothing is dropped.
    """
    instance = attempt.instance
    bare = repo_root / f"{instance['instance_id']}.git"
    started = time.monotonic()
    error: str | None = None
    leaf_count = 0
    leaf_retries = 0
    whole_task_retries = 0
    clarifications = 0

    try:
        await client.register_project(
            name=f"bench-{instance['instance_id']}-{attempt.condition.key}-{uuid.uuid4().hex[:6]}",
            repo_url=str(bare),
            worker=attempt.worker,
            # Condition C and the matched baseline A run WITHOUT a verify gate.
            verify_cmd=(
                instance.get("verify_cmd") if attempt.condition.verify_gate else None
            ),
            adaptive_split=attempt.condition.adaptive_split,
        )

        if attempt.condition.decompose:
            plan_id = await client.execute_plan(
                str(bare), _issue_prompt(instance), attempt.worker
            )
            deadline = time.monotonic() + _ATTEMPT_TIMEOUT_S
            while time.monotonic() < deadline:
                plan = await client.poll_plan(plan_id)
                if plan["status"] in ("completed", "failed", "rejected"):
                    break
                await asyncio.sleep(_POLL_INTERVAL_S)
            else:
                error = "attempt timed out"
            tasks = await client.plan_tasks(plan_id)
            leaf_count = len(tasks)
            leaf_retries = sum(max(int(t.get("attempt", 1)) - 1, 0) for t in tasks)
            clarifications = sum(
                1 for t in tasks if t.get("clarification_question")
            )
        else:
            task_id = await client.dispatch(
                str(bare), _issue_prompt(instance), attempt.worker
            )
            deadline = time.monotonic() + _ATTEMPT_TIMEOUT_S
            leaf_count = 1
            while time.monotonic() < deadline:
                response = await client._client.get(f"/api/tasks/{task_id}")
                response.raise_for_status()
                task = response.json()["task"]
                if task["status"] in ("merged", "failed", "passed"):
                    whole_task_retries = max(int(task.get("attempt", 1)) - 1, 0)
                    break
                await asyncio.sleep(_POLL_INTERVAL_S)
            else:
                error = "attempt timed out"
    except Exception as exc:  # noqa: BLE001 - an errored cell is data, not a crash
        logger.exception("attempt failed: %s", instance["instance_id"])
        error = f"{type(exc).__name__}: {exc}"

    record = AttemptRecord(
        run_id=run_id,
        instance_id=instance["instance_id"],
        condition=attempt.condition.key,
        worker=attempt.worker.key,
        seed=attempt.seed,
        stratum_patch=instance["stratum_patch"],
        stratum_repo=instance["stratum_repo"],
        # Grading happens later, against the official harness.
        resolved=False,
        plausible=False,
        leaf_count=leaf_count,
        leaf_retries=leaf_retries,
        whole_task_retries=whole_task_retries,
        clarifications=clarifications,
        human_gate_touches=0,
        brain_tokens=0,
        worker_tokens=0,
        wall_clock_s=time.monotonic() - started,
        error=error,
    )
    append_record(out_path, record)
    return record


async def _main_async(args: argparse.Namespace) -> int:
    sample = json.loads(Path(args.sample).read_text(encoding="utf-8"))
    attempts = plan_attempts(
        sample["instances"],
        [k.strip() for k in args.conditions.split(",") if k.strip()],
        [k.strip() for k in args.worker.split(",") if k.strip()],
        [int(s) for s in args.seeds.split(",")] if args.seeds else list(SEEDS),
    )
    run_id = args.run_id or f"run-{uuid.uuid4().hex[:8]}"
    out_path = Path(args.out or f"bench/.work/runs/{run_id}/attempts.jsonl")
    logger.info("run %s: %d attempts to %s", run_id, len(attempts), out_path)

    client = BenchClient(args.api, args.token)
    try:
        for index, attempt in enumerate(attempts, start=1):
            logger.info(
                "[%d/%d] %s condition %s worker %s seed %d",
                index,
                len(attempts),
                attempt.instance["instance_id"],
                attempt.condition.key,
                attempt.worker.key,
                attempt.seed,
            )
            await run_attempt(attempt, client, run_id, Path(args.repos), out_path)
    finally:
        await client.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description="Run the Praxis bench matrix")
    parser.add_argument("--sample", required=True)
    parser.add_argument("--conditions", default="A,B")
    parser.add_argument("--worker", default="local-openweight")
    parser.add_argument("--seeds", default="1")
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--out", default=None)
    parser.add_argument("--repos", default="bench/.work/repos")
    parser.add_argument(
        "--api", default=os.environ.get("PRAXIS_API", "http://127.0.0.1:12323")
    )
    parser.add_argument("--token", default=os.environ.get("AUTH_TOKEN", ""))
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    return asyncio.run(_main_async(args))


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run the runner test to verify it passes**

Run: `uv run pytest tests/bench/test_runner.py -v`
Expected: PASS (9 tests).

- [ ] **Step 5: Mutation-check the confounded-design guard**

Temporarily change `if "C" in condition_keys and "A" not in condition_keys:` to
`if False:`.
Run: `uv run pytest tests/bench/test_runner.py::test_c_without_a_is_refused -v`
Expected: FAIL. Restore and re-run to confirm PASS.

- [ ] **Step 6: Write the failing grader test**

Create `tests/bench/test_grade.py`:

```python
"""Grading uses the OFFICIAL harness. This module only extracts and delegates."""

import subprocess

import pytest

from bench.grade import GradeResult, extract_patch, parse_official_report


def _git(*args, cwd):
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def merged_repo(tmp_path):
    """A bare repo whose main carries one merged change over the base."""
    work, bare = tmp_path / "w", tmp_path / "r.git"
    work.mkdir()
    _git("init", "-b", "main", cwd=work)
    _git("config", "user.email", "t@e.com", cwd=work)
    _git("config", "user.name", "t", cwd=work)
    (work / "app.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    _git("add", ".", cwd=work)
    _git("commit", "-m", "base", cwd=work)
    base = _git("rev-parse", "HEAD", cwd=work)
    (work / "app.py").write_text("def f():\n    return 2\n", encoding="utf-8")
    _git("commit", "-am", "the model's fix", cwd=work)
    _git("clone", "--bare", str(work), str(bare))
    return bare, base


@pytest.mark.integration
def test_extract_patch_returns_the_change_against_base(merged_repo):
    bare, base = merged_repo
    patch = extract_patch(bare, base=base, head="main")
    assert "-    return 1" in patch
    assert "+    return 2" in patch


@pytest.mark.integration
def test_extract_patch_is_empty_when_nothing_changed(merged_repo):
    bare, base = merged_repo
    assert extract_patch(bare, base=base, head=base).strip() == ""


@pytest.mark.unit
def test_parse_official_report_reads_a_resolved_instance():
    report = {
        "astropy__astropy-12907": {
            "patch_is_None": False,
            "patch_exists": True,
            "patch_successfully_applied": True,
            "resolved": True,
        }
    }
    result = parse_official_report(report, "astropy__astropy-12907")
    assert result == GradeResult(resolved=True, plausible=True, applied=True)


@pytest.mark.unit
def test_parse_official_report_reads_an_applied_but_unresolved_instance():
    report = {
        "x__y-1": {
            "patch_is_None": False,
            "patch_exists": True,
            "patch_successfully_applied": True,
            "resolved": False,
        }
    }
    result = parse_official_report(report, "x__y-1")
    assert result.resolved is False
    assert result.plausible is True


@pytest.mark.unit
def test_parse_official_report_reads_an_unapplied_patch():
    report = {
        "x__y-1": {
            "patch_is_None": False,
            "patch_exists": True,
            "patch_successfully_applied": False,
            "resolved": False,
        }
    }
    result = parse_official_report(report, "x__y-1")
    assert result.plausible is False


@pytest.mark.unit
def test_a_missing_instance_grades_as_unresolved_not_as_an_error():
    """A crashed attempt must count against the condition, not vanish."""
    result = parse_official_report({}, "x__y-1")
    assert result == GradeResult(resolved=False, plausible=False, applied=False)
```

- [ ] **Step 7: Write the grader**

Create `bench/grade.py`:

```python
"""Extract each attempt's patch and grade it with the OFFICIAL SWE-bench harness.

Praxis never grades itself.  This module produces the prediction file the
upstream harness expects, shells out to it, and reads its report back.  The only
judgment here is the mapping from the harness's fields to ``resolved`` and
``plausible``.
"""

from __future__ import annotations

import argparse
import json
import logging
import subprocess  # nosec B404 - git and the official harness are the interface
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bench.metrics import read_records


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class GradeResult:
    """What the official harness said about one instance."""

    resolved: bool
    plausible: bool
    applied: bool


def extract_patch(bare: Path, base: str, head: str = "main") -> str:
    """Return ``git diff base...head`` from the prepared bare repo."""
    result = subprocess.run(  # nosec B603 - fixed argv, no shell
        ["git", "-C", str(bare), "diff", f"{base}...{head}"],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


def write_predictions(
    records: list[Any], repo_root: Path, out_path: Path, model_name: str
) -> Path:
    """Write the prediction JSONL the official harness consumes."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as handle:
        for record in records:
            bare = repo_root / f"{record.instance_id}.git"
            base_file = bare / "praxis-bench-base"
            base = (
                base_file.read_text(encoding="utf-8").strip()
                if base_file.is_file()
                else "main"
            )
            patch = extract_patch(bare, base=base) if bare.is_dir() else ""
            handle.write(
                json.dumps(
                    {
                        "instance_id": record.instance_id,
                        "model_name_or_path": model_name,
                        "model_patch": patch,
                    }
                )
                + "\n"
            )
    return out_path


def parse_official_report(
    report: dict[str, dict[str, Any]], instance_id: str
) -> GradeResult:
    """Map the harness's per-instance fields onto our two outcome flags.

    A missing instance grades as unresolved and not plausible: an attempt that
    crashed before producing a patch must count against its condition, not
    disappear from the denominator.
    """
    entry = report.get(instance_id)
    if entry is None:
        return GradeResult(resolved=False, plausible=False, applied=False)
    applied = bool(entry.get("patch_successfully_applied"))
    return GradeResult(
        resolved=bool(entry.get("resolved")),
        plausible=applied,
        applied=applied,
    )


def main(argv: list[str] | None = None) -> int:
    """CLI: build predictions, invoke the official harness, merge results back."""
    parser = argparse.ArgumentParser(description="Grade a bench run officially")
    parser.add_argument("--run", required=True, help="run directory")
    parser.add_argument("--repos", default="bench/.work/repos")
    parser.add_argument("--dataset", default="princeton-nlp/SWE-bench_Lite")
    parser.add_argument(
        "--harness",
        default="swebench.harness.run_evaluation",
        help="python -m target for the official evaluation harness",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    run_dir = Path(args.run)
    records = read_records(run_dir / "attempts.jsonl")
    predictions = write_predictions(
        records, Path(args.repos), run_dir / "predictions.jsonl", "praxis"
    )

    logger.info("invoking the official harness on %s", predictions)
    subprocess.run(  # nosec B603 - operator-provided module name, no shell
        [
            sys.executable,
            "-m",
            args.harness,
            "--dataset_name",
            args.dataset,
            "--predictions_path",
            str(predictions),
            "--run_id",
            run_dir.name,
        ],
        check=True,
    )

    report_path = next(Path.cwd().glob(f"*{run_dir.name}*.json"), None)
    if report_path is None:
        logger.error("official harness produced no report file; nothing graded")
        return 1
    report = json.loads(report_path.read_text(encoding="utf-8"))

    graded = []
    for record in records:
        result = parse_official_report(report, record.instance_id)
        graded.append(
            {
                **json.loads(json.dumps(record.__dict__)),
                "resolved": result.resolved,
                "plausible": result.plausible,
            }
        )
    (run_dir / "graded.jsonl").write_text(
        "\n".join(json.dumps(row, sort_keys=True) for row in graded) + "\n",
        encoding="utf-8",
    )
    logger.info("graded %d attempts into %s", len(graded), run_dir / "graded.jsonl")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 8: Run the grader test to verify it passes**

Run: `uv run pytest tests/bench/test_grade.py -v`
Expected: PASS (6 tests).

- [ ] **Step 9: Mutation-check the missing-instance rule**

Temporarily make `parse_official_report` raise `KeyError` for a missing instance
instead of returning an unresolved result.
Run: `uv run pytest tests/bench/test_grade.py::test_a_missing_instance_grades_as_unresolved_not_as_an_error -v`
Expected: FAIL. Restore and re-run to confirm PASS. This rule is the difference
between an honest denominator and a silently inflated resolve rate.

- [ ] **Step 10: Commit**

```bash
git add bench/runner.py bench/grade.py tests/bench/test_runner.py tests/bench/test_grade.py
git commit -m "feat(bench): add the matrix runner and the official-harness grader

The runner refuses a confounded design (C without its matched A baseline)
and records every attempt including crashes; grading delegates entirely to
the upstream SWE-bench harness."
```

---

### Task 12: Run the pilot and check it against its exit criteria

**Files:**
- Create: `docs/bench/pilot-notes.md`
- Modify: `bench/README.md`

**Depends on:** Task 11

The pilot exists to debug the harness, not to conclude. It passes only when all
three criteria below hold. Do not proceed to Phase C until they do.

- [ ] **Step 1: Prepare the instances**

```bash
uv run python -m bench.prepare --sample bench/samples/lite-pilot-30.json
ls bench/.work/repos | wc -l
```

Expected: 30 bare repos.

- [ ] **Step 2: Confirm the orchestrator is up and current**

```bash
curl -s http://localhost:12323/health
git rev-parse --short HEAD
```

Expected: the health payload's build sha matches `git rev-parse --short HEAD`.
If it does not, rebuild: a stale image is running the pre-local-backend code and
every local-mode dispatch will fail.

- [ ] **Step 3: Run the pilot**

Conditions A and B only, one worker, one seed:

```bash
AUTH_TOKEN=<your token> uv run python -m bench.runner \
  --sample bench/samples/lite-pilot-30.json \
  --conditions A,B \
  --worker local-openweight \
  --seeds 1 \
  --run-id pilot-01
```

- [ ] **Step 4: Grade the pilot**

```bash
uv run python -m bench.grade --run bench/.work/runs/pilot-01
```

- [ ] **Step 5: Check exit criterion 1, zero manual steps per task**

Read `bench/.work/runs/pilot-01/attempts.jsonl` and count rows whose `error`
field is non-null and describes an operator intervention (a wedged task, a
missing credential, a manual restart) rather than a genuine model failure.

Criterion: zero. If any row required a human to unstick it, fix the harness and
re-run. Record what broke and what fixed it in `docs/bench/pilot-notes.md`.

- [ ] **Step 6: Check exit criterion 2, grading matches a hand-checked subsample 10 out of 10**

Pick 10 attempts spanning both conditions and all three patch-size strata. For
each, extract the patch and read it against the instance's gold patch and its
`FAIL_TO_PASS` tests, then record your own verdict:

```bash
for id in $(python -c "import json,sys;print('\n'.join(r['instance_id'] for r in [json.loads(l) for l in open('bench/.work/runs/pilot-01/graded.jsonl')][:10]))"); do
  echo "=== $id ==="
  git -C "bench/.work/repos/$id.git" diff "$(cat bench/.work/repos/$id.git/praxis-bench-base)"...main
done
```

Criterion: your verdict agrees with the official grader on all 10. A single
disagreement means the patch extraction or the base commit is wrong; fix it
before any conclusion is drawn from any number.

- [ ] **Step 7: Check exit criterion 3, cost per task is measured**

Compute mean wall clock and mean tokens per attempt:

```bash
uv run python -c "
import json
rows=[json.loads(l) for l in open('bench/.work/runs/pilot-01/attempts.jsonl')]
n=len(rows)
print('attempts', n)
print('mean wall clock (s)', sum(r['wall_clock_s'] for r in rows)/n)
print('mean total tokens', sum(r['brain_tokens']+r['worker_tokens'] for r in rows)/n)
"
```

Criterion: the numbers are real, not zero. If token counts are zero the runner
is not capturing them; wire them from the orchestrator's task detail before
proceeding, because the full run cannot be budgeted without them.

- [ ] **Step 8: Write the pilot notes**

Create `docs/bench/pilot-notes.md` recording: the run id, the exact commit, what
broke and how it was fixed, the three criteria with their measured values, and
the projected cost of the full matrix (attempts times mean cost, where attempts
is 4 conditions times 2 workers times 2 seeds times the full sample size).

State plainly whether the projected full-run cost is affordable. If it is not,
say so and cut the sample size in `bench/config.FULL_PER_STRATUM` rather than
cutting conditions: the ablation is the point.

- [ ] **Step 9: Commit**

```bash
git add docs/bench/pilot-notes.md bench/README.md bench/.work/runs/pilot-01/attempts.jsonl
git commit -m "bench: run the 30-task pilot and record its exit criteria

Harness debugging only, no conclusions drawn. Records the measured cost
per task so the full matrix can be budgeted before it is started."
```

**Phase B is complete.** Continue directly to Phase C.

---

## Phase C: the full matrix and the report

### Task 13: The double-gated bench-mode verify-disable flag

**Files:**
- Create: `src/orchestrator/core/bench_mode.py`
- Modify: `src/orchestrator/core/orchestrator_review.py`
- Test: `tests/test_bench_mode.py`

**Depends on:** Task 12

- [ ] **Step 1: Write the failing test**

Create `tests/test_bench_mode.py`:

```python
"""The verify-gate kill switch must be unreachable in normal operation.

Two independent env vars must BOTH be set, and either alone is refused.
"""

import pytest

from orchestrator.core.bench_mode import verify_gate_disabled


@pytest.mark.unit
def test_no_env_means_the_gate_is_on(monkeypatch):
    monkeypatch.delenv("PRAXIS_BENCH", raising=False)
    monkeypatch.delenv("PRAXIS_BENCH_DISABLE_VERIFY", raising=False)
    assert verify_gate_disabled() is False


@pytest.mark.unit
def test_the_disable_flag_alone_is_refused(monkeypatch):
    monkeypatch.delenv("PRAXIS_BENCH", raising=False)
    monkeypatch.setenv("PRAXIS_BENCH_DISABLE_VERIFY", "1")
    assert verify_gate_disabled() is False


@pytest.mark.unit
def test_bench_mode_alone_does_not_disable_the_gate(monkeypatch):
    monkeypatch.setenv("PRAXIS_BENCH", "1")
    monkeypatch.delenv("PRAXIS_BENCH_DISABLE_VERIFY", raising=False)
    assert verify_gate_disabled() is False


@pytest.mark.unit
def test_both_flags_together_disable_the_gate(monkeypatch):
    monkeypatch.setenv("PRAXIS_BENCH", "1")
    monkeypatch.setenv("PRAXIS_BENCH_DISABLE_VERIFY", "1")
    assert verify_gate_disabled() is True


@pytest.mark.unit
@pytest.mark.parametrize("value", ["0", "", "true", "yes", "no"])
def test_only_the_literal_one_counts(value, monkeypatch):
    """A loose truthiness check is how a kill switch leaks into production."""
    monkeypatch.setenv("PRAXIS_BENCH", value)
    monkeypatch.setenv("PRAXIS_BENCH_DISABLE_VERIFY", value)
    assert verify_gate_disabled() is (value == "1")


@pytest.mark.unit
async def test_the_review_gate_still_runs_without_bench_mode(
    orchestrator_fixture, monkeypatch
):
    monkeypatch.delenv("PRAXIS_BENCH", raising=False)
    monkeypatch.delenv("PRAXIS_BENCH_DISABLE_VERIFY", raising=False)
    orch, task_id, project = orchestrator_fixture
    local = dict(project)
    local["verify_cmd"] = "exit 1"
    called: list[str] = []

    async def _fake_verify(checkout, cmd):
        called.append(cmd)
        return False, "boom"

    import orchestrator.core.orchestrator_review as review_module

    monkeypatch.setattr(review_module, "run_verify", _fake_verify)
    orch._git.clone_pr_head.side_effect = None
    orch._git.clone_pr_head.return_value = "/tmp/x"
    await orch.review_task(task_id, local)
    assert called == ["exit 1"]


@pytest.mark.unit
async def test_bench_mode_skips_the_review_gate(orchestrator_fixture, monkeypatch):
    monkeypatch.setenv("PRAXIS_BENCH", "1")
    monkeypatch.setenv("PRAXIS_BENCH_DISABLE_VERIFY", "1")
    orch, task_id, project = orchestrator_fixture
    local = dict(project)
    local["verify_cmd"] = "exit 1"
    called: list[str] = []

    async def _fake_verify(checkout, cmd):
        called.append(cmd)
        return False, "boom"

    import orchestrator.core.orchestrator_review as review_module

    monkeypatch.setattr(review_module, "run_verify", _fake_verify)
    orch._git.clone_pr_head.side_effect = None
    orch._git.clone_pr_head.return_value = "/tmp/x"
    await orch.review_task(task_id, local)
    assert called == []
```

Note the patch target: `run_verify` is patched on
`orchestrator.core.orchestrator_review`, the MIXIN module that calls it, not on
`core.orchestrator`.

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_bench_mode.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'orchestrator.core.bench_mode'`.

- [ ] **Step 3: Write the module**

Create `src/orchestrator/core/bench_mode.py`:

```python
"""Bench-only switches, double-gated so they cannot leak into normal operation.

Condition C of the benchmark is condition B with the per-leaf verify gate
disabled, which isolates whether the measured effect comes from decomposition
or from verification.  That is a genuinely dangerous switch: a Praxis running
without its verify gate merges unverified worker output.

So it takes TWO independent environment variables, both set to the literal
string "1".  A loose truthiness check (``bool(os.environ.get(...))``) is exactly
how a kill switch ends up live in production, so this module deliberately does
not use one.
"""

from __future__ import annotations

import logging
import os


logger = logging.getLogger(__name__)

_BENCH_MODE_ENV = "PRAXIS_BENCH"
_DISABLE_VERIFY_ENV = "PRAXIS_BENCH_DISABLE_VERIFY"
_ENABLED = "1"


def bench_mode() -> bool:
    """True only when ``PRAXIS_BENCH`` is exactly ``"1"``."""
    return os.environ.get(_BENCH_MODE_ENV) == _ENABLED


def verify_gate_disabled() -> bool:
    """True only when bench mode AND the explicit disable flag are both set.

    Returns False for every other combination, including the disable flag
    alone.  Logs loudly when it does return True: a run with no verify gate
    must never be mistaken for a normal one in the logs.
    """
    if not bench_mode():
        return False
    if os.environ.get(_DISABLE_VERIFY_ENV) != _ENABLED:
        return False
    logger.warning(
        "BENCH MODE: the mechanical verify gate is DISABLED. This is only "
        "valid for benchmark condition C and must never run in production."
    )
    return True
```

- [ ] **Step 4: Honour the flag in review**

In `src/orchestrator/core/orchestrator_review.py`, add the import:

```python
from orchestrator.core.bench_mode import verify_gate_disabled
```

and change the gate condition inside `review_task` from:

```python
            verify_cmd = project.get("verify_cmd")
            if verify_cmd and checkout is not None:
```

to:

```python
            # Bench condition C runs decomposition WITHOUT the verify gate, to
            # isolate whether the measured effect is decomposition or
            # verification. Double-gated; see core/bench_mode.py.
            verify_cmd = None if verify_gate_disabled() else project.get("verify_cmd")
            if verify_cmd and checkout is not None:
```

Apply the same guard in `DispatchMixin._wave_verify_gate` and
`ReviewMixin.on_plan_completed`, so a bench-mode run has no verify gate at any
level:

```python
        verify_cmd = None if verify_gate_disabled() else project.get("verify_cmd")
        if not verify_cmd:
            return True
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `uv run pytest tests/test_bench_mode.py tests/test_orchestrator.py -v`
Expected: PASS.

- [ ] **Step 6: Mutation-check the double gate**

Temporarily change `verify_gate_disabled` to skip the `bench_mode()` check.
Run: `uv run pytest tests/test_bench_mode.py::test_the_disable_flag_alone_is_refused -v`
Expected: FAIL. Restore and re-run to confirm PASS. Then temporarily change the
comparison to `bool(os.environ.get(_ENABLED_ENV))` truthiness.
Run: `uv run pytest tests/test_bench_mode.py -v -k literal_one`
Expected: FAIL on the `"true"`, `"yes"`, and `"no"` cases. Restore.

- [ ] **Step 7: Add the gotcha**

Append to `docs/gotchas.md`:

```markdown
- **The verify-gate kill switch is double-gated and literal** 
  `core/bench_mode.verify_gate_disabled()` returns True only when BOTH
  `PRAXIS_BENCH` and `PRAXIS_BENCH_DISABLE_VERIFY` equal the literal string
  `"1"`. Either alone is refused, and a truthiness check is deliberately NOT
  used: a loose check is how a kill switch ends up live. It exists solely for
  benchmark condition C (decomposition without verification), disables the
  per-task gate, the per-wave gate, and the whole-plan gate together, and logs a
  warning every time it fires so a gateless run is never mistaken for a normal
  one in the logs.
```

Add a matching one-line CLAUDE.md index entry.

- [ ] **Step 8: Commit**

```bash
git add src/orchestrator/core/bench_mode.py src/orchestrator/core/orchestrator_review.py src/orchestrator/core/orchestrator_dispatch.py tests/test_bench_mode.py docs/gotchas.md CLAUDE.md
git commit -m "feat(bench-mode): add the double-gated verify-gate disable for condition C

Both PRAXIS_BENCH and PRAXIS_BENCH_DISABLE_VERIFY must equal the literal
'1'; either alone is refused and a warning is logged whenever it fires."
```

---

### Task 14: Wire conditions C and D into the runner

**Files:**
- Modify: `bench/runner.py`
- Test: `tests/bench/test_runner_conditions.py`

**Depends on:** Task 13

- [ ] **Step 1: Write the failing test**

Create `tests/bench/test_runner_conditions.py`:

```python
"""Each condition must translate into the exact switches it declares."""

import pytest

from bench.config import CONDITIONS
from bench.runner import condition_env, condition_project_overrides


def _condition(key: str):
    return next(c for c in CONDITIONS if c.key == key)


@pytest.mark.unit
def test_condition_b_runs_with_the_verify_gate_and_no_bench_flags():
    env = condition_env(_condition("B"))
    assert "PRAXIS_BENCH_DISABLE_VERIFY" not in env
    assert condition_project_overrides(_condition("B"))["verify_cmd_enabled"] is True


@pytest.mark.unit
def test_condition_c_sets_both_bench_flags():
    env = condition_env(_condition("C"))
    assert env["PRAXIS_BENCH"] == "1"
    assert env["PRAXIS_BENCH_DISABLE_VERIFY"] == "1"


@pytest.mark.unit
def test_condition_a_also_runs_without_a_verify_gate():
    """A is C's matched baseline; both must be gateless."""
    assert condition_project_overrides(_condition("A"))["verify_cmd_enabled"] is False
    assert condition_project_overrides(_condition("C"))["verify_cmd_enabled"] is False


@pytest.mark.unit
def test_only_condition_d_enables_adaptive_split():
    for key in ("A", "B", "C"):
        assert condition_project_overrides(_condition(key))["adaptive_split"] is False
    assert condition_project_overrides(_condition("D"))["adaptive_split"] is True


@pytest.mark.unit
def test_condition_d_keeps_the_verify_gate():
    """Finer granularity must be paired with MORE verification, not less."""
    assert condition_project_overrides(_condition("D"))["verify_cmd_enabled"] is True


@pytest.mark.unit
def test_no_condition_env_leaks_a_bench_flag_when_the_gate_is_on():
    for key in ("B", "D"):
        assert condition_env(_condition(key)) == {}
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/bench/test_runner_conditions.py -v`
Expected: FAIL with `ImportError: cannot import name 'condition_env'`.

- [ ] **Step 3: Implement the two translators**

Add to `bench/runner.py`:

```python
def condition_env(condition: Condition) -> dict[str, str]:
    """Environment overrides the orchestrator needs for this condition.

    Only a gateless condition sets anything, and it sets BOTH flags: the
    orchestrator refuses either alone (see core/bench_mode.py).
    """
    if condition.verify_gate:
        return {}
    return {"PRAXIS_BENCH": "1", "PRAXIS_BENCH_DISABLE_VERIFY": "1"}


def condition_project_overrides(condition: Condition) -> dict[str, Any]:
    """Per-project switches for this condition, as plain data for the record."""
    return {
        "decompose": condition.decompose,
        "verify_cmd_enabled": condition.verify_gate,
        "adaptive_split": condition.adaptive_split,
    }
```

Adaptive split is controlled by the engine plan's triage path, which is always
on once merged. Conditions A, B, and C therefore disable it per project rather
than per process, via `max_retries=1`: a leaf never reaches the second
worker-attributable failure that triggers triage. `BenchClient.register_project`
already takes `adaptive_split` and sets `"max_retries": 3 if adaptive_split
else 1` (Task 11), so no client change is needed here; `condition_project_overrides`
only has to report the same fact as data.

Document the mechanism in `bench/README.md` under a "How each condition is
realized" heading, naming `max_retries=1` explicitly. A reader must be able to
see how each arm was implemented, not just what it claims to isolate.

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/bench/test_runner_conditions.py -v`
Expected: PASS (6 tests).

- [ ] **Step 5: Mutation-check the flag pairing**

Temporarily change `condition_env` to return only `{"PRAXIS_BENCH_DISABLE_VERIFY": "1"}`.
Run: `uv run pytest tests/bench/test_runner_conditions.py::test_condition_c_sets_both_bench_flags -v`
Expected: FAIL. Restore and re-run to confirm PASS.

- [ ] **Step 6: Commit**

```bash
git add bench/runner.py bench/README.md tests/bench/test_runner_conditions.py
git commit -m "feat(bench): realize conditions C and D in the runner

C and its matched baseline A both run gateless via the double-gated bench
flags; A, B, and C avoid adaptive split via max_retries=1, documented in
the bench README so the arm's mechanism is inspectable."
```

---

### Task 15: The analysis math

**Files:**
- Create: `bench/stats.py`
- Test: `tests/bench/test_stats.py`

**Depends on:** Task 10

- [ ] **Step 1: Write the failing test**

Create `tests/bench/test_stats.py`:

```python
"""Known-answer fixtures. These numbers appear in a published report."""

import pytest

from bench.stats import mcnemar_exact, wilson_interval


@pytest.mark.unit
def test_wilson_matches_a_published_worked_example():
    """R binom.test/Hmisc binconf: 8/10 at 95 percent is (0.4901, 0.9427)."""
    low, high = wilson_interval(successes=8, trials=10, confidence=0.95)
    assert low == pytest.approx(0.4901, abs=5e-4)
    assert high == pytest.approx(0.9427, abs=5e-4)


@pytest.mark.unit
def test_wilson_at_zero_successes_has_a_zero_lower_bound():
    low, high = wilson_interval(successes=0, trials=20, confidence=0.95)
    assert low == pytest.approx(0.0, abs=1e-9)
    assert 0.0 < high < 0.2


@pytest.mark.unit
def test_wilson_at_all_successes_has_a_one_upper_bound():
    low, high = wilson_interval(successes=20, trials=20, confidence=0.95)
    assert high == pytest.approx(1.0, abs=1e-9)
    assert 0.8 < low < 1.0


@pytest.mark.unit
def test_wilson_narrows_as_the_sample_grows():
    narrow = wilson_interval(80, 100, 0.95)
    wide = wilson_interval(8, 10, 0.95)
    assert (narrow[1] - narrow[0]) < (wide[1] - wide[0])


@pytest.mark.unit
def test_wilson_on_zero_trials_returns_the_whole_interval():
    assert wilson_interval(0, 0, 0.95) == (0.0, 1.0)


@pytest.mark.unit
def test_mcnemar_with_no_discordant_pairs_is_not_significant():
    """b = c = 0: nothing changed, so there is nothing to detect."""
    assert mcnemar_exact(b=0, c=0) == pytest.approx(1.0)


@pytest.mark.unit
def test_mcnemar_symmetric_discordance_is_not_significant():
    assert mcnemar_exact(b=5, c=5) == pytest.approx(1.0)


@pytest.mark.unit
def test_mcnemar_matches_a_worked_example():
    """Exact binomial two-sided, b=1, c=9: p = 2 * sum_{k<=1} C(10,k) / 2^10."""
    assert mcnemar_exact(b=1, c=9) == pytest.approx(2 * 11 / 1024, abs=1e-9)


@pytest.mark.unit
def test_mcnemar_is_symmetric_in_its_arguments():
    assert mcnemar_exact(b=2, c=8) == pytest.approx(mcnemar_exact(b=8, c=2))


@pytest.mark.unit
def test_mcnemar_p_value_never_exceeds_one():
    for b, c in [(0, 1), (1, 1), (3, 4), (10, 11)]:
        assert 0.0 <= mcnemar_exact(b=b, c=c) <= 1.0


@pytest.mark.unit
def test_strong_discordance_is_significant_at_five_percent():
    assert mcnemar_exact(b=0, c=10) < 0.05
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/bench/test_stats.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'bench.stats'`.

- [ ] **Step 3: Write the module**

Create `bench/stats.py`:

```python
"""The two statistics the report is allowed to use.

Wilson score intervals for per-stratum resolve rates, because a normal-
approximation interval is wrong at the small n and extreme proportions this
benchmark produces.  Exact McNemar for the paired A-versus-B and B-versus-C
comparisons, because the design is within-subject: the same instances run under
both arms, so the unpaired chi-square is the wrong test.

No scipy dependency: both are short and this package must stay installable with
nothing beyond the project's existing deps.
"""

from __future__ import annotations

import math


# Two-sided normal quantiles for the confidence levels the report uses.
_Z = {0.90: 1.6448536269514722, 0.95: 1.959963984540054, 0.99: 2.5758293035489004}


def wilson_interval(
    successes: int, trials: int, confidence: float = 0.95
) -> tuple[float, float]:
    """Return the Wilson score interval for a binomial proportion.

    Args:
        successes: Number of resolved instances.
        trials: Number of attempts in the cell.
        confidence: Two-sided confidence level; 0.90, 0.95, or 0.99.

    Returns:
        ``(low, high)``, clamped to [0, 1].  An empty cell returns ``(0.0, 1.0)``:
        no evidence, not a point estimate of zero.
    """
    if trials <= 0:
        return (0.0, 1.0)
    z = _Z.get(confidence)
    if z is None:
        message = f"unsupported confidence level {confidence}; use 0.90, 0.95, or 0.99"
        raise ValueError(message)

    n = float(trials)
    phat = successes / n
    denominator = 1.0 + z * z / n
    centre = (phat + z * z / (2.0 * n)) / denominator
    margin = (
        z
        * math.sqrt(phat * (1.0 - phat) / n + z * z / (4.0 * n * n))
        / denominator
    )
    return (max(0.0, centre - margin), min(1.0, centre + margin))


def mcnemar_exact(b: int, c: int) -> float:
    """Return the two-sided exact-binomial McNemar p-value.

    Args:
        b: Instances resolved by arm 1 but not arm 2.
        c: Instances resolved by arm 2 but not arm 1.

    Returns:
        The p-value in [0, 1].  With no discordant pairs the result is 1.0:
        the arms are indistinguishable on this sample, which is a real finding
        and not an error.

    The exact test is used rather than the chi-square approximation because the
    discordant counts here are small, which is exactly where the approximation
    is unreliable.
    """
    n = b + c
    if n == 0:
        return 1.0
    k = min(b, c)
    tail = sum(math.comb(n, i) for i in range(k + 1)) / (2.0**n)
    return min(1.0, 2.0 * tail)


def resolve_rate(successes: int, trials: int) -> float:
    """Point estimate of the resolve rate; 0.0 for an empty cell."""
    return successes / trials if trials else 0.0
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/bench/test_stats.py -v`
Expected: PASS (11 tests). If the Wilson known-answer test misses, the bug is in
the module, not the fixture: the values come from a standard implementation.

- [ ] **Step 5: Mutation-check the paired test**

Temporarily change `tail = sum(...)` to use `range(k)` instead of `range(k + 1)`.
Run: `uv run pytest tests/bench/test_stats.py::test_mcnemar_matches_a_worked_example -v`
Expected: FAIL. Restore and re-run to confirm PASS.

- [ ] **Step 6: Mutation-check the empty-cell rule**

Temporarily change `if trials <= 0: return (0.0, 1.0)` to `return (0.0, 0.0)`.
Run: `uv run pytest tests/bench/test_stats.py::test_wilson_on_zero_trials_returns_the_whole_interval -v`
Expected: FAIL. Restore and re-run to confirm PASS. An empty cell is no
evidence, not a measured zero, and reporting it as zero is a real way to publish
a false claim.

- [ ] **Step 7: Commit**

```bash
git add bench/stats.py tests/bench/test_stats.py
git commit -m "feat(bench): add Wilson intervals and exact paired McNemar

Known-answer fixtures for both. Wilson because the normal approximation is
wrong at this n; exact McNemar because the design is within-subject and the
discordant counts are small."
```

---

### Task 16: The report renderer with mandatory honesty sections

**Files:**
- Create: `bench/templates/report.md.tmpl`
- Create: `bench/report.py`
- Test: `tests/bench/test_report.py`

**Depends on:** Task 15

- [ ] **Step 1: Write the failing test**

Create `tests/bench/test_report.py`:

```python
"""The report template's caveats are structural, not a matter of remembering."""

import json

import pytest

from bench.report import build_report, per_stratum_table


def _row(**overrides) -> dict:
    base = {
        "run_id": "full-01",
        "instance_id": "a__b-1",
        "condition": "B",
        "worker": "local-openweight",
        "seed": 1,
        "stratum_patch": "medium",
        "stratum_repo": "mid",
        "resolved": True,
        "plausible": True,
        "leaf_count": 3,
        "leaf_retries": 0,
        "whole_task_retries": 0,
        "clarifications": 0,
        "human_gate_touches": 0,
        "brain_tokens": 1000,
        "worker_tokens": 4000,
        "wall_clock_s": 100.0,
        "error": None,
    }
    base.update(overrides)
    return base


@pytest.mark.unit
def test_per_stratum_table_groups_by_cell_and_condition():
    rows = [
        _row(condition="A", resolved=False),
        _row(condition="B", resolved=True),
        _row(condition="B", instance_id="a__b-2", resolved=False),
    ]
    table = per_stratum_table(rows)
    key = ("medium", "mid", "B")
    assert table[key]["trials"] == 2
    assert table[key]["resolved"] == 1
    assert 0.0 < table[key]["ci_low"] < table[key]["rate"] < table[key]["ci_high"] < 1.0


@pytest.mark.unit
def test_errored_rows_count_in_the_denominator():
    """Dropping a crashed attempt inflates the rate. It stays in trials."""
    rows = [_row(resolved=True), _row(instance_id="a__b-2", resolved=False, error="timeout")]
    table = per_stratum_table(rows)
    assert table[("medium", "mid", "B")]["trials"] == 2


@pytest.mark.unit
def test_cost_per_resolved_task_is_reported_not_cost_per_attempt():
    rows = [
        _row(resolved=True, brain_tokens=1000, worker_tokens=1000),
        _row(instance_id="a__b-2", resolved=False, brain_tokens=1000, worker_tokens=1000),
    ]
    table = per_stratum_table(rows)
    cell = table[("medium", "mid", "B")]
    # 4000 tokens spent, 1 resolved.
    assert cell["tokens_per_resolved"] == pytest.approx(4000.0)


@pytest.mark.unit
def test_cost_per_resolved_is_infinite_when_nothing_resolved():
    """Reporting 0 there would read as free, which is the opposite of true."""
    rows = [_row(resolved=False, brain_tokens=1000, worker_tokens=1000)]
    cell = per_stratum_table(rows)[("medium", "mid", "B")]
    assert cell["tokens_per_resolved"] == float("inf")


@pytest.mark.unit
def test_the_report_contains_every_mandatory_honesty_section(tmp_path):
    rows = [_row(condition="A", resolved=False), _row(condition="B", resolved=True)]
    path = tmp_path / "rows.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    report = build_report(rows, run_id="full-01", model_cutoff="2026-03")
    for heading in (
        "Contamination",
        "Correlational anchors",
        "Failure-class analysis",
        "Predicted shape",
    ):
        assert heading in report


@pytest.mark.unit
def test_the_report_names_the_model_cutoff_in_the_contamination_note():
    report = build_report([_row()], run_id="full-01", model_cutoff="2026-03")
    assert "2026-03" in report
    assert "SWE-rebench" in report


@pytest.mark.unit
def test_the_report_states_the_predicted_shape_up_front():
    """Stating the expectation before the numbers is what stops post-hoc storytelling."""
    report = build_report([_row()], run_id="full-01", model_cutoff="2026-03")
    assert "mid-difficulty" in report


@pytest.mark.unit
def test_the_report_reports_the_plausible_but_wrong_rate():
    rows = [_row(resolved=False, plausible=True), _row(instance_id="a__b-2")]
    report = build_report(rows, run_id="full-01", model_cutoff="2026-03")
    assert "plausible" in report.lower()


@pytest.mark.unit
def test_the_report_renders_the_ab_and_bc_mcnemar_p_values():
    rows = [
        _row(condition="A", instance_id=f"i{i}", resolved=False) for i in range(5)
    ] + [_row(condition="B", instance_id=f"i{i}", resolved=True) for i in range(5)]
    report = build_report(rows, run_id="full-01", model_cutoff="2026-03")
    assert "A vs B" in report
    assert "p =" in report
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/bench/test_report.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'bench.report'`.

- [ ] **Step 3: Write the template**

Create `bench/templates/report.md.tmpl`. The four caveat headings are part of the
template, so a report physically cannot be produced without them:

````markdown
# Praxis decomposition benchmark: {run_id}

Generated from `{rows}` attempts. Raw JSONL is committed alongside this report.

## Predicted shape (stated before the numbers)

The expected effect concentrates in the **mid-difficulty** stratum. At the small
end a monolithic attempt already succeeds, so there is nothing for decomposition
to add; at the large end the task is beyond the worker whatever its shape. A
near-zero effect at both extremes is the predicted and honest result, not a
disappointment. This paragraph was written before the run.

## Design

| Condition | What runs | Isolates |
|-----------|-----------|----------|
| A | monolithic, no verify gate | baseline |
| B | Praxis decomposition, verify gate on | decomposition |
| C | decomposition, verify gate off | decomposition versus verification |
| D | decomposition plus adaptive split | the adaptive policy delta |

A and C are a matched gateless pair; the runner refuses to run C without A.

## Per-stratum resolve rates

{stratum_table}

Intervals are Wilson score intervals at 95 percent. An empty cell is reported as
`(0.00, 1.00)`: no evidence, not a measured zero.

## Paired comparisons

{mcnemar_table}

Exact binomial McNemar on the discordant pairs. The design is within-subject, so
the unpaired chi-square would be the wrong test.

## Cost

{cost_table}

Cost is reported per RESOLVED task, not per attempt. A condition that resolves
nothing shows an infinite cost, because reporting zero there would read as free.

## Plausible but wrong

{plausible_table}

Patches that applied and built but did not resolve the issue. AutoCodeRover
(arXiv 2404.05427) found 35 percent of plausible patches wrong; a worker gaming
surface checks shows up here first.

## Contamination

The worker models used were {workers}, with a training cutoff of {model_cutoff}.
SWE-bench instances are drawn from public repositories that predate that cutoff,
so memorization cannot be excluded. Treat every absolute number as an upper
bound. The decontaminated alternative is **SWE-rebench**; a robustness check on
its subset is the correct follow-up and has not been run here unless stated
below.

## Correlational anchors

The stratum boundaries come from SWE-bench Goes Live! (arXiv 2505.23419), which
reports correlations between gold-patch shape and resolve rate. They are not
causal claims and the strata inherit that limitation. The comparisons WITHIN a
stratum are the causal part of this design; the boundaries themselves are not.

## Failure-class analysis

{failure_analysis}

Ten failures were hand-inspected and classified plan-shaped (the decomposition
was wrong) versus execution-shaped (the decomposition was fine and the worker
failed anyway). Hierarchical failure analysis (arXiv 2603.14248) finds that
decomposition only fixes the former, so this classification, not the headline
number, is what says whether more decomposition work is worth doing.

## Limitations

{limitations}
````

- [ ] **Step 4: Write the renderer**

Create `bench/report.py` implementing `per_stratum_table(rows)` and
`build_report(rows, run_id, model_cutoff)`:

```python
"""Render a bench run into the committed report.

Every caveat section in the template is mandatory by construction: the renderer
fills placeholders in a template that already contains the headings, so a report
without them is not producible.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

from bench.stats import mcnemar_exact, resolve_rate, wilson_interval


logger = logging.getLogger(__name__)

_TEMPLATE = Path(__file__).parent / "templates" / "report.md.tmpl"


def per_stratum_table(rows: list[dict[str, Any]]) -> dict[tuple[str, str, str], dict]:
    """Aggregate attempts into (patch stratum, repo stratum, condition) cells.

    Errored attempts stay in the denominator: dropping a crashed attempt
    inflates the resolve rate, which is the easiest way to publish a wrong
    number without lying on purpose.
    """
    cells: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        cells[
            (row["stratum_patch"], row["stratum_repo"], row["condition"])
        ].append(row)

    table: dict[tuple[str, str, str], dict] = {}
    for key, group in cells.items():
        trials = len(group)
        resolved = sum(1 for r in group if r["resolved"])
        tokens = sum(r["brain_tokens"] + r["worker_tokens"] for r in group)
        low, high = wilson_interval(resolved, trials, 0.95)
        table[key] = {
            "trials": trials,
            "resolved": resolved,
            "rate": resolve_rate(resolved, trials),
            "ci_low": low,
            "ci_high": high,
            "tokens": tokens,
            "tokens_per_resolved": (
                tokens / resolved if resolved else float("inf")
            ),
            "plausible_but_wrong": sum(
                1 for r in group if r["plausible"] and not r["resolved"]
            ),
        }
    return table


def paired_comparison(
    rows: list[dict[str, Any]], arm_one: str, arm_two: str
) -> tuple[int, int, float]:
    """Return ``(b, c, p)`` for a within-subject comparison of two conditions."""
    by_instance: dict[str, dict[str, bool]] = defaultdict(dict)
    for row in rows:
        by_instance[row["instance_id"]][row["condition"]] = bool(row["resolved"])
    b = c = 0
    for outcomes in by_instance.values():
        if arm_one not in outcomes or arm_two not in outcomes:
            continue
        if outcomes[arm_one] and not outcomes[arm_two]:
            b += 1
        elif outcomes[arm_two] and not outcomes[arm_one]:
            c += 1
    return b, c, mcnemar_exact(b=b, c=c)


def build_report(
    rows: list[dict[str, Any]], run_id: str, model_cutoff: str
) -> str:
    """Render the full report markdown."""
    table = per_stratum_table(rows)

    stratum_lines = [
        "| patch stratum | repo stratum | condition | n | resolved | rate | 95% CI |",
        "|---|---|---|---|---|---|---|",
    ]
    for (patch, repo, condition) in sorted(table):
        cell = table[(patch, repo, condition)]
        stratum_lines.append(
            f"| {patch} | {repo} | {condition} | {cell['trials']} | "
            f"{cell['resolved']} | {cell['rate']:.2f} | "
            f"({cell['ci_low']:.2f}, {cell['ci_high']:.2f}) |"
        )

    mcnemar_lines = ["| comparison | b | c | p |", "|---|---|---|---|"]
    for one, two in (("A", "B"), ("B", "C"), ("B", "D")):
        b, c, p = paired_comparison(rows, one, two)
        mcnemar_lines.append(f"| {one} vs {two} | {b} | {c} | p = {p:.4f} |")

    cost_lines = ["| condition | tokens | resolved | tokens per resolved |", "|---|---|---|---|"]
    by_condition: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_condition[row["condition"]].append(row)
    for condition in sorted(by_condition):
        group = by_condition[condition]
        tokens = sum(r["brain_tokens"] + r["worker_tokens"] for r in group)
        resolved = sum(1 for r in group if r["resolved"])
        per = f"{tokens / resolved:.0f}" if resolved else "infinite (nothing resolved)"
        cost_lines.append(f"| {condition} | {tokens} | {resolved} | {per} |")

    plausible_lines = ["| condition | plausible but wrong | n |", "|---|---|---|"]
    for condition in sorted(by_condition):
        group = by_condition[condition]
        wrong = sum(1 for r in group if r["plausible"] and not r["resolved"])
        plausible_lines.append(f"| {condition} | {wrong} | {len(group)} |")

    workers = ", ".join(sorted({r["worker"] for r in rows})) or "unknown"

    return _TEMPLATE.read_text(encoding="utf-8").format(
        run_id=run_id,
        rows=len(rows),
        stratum_table="\n".join(stratum_lines),
        mcnemar_table="\n".join(mcnemar_lines),
        cost_table="\n".join(cost_lines),
        plausible_table="\n".join(plausible_lines),
        workers=workers,
        model_cutoff=model_cutoff,
        failure_analysis="TO BE FILLED BY HAND: see Task 17 step 4.",
        limitations="TO BE FILLED BY HAND: see Task 17 step 5.",
    )


def main(argv: list[str] | None = None) -> int:
    """CLI: render a graded run into docs/bench/."""
    parser = argparse.ArgumentParser(description="Render a bench report")
    parser.add_argument("--run", required=True)
    parser.add_argument("--model-cutoff", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    graded = Path(args.run) / "graded.jsonl"
    rows = [
        json.loads(line)
        for line in graded.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(
        build_report(rows, Path(args.run).name, args.model_cutoff), encoding="utf-8"
    )
    logger.info("wrote %s from %d rows", args.out, len(rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `uv run pytest tests/bench/test_report.py -v`
Expected: PASS (9 tests).

- [ ] **Step 6: Mutation-check the honesty sections**

Temporarily delete the `## Contamination` heading from the template.
Run: `uv run pytest tests/bench/test_report.py::test_the_report_contains_every_mandatory_honesty_section -v`
Expected: FAIL. Restore and re-run to confirm PASS.

- [ ] **Step 7: Mutation-check the infinite-cost rule**

Temporarily change `tokens / resolved if resolved else float("inf")` to
`tokens / resolved if resolved else 0.0`.
Run: `uv run pytest tests/bench/test_report.py::test_cost_per_resolved_is_infinite_when_nothing_resolved -v`
Expected: FAIL. Restore and re-run to confirm PASS.

- [ ] **Step 8: Commit**

```bash
git add bench/report.py bench/templates/ tests/bench/test_report.py
git commit -m "feat(bench): render the report with structurally mandatory caveats

The contamination note, the correlational-anchor caveat, the predicted
shape, and the failure-class analysis are headings in the template, so a
report without them is not producible. Errored attempts stay in the
denominator and a zero-resolve condition reports infinite cost."
```

---

### Task 17: Run the full matrix and publish

**Files:**
- Create: `docs/bench/2026-XX-XX-report.md`
- Create: `docs/bench/raw/<run-id>-graded.jsonl`
- Modify: `README.md`

**Depends on:** Task 14, Task 16

- [ ] **Step 1: Draw and prepare the full sample**

```bash
uv run python -m bench.sample \
  --pool bench/.work/pool-verified.json \
  --out bench/samples/verified-full.json \
  --per-stratum 16 \
  --corpus swe-bench-verified
uv run python -m bench.prepare --sample bench/samples/verified-full.json
```

If the pilot's measured cost makes this unaffordable, reduce `--per-stratum`
rather than dropping a condition. The ablation is the point; a smaller n with
all four arms is a real result, three arms at full n is not.

- [ ] **Step 2: Run the matrix**

Four conditions, two workers, two seeds:

```bash
AUTH_TOKEN=<token> uv run python -m bench.runner \
  --sample bench/samples/verified-full.json \
  --conditions A,B,C,D \
  --worker local-openweight,hosted-flash \
  --seeds 1,2 \
  --run-id full-01
```

The runner will refuse to start if `C` is requested without `A`. That is
intentional.

- [ ] **Step 3: Grade and render**

```bash
uv run python -m bench.grade --run bench/.work/runs/full-01
uv run python -m bench.report \
  --run bench/.work/runs/full-01 \
  --model-cutoff "<the worker models' training cutoff>" \
  --out docs/bench/$(date +%Y-%m-%d)-report.md
```

- [ ] **Step 4: Hand-classify ten failures**

Pick 10 failed attempts spanning both workers and all three patch strata. For
each, read the decomposition and the diff and classify it:

- **plan-shaped**: the decomposition was wrong (a leaf was mis-scoped, a
  dependency was missed, the contract was incomplete). Decomposition work fixes
  these.
- **execution-shaped**: the decomposition was correct and the worker still
  failed. More decomposition does not fix these.

Replace the `TO BE FILLED BY HAND` placeholder in the `Failure-class analysis`
section with a table of the 10, their classification, and one sentence each.
Then state plainly which class dominates. Per arXiv 2603.14248 this, not the
headline number, is what says whether further decomposition work is worth doing.

- [ ] **Step 5: Write the limitations section**

Replace the second `TO BE FILLED BY HAND` with the honest list: the sample size
and its power, the single-machine single-operator setup, any instances that
errored and why, the fact that a Praxis run and a bare-model run differ in more
than decomposition (Praxis also reviews and gates), and anything you had to
change mid-run.

If any condition could not be completed, say which and why, in this section, at
the top.

- [ ] **Step 6: Commit the raw data alongside the report**

```bash
mkdir -p docs/bench/raw
cp bench/.work/runs/full-01/graded.jsonl docs/bench/raw/full-01-graded.jsonl
cp bench/.work/runs/full-01/attempts.jsonl docs/bench/raw/full-01-attempts.jsonl
git add docs/bench/ bench/samples/verified-full.json
git commit -m "bench: publish the full stratified decomposition report

Four conditions, two workers, two seeds, per-stratum Wilson intervals and
paired McNemar. Raw JSONL committed. Ten failures hand-classified
plan-shaped versus execution-shaped."
```

- [ ] **Step 7: Feed the outcomes back into calibration**

The run's terminal verdicts are already in `task_outcomes` (every review wrote
one). Confirm:

```bash
docker exec orchestrator python -c "
import sqlite3
c = sqlite3.connect('data/orchestrator.db')
print(c.execute('SELECT model_name, outcome, COUNT(*) FROM task_outcomes GROUP BY 1,2').fetchall())
"
```

Expected: rows for both worker models with both `pass` and `fail` outcomes. This
is the calibration loop's training data, not just report data. If the table is
empty, outcome recording did not fire during the run and the report should say
so in its limitations section.

- [ ] **Step 8: Link the report from the README**

Add one line to `README.md`:

```markdown
- **Does it actually work?** [`docs/bench/<date>-report.md`](docs/bench/): a stratified SWE-bench evaluation with a decomposition-versus-verification ablation, raw data committed.
```

If the result is null or negative, link it anyway and say so in the link text. A
rigorous null result plus the engineering is still the artifact; hiding it is
the only outcome that would not be.

- [ ] **Step 9: Commit**

```bash
git add README.md
git commit -m "docs: link the benchmark report from the README"
```

---

## Parallel Execution Map

- **Wave 1:** Task 1 (`Depends on: None`)
- **Wave 2:** Task 2 (Task 1), Task 3 (Task 1), Task 4 (Task 1)
- **Wave 3:** Task 5 (Task 4)
- **Wave 4:** Task 6 (Tasks 2, 3, 4, 5), Phase A gate
- **Wave 5:** Task 7 (Task 6)
- **Wave 6:** Task 8 (Task 7), Task 9 (Task 7), Task 10 (Task 7)
- **Wave 7:** Task 11 (Tasks 8, 9, 10), Task 15 (Task 10)
- **Wave 8:** Task 12 (Task 11), pilot gate
- **Wave 9:** Task 13 (Task 12), Task 16 (Task 15)
- **Wave 10:** Task 14 (Task 13)
- **Wave 11:** Task 17 (Tasks 14, 16)

Tasks 2, 3, and 4 touch disjoint modules (review, preflight, agent manager) and
all depend only on the backend seam, so they parallelize. Task 15 (the stats
math) is pure and depends only on the metrics schema, so it can run alongside
the runner work.

## Definition of done for this plan

Mapped from the umbrella spec's section 10 item 4:

`docs/bench/<date>-report.md` is published with per-stratum Wilson intervals, the
A/B/C ablation, both workers, and raw JSONL committed. Plus the prerequisite the
report needed: a local git backend that lets anyone evaluate Praxis without a
GitHub credential, documented as evaluation mode with GitHub remaining the
recommendation for real work.

A null or negative result satisfies this definition. An unpublished result does
not.

