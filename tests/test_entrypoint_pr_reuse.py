"""A worker may reuse only an OPEN PR for its own (head, base) pair.

The defect this pins, found live in walkthrough #6: both entrypoints looked a
PR up with ``gh pr view "${BRANCH}"``, which resolves a branch to a PR
REGARDLESS of state. Two plans built from the same spec text get the same
slugs, so the second reuses the first's ``agent/<task-slug>``; the lookup then
handed back the FIRST plan's already-merged PR and this run's real commit was
attached to it. Every layer downstream reported success on a change that was
not there: the review fetched the old merged diff and passed, ``merge_pr`` saw
an already-merged PR and treated that as success, and the task was marked
MERGED while its commit never reached the plan branch.

It fails silently in the worst way, so it is tested by EXECUTION, not by grep.
A static check that ``--state open`` appears near ``gh pr`` cannot tell a real
lookup from the string sitting in the comment beside it, and says nothing about
which URL ends up in ``PR_URL``. The tests below slice the real PR block out of
each shipped entrypoint and run it under bash with ``gh`` replaced by a spy.

The spy is a FAKE, not a stub: it holds one fixture PR with a state, a head and
a base, and answers ``gh pr list`` only when every flag the query carries agrees
with it, while ``gh pr view <branch>`` matches on head alone and ignores state,
exactly as the real thing does. That is what makes each of the three flags
independently load-bearing, so no two of them collapse into one guard:

- drop ``--state open`` and the merged-PR case reuses a landed diff,
- drop ``--base`` and a task reuses its own branch's PR against a PREVIOUS
  plan branch, pointing this review at the wrong base,
- drop ``--head`` and a task reuses a SIBLING task's open PR against the same
  plan branch, reviewing someone else's work as its own.

Each of those has its own test below, and each fails for its own reason.
"""

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.test_entrypoint_local_backend import (
    ENTRYPOINTS,
    _find,
    _slice_function,
    _to_posix,
)


_TEST_BRANCH = "agent/implement-format-duration"
_TEST_BASE = "plan/2026-08-21-format-duration"

# The plan branch of the PREVIOUS plan built from the same spec text. Same
# slugs, one day earlier, which is the collision that made the defect reachable.
_PREVIOUS_BASE = "plan/2026-08-20-format-duration"
# A sibling leaf of the SAME plan: different head, same base.
_SIBLING_BRANCH = "agent/test-format-duration"

_FIXTURE_PR = "https://github.test/owner/repo/pull/49"
_CREATED_PR = "https://github.test/owner/repo/pull/72"

_SPY_GH = """\
#!/usr/bin/env bash
printf '%s\\n' "gh $*" >> "$PRAXIS_SPY_LOG"
case "${1:-} ${2:-}" in
    "pr view")
        # The real `gh pr view <branch>` resolves a branch to its PR and has no
        # state filter at all. Modelled faithfully, including that it ignores
        # state, because that is precisely the defect.
        if [ -n "${PRAXIS_PR_URL:-}" ] && [ "${3:-}" = "${PRAXIS_PR_HEAD}" ]; then
            printf '%s\\n' "${PRAXIS_PR_URL}"
            exit 0
        fi
        exit 1
        ;;
    "pr list")
        # Absent flags do not filter, exactly like gh, EXCEPT that an absent
        # --state means gh's own default of open rather than "any".
        q_state="open"
        q_head=""
        q_base=""
        while [ "$#" -gt 0 ]; do
            case "$1" in
                --state) q_state="${2:-}"; shift ;;
                --head)  q_head="${2:-}";  shift ;;
                --base)  q_base="${2:-}";  shift ;;
            esac
            shift
        done
        if [ -z "${PRAXIS_PR_URL:-}" ]; then exit 0; fi
        if [ "${q_state}" != "all" ] && [ "${q_state}" != "${PRAXIS_PR_STATE}" ]; then
            exit 0
        fi
        if [ -n "${q_head}" ] && [ "${q_head}" != "${PRAXIS_PR_HEAD}" ]; then exit 0; fi
        if [ -n "${q_base}" ] && [ "${q_base}" != "${PRAXIS_PR_BASE}" ]; then exit 0; fi
        # A match prints the URL. No match prints NOTHING and still exits 0,
        # which is why the emptiness of the output has to be the signal.
        printf '%s\\n' "${PRAXIS_PR_URL}"
        exit 0
        ;;
    "pr create")
        printf '%s\\n' "${PRAXIS_CREATED_PR}"
        exit 0
        ;;
esac
exit 0
"""

_SPY_GIT = """\
#!/usr/bin/env bash
printf '%s\\n' "git $*" >> "$PRAXIS_SPY_LOG"
exit 0
"""

_PREAMBLE = f"""\
set -euo pipefail
BRANCH="{_TEST_BRANCH}"
BASE_BRANCH="{_TEST_BASE}"
PR_URL=""
MODEL="a-test-model"
GH_TOKEN="placeholder-token"
TASK_SUMMARY="a task summary"
IS_LOCAL_BACKEND=0
"""

_POSTAMBLE = """
printf '__PRAXIS_PR_URL__=%s\\n' "${PR_URL}"
"""


