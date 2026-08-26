"""Agent-run reconciliation and live-log monitoring.

Extracted verbatim from core/orchestrator.py (2026-07-02 refactor). This is a
mixin: it is only ever mixed into ``Orchestrator`` and reads attributes set in
``Orchestrator.__init__``.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal, cast

import httpx

from orchestrator.core import branch_sweeper
from orchestrator.core.approvals import fetch_pending_approvals
from orchestrator.core.git_backend import PullRequestRef
from orchestrator.core.status_vocab import GATED_STATUSES, TERMINAL_STATUSES
from orchestrator.models.schemas import PlanStatus, TaskStatus


if TYPE_CHECKING:
    from orchestrator.core.event_bus import EventBus
    from orchestrator.core.task_queue import TaskQueue


logger = logging.getLogger(__name__)

#: git's exact wording when a branch of that name already exists. Anchored on
#: the ``fatal:`` prefix and the opening quote so it cannot be satisfied by a
#: worker's prose in the same transcript.
_GIT_BRANCH_EXISTS_RE = re.compile(r"fatal: a branch named '", re.IGNORECASE)

# Small cap on consecutive per-branch delete failures. The delete itself is
# known-safe by hand (see the module docstring in git_ops.py), so a repeated
# failure here is specific to the inline credential-helper invocation and is
# typically persistent for the rest of the process's life, not a one-off
# blip. The sweeper runs every reconcile pass (~6s); without a cap it retries
# forever and dumps a fresh traceback each time, which is exactly the noise
# this cap exists to remove. Kept fail-safe: reaching the cap only stops
# further ATTEMPTS for that branch, it never raises or wedges the loop.
BRANCH_DELETE_FAILURE_CAP: int = 3

# Consecutive `git ls-remote` failures against the SAME repo_url (a distinct
# failure mode from BRANCH_DELETE_FAILURE_CAP above: this one guards the
# LISTING call, which runs unconditionally at the top of every sweep pass,
# not the per-branch delete). A project whose repo path no longer exists
# fails this every ~5s reconcile pass (loop_interval's shipped default)
# forever, and each failure used to be a full traceback -- in one field
# report a 9 400-line log dump was overwhelmingly this traceback, burying two
# real dispatch failures the operator was trying to find. Set equal to
# BRANCH_DELETE_FAILURE_CAP for the same reason: three tries is enough to
# tell a genuine outage from a one-off blip without needlessly delaying the
# quarantine.
REPO_PROBE_FAILURE_QUARANTINE_THRESHOLD: int = 3

# Sweep passes to skip once quarantined, doubling on every re-probe that
# still fails, capped at the ceiling. Chosen over a flat periodic re-probe
# because it recovers a transient blip (a network hiccup, a momentarily
# unreachable host) quickly -- the first re-probe is only ~10 passes away --
# while a repository that is truly gone for good is retried with growing
# patience instead of at a fixed rate, without ever requiring an operator to
# restart the orchestrator just to get one more attempt. At the shipped ~5s
# reconcile interval this starts at roughly 50s and ceils around 10 minutes.
_QUARANTINE_INITIAL_COOLDOWN_PASSES: int = 10
_QUARANTINE_MAX_COOLDOWN_PASSES: int = 120

# Doublings past which the product is already at or above the ceiling, so the
# shift below is clamped BEFORE it happens rather than after. Clamping only the
# product is correct and arrives too late: ``consecutive_failures`` keeps
# growing for as long as the repository stays unreachable, so a repository that
# has been gone for a year builds an enormous integer on every re-probe purely
# to throw it away in ``min``. Derived from the two constants above rather than
# written out, so it cannot go stale when either moves: ``bit_length`` gives
# the smallest k with ``2**k`` greater than the ratio.
_QUARANTINE_MAX_COOLDOWN_DOUBLINGS: int = (
    _QUARANTINE_MAX_COOLDOWN_PASSES // _QUARANTINE_INITIAL_COOLDOWN_PASSES
).bit_length()

# --- Merge-gate reconciliation ------------------------------------------
#
# What GitHub reports for a pull request. Named rather than written inline at
# the three comparison sites, because the whole feature turns on telling these
# apart and a typo in a bare string literal is a silent no-op: the row simply
# stays parked, which is exactly what the unfixed defect looks like.
_PR_MERGED = "MERGED"
_PR_CLOSED = "CLOSED"

# Passes to skip between two probes of the SAME pull request. Each probe is a
# `gh` call over the network, and reconcile runs every pass (~5s at the shipped
# loop_interval), so an unthrottled reconciler would spend one call per parked
# row every five seconds forever. Expressed in PASSES, not seconds, to match
# the quarantine constants above; at the shipped interval 60 passes is roughly
# five minutes, or about twelve calls an hour per pull request. The thing being
# waited on is a human, who acts on a scale of minutes to days, so a five-minute
# cadence loses nothing a faster one would catch.
MERGE_GATE_PROBE_COOLDOWN_PASSES: int = 60

# How long a row must have been parked before it is probed at all. On the happy
# path the operator runs `praxis merge`, which does the whole follow-through
# itself; probing on the pass that parks the row would spend a `gh` call for
# every task that ever passes review, to learn that a pull request opened
# seconds ago is open. Fifteen minutes is well under any human's response time
# and removes that call entirely. Nothing is broken while it waits: the row is
# parked, which is where it belongs until somebody acts.
MERGE_GATE_MIN_PARKED_AGE_HOURS: float = 0.25

# Ceiling for the failure backoff, and the clamp on the doublings that reach
# it, derived exactly as the branch-sweep pair above so neither can go stale
# when a constant moves. ~1 hour at the shipped interval.
_MERGE_GATE_MAX_COOLDOWN_PASSES: int = 720
_MERGE_GATE_MAX_COOLDOWN_DOUBLINGS: int = (
    _MERGE_GATE_MAX_COOLDOWN_PASSES // MERGE_GATE_PROBE_COOLDOWN_PASSES
).bit_length()

# The observable outcome of one sweep_dead_branches call. Kept explicit (over
# returning None) so a repo that was genuinely PROBED and found to have
# nothing to reclaim ("swept") stays distinguishable in tests from one that
# was skipped outright because it is quarantined ("quarantined"): the two
# read identically from "nothing got deleted" alone, and collapsing them is
# exactly how a sweep that silently stopped working looks the same as a
# sweep with nothing to do.
SweepOutcome = Literal["refused", "probe_failed", "quarantined", "swept"]


@dataclass
class RepoProbeState:
    """Per-repo consecutive-failure tracking for ``list_remote_branches``.

    Lives in an in-memory dict on the caller (``ReconcileMixin`` holds one at
    ``_repo_probe_failures``, lazily created), for the process's lifetime
    only -- exactly like ``failure_counts`` in ``sweep_dead_branches`` below.
    A restart clears it, which is correct here too: a restart may itself have
    fixed whatever made the repo unreachable, and a restart is exactly when
    an operator wants a fresh attempt regardless of backoff.

    Attributes:
        consecutive_failures: Failed probes since the last success. Reset to
            0 the moment a probe succeeds.
        cooldown_remaining: Sweep passes left to skip before the next
            re-probe is attempted. Counts down by one on every quarantined
            pass; a probe is attempted again once it reaches 0. The merge-gate
            reconciler shares this dataclass and additionally sets it after a
            SUCCESSFUL probe, to space out a poll that would otherwise run
            every pass; there the failure backoff simply extends the same
            counter rather than introducing a second one.
        warned: Whether THIS quarantine episode has already logged its one
            warning. Reset to False on recovery, so a repo that goes bad
            again later gets a fresh single warning rather than permanent
            silence.
    """

    consecutive_failures: int = 0
    cooldown_remaining: int = 0
    warned: bool = False


# Plan statuses past which nothing more will ever be dispatched onto the plan's
# branch. Deliberately expressed as the TERMINAL set and complemented at the
# call site, so a plan status added later reads as LIVE (keep the branch)
# rather than as dead (delete it). It covers the 'merged' value the merged-plan
# query also tolerates even though PlanStatus has no such member. The task-side
# equivalent is status_vocab.TERMINAL_STATUSES, which is already frozen.
TERMINAL_PLAN_STATUSES: frozenset[str] = frozenset(
    {
        PlanStatus.COMPLETED.value,
        PlanStatus.REJECTED.value,
        PlanStatus.FAILED.value,
        "merged",
    }
)

# Ledger keys carrying the two "do not delete this" signals. Their absence is
# treated as an inability to establish that anything is dead, not as an empty
# veto set: see the refusal in sweep_dead_branches.
_REQUIRED_LEDGER_KEYS: tuple[str, ...] = ("live_branches", "protected_branches")


async def sweep_dead_branches(
    repo_url: str,
    list_remote_branches: Callable[[str], Awaitable[list[str]]],
    delete_remote_branch: Callable[[str, str], Awaitable[None]],
    ledger: dict[str, set[str]],
    failure_counts: dict[tuple[str, str], int] | None = None,
    repo_probe_state: dict[str, RepoProbeState] | None = None,
    project_ids: Sequence[str] = (),
) -> SweepOutcome:
    """Sweep dead branches on repo_url using ledger sets best-effort.

    Args:
        repo_url: Remote repository URL whose branches are being swept.
        list_remote_branches: Awaitable returning every branch on the remote.
        delete_remote_branch: Awaitable that deletes one remote branch.
        ledger: ``open_pr_branches`` / ``terminal_failed`` / ``merged_plan`` /
            ``live_branches`` / ``protected_branches`` branch-name sets used by
            ``branch_sweeper.dead_branches`` to decide reclaimability. The two
            veto sets are mandatory: a ledger missing either is refused
            outright (logged, nothing deleted) rather than swept as though
            nothing were live, because that reading deletes work.
        failure_counts: Mutable ``(repo_url, branch) -> consecutive failure
            count`` map, shared by the caller across sweep passes (the
            caller, e.g. ``ReconcileMixin.reconcile_runs``, owns an
            in-memory dict that lives for the process's lifetime; a restart
            clears it, which is correct here since a restart may itself
            have fixed the credential-helper problem). Once a branch's count
            reaches ``BRANCH_DELETE_FAILURE_CAP`` it is skipped silently on
            every later pass: no further delete attempt and no repeat log,
            until either the branch disappears out-of-band or the process
            restarts. Pass ``None`` (the default) for a call with no
            cross-pass memory.
        repo_probe_state: Mutable ``repo_url -> RepoProbeState`` map, shared
            by the caller across sweep passes exactly like ``failure_counts``.
            Once ``list_remote_branches`` has failed
            ``REPO_PROBE_FAILURE_QUARANTINE_THRESHOLD`` times in a row for
            this repo, further passes are skipped outright (no call, no log)
            until a backoff cooldown lapses. Pass ``None`` (the default) for
            a call with no cross-pass memory, which disables quarantine.
        project_ids: Project id(s) whose ``repo_url`` this is, used only to
            name the project in the one quarantine warning. Cosmetic: an
            empty sequence still quarantines correctly, it just cannot name
            a project in the log line.

    Returns:
        ``"refused"`` when the ledger was incomplete (nothing attempted),
        ``"quarantined"`` when this pass was skipped because the repo is in
        its cooldown, ``"probe_failed"`` when ``list_remote_branches`` was
        called and raised, or ``"swept"`` when it was called and succeeded
        (whether or not any branch was actually dead). Callers that do not
        care may discard it.
    """
    if failure_counts is None:
        failure_counts = {}
    if repo_probe_state is None:
        repo_probe_state = {}

    # Refuse to sweep at all rather than sweep half-informed. A missing veto
    # set means the caller could not establish what is live or protected, and
    # the only safe reading of that is "delete nothing this pass".
    missing = [key for key in _REQUIRED_LEDGER_KEYS if key not in ledger]
    if missing:
        logger.error(
            "Refusing to sweep branches on %s: ledger is missing %s, so no "
            "branch can be shown to be unused. Nothing was deleted.",
            repo_url,
            ", ".join(missing),
        )
        return "refused"

    state = repo_probe_state.setdefault(repo_url, RepoProbeState())
    if state.cooldown_remaining > 0:
        # Quarantined: this repo has already failed
        # REPO_PROBE_FAILURE_QUARANTINE_THRESHOLD times in a row and its one
        # warning has already been logged. Skip the call entirely -- no
        # attempt, no log -- until the cooldown lapses.
        state.cooldown_remaining -= 1
        return "quarantined"

    try:
        remote_branches = await list_remote_branches(repo_url)
    except (RuntimeError, httpx.HTTPError, OSError) as exc:
        # These three cover every way "this remote is not reachable right
        # now" is known to surface from this call, and all of them are a
        # CONDITION, not a crash:
        #   - RuntimeError: git_ops raises this for an ordinary
        #     `git ls-remote` failure (bad host, a deleted local/scratch
        #     path, repository gone), and CredentialError (PAT/App auth
        #     failures) is itself a RuntimeError subclass.
        #   - httpx.HTTPError: GitHubAppCredentialProvider.token_for_repo
        #     mints tokens over the network (core/github_credentials.py);
        #     a connect error, timeout, or DNS failure there raises this,
        #     NOT a RuntimeError, and is exactly as ordinary/transient as
        #     the git-command failure above on an install using GitHub App
        #     auth.
        #   - OSError: if the `git` binary itself is missing from PATH,
        #     asyncio.create_subprocess_exec raises FileNotFoundError (an
        #     OSError subclass) rather than a git exit code.
        # logger.exception's stack trace is reserved for the genuinely
        # unexpected `except Exception` fallback below; none of these three
        # get a traceback, and nothing is logged below the quarantine
        # threshold (the previous behaviour -- a traceback on every single
        # attempt, every ~5s -- is exactly the noise this replaces).
        state.consecutive_failures += 1
        if (
            state.consecutive_failures >= REPO_PROBE_FAILURE_QUARANTINE_THRESHOLD
            and not state.warned
        ):
            state.warned = True
            who = f" for project(s) {', '.join(project_ids)}" if project_ids else ""
            logger.warning(
                "Reconcile cannot reach repo %s%s after %d consecutive "
                "attempts; disabling branch sweeps for it. It will retry "
                "automatically with backoff; delete the project if this "
                "repository is gone for good. Last error: %s",
                repo_url,
                who,
                state.consecutive_failures,
                exc,
            )
        if state.consecutive_failures >= REPO_PROBE_FAILURE_QUARANTINE_THRESHOLD:
            doublings = min(
                state.consecutive_failures - REPO_PROBE_FAILURE_QUARANTINE_THRESHOLD,
                _QUARANTINE_MAX_COOLDOWN_DOUBLINGS,
            )
            cooldown = _QUARANTINE_INITIAL_COOLDOWN_PASSES * (2**doublings)
            state.cooldown_remaining = min(cooldown, _QUARANTINE_MAX_COOLDOWN_PASSES)
        return "probe_failed"
    except Exception:
        logger.exception("Failed to list remote branches for %s", repo_url)
        return "probe_failed"

    if state.consecutive_failures > 0:
        # Recovery: the repo answered after a run of failures. Reset the
        # streak and the one-warning latch so a LATER outage gets its own
        # fresh warning rather than permanent silence.
        logger.info(
            "Repo %s is reachable again after %d consecutive failures; "
            "resuming branch sweeps.",
            repo_url,
            state.consecutive_failures,
        )
        state.consecutive_failures = 0
        state.cooldown_remaining = 0
        state.warned = False

    dead = branch_sweeper.dead_branches(
        remote_branches,
        open_pr_branches=ledger.get("open_pr_branches", set()),
        terminal_failed=ledger.get("terminal_failed", set()),
        merged_plan=ledger.get("merged_plan", set()),
        live_branches=ledger["live_branches"],
        protected_branches=ledger["protected_branches"],
    )

    for branch in dead:
        key = (repo_url, branch)
        if failure_counts.get(key, 0) >= BRANCH_DELETE_FAILURE_CAP:
            continue
        try:
            await delete_remote_branch(repo_url, branch)
        except Exception as exc:  # noqa: BLE001 - best-effort per branch
            count = failure_counts.get(key, 0) + 1
            failure_counts[key] = count
            if count >= BRANCH_DELETE_FAILURE_CAP:
                logger.warning(
                    "Giving up deleting dead branch %s on %s after %d "
                    "consecutive failed attempts; not retrying until the "
                    "orchestrator restarts. Last error: %s",
                    branch,
                    repo_url,
                    count,
                    exc,
                )
            else:
                logger.warning(
                    "Failed to delete dead branch %s on %s (attempt %d/%d): %s",
                    branch,
                    repo_url,
                    count,
                    BRANCH_DELETE_FAILURE_CAP,
                    exc,
                )
        else:
            failure_counts.pop(key, None)

    return "swept"


class ReconcileMixin:
    """Reconciliation half of the Orchestrator (see class Orchestrator)."""

    # Attributes provided by Orchestrator.__init__ (declared for mypy only).
    if TYPE_CHECKING:
        _agents: Any
        _tq: TaskQueue
        _bus: EventBus
        _monitors: dict[str, asyncio.Task[None]]
        _callback_grace: float
        _monitor_poll_interval: float
        _effective_settings: Any
        _git: Any
        _branch_delete_failures: dict[tuple[str, str], int]
        _repo_probe_failures: dict[str, RepoProbeState]
        _merge_gate_probe_state: dict[str, RepoProbeState]

    def _safe_logs(self, container_id: str) -> str:
        """Fetch full container logs, swallowing any backend errors."""
        if self._agents is None:
            return ""
        try:
            return str(self._agents.get_container_logs(container_id, tail="all"))
        except Exception:  # noqa: BLE001 - log fetch is best-effort
            return ""

    async def _stop_superseded_container(self, run: dict[str, Any]) -> bool:
        """Stop the container of a superseded task's abandoned run.

        Args:
            run: The agent-run row being closed out.

        Returns:
            True when Docker was asked and did not refuse, which is what
            entitles the caller to record the run as ``stopped``. False when the
            agent manager is absent or the stop raised: the container may still
            be running, and saying otherwise is the false report this exists to
            prevent.
        """
        if self._agents is None:
            return False
        try:
            await self._agents.stop_agent(run["container_id"])
        except Exception:  # noqa: BLE001 - a stop failure must not wedge the sweep
            logger.warning(
                "Could not stop the container of superseded run %s (%s); it may "
                "still be running",
                run["id"],
                run["container_id"],
                exc_info=True,
            )
            return False
        return True

    async def reconcile_runs(self) -> None:
        """Reconcile every running agent run with its container's real state.

        Runs each orchestration pass (and therefore at startup). It:
        - fails orphaned runs when the agent manager is unavailable or the
          container has vanished/exited without a completion callback,
        - closes out (never retries) a run whose task was SUPERSEDED by split
          children, and
        - (re)attaches a live-log monitor to runs whose container is alive.

        This is what lets a task that was ``in_progress`` when the
        orchestrator died self-heal into a retryable ``failed`` state instead
        of hanging forever.
        """
        running = await self._tq.get_running_runs()
        if running:
            if self._agents is None:
                for run in running:
                    await self._fail_orphan(run, "Agent manager unavailable")
            else:
                for run in running:
                    monitor = self._monitors.get(run["id"])
                    if monitor is not None and not monitor.done():
                        continue
                    task = await self._tq.get_task(run["task_id"])
                    if task is not None and task["status"] == TaskStatus.SUPERSEDED:
                        # The leaf was replaced by split children; its container
                        # is abandoned work, not a run to retry. Reconciling it
                        # normally would fail_task then retry_task, silently
                        # resurrecting the parent as pending.
                        # Actually stop it before recording that it stopped.
                        # This closed the run row with the word "stopped" while
                        # contacting nothing, so the container kept running,
                        # kept pushing commits and eventually POSTed
                        # agent-done. Praxis has already fixed this exact shape
                        # once, in the task-stop endpoint, which separates
                        # "run rows closed" from "containers actually
                        # contacted". A container Docker will not stop is
                        # recorded as ``failed`` with the reason, because
                        # "stopped" would be the same false claim in a quieter
                        # form.
                        stopped = await self._stop_superseded_container(run)
                        await self._tq.complete_agent_run(
                            run["id"],
                            "stopped" if stopped else "failed",
                            str(run.get("logs") or "")
                            or (
                                "Task superseded; agent container stopped."
                                if stopped
                                else "Task superseded, but its agent container "
                                "could not be stopped and may still be running."
                            ),
                        )
                        continue
                    status = self._agents.get_container_status(run["container_id"])
                    if status is None:
                        await self._fail_orphan(run, "Agent container missing")
                        continue
                    if status["status"] in {"exited", "dead"}:
                        await self._reconcile_exited(run, status)
                        continue
                    self._start_monitor(run["id"], run["task_id"], run["container_id"])

        try:
            # Default branches are read alongside repo_url in one query and
            # folded into a repo_url -> {default branches} map, which keeps the
            # DISTINCT-repo iteration below (dict keys are unique) while
            # carrying the per-repo protection facts. Two project rows sharing
            # a remote but disagreeing on the default branch protect both,
            # which is the fail-safe direction.
            project_rows = await self._tq._db.fetch_all(
                "SELECT id, repo_url, default_branch FROM projects"
            )
            default_branch_by_repo: dict[str, set[str]] = {}
            # Which project(s) name this repo_url, carried only so the
            # quarantine warning below can name the project rather than just
            # the (possibly-cryptic) remote URL. Always non-empty for any
            # repo_url that reaches the sweep call, since both maps are built
            # from the same rows.
            project_ids_by_repo: dict[str, list[str]] = {}
            for row in project_rows:
                row_repo = row.get("repo_url")
                if not row_repo:
                    continue
                bucket = default_branch_by_repo.setdefault(row_repo, set())
                row_default = (row.get("default_branch") or "").strip()
                if row_default:
                    bucket.add(row_default)
                row_id = row.get("id")
                if row_id:
                    project_ids_by_repo.setdefault(row_repo, []).append(str(row_id))

            git_ops = getattr(self, "_git", None)
            if default_branch_by_repo and git_ops is not None:
                # EVERY ledger set is per repository. They used to be global
                # while the sweep below runs per remote, so a failed task in
                # repository A nominated an identically named branch in
                # repository B for irreversible deletion. Two projects in one
                # install is the ordinary case, the DB is routinely reset with
                # `rm data/orchestrator.db` while the remotes are not, and
                # `agent/{slug}` names are derived from task titles, so a
                # collision across repos is expected rather than a coincidence.
                # branch_sweeper's own standard is that a branch is deleted only
                # when POSITIVELY known to be finished with, and a row in
                # another repository is not that knowledge.
                task_rows = await self._tq._db.fetch_all(
                    "SELECT p.repo_url AS repo_url, t.branch_name AS branch_name, "
                    "t.status AS status, t.pr_url AS pr_url "
                    "FROM tasks t "
                    "LEFT JOIN plans pl ON t.plan_id = pl.id "
                    "LEFT JOIN projects p ON pl.project_id = p.id"
                )
                plan_rows = await self._tq._db.fetch_all(
                    "SELECT p.repo_url AS repo_url, "
                    "pl.plan_branch_name AS plan_branch_name, "
                    "pl.status AS status, "
                    "pl.integration_pr_url AS integration_pr_url, "
                    "pl.integration_merged_at AS integration_merged_at "
                    "FROM plans pl "
                    "LEFT JOIN projects p ON pl.project_id = p.id"
                )

                open_pr_by_repo: dict[str, set[str]] = {}
                terminal_failed_by_repo: dict[str, set[str]] = {}
                merged_plan_by_repo: dict[str, set[str]] = {}
                live_by_repo: dict[str, set[str]] = {}
                # A row whose repository cannot be resolved (a LEFT JOIN that
                # found nothing) can only ever SPARE a branch, never condemn
                # one: it joins every repo's live set and none of the dead sets.
                # Same fail-safe polarity as the live/dead complement below.
                unresolved_live: set[str] = set()

                def _bucket(store: dict[str, set[str]], repo: str) -> set[str]:
                    return store.setdefault(repo, set())

                for row in task_rows:
                    branch = (row.get("branch_name") or "").strip()
                    if not branch:
                        continue
                    repo = (row.get("repo_url") or "").strip()
                    if not repo:
                        unresolved_live.add(branch)
                        continue
                    status = row.get("status")
                    pr_url = (row.get("pr_url") or "").strip()
                    if pr_url and status not in ("failed", "merged"):
                        _bucket(open_pr_by_repo, repo).add(branch)
                    if status == "failed":
                        _bucket(terminal_failed_by_repo, repo).add(branch)
                    # The live half is computed by complementing the terminal
                    # set rather than by listing the live statuses, so a status
                    # added later counts as LIVE. That polarity is the whole
                    # point: a false "live" leaves a stale ref lying around, a
                    # false "dead" deletes a container's uncommitted work
                    # irreversibly.
                    #
                    # Since tasks.branch_name records the branch actually pushed
                    # to, single-branch (auto-delegate) mode has many rows
                    # sharing one work branch, and ONE of them failing is enough
                    # to put that shared branch into terminal_failed while
                    # siblings still run on it. The open-PR veto cannot cover
                    # that: a task still in progress has opened no PR yet.
                    if status not in TERMINAL_STATUSES:
                        _bucket(live_by_repo, repo).add(branch)

                for row in plan_rows:
                    branch = (row.get("plan_branch_name") or "").strip()
                    if not branch:
                        continue
                    repo = (row.get("repo_url") or "").strip()
                    if not repo:
                        unresolved_live.add(branch)
                        continue
                    status = row.get("status")
                    # A plan branch with an unmerged integration PR bears an
                    # open PR just as literally as a task branch does, and this
                    # is the veto that wins outright. Stated explicitly rather
                    # than left to the merged-plan rule alone: two independent
                    # signals have to agree before that branch can be deleted,
                    # and deleting it would close the integration PR based on
                    # it.
                    if row.get("integration_pr_url") and not row.get(
                        "integration_merged_at"
                    ):
                        _bucket(open_pr_by_repo, repo).add(branch)
                    if status in ("failed", "rejected"):
                        _bucket(terminal_failed_by_repo, repo).add(branch)
                    # "completed" does NOT mean the plan branch is reclaimable.
                    # It means every task merged ONTO that branch; the work then
                    # sits there, off the base branch, until the integration PR
                    # is merged. Treating completed-with-an-open-PR as merged
                    # classified a branch carrying the whole plan's work as
                    # dead, and deleting it would also have closed the
                    # integration PR based on it (docs/gotchas.md). So the
                    # branch is reclaimable only once integration actually
                    # landed.
                    #
                    # The delete path is ARMED: GitOps.delete_remote_branch
                    # shells a real `git push <repo_url> --delete <branch>` and
                    # logs the deletion. A comment here used to say it was inert
                    # and refused unconditionally, which was already false when
                    # it was written, and it read as a blanket assurance that
                    # classification bugs on this path could not destroy
                    # anything.
                    if status == "merged" or (
                        status == "completed" and row.get("integration_merged_at")
                    ):
                        _bucket(merged_plan_by_repo, repo).add(branch)
                    if status not in TERMINAL_PLAN_STATUSES:
                        _bucket(live_by_repo, repo).add(branch)

                # Per-branch delete-failure streaks, kept for the process's
                # lifetime (an in-memory dict on the instance, lazily
                # initialized -- Orchestrator.__init__ lives outside this
                # file). A restart clears it and gives every branch a fresh
                # set of attempts, which is correct: a restart may itself
                # have fixed the credential-helper problem the failures were
                # caused by. Bounded in practice: an entry is popped on the
                # first successful delete, so only branches that are STILL
                # failing accumulate keys, and each is a single int.
                branch_delete_failures = getattr(self, "_branch_delete_failures", None)
                if branch_delete_failures is None:
                    branch_delete_failures = {}
                    self._branch_delete_failures = branch_delete_failures

                # Per-repo probe-failure/quarantine state, kept the same way
                # and for the same reason as branch_delete_failures above: an
                # in-memory dict lazily attached to the instance, cleared by
                # a restart. This is what stops a repo whose path no longer
                # exists from being re-probed (and re-tracebacked) every
                # single reconcile pass forever.
                repo_probe_failures = getattr(self, "_repo_probe_failures", None)
                if repo_probe_failures is None:
                    repo_probe_failures = {}
                    self._repo_probe_failures = repo_probe_failures

                for repo_url, repo_defaults in default_branch_by_repo.items():
                    # A plan with no plan_branch_name dispatches straight onto
                    # project["default_branch"], so that branch reaches
                    # terminal_failed like any other. branch_sweeper's
                    # main/master/release* prefixes are a guess about naming
                    # and miss a repo whose trunk is 'develop' or 'trunk'; the
                    # project row knows the real answer, so pass it in.
                    await sweep_dead_branches(
                        repo_url=repo_url,
                        list_remote_branches=git_ops.list_remote_branches,
                        delete_remote_branch=git_ops.delete_remote_branch,
                        ledger={
                            "open_pr_branches": open_pr_by_repo.get(repo_url, set()),
                            "terminal_failed": terminal_failed_by_repo.get(
                                repo_url, set()
                            ),
                            "merged_plan": merged_plan_by_repo.get(repo_url, set()),
                            "live_branches": live_by_repo.get(repo_url, set())
                            | unresolved_live,
                            "protected_branches": repo_defaults,
                        },
                        failure_counts=branch_delete_failures,
                        repo_probe_state=repo_probe_failures,
                        project_ids=project_ids_by_repo.get(repo_url, ()),
                    )
        except Exception:  # noqa: BLE001 - sweeper call is best-effort
            logger.exception("Failed to sweep dead branches during reconcile pass")

        try:
            # AFTER the sweep, not before. This pass can mark a task merged or
            # failed, which moves its branch between the sweeper's live and
            # dead sets; letting the sweeper act on one-pass-stale facts errs
            # towards keeping a branch one pass longer, and the other order
            # errs towards deleting one. Only one of those is reversible.
            #
            # Its own try/except for the same reason the sweep has one:
            # ``run_once`` calls ``reconcile_runs`` with NO guard around it, so
            # anything escaping here stops every plan on the install, on every
            # tick, and looks like a loop that has simply gone quiet.
            await self.reconcile_merge_gate()
        except Exception:  # noqa: BLE001 - reconciliation is best-effort
            logger.exception("Failed to reconcile the merge gate this pass")

    def _merge_gate_probes(self) -> dict[str, RepoProbeState]:
        """Return the per-pull-request probe state, creating it on first use.

        Lazily attached to the instance exactly like ``_branch_delete_failures``
        and ``_repo_probe_failures``, and for the same reasons:
        ``Orchestrator.__init__`` lives outside this file, and a restart
        clearing the map is correct because a restart is when an operator wants
        a fresh attempt regardless of any backoff. Bounded in practice by the
        number of pull requests parked at the gate.

        Returns:
            A ``pr_url -> RepoProbeState`` map that lives for the process.
        """
        state = getattr(self, "_merge_gate_probe_state", None)
        if state is None:
            state = {}
            self._merge_gate_probe_state = state
        return state

    async def reconcile_merge_gate(self) -> None:
        """Reconcile each parked row against its pull request's real state.

        Praxis parks reviewed work at the merge gate and hands a human a
        ``pr_url``. The obvious way to act on that is the hosting provider's
        UI, and nothing reconciled the row afterwards, so a human who merged or
        closed a pull request left the row parked forever. Every surface that
        reports parked work then repeated it: ``praxis pending``, the
        dashboard, ``GET /api/approvals/pending`` and MCP
        ``pending_approvals``, all offering ``praxis merge`` on a pull request
        that no longer exists to merge.

        The four outcomes are NOT symmetric:

        - MERGED: the work landed. The row leaves the gate with the same
          follow-through the human path performs, siblings included.
        - CLOSED: the work did NOT land. Recording MERGED would fabricate a
          verdict and, because ``MERGED`` is in ``SATISFIED_STATUSES``, release
          every dependent leaf onto work that is not on the branch.
        - OPEN: parked is the correct state. The common case. Do nothing.
        - Unknown: never guess. Leave it parked and say so once. Same standing
          rule as ``core/context_window`` and ``verify_gate`` -- an unknown
          value says it is unknown rather than picking a plausible answer.

        The rows come from ``fetch_pending_approvals``, the SAME reader the two
        surfaces use, so the reconciler can never act on a different set than
        the one a human is being shown; a second, near-identical query here is
        precisely how the digest once came to omit every improvement proposal.

        Never raises: every row is handled independently and the caller wraps
        the whole call as well.
        """
        summary = await fetch_pending_approvals(self._tq._db)
        # One probe per pull request per pass, not one per row. Single-branch
        # (auto-delegate) mode puts N tasks on ONE shared pull request, which is
        # its designed shape and was three of the eight rows measured live.
        # Keyed on the URL alone is safe because a GitHub URL encodes its
        # repository; a local ref does not, but a local ref is never probed.
        probed: dict[str, str | None] = {}

        for entry in summary.get("tasks", []):
            try:
                await self._reconcile_parked_task(entry, probed)
            except Exception:  # noqa: BLE001 - one row, not the whole gate
                logger.exception(
                    "Could not reconcile parked task %s against %s; it stays "
                    "at the merge gate",
                    entry.get("task_id"),
                    entry.get("pr_url"),
                )

        for entry in summary.get("plans", []):
            try:
                await self._reconcile_parked_plan(entry, probed)
            except Exception:  # noqa: BLE001 - one row, not the whole gate
                logger.exception(
                    "Could not reconcile plan %s against %s; it stays at the "
                    "merge gate",
                    entry.get("plan_id"),
                    entry.get("pr_url"),
                )

    async def _reconcile_parked_task(
        self, entry: dict[str, Any], probed: dict[str, str | None]
    ) -> None:
        """Act on one parked task once its pull request's state is known.

        Args:
            entry: One item from ``summarize_pending``'s ``tasks`` list.
            probed: This pass's ``pr_url -> state`` memo, shared with the plan
                loop so a task and its plan's integration PR never cost two
                calls for one URL.
        """
        pr_url = str(entry.get("pr_url") or "").strip()
        if not pr_url:
            # A parked row with no pull request has nothing to ask about. It is
            # still something a human must decide, which is why
            # ``summarize_pending`` keeps listing it.
            return
        if float(entry.get("age_hours") or 0.0) < MERGE_GATE_MIN_PARKED_AGE_HOURS:
            return

        task_id = str(entry.get("task_id") or "")
        task = await self._tq.get_task(task_id)
        if task is None or str(task["status"]) not in GATED_STATUSES:
            # Re-read rather than trust the snapshot. An EARLIER row in this
            # same pass may have merged the shared pull request and swept this
            # one out of the gate already, and acting on the stale copy would
            # publish a second task_completed for work recorded once.
            return

        plan = await self._tq.get_plan(str(task["plan_id"]))
        project = (
            await self._tq.get_project(str(plan["project_id"]))
            if plan is not None
            else None
        )
        if project is None:
            return

        state = await self._probe_pull_request_state(pr_url, project, probed)
        if state == _PR_MERGED:
            await self._record_task_merged_elsewhere(task, pr_url)
        elif state == _PR_CLOSED:
            await self._record_task_rejected_elsewhere(task, pr_url)

    async def _record_task_merged_elsewhere(
        self, task: dict[str, Any], pr_url: str
    ) -> None:
        """Take a task out of the gate on a merge Praxis did not perform.

        The follow-through is deliberately identical to ``approve_task_merge``
        and in the same order, because the checkbox sync and the
        ``task_completed`` event are what the plan document and every SSE
        consumer read: a status flipped without them leaves the row quiet
        rather than merged.

        Args:
            task: The parked task row.
            pr_url: Its pull request, already known to be MERGED.
        """
        task_id = str(task["id"])
        await self._tq.mark_merged(task_id)
        await cast(Any, self)._sync_plan_checkbox(task)
        self._bus.publish(
            {"type": "task_completed", "task_id": task_id, "pr_url": pr_url}
        )
        logger.info(
            "Task %s left the merge gate: %s is merged on the remote, so its "
            "work has already landed",
            task_id,
            pr_url,
        )
        # LAST, and reusing the review mixin's helper rather than repeating its
        # three scope conditions here. It is the single place a SET of tasks
        # leaves the gate for one pull request, and re-deriving that scope is
        # how the two copies drift into disagreeing about which rows a merge
        # landed.
        await cast(Any, self)._sweep_merged_siblings(task)

    async def _record_task_rejected_elsewhere(
        self, task: dict[str, Any], pr_url: str
    ) -> None:
        """Fail a task whose pull request a human closed without merging.

        FAILED, not MERGED and not left parked. Closing a pull request is that
        human rejecting the change outside Praxis, and ``reject-merge`` is the
        verb for exactly that intent, so this lands on the same status that verb
        does. FAILED is not in ``SATISFIED_STATUSES``, so no dependent leaf is
        released onto work that is not on the branch.

        It does NOT re-dispatch, which is where it parts company with
        ``reject_task_merge``. That verb retries because a human running it
        supplies feedback the worker can act on. A closed pull request supplies
        none, so a retry would reproduce the same change, re-park it at the
        gate and loop autonomously off a human's "no" -- and this runs on the
        loop, unattended, where that loop has nothing to stop it.

        Args:
            task: The parked task row.
            pr_url: Its pull request, already known to be CLOSED.
        """
        task_id = str(task["id"])
        reason = (
            f"Pull request {pr_url} was closed on the remote without being "
            "merged, so this task's work did not land. Praxis did not close "
            "it: closing a pull request is a rejection made outside Praxis, "
            "and it is recorded here as one. Re-dispatch the task with "
            "feedback if the work is still wanted."
        )
        await self._tq.fail_task(task_id, reason)
        # Same event shape reject_task_merge publishes for the same intent, so
        # no consumer has to learn a second one.
        self._bus.publish(
            {"type": "task_failed", "task_id": task_id, "feedback": reason}
        )
        logger.warning(
            "Task %s left the merge gate as failed: %s was closed without merging",
            task_id,
            pr_url,
        )

    async def _reconcile_parked_plan(
        self, entry: dict[str, Any], probed: dict[str, str | None]
    ) -> None:
        """Act on one completed plan whose integration PR is still listed.

        The MERGED half mirrors ``approve_plan_integration``. The CLOSED half
        deliberately does LESS: it records the reason and leaves the row parked.
        There is no plan-rejection verb to follow, and the two states that would
        take the row off the gate are both wrong. Stamping
        ``integration_merged_at`` claims a merge nobody made. Setting the plan
        REJECTED puts its plan branch into the branch sweeper's
        ``terminal_failed`` set, and that branch carries the ENTIRE plan's work:
        a background probe must not be able to delete it. So the discrepancy is
        made visible on ``plans.error`` -- which ``PlanResponse`` and MCP
        ``poll_plan`` already surface -- and a human decides.

        Args:
            entry: One item from ``summarize_pending``'s ``plans`` list.
            probed: This pass's ``pr_url -> state`` memo.
        """
        pr_url = str(entry.get("pr_url") or "").strip()
        if not pr_url:
            return
        if float(entry.get("age_hours") or 0.0) < MERGE_GATE_MIN_PARKED_AGE_HOURS:
            return

        plan_id = str(entry.get("plan_id") or "")
        plan = await self._tq.get_plan(plan_id)
        if plan is None or plan.get("integration_merged_at"):
            return
        project = await self._tq.get_project(str(plan["project_id"]))
        if project is None:
            return

        state = await self._probe_pull_request_state(pr_url, project, probed)
        if state == _PR_MERGED:
            await self._tq.mark_plan_integrated(plan_id)
            self._bus.publish(
                {
                    "type": "plan_integrated",
                    "plan_id": plan_id,
                    "project_id": project["id"],
                    "pr_url": pr_url,
                }
            )
            logger.info(
                "Plan %s left the merge gate: integration PR %s is merged on "
                "the remote",
                plan_id,
                pr_url,
            )
        elif state == _PR_CLOSED:
            reason = (
                f"Integration PR {pr_url} was closed on the remote without "
                "being merged, so this plan's work is still on its plan branch "
                "and not on the base branch. Praxis has not changed the plan's "
                "state: reopen or re-create the integration PR if the work is "
                "still wanted."
            )
            # The stored reason IS the once-only latch, and it survives a
            # restart in a way an in-memory flag would not. Re-writing an
            # identical string every five minutes would also re-log it every
            # five minutes, which is the noise the branch sweeper's `warned`
            # latch exists to prevent one screen up.
            if str(plan.get("error") or "") == reason:
                return
            await self._tq.set_plan_error(plan_id, reason)
            logger.warning(
                "Plan %s is still listed at the merge gate but its integration "
                "PR %s was closed without merging",
                plan_id,
                pr_url,
            )

    async def _probe_pull_request_state(
        self,
        pr_url: str,
        project: dict[str, Any],
        probed: dict[str, str | None],
    ) -> str | None:
        """Return the pull request's state, or None for "no verdict this pass".

        None covers every reason not to act, and they are handled differently
        INSIDE this function even though they produce the same outcome: a local
        ref is skipped silently and never probed, an unreachable remote is
        counted towards a quarantine and warned about once, and a cooled-down
        URL is not called at all.

        Args:
            pr_url: The stored ``tasks.pr_url`` / ``plans.integration_pr_url``.
            project: The owning project row, for backend resolution.
            probed: This pass's memo, read and written here.

        Returns:
            The state string, or None to leave the row parked.
        """
        if pr_url in probed:
            return probed[pr_url]
        # Recorded as "no verdict" up front so every early return below memoizes
        # correctly without repeating the assignment on each branch.
        probed[pr_url] = None

        try:
            ref = PullRequestRef.from_url(pr_url)
        except ValueError:
            # DEBUG, not WARNING: ``approve_task_merge`` already raises loudly
            # on this exact value the moment a human acts on the row, so the
            # operator-facing report exists. A background loop repeating it
            # every cadence would only bury it.
            logger.debug("Merge gate: unparseable pull-request ref %r", pr_url)
            return None

        if ref.backend != "github":
            # A ``praxis-local://`` ref. The local backend has no pull-request
            # object, so there is no state to ask for, and nothing could have
            # merged or closed it behind Praxis's back: this whole reconciler
            # exists because a human can click Merge in a hosting provider's
            # UI, and a bare repo has no UI. Skipped OUTRIGHT rather than
            # treated as "could not ask", which would warn about a question
            # that does not exist and put every local row into a backoff path
            # built for an unreachable remote.
            #
            # Keyed on the REF, not on the resolved backend, because the two
            # come from independent sources that can disagree (see
            # ``GitHubBackend._repo``): editing a project's repo_url while
            # tasks exist is enough. A local ref reaching a GitHub backend is
            # that disagreement, and probing it would run gh against a ref that
            # carries no repository.
            return None

        backend = cast(Any, self)._resolve_backend(project["repo_url"])
        probe = getattr(backend, "pull_request_state", None)
        if probe is None:
            # A backend with no pull-request state at all: LocalGitBackend, or
            # a test double predating this capability. Same fact as a local
            # ref, reached from the other direction, and equally not a failure.
            return None

        state = self._merge_gate_probes().setdefault(pr_url, RepoProbeState())
        if state.cooldown_remaining > 0:
            state.cooldown_remaining -= 1
            return None

        answer: str | None = None
        failure: str | None = None
        try:
            answer = await probe(ref)
        except Exception as exc:  # noqa: BLE001 - unreachable is a condition
            failure = f"{type(exc).__name__}: {exc}"
        if answer is None and failure is None:
            failure = "gh could not report a state"

        if answer is not None:
            if state.consecutive_failures > 0:
                logger.info(
                    "Merge gate can read %s again after %d consecutive failures",
                    pr_url,
                    state.consecutive_failures,
                )
            state.consecutive_failures = 0
            state.warned = False
            state.cooldown_remaining = MERGE_GATE_PROBE_COOLDOWN_PASSES
            probed[pr_url] = answer
            return answer

        state.consecutive_failures += 1
        cooldown = MERGE_GATE_PROBE_COOLDOWN_PASSES
        if state.consecutive_failures >= REPO_PROBE_FAILURE_QUARANTINE_THRESHOLD:
            if not state.warned:
                state.warned = True
                logger.warning(
                    "Cannot read the state of %s after %d consecutive "
                    "attempts; it stays parked at the merge gate and will be "
                    "retried with backoff. Last reason: %s",
                    pr_url,
                    state.consecutive_failures,
                    failure,
                )
            doublings = min(
                state.consecutive_failures - REPO_PROBE_FAILURE_QUARANTINE_THRESHOLD,
                _MERGE_GATE_MAX_COOLDOWN_DOUBLINGS,
            )
            cooldown = min(cooldown * (2**doublings), _MERGE_GATE_MAX_COOLDOWN_PASSES)
        state.cooldown_remaining = cooldown
        return None

    def _start_monitor(self, run_id: str, task_id: str, container_id: str) -> None:
        task = asyncio.create_task(self.monitor_run(run_id, task_id, container_id))
        self._monitors[run_id] = task
        task.add_done_callback(lambda _t: self._monitors.pop(run_id, None))

    async def monitor_run(
        self,
        run_id: str,
        task_id: str,
        container_id: str,
    ) -> None:
        """Stream a running container's logs to the bus until it exits.

        Publishes incremental ``agent_log`` events (the only producer of
        them) and checkpoints the full log to the run row so the live-log
        SSE endpoint has data even when Docker is later unavailable. On
        container exit it hands off to ``_reconcile_exited``.
        """
        if self._agents is None:
            return
        sent = 0
        last_status: dict[str, Any] | None = None
        try:
            while True:
                logs = self._safe_logs(container_id)
                if len(logs) > sent:
                    chunk = logs[sent:]
                    sent = len(logs)
                    await self._tq.update_agent_run_logs(run_id, logs)
                    self._bus.publish(
                        {
                            "type": "agent_log",
                            "task_id": task_id,
                            "run_id": run_id,
                            "logs": chunk,
                        }
                    )
                last_status = self._agents.get_container_status(container_id)
                if last_status is None or last_status["status"] in {"exited", "dead"}:
                    break
                await asyncio.sleep(self._monitor_poll_interval)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - monitor must never crash the loop
            logger.exception("Log monitor failed for run %s", run_id)
            return
        await self._reconcile_exited(
            {"id": run_id, "task_id": task_id, "container_id": container_id},
            last_status,
        )

    async def _reconcile_exited(
        self,
        run: dict[str, Any],
        status: dict[str, Any] | None,
    ) -> None:
        """Fail a run whose container exited without a completion callback.

        Waits a grace period first: the agent-done callback may still be in
        flight, in which case the run is already past ``running`` and we do
        nothing.
        """
        await asyncio.sleep(self._callback_grace)
        current = await self._tq.get_agent_run(run["id"])
        if current is None or current["status"] != "running":
            return
        logs = self._safe_logs(run["container_id"]) or str(current["logs"] or "")
        if status is None:
            # ``get_container_status`` returns None for docker NotFound ONLY,
            # which is "Docker has no such container", not "the container
            # exited". monitor_run breaks on that too and used to hand it
            # straight here, where it became "exited (code None)" and sent the
            # operator looking at the worker and the model for a fault that was
            # Docker losing the container (a prune, a `docker rm -f`, a Docker
            # Desktop or WSL2 restart). reconcile_runs already reports the same
            # answer honestly one screen up.
            reason = (
                "Agent container is no longer known to Docker (removed, pruned, "
                "or the daemon lost it) and no completion callback arrived"
            )
        else:
            reason = (
                f"Agent container exited (code {status.get('exit_code')}) "
                "without a completion callback"
            )
        # If the container logs reveal a gh/GraphQL PR-create failure (e.g. zero
        # commits), surface a clear explanation instead of the generic exit reason.
        if logs and "No commits between" in logs:
            # gh's exact wording only. ``"no commits" in logs.lower()`` matched
            # the worker's own prose anywhere in the transcript, so a run that
            # failed for an unrelated reason after the model wrote "there are no
            # commits on this branch yet" was explained to the operator as a
            # zero-commit weak-model failure.
            reason = self._classify_pr_failure(logs)
        # A deterministic branch-setup failure (protected base) will recur on
        # every attempt, so it must NOT burn the retry budget. Detect the
        # entrypoint sentinel / git "branch already exists" message and mark it
        # terminal.
        deterministic = self._nonretryable_reason(logs) if logs else None
        if deterministic is not None:
            await self._resolve_failed_run(
                run, f"{deterministic} Original: {reason}", logs=logs, can_retry=False
            )
            return
        # A bounded retry is allowed on both branches. On the exit branch we
        # observed the container exit, so Docker is answering; on the missing
        # branch Docker answered too (NotFound is an answer), and the container
        # being gone is exactly the condition a fresh dispatch fixes.
        # Provider/gateway errors are transient and must not burn the budget.
        await self._resolve_failed_run_or_pause(run, reason, logs=logs, can_retry=True)

    async def _fail_orphan(self, run: dict[str, Any], reason: str) -> None:
        """Resolve an unmonitorable running run (and its task).

        Retries when the agent manager is available (the container merely
        vanished); fails terminally when Docker itself is unavailable, since
        re-dispatch would only thrash.
        """
        await self._resolve_failed_run(run, reason, can_retry=self._agents is not None)

    async def _resolve_failed_run(
        self,
        run: dict[str, Any],
        reason: str,
        *,
        can_retry: bool,
        logs: str | None = None,
    ) -> None:
        """Finalize a failed run as either a bounded retry or terminal failure.

        Marks the agent run ``failed``, then re-queues the task as ``pending``
        (incrementing its attempt) when retries remain and ``can_retry`` is
        set, otherwise marks the task ``failed``. This is what makes a lost
        completion callback self-recover instead of stalling.
        """
        log_text = logs if logs is not None else self._safe_logs(run["container_id"])
        # ``log_text``, never ``log_text or reason``. This column is what
        # ``praxis logs <task-id>`` prints as the worker's captured output, and
        # its empty-log branch exists to say "an empty value means it could not
        # read the container, not that the worker was silent". Substituting the
        # orchestrator's own reason made that branch unreachable for exactly the
        # case it was written for, and it also broke the provider-error streak,
        # which reads this column back: a genuine provider failure whose logs
        # Docker could not serve stopped matching and reset the count.
        await self._tq.complete_agent_run(run["id"], "failed", log_text)

        task = await self._tq.get_task(run["task_id"])
        max_retries = 0
        # Bound before the branch that assigns it: the else arm below reads
        # ``project`` unconditionally, so a task deleted between the running-run
        # query and this read (a race with DELETE /api/projects/{id}, which
        # removes agent_runs before tasks) raised UnboundLocalError after the
        # run row was already marked failed, and the task never got its verdict.
        project: dict[str, Any] | None = None
        if task is not None:
            plan = await self._tq.get_plan(task["plan_id"])
            project = (
                await self._tq.get_project(plan["project_id"])
                if plan is not None
                else None
            )
            if project is not None:
                max_retries = int(project["max_retries"])

        await self._tq.fail_task(run["task_id"], reason)
        if can_retry and task is not None and int(task["attempt"]) < max_retries:
            await self._tq.retry_task(run["task_id"])
            self._bus.publish(
                {
                    "type": "task_retry",
                    "task_id": run["task_id"],
                    "attempt": int(task["attempt"]) + 1,
                    "reason": reason,
                }
            )
            logger.warning(
                "Reconciled run %s -> retry %d/%d: %s",
                run["id"],
                int(task["attempt"]) + 1,
                max_retries,
                reason,
            )
        else:
            escalation = "block"
            if project is not None:
                escalation = await self._decide_escalation(
                    project, retries_exhausted=True
                )
            self._bus.publish(
                {
                    "type": "task_failed",
                    "task_id": run["task_id"],
                    "feedback": reason,
                    "escalation": escalation,
                }
            )
            logger.warning(
                "Reconciled run %s -> failed (escalation=%s): %s",
                run["id"],
                escalation,
                reason,
            )

    async def _decide_escalation(self, project: dict, retries_exhausted: bool) -> str:
        """Return the escalation action for a failing leaf.

        Args:
            project: The owning project row (must expose ``id``).
            retries_exhausted: True once bounded retries are spent.

        Returns:
            ``"retry"`` while retries remain, else the configured policy
            (``"block"`` | ``"brain"`` | ``"paid_fallback"``); defaults to
            ``"block"`` when no effective-settings resolver is wired.
        """
        if not retries_exhausted:
            return "retry"
        if self._effective_settings is None:
            return "block"
        return str(await self._effective_settings.escalation_policy(project["id"]))

    @staticmethod
    def _classify_pr_failure(raw: str) -> str:
        """Turn an opaque gh/GraphQL PR-create error into an explained failure."""
        if "No commits between" in raw:
            return (
                "Worker produced zero commits: the agent made no changes "
                "(model likely too weak for this task, or the plan was unclear). "
                f"Original error: {raw.strip()}"
            )
        return raw.strip()

    @staticmethod
    def _nonretryable_reason(logs: str) -> str | None:
        """Return the reason a branch-setup failure is deterministic, or None.

        These failures recur identically on every attempt, so a bounded retry
        only wastes the budget. There are TWO of them and they are unrelated,
        which is why this returns the reason rather than a bool: both used to
        produce the protected-base sentence, so a worker whose prose happened to
        contain the git phrasing was reported as having targeted a protected
        base, told to "re-dispatch with a feature branch" it had already been
        given, and denied its retries. The entrypoint's own protected-base guard
        had passed.

        The git test is anchored on ``fatal: a branch named '``, git's exact
        wording. It used to be two unanchored substrings ANDed over the WHOLE
        container log, which is the full worker transcript: the two phrases did
        not have to be on the same line or in the same context, or even come
        from git.

        Args:
            logs: Full container log text.

        Returns:
            A sentence naming the deterministic failure, or None when the
            failure is not one of them and the retry budget applies.
        """
        if "PRAXIS_FATAL_PROTECTED_BASE" in logs:
            return (
                "Deterministic branch-setup failure: the base branch is protected "
                "(workers must never target main/master/release*). Re-dispatch "
                "with a feature branch."
            )
        if _GIT_BRANCH_EXISTS_RE.search(logs):
            return (
                "Deterministic branch-setup failure: git refused to create the "
                "work branch because one of that name already exists in the "
                "clone. This is not about the base branch."
            )
        return None

    @staticmethod
    def is_provider_error(logs: str) -> bool:
        """Return True when logs indicate a transient worker-side provider/gateway error.

        These are errors from the model endpoint (403 Forbidden, 429 Too Many
        Requests, 5xx server errors, connection refused) rather than genuine task
        failures. They should NOT count against the task's retry budget.

        Args:
            logs: Full container log text.

        Returns:
            True when the logs reveal a provider/gateway error, False otherwise.
        """
        from orchestrator.core.provider_errors import is_provider_error as _shared

        return _shared(logs)

    async def _resolve_failed_run_or_pause(
        self,
        run: dict[str, Any],
        reason: str,
        *,
        can_retry: bool,
        logs: str | None = None,
    ) -> None:
        """Like ``_resolve_failed_run`` but pauses on provider/gateway errors.

        When the container logs reveal a transient provider error (403/429/5xx,
        connection refused) the run is marked failed but the task is re-queued
        WITHOUT consuming a retry attempt, and a ``worker_provider_error`` event
        is emitted so the dashboard can surface it.

        Args:
            run: Agent run dict (must have ``id``, ``task_id``, ``container_id``).
            reason: Human-readable failure reason.
            can_retry: Whether a normal bounded retry is allowed.
            logs: Container log text (fetched if None).
        """
        log_text = logs if logs is not None else self._safe_logs(run["container_id"])
        if log_text and self.is_provider_error(log_text):
            # Transient provider error: do NOT consume a retry. Mark the run
            # failed but reset the task to PENDING without touching attempt.
            await self._tq.complete_agent_run(run["id"], "failed", log_text or reason)
            task = await self._tq.get_task(run["task_id"])
            if task is not None:
                streak = await self._provider_error_streak(run["task_id"])
                cap = cast(int, cast(Any, self).PROVIDER_ERROR_RESPAWN_CAP)
                if streak >= cap:
                    # Persistent block, not a transient blip: stop respawning.
                    terminal = (
                        f"Worker endpoint unreachable: {streak} consecutive "
                        "provider/gateway errors (e.g. Cloudflare/WAF 403, VPN "
                        "down, or endpoint offline). Halting respawns; check the "
                        f"worker endpoint. Original: {reason}"
                    )
                    await self._tq.fail_task(run["task_id"], terminal)
                    self._bus.publish(
                        {
                            "type": "worker_endpoint_unreachable",
                            "task_id": run["task_id"],
                            "reason": terminal,
                            "consecutive_errors": streak,
                        }
                    )
                    logger.error(
                        "Worker endpoint unreachable for task %s after %d "
                        "consecutive provider errors; halting respawns.",
                        run["task_id"],
                        streak,
                    )
                    return
                # Bounded backoff before re-queue so we do not hammer a blocked
                # gateway (which can worsen a WAF bot-fight block).
                backoff = min(cast(Any, self)._provider_error_backoff * streak, 30.0)
                if backoff > 0:
                    await asyncio.sleep(backoff)
                now = datetime.now(UTC).isoformat()
                await self._tq._db.execute(
                    "UPDATE tasks SET status = ?, review_feedback = ?, updated_at = ? "
                    "WHERE id = ?",
                    (TaskStatus.PENDING, reason, now, run["task_id"]),
                )
                self._bus.publish(
                    {
                        "type": "worker_provider_error",
                        "task_id": run["task_id"],
                        "reason": reason,
                        "consecutive_errors": streak,
                    }
                )
                logger.warning(
                    "Worker provider/gateway error for task %s (streak %d/%d); "
                    "re-queued without consuming a retry attempt: %s",
                    run["task_id"],
                    streak,
                    cap,
                    reason,
                )
            return
        await self._resolve_failed_run(run, reason, can_retry=can_retry, logs=log_text)

    async def _provider_error_streak(self, task_id: str) -> int:
        """Count trailing consecutive failed provider-error runs for a task.

        The current run (just marked ``failed``) is included. A non-provider
        failed run breaks the streak, so a single transient blip after real
        progress does not accumulate toward the cap.
        """
        runs = await self._tq.get_runs_for_task(task_id)
        streak = 0
        for run in reversed(runs):
            if run["status"] == "completed":
                # A completed run PROVED the endpoint reachable, so the streak
                # cannot span it. Skipping every non-failed run counted two
                # provider errors either side of a successful call as
                # consecutive, and at the cap told the operator the worker
                # endpoint was unreachable with a successful call to it sitting
                # in the same task's run history.
                break
            if run["status"] != "failed":
                # ``stopped`` is abandoned work: it neither proves nor disproves
                # reachability, so it is passed over rather than counted.
                continue
            if self.is_provider_error(str(run["logs"] or "")):
                streak += 1
            else:
                break
        return streak