def _pr_block(path: Path) -> str:
    """Slice the shipped PR block, from its backend guard to the final banner.

    Anchored on the completion banner rather than on brace matching for the
    reason ``test_entrypoint_local_backend`` records: closing the guard early
    would make a brace-matched slice stop short and quietly drop the very gh
    calls under test.
    """
    source = path.read_text(encoding="utf-8")
    lines = source.splitlines()
    local_assign = _find(
        lines,
        lambda ln: 'PR_URL="praxis-local://pr' in ln,
        "the local PR_URL assignment",
    )
    guard = max(i for i in range(local_assign) if lines[i].startswith("if "))
    banner = _find(lines, lambda ln: "completed ===" in ln, "the completion banner")
    return _slice_function(source, "url_encode") + "\n" + "\n".join(lines[guard:banner])


def _run_pr_block(
    path: Path,
    tmp_path: Path,
    *,
    pr_url: str = "",
    pr_state: str = "open",
    pr_head: str = _TEST_BRANCH,
    pr_base: str = _TEST_BASE,
) -> tuple[subprocess.CompletedProcess, list[str], str | None]:
    """Execute the real PR block against one fixture PR; return (result, calls, url)."""
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash is not available on this host")

    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "spy.log"
    for name, body in (("gh", _SPY_GH), ("git", _SPY_GIT)):
        spy = bindir / name
        # newline="\n": a CRLF shebang makes the spy unrunnable and the failure
        # reads as the entrypoint's fault rather than the harness's.
        spy.write_text(body, encoding="utf-8", newline="\n")
        spy.chmod(0o755)

    env = {**os.environ}
    env["PATH"] = f"{bindir}{os.pathsep}{env.get('PATH', '')}"
    env["PRAXIS_SPY_LOG"] = log.as_posix()
    env["PRAXIS_PR_URL"] = pr_url
    env["PRAXIS_PR_STATE"] = pr_state
    env["PRAXIS_PR_HEAD"] = pr_head
    env["PRAXIS_PR_BASE"] = pr_base
    env["PRAXIS_CREATED_PR"] = _CREATED_PR
    env.pop("GIT_BACKEND", None)

    # Re-export PATH inside the script too: Git for Windows' bash rewrites PATH
    # at startup and would otherwise shadow the spy with the real git.
    spy_path_line = f'export PATH="{_to_posix(bindir)}:$PATH"\n'
    script = spy_path_line + _PREAMBLE + _pr_block(path) + _POSTAMBLE

    result = subprocess.run(
        [bash, "-c", script],
        capture_output=True,
        text=True,
        env=env,
    )
    calls = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
    urls = re.findall(r"^__PRAXIS_PR_URL__=(.*)$", result.stdout, re.MULTILINE)
    return result, calls, (urls[-1] if urls else None)


def _created(calls: list[str]) -> bool:
    return any(c.startswith("gh pr create") for c in calls)


@pytest.mark.integration
@pytest.mark.parametrize("path", ENTRYPOINTS, ids=lambda p: p.parent.name)
def test_a_merged_pr_on_the_same_branch_is_never_reused(path, tmp_path):
    """The walkthrough-#6 defect itself, reproduced end to end.

    The previous plan's PR is MERGED and sits on this exact (head, base) pair.
    Both names collide because the plan branch is ``plan/{date}-{plan_slug}``
    and the agent branch is ``agent/{task_slug}``: re-submit the same spec on
    the same day and every name repeats. Its diff has already landed, so
    attaching this run's commit to it reports a merge that moved nothing.

    Head AND base match deliberately. Giving the fixture a different base would
    let ``--base`` alone carry this test, and a mutation to ``--state all``
    would survive it; that mutation was measured surviving before this fixture
    was tightened.
    """
    result, calls, url = _run_pr_block(
        path,
        tmp_path,
        pr_url=_FIXTURE_PR,
        pr_state="merged",
        pr_head=_TEST_BRANCH,
        pr_base=_TEST_BASE,
    )

    assert result.returncode == 0, result.stderr
    assert url != _FIXTURE_PR, (
        f"the worker attached its commit to an already-merged PR; calls={calls}"
    )
    assert url == _CREATED_PR, f"expected a freshly created PR, got {url!r}"
    assert _created(calls), f"a new PR must be created when none is open: {calls}"


@pytest.mark.integration
@pytest.mark.parametrize("path", ENTRYPOINTS, ids=lambda p: p.parent.name)
def test_a_closed_pr_on_the_same_branch_is_never_reused(path, tmp_path):
    """Deduplicating branch names would not have fixed this one.

    The state-blind lookup, not the slug collision, is the defect: a CLOSED PR
    reaches it just as a merged one does, and a closed PR is what a rejected or
    superseded attempt leaves behind on a branch this run then rebuilds.
    """
    result, calls, url = _run_pr_block(
        path, tmp_path, pr_url=_FIXTURE_PR, pr_state="closed"
    )

    assert result.returncode == 0, result.stderr
    assert url == _CREATED_PR, f"a closed PR must not be reused, got {url!r}"
    assert _created(calls), f"a new PR must be created: {calls}"


@pytest.mark.integration
@pytest.mark.parametrize("path", ENTRYPOINTS, ids=lambda p: p.parent.name)
def test_an_open_pr_for_the_same_head_and_base_is_reused(path, tmp_path):
    """The other side: retries must stay idempotent.

    Without this, "fixing" the defect by never reusing anything would look
    correct in every test above and then open a duplicate PR on each worker
    retry, or fail outright, since GitHub refuses a second open PR for one
    (base, head) pair.
    """
    result, calls, url = _run_pr_block(
        path, tmp_path, pr_url=_FIXTURE_PR, pr_state="open"
    )

    assert result.returncode == 0, result.stderr
    assert url == _FIXTURE_PR, (
        f"an open PR for this head and base must be reused: {url!r}"
    )
    assert not _created(calls), f"reuse must skip creation, got: {calls}"


@pytest.mark.integration
@pytest.mark.parametrize("path", ENTRYPOINTS, ids=lambda p: p.parent.name)
def test_an_open_pr_against_a_different_base_is_not_reused(path, tmp_path):
    """``--base`` earns its place here, and nowhere else.

    The same agent branch can still carry an OPEN PR against the previous
    plan's branch when that plan never merged. Reusing it would point this
    task's review, and then its merge, at the wrong base.
    """
    result, calls, url = _run_pr_block(
        path,
        tmp_path,
        pr_url=_FIXTURE_PR,
        pr_state="open",
        pr_head=_TEST_BRANCH,
        pr_base=_PREVIOUS_BASE,
    )

    assert result.returncode == 0, result.stderr
    assert url == _CREATED_PR, (
        f"an open PR against a different base must not be reused, got {url!r}"
    )


@pytest.mark.integration
@pytest.mark.parametrize("path", ENTRYPOINTS, ids=lambda p: p.parent.name)
def test_an_open_pr_from_a_sibling_branch_is_not_reused(path, tmp_path):
    """``--head`` earns its place here.

    Every leaf of a plan opens its PR against the SAME plan branch, so an
    unscoped open-state query matches a sibling task's PR. Reusing it would
    review another leaf's diff and report it as this leaf's work.
    """
    result, calls, url = _run_pr_block(
        path,
        tmp_path,
        pr_url=_FIXTURE_PR,
        pr_state="open",
        pr_head=_SIBLING_BRANCH,
        pr_base=_TEST_BASE,
    )

    assert result.returncode == 0, result.stderr
    assert url == _CREATED_PR, (
        f"a sibling task's open PR must not be reused, got {url!r}"
    )


@pytest.mark.integration
@pytest.mark.parametrize("path", ENTRYPOINTS, ids=lambda p: p.parent.name)
def test_no_pr_at_all_creates_one(path, tmp_path):
    """The ordinary first attempt, kept so the lookup cannot swallow creation."""
    result, _calls, url = _run_pr_block(path, tmp_path, pr_url="")

    assert result.returncode == 0, result.stderr
    assert url == _CREATED_PR, f"expected a freshly created PR, got {url!r}"


@pytest.mark.integration
@pytest.mark.parametrize("path", ENTRYPOINTS, ids=lambda p: p.parent.name)
def test_the_reuse_lookup_states_all_three_filters_explicitly(path, tmp_path):
    """Every filter is spelled out rather than left to a gh default.

    The behavioural tests above already fail if any filter stops being
    APPLIED. This one additionally pins that ``--state open`` is written down,
    which behaviour cannot: gh's default state happens to be open today, so an
    entrypoint that omitted the flag would pass every test above and then
    change meaning the day that default does. The assertion reads the SPY LOG,
    so it describes the command that actually ran, not a string in the file.
    """
    _result, calls, _url = _run_pr_block(path, tmp_path, pr_url="")

    lookups = [c for c in calls if c.startswith("gh pr list")]
    assert lookups, f"the reuse lookup must go through `gh pr list`, got: {calls}"
    lookup = lookups[0]
    assert "--state open" in lookup, f"the lookup must be open-state only: {lookup}"
    assert f"--head {_TEST_BRANCH}" in lookup, (
        f"the lookup must scope the head: {lookup}"
    )
    assert f"--base {_TEST_BASE}" in lookup, f"the lookup must scope the base: {lookup}"


@pytest.mark.unit
@pytest.mark.parametrize("path", ENTRYPOINTS, ids=lambda p: p.parent.name)
def test_no_entrypoint_resolves_a_branch_to_a_pr_state_blind(path):
    """The cheap early signal, since an entrypoint change needs an image rebuild.

    ``gh pr view <branch>`` has no state filter at all, so there is no correct
    way to spell this lookup with it. Banning the form outright is what stops
    it being reintroduced by someone who reads the reuse comment and not these
    tests.
    """
    lines = [
        line
        for line in path.read_text(encoding="utf-8").splitlines()
        if "gh pr view" in line and not line.lstrip().startswith("#")
    ]
    assert lines == [], (
        "`gh pr view <branch>` ignores PR state; use `gh pr list --state open`. "
        f"Found: {lines}"
    )
