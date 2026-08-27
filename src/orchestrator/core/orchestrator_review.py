"""PR review, merge approval, and plan-completion handling.

Extracted verbatim from core/orchestrator.py (2026-07-02 refactor). This is a
mixin: it is only ever mixed into ``Orchestrator`` and reads attributes set in
``Orchestrator.__init__``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import tempfile
from collections import Counter
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from orchestrator.core.bench_mode import verify_gate_disabled
from orchestrator.core.blast_radius import (
    BlastRadius,
    identifier_noun,
    measure_blast_radius,
    render_blast_radius,
)
from orchestrator.core.capability_events import (
    LeafDifficultyScoredEvent,
    LeafRejectedEvent,
    TaskEscalatedEvent,
    TaskSplitEvent,
    TaskTriagedEvent,
)
from orchestrator.core.clarification_states import (
    ANSWERED_BY_BRAIN,
    ASKED,
    AWAITING_HUMAN,
)
from orchestrator.core.diff_guard import (
    added_dependencies,
    destructive_deletions,
    detect_secrets,
)
from orchestrator.core.diff_stats import diff_stats
from orchestrator.core.escalation import next_escalation
from orchestrator.core.execute_plan_decompose import score_split_children
from orchestrator.core.failure_taxonomy import FailureClass
from orchestrator.core.git_backend import GitBackend, PullRequestRef
from orchestrator.core.git_ops import (
    GitOps,
    checkout_branch,
    clone_with_token,
    commit_and_push,
    flip_checklist_item,
)
from orchestrator.core.leaf_split import child_slugs
from orchestrator.core.leaf_triage import TriageEvidence, triage_leaf
from orchestrator.core.leaf_validator import (
    Violation,
    discriminating_leaf_command,
    restates_project_command,
    validate_split_children,
)
from orchestrator.core.llm_router import ProviderRateLimitError
from orchestrator.core.log_context import task_logger
from orchestrator.core.merge_policy import auto_merge_eligible
from orchestrator.core.micro_edit import BRAIN_IMPLEMENTER
from orchestrator.core.opus_bridge import parking_brain_runner
from orchestrator.core.outcome_recorder import record_outcome
from orchestrator.core.plan_graph import (
    build_graph_index,
    declared_paths,
    graph_entry_for_row,
    parse_graph_tasks,
    resolve_task_slug,
    slug_to_graph_task,
)
from orchestrator.core.status_vocab import GATED_STATUSES
from orchestrator.core.verify_gate import (
    LEAF_CHECK_NONDISCRIMINATING,
    LEAF_CHECK_NONE,
    SCOPE_VERIFY_PASSED,
    SCOPE_VERIFY_UNATTRIBUTED,
    FailureComparison,
    base_comparison_unavailable,
    base_failure_clause,
    compare_failures,
    normalize_verify_cmd,
    run_exit_code,
    run_verify,
)
from orchestrator.models.schemas import TaskStatus, TriageDecision


if TYPE_CHECKING:
    from orchestrator.core.event_bus import EventBus
    from orchestrator.core.task_queue import TaskQueue


logger = logging.getLogger(__name__)

# Cap on the verify output threaded through the SSE/plan_verify_failed event so
# a huge test log does not bloat the in-memory event or the dashboard payload.
_VERIFY_OUTPUT_MAX = 4000

# Every ``skipped`` names its reason.  A skip that could not say why is what
# let the local-mode hole hide: neither caller distinguishes ``skipped`` from
# ``passed`` by control flow, so an unexplained skip reads as a green gate.
_SKIP_NO_VERIFY_CMD = "no verify_cmd configured"
_SKIP_NO_CREDENTIAL_PROVIDER = "no GitHub credential provider"
_SKIP_NO_TOKEN = "no GitHub token for repo"  # nosec B105 - a log reason string
# The per-task gate's two extra skip reasons.  Bench-mode-disabled and
# no-verify_cmd both leave the task's ``verify_cmd`` local variable falsy, so
# without a distinct reason string they would read as the SAME skip in the
# log -- exactly the confusion this logging exists to remove.
_SKIP_BENCH_MODE_DISABLED = "bench mode disabled the gate"
_SKIP_CHECKOUT_UNAVAILABLE = "PR checkout unavailable"

# The two NON-skip outcomes of the per-task gate, named so ``verify_state``
# covers all five outcomes with ONE variable. Without them the scope statement
# would have to infer "it ran and passed" from the ABSENCE of a skip reason,
# and absence is exactly what the existing ``gate_skipped`` already means
# something narrower by (a CONFIGURED gate that could not run).
#
# ``_GATE_FAILED`` is RECORDED and deliberately never RENDERED: a failed gate
# fails the task where it runs, so it cannot reach the scope statement. See
# ``_review_scope_statement``.
_GATE_PASSED = "passed"
_GATE_FAILED = "failed"

# The sixth outcome, and the only one where a FAILING project gate does NOT
# fail the task: the same command fails identically on the branch the pull
# request targets, so the failure pre-dates this task's work and attributing it
# here would be a false accusation. Unlike ``_GATE_FAILED`` this one DOES reach
# ``_review_scope_statement``, and it must: the human at the merge gate is being
# handed a PASS on a repository whose verify command is red.
_GATE_UNATTRIBUTED = "failed on the PR head and identically on the base branch"

# The seventh outcome: the project gate went RED on the PR head and whether that
# failure pre-dates this task could not be established at all, because the base
# branch could not be ASKED. The task still fails -- an unanswered question must
# never buy a pass -- but the attribution is UNKNOWN, and a ``task_outcomes`` row
# claiming ``verify_fail`` would assert exactly the thing the stored feedback
# says was not established. Like ``_GATE_FAILED`` and unlike ``_GATE_UNATTRIBUTED``
# this never reaches ``_review_scope_statement``: it always carries a failing
# verdict, and that path is only walked under ``verdict == "pass"``.
_GATE_UNCOMPARED = "failed on the PR head with no base comparison available"

# The PLAN-scope twin of ``_GATE_UNATTRIBUTED``, and the value
# ``plan_integration_ready`` carries for the one outcome that has no older
# spelling: the whole-plan gate RAN, went RED, and the same command is red
# identically on the branch the plan was cut from.
#
# A distinct value rather than ``"failed"`` because ``verify_status in
# ("failed", "error")`` and "``plan_verify_failed`` was published" have been the
# same fact at every reader this event has ever had. Reporting ``"failed"`` with
# no alarm silently breaks that pairing; reporting ``"passed"`` would be the
# larger lie, since the gate did run and did go red. Not attributing is not
# passing -- the same rule the per-task seat states with ``_GATE_UNATTRIBUTED``.
_PLAN_VERIFY_UNATTRIBUTED = "unattributed"

# The marker ``review_task`` classifies a failure by. Shared between the two
# places that write it and the one that reads it, because a feedback string
# that stopped containing it would silently reclassify every mechanical failure
# as ``fixable_in_place`` -- "retry with feedback will probably work" -- with
# nothing raised and nothing to grep.
_VERIFY_FAIL_MARKER = "Automated verification failed"

# How much of a failed/errored verify command's output rides in the log line
# itself.  The full output already reaches the operator via the PR review
# feedback or the ``plan_verify_failed`` event payload; the log line only
# needs enough to identify the failure at a glance without flooding the
# orchestrator log with an up-to-8000-char command dump.
_LOG_EXCERPT_CHARS = 200

# The four outcomes of the integration stage, named so a consumer of
# ``plan_integration_ready`` never has to infer which one it got from the
# emptiness of ``pr_url``. Two of them carry no URL and they mean OPPOSITE
# things: ``nothing_to_integrate`` says the work is already on the base branch,
# ``failed`` says it is STRANDED on the plan branch. Until this existed both
# published the same payload, and four completed plans in a live database were
# indistinguishable from every surface.
_INTEGRATION_OPENED = "opened"
_INTEGRATION_REUSED = "reused"
_INTEGRATION_NOTHING = "nothing_to_integrate"
_INTEGRATION_FAILED = "failed"

# Consecutive ticks a task may fail its review on something that is NOT a
# genuine throttle before the task is failed and the plan allowed to move on.
#
# Three, matching ``REPO_PROBE_FAILURE_QUARANTINE_THRESHOLD`` in
# core/orchestrator_reconcile.py and for the same reason: at the shipped
# five-second loop interval it is enough to tell a real outage from a one-off
# gateway blip, without needlessly converting that blip into a re-dispatched
# container and a consumed retry.
#
# There is no bound at all on a genuine throttle, deliberately. That one parks
# ``opus_state``, the ``is_available()`` gate at the top of ``review_task``
# then returns before any call is made, and waiting therefore costs nothing.
# An auth failure or a gateway 403/5xx never parks, which is exactly why it
# needs a bound: it spent a real provider call on every tick, forever.
REVIEW_ERROR_ATTEMPT_CAP: int = 3


def _render_child_violations(
    violations: list[Violation],
    id_to_slug: dict[str, str],
) -> str:
    """Render split-child findings, naming each child by the slug it would get.

    A violation carries the brain's own child id (``c1``, ``child-2``, whatever
    the model wrote), which appears nowhere else in the system. Translating to
    the deterministic ``{parent}-sN`` slug is what makes the log line a thing an
    operator can grep the plan graph for. An id with no slug is printed as-is
    rather than dropped: a finding nobody can name is still a finding.

    Args:
        violations: The HARD or SOFT half of a ``ValidationResult``.
        id_to_slug: Brain child id to the slug the rewiring would assign.

    Returns:
        One semicolon-joined line.
    """
    return "; ".join(
        f"[{v.rule}] {id_to_slug.get(v.task_id, v.task_id)}: {v.message}"
        for v in violations
    )


def _log_excerpt(output: str) -> str:
    """Collapse whitespace and cap ``output`` for a single log line."""
    flat = " ".join(output.split())
    if len(flat) <= _LOG_EXCERPT_CHARS:
        return flat
    return f"{flat[:_LOG_EXCERPT_CHARS]}..."


async def _blast_radius_for_review(
    diff: str, checkout: str | None
) -> tuple[BlastRadius | None, str | None]:
    """Measure how far the identifiers in ``diff`` reach, and render it.

    FAILS OPEN, and that is the whole contract. This runs on every review, in
    front of a brain call, over a repository whose size nobody controls. A
    review that wedged on a repo walk would be strictly worse than the defect
    this measurement exists to catch, so ANY exception drops the section and
    the review proceeds unchanged.

    Off the event loop via ``asyncio.to_thread``. The walk is up to five seconds
    of synchronous filesystem work (measured: 1.4s on this checkout), and it is
    the only blocking call left on this path -- ``backend.checkout``,
    ``run_verify`` and every ``gh`` call are already async. Blocking the loop
    here would stall every other plan's tick on this install, which is a defect
    this repository has already shipped once.

    ``None`` is reserved for what the prompt's absent-section line actually
    claims: no checkout, or the measurement raised. A real walk that found
    nothing reused is NOT that, and must never be collapsed into it -- the
    prompt would then say "Not measured for this review", which is false and
    destroys the distinction between absent evidence and evidence of absence.
    ``render_blast_radius`` therefore always returns a sentence, and there is
    deliberately no ``or None`` on it here.

    Rendering happens inside the same ``try`` as the walk, deliberately: the
    guarantee is "any exception and the review proceeds with no section", and a
    guard that covered only the half most likely to raise would leave the other
    half able to wedge the loop.

    Args:
        diff: The change under review.
        checkout: A clean checkout of the PR head, or None when the clone
            failed and the review degraded to diff-only.

    Returns:
        ``(radius, section)``, both None only when nothing was measured at all.
    """
    if checkout is None:
        return None, None
    try:
        radius = await asyncio.to_thread(measure_blast_radius, diff, Path(checkout))
        return radius, render_blast_radius(radius)
    except Exception:  # noqa: BLE001 - a repo walk must never wedge a review
        logger.warning(
            "review: blast radius could not be measured; reviewing without it",
            exc_info=True,
        )
        return None, None


@dataclass(frozen=True)
class _UnattributedVerify:
    """A project verify failure that was NOT charged to the task under review.

    Carries the facts a human needs to act on it, rather than encoding them
    into ``verify_state``: which branch the same command already fails on, HOW
    the two failures compared, and what the leaf's OWN verification did in its
    place. A state string that embedded a branch name could not be compared,
    logged or grepped as a state.

    ``comparison`` and the two codes are REQUIRED rather than defaulted, on the
    same ground the sweeper's ``carrying_merged_work`` veto is: the sentence
    this feeds says whether the base failed the same way, and a default would
    let a construction site quietly assert the strongest of the three answers
    by saying nothing.

    ``nondiscriminating`` splits the ``leaf_check is None`` case in two. The
    leaf declared nothing runnable, or it declared the project command itself
    -- one remedy is "write a check", the other is "your leaf's acceptance and
    your project's verify command are the same string", and rendering them
    identically sent operators to do the first when they needed the second.
    """

    base_branch: str
    leaf_check: str | None
    comparison: FailureComparison
    head_code: int | None
    base_code: int | None
    nondiscriminating: bool = False


def _unattributed_clause(
    unattributed: _UnattributedVerify, verify_cmd: str | None
) -> str:
    """State a project gate that failed on both branches, in plain words.

    Written for the person at the merge gate, so it says what happened, on
    which branch, and what was checked INSTEAD. Silence here would be the exact
    overclaim the whole scope statement exists to end: a PASS parked for
    approval on a repository whose configured verification is red.

    Args:
        unattributed: The base branch, how the two failures compared, and the
            leaf's own check if it had a usable one.
        verify_cmd: The project command, named because it is the thing failing.

    Returns:
        One clause for ``_review_scope_statement``.
    """
    if unattributed.leaf_check:
        instead = (
            f"this task's own verification passed instead (`{unattributed.leaf_check}`)"
        )
    elif unattributed.nondiscriminating:
        instead = LEAF_CHECK_NONDISCRIMINATING
    else:
        instead = LEAF_CHECK_NONE
    compared = base_failure_clause(
        unattributed.comparison,
        unattributed.base_branch,
        unattributed.head_code,
        unattributed.base_code,
    )
    return (
        f"verify gate FAILED (`{verify_cmd}`) but {compared}, so it was "
        f"{SCOPE_VERIFY_UNATTRIBUTED}; {instead}"
    )


def _review_scope_statement(
    *,
    checkout_available: bool,
    verify_state: str,
    verify_cmd: str | None,
    radius: BlastRadius | None,
    unattributed: _UnattributedVerify | None = None,
) -> str:
    """State what this review actually observed, for the human at the gate.

    The report's strongest general point: a green that reads as verification
    when it is only a diff summary is actively misleading. The review already
    knows exactly what it did and did not look at, and until now none of it
    reached the person clicking approve.

    Assembled from the EXISTING vocabulary (``_SKIP_*``) rather than a second
    set of words for the same facts, so a reason in this sentence greps against
    the same reason in the log.

    Args:
        checkout_available: Whether a clean PR-head checkout backed the review.
        verify_state: One of ``_GATE_PASSED``, ``_GATE_FAILED``,
            ``_GATE_UNCOMPARED``, ``_GATE_UNATTRIBUTED`` or a ``_SKIP_*`` reason.
        verify_cmd: The project's command, named only when it actually ran.
        radius: The blast-radius measurement, or None when none was made.
        unattributed: Set with ``verify_state == _GATE_UNATTRIBUTED``, and
            required there: without it the clause could not name the branch the
            comparison was made against, which is the only part of the sentence
            a human can act on.

    Returns:
        One sentence, always non-empty. An empty scope statement would be
        indistinguishable from a surface that forgot to attach one.
    """
    clauses: list[str] = []

    if checkout_available:
        clauses.append("read a clean checkout of the PR head and the diff")
    else:
        clauses.append(f"read the diff text only ({_SKIP_CHECKOUT_UNAVAILABLE})")

    # Three arms, and still deliberately no ``_GATE_FAILED`` one -- nor a
    # ``_GATE_UNCOMPARED`` one, which is unreachable here for the identical
    # reason. A gate that
    # failed AND was charged to this task writes ``verdict: fail`` where it
    # runs, before any brain call, and this function is reached only under
    # ``verdict == "pass"``, so ``_GATE_FAILED`` cannot arrive here. The arm
    # that rendered it (and its twin in the CLI's ``_scope_glance``) was
    # unreachable while reading as a live feature: someone reasoning about the
    # merge gate would conclude a failed gate is surfaced there. It is not for
    # THAT state, and it should not be -- an attributed failure does not park at
    # the merge gate at all. It takes the failure path, which comments on the PR
    # and re-dispatches, and whose feedback ``core/worker_bible`` injects
    # verbatim into the next worker's prompt, where a sentence about what the
    # REVIEW covered is noise to a floor model at best.
    #
    # ``_GATE_UNATTRIBUTED`` is the state that DOES arrive here, and it is
    # routed deliberately rather than left to inherit the ``else``: that arm
    # would report a gate which ran and went red as one that never ran, to the
    # one person who could act on it.
    if verify_state == _GATE_PASSED:
        clauses.append(f"{SCOPE_VERIFY_PASSED} (`{verify_cmd}`)")
    elif verify_state == _GATE_UNATTRIBUTED and unattributed is not None:
        clauses.append(_unattributed_clause(unattributed, verify_cmd))
    else:
        clauses.append(f"verify gate did not run ({verify_state})")

    # Ordered by how much may be concluded, least first. ``radius.complete`` is
    # checked BEFORE the emptiness of the list, because a walk that was cut off
    # cannot support "nothing is reused" at all: that sentence would be a
    # positive claim about the repository sourced from a partial read of it,
    # delivered to `tasks.review_feedback` and the parked-PR event, which are
    # the two surfaces this whole statement exists to keep honest.
    if radius is None:
        clauses.append("blast radius not measured")
    elif not radius.complete:
        found = len(radius.occurrences)
        clauses.append(
            f"blast radius INCOMPLETE, the walk hit a cap "
            f"({found} reused {identifier_noun(found)} found before it stopped)"
        )
    elif radius.identifiers == 0:
        clauses.append("blast radius not applicable, this diff defines nothing")
    elif not radius.occurrences and radius.omitted:
        clauses.append(
            f"blast radius measured, {radius.omitted} reused "
            f"{identifier_noun(radius.omitted)} too generic to report"
        )
    elif not radius.occurrences:
        clauses.append("blast radius measured, nothing changed here is reused")
    else:
        found = len(radius.occurrences)
        # "and N more" rather than a bare count: the list is TOP_N-capped and
        # filtered, so stating its length alone reports a capped number as if it
        # were the whole answer.
        more = f" and {radius.omitted} more" if radius.omitted else ""
        clauses.append(
            f"blast radius measured ({found} reused {identifier_noun(found)}{more})"
        )

    return "Review scope: " + "; ".join(clauses) + "."


@dataclass(frozen=True)
class _DeclaredPathCheck:
    """Which of a leaf's declared edit locations the branch actually carries.

    Three buckets, not two, because "absent" and "not a question we could ask"
    are opposite facts and collapsing them is how a check turns into a false
    accusation. A glob, a directory-wide pattern, an absolute path or anything
    that would read outside the checkout is ``unresolvable``: the leaf declared
    something this check cannot decide, so it decides nothing.

    ``checked`` is therefore the only honest denominator. An empty ``missing``
    with ``checked == 0`` means NOTHING was established, and reporting that as
    "all declared locations exist" would be the same shape of false success
    this whole check exists to end.
    """

    present: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()
    unresolvable: tuple[str, ...] = ()

    @property
    def checked(self) -> int:
        """How many declared locations this check could actually decide."""
        return len(self.present) + len(self.missing)


# Shell/glob metacharacters. A declaration carrying one names a SET of paths,
# and ``Path.exists`` on the literal string would report the set as missing.
_GLOB_CHARS = ("*", "?", "[", "]")


def _resolve_declared_path(root: Path, raw: str) -> Path | None:
    """Return ``raw`` as a path inside ``root``, or None if it cannot be one.

    None is returned for every shape whose existence this check must not
    pronounce on: a glob, an absolute path, a Windows drive or UNC path, or
    anything climbing out of the checkout with ``..``. The last three are also
    the security half: ``Path(root) / "/etc/passwd"`` is ``/etc/passwd``, so an
    unguarded join would stat a file outside the temporary clone and let a
    brain-authored string decide a governance outcome from the host filesystem.

    Args:
        root: The already-resolved checkout directory.
        raw: One declared path, verbatim as the brain wrote it.

    Returns:
        The path to test, or None when the declaration is not decidable.
    """
    # ``src/api.py::make_widget`` addresses a symbol in a file. The file is the
    # part this check can decide, and deciding it is strictly better than
    # declining: a missing FILE is still a missing file.
    candidate = raw.split("::", 1)[0].strip().replace("\\", "/")
    if not candidate or any(char in candidate for char in _GLOB_CHARS):
        return None
    # A single leading "/" is how a brain writes "from the repository root",
    # and the segment split below simply drops it. A DOUBLE one is a UNC host,
    # which names no file in this repository; without this it would be read as
    # the repo-relative ``host/share/...`` and reported MISSING, failing a leaf
    # over a declaration nobody could have satisfied.
    if candidate.startswith("//"):
        return None
    segments = [part for part in candidate.split("/") if part not in ("", ".")]
    if not segments or any(part == ".." for part in segments):
        return None
    # A drive letter is the other absolute form, and is refused for the same
    # reason: it addresses the host, not the checkout.
    if ":" in segments[0]:
        return None
    target = root.joinpath(*segments)
    try:
        if not target.resolve().is_relative_to(root):
            return None
    except (OSError, ValueError):
        return None
    return target


def _check_declared_paths(root: str, paths: Sequence[str]) -> _DeclaredPathCheck:
    """Sort a leaf's declared edit locations into present, missing, and undecidable.

    Never raises. This runs inside the no-op decision, and an exception here
    would turn a governance answer into a loop-level failure.

    Args:
        root: A checkout of the branch the leaf was cut from.
        paths: The leaf's declared edit locations, verbatim.

    Returns:
        The three buckets, each holding the VERBATIM declaration rather than a
        normalized form, so the reason stored on the task names what the brain
        actually wrote and an operator can grep the plan for it.
    """
    try:
        root_path = Path(root).resolve()
    except (OSError, ValueError):  # pragma: no cover - defensive
        return _DeclaredPathCheck(unresolvable=tuple(paths))

    present: list[str] = []
    missing: list[str] = []
    unresolvable: list[str] = []
    for raw in paths:
        target = _resolve_declared_path(root_path, raw)
        if target is None:
            unresolvable.append(raw)
            continue
        try:
            # A directory counts: the leaf declared a location, and a location
            # that is there is there. ``exists`` follows symlinks, which is
            # what "the tree carries this" means to the worker.
            exists = target.exists()
        except OSError:
            unresolvable.append(raw)
            continue
        (present if exists else missing).append(raw)
    return _DeclaredPathCheck(
        present=tuple(present),
        missing=tuple(missing),
        unresolvable=tuple(unresolvable),
    )


@dataclass(frozen=True)
class _LeafVerifyRun:
    """What a leaf's OWN declared verification did on the tree just inspected.

    A separate carrier rather than a second status on the gate result, because
    the two answer different questions about the same tree and collapsing them
    is the conflation this whole seat exists to end: the project command asks
    "is this repository healthy", the declared command asks "was THIS leaf's
    work done". The gate keeps its own verdict unchanged, and this rides beside
    it.

    ``command`` is carried so the reason stored on the task can NAME what was
    run. A sentence claiming a leaf check without saying which one is the same
    quiet overclaim as a scope statement that omits the gate.
    """

    command: str
    passed: bool
    output: str = ""


@dataclass(frozen=True)
class _PlanVerifyResult:
    """Outcome of the whole-plan verify gate.

    ``status`` is one of ``"skipped"`` (genuinely nothing to run), ``"passed"``,
    ``"failed"``, or ``"error"`` (clone/checkout/verify raised).

    ``skipped`` must never mean "we could not work out how to run it": that
    case is an ``error``, which both callers fail closed on.  ``reason`` is set
    on every skip so the distinction stays auditable in a log or a debugger.

    ``paths`` is the SECOND question this gate answers, and only when a caller
    asks it: did the tree it just materialized carry the edit locations the
    leaf declared. ``None`` means the question was not asked or no tree was
    fetched, which is neither a yes nor a no.

    ``leaf`` is the THIRD, asked only by ``no_change_outcome`` and only when the
    project command went RED on the branch the leaf was cut from. ``None`` means
    the question was not asked, the leaf declared nothing runnable, or the
    project command did not fail -- three different absences that all mean "this
    settles nothing", which is why the caller may only ever act on a PRESENT
    answer.

    ``returncode`` is the runner's OWN classification of a failure, carried so
    that two red runs can be compared on HOW they failed rather than merely on
    the fact that both did. ``None`` means no classification is available -- a
    skip, an error, a timeout, or a caller that never ran a command -- and
    every one of those routes the comparison to ``INCOMPARABLE``, which
    licenses exactly what it licensed before the comparison existed.
    """

    status: str
    output: str = ""
    reason: str = ""
    paths: _DeclaredPathCheck | None = None
    leaf: _LeafVerifyRun | None = None
    returncode: int | None = None


@dataclass(frozen=True)
class _VerifyAttribution:
    """What a FAILING project verify command on the PR head turned out to mean.

    Three fields because the answer is three facts, and the caller needs all
    three in the same breath: whether the review is over (``review``), what to
    tell the human (``verify_state`` plus ``unattributed``), and nothing else.

    ``review`` is the verdict dict ``review_task`` already builds at the gate,
    or None to mean "carry on to the brain review". None is NOT "it passed":
    the project command DID fail, and ``verify_state`` is what says so.
    """

    review: dict[str, Any] | None
    verify_state: str
    unattributed: _UnattributedVerify | None = None


@dataclass(frozen=True)
class NoChangeDecision:
    """What an empty diff MEANT, and whether the answer is about the worker.

    ``closed`` and ``why`` are the answer this has always carried.
    ``worker_attributable`` is the third fact, and it exists because a decline
    is not one thing: ``no_change_outcome`` declines for five unrelated
    reasons, and only some of them are evidence about the worker's output.

    That distinction decides whether a repeated failure may spend a brain call
    on adaptive triage. It is settled HERE, where the verify verdict and the
    declared-path check are both in hand, rather than recovered afterwards by
    reading ``why``: a substring match over prose is the same "interpret a
    failure after the fact" pattern this module rejects everywhere else, and it
    would silently start answering differently the day a sentence is reworded.

    Iterating yields exactly ``(closed, why)`` so the two call sites outside
    this module -- the worker callback in ``api/internal.py`` and the micro-edit
    lane in ``orchestrator_dispatch.py`` -- keep unpacking it unchanged. Both
    reach this method through an untyped object, so mypy could not have caught
    a widening that broke them.
    """

    closed: bool
    why: str
    worker_attributable: bool = False

    def __iter__(self) -> Iterator[Any]:
        """Unpack as the ``(closed, why)`` pair this method has always returned."""
        yield self.closed
        yield self.why


def _no_op_evidence(verdict: _PlanVerifyResult, base_branch: str) -> str | None:
    """Return the evidence that closes a leaf as a no-op, or None for "not established".

    A no-op is terminal and SATISFIED: it unblocks dependents and lets the plan
    complete with nothing committed. So it needs a POSITIVE answer, and the
    stored reason has to say which answer it was, because
    ``mark_no_changes`` writes this string to ``tasks.review_feedback``, where
    the dashboard renders it and MCP returns it.

    Exactly three answers qualify, and ``skipped`` alone is not one of them:

    - ``passed``: the gate ran on the branch the leaf was cut from and the
      tree there already satisfies it.
    - ``failed``, but the leaf's OWN declared verification passed on that same
      tree. The project command is the bar for a REGRESSION and the wrong bar
      for a leaf (cd0c127, measured live twice): on a dependent chain it is
      routinely red for a SIBLING's contract, and on THIS path it is red on the
      very tree the worker was handed, so it can never be about work the worker
      did or did not do. The declared command is the only evidence here that is
      about this leaf, and a PASS from it is strictly stronger than either of
      the other two answers -- it is a statement about the leaf's contract
      rather than about the repository's health.
    - ``skipped`` BECAUSE no verify command is configured, or because bench
      mode disabled the gate on purpose: no independent evidence, only the
      harness's clean exit. Deliberate, documented, and the weakest link here;
      the measured alternative is worse. The two share the carve-out because
      the DECISION is the same for both, and are kept apart in the stored
      string because the STATEMENT is not: a bench run whose project does
      configure a verify command used to record that it had none.

    A leaf's declared paths merely being PRESENT is deliberately NOT a fourth
    answer. A path that exists proves a file is there, not that it does the
    leaf's job -- a sibling that wrote a stub satisfies it -- and greening a
    leaf on it would unblock every dependent onto work nobody finished, which is
    the exact shape of false success the 2026-08-25 measurement produced from
    the other direction. The positive path answer stays what it has always
    been: a REFUTER (``paths.missing``) and a clause in the stored reason.

    The other two skips (``_SKIP_NO_CREDENTIAL_PROVIDER``, ``_SKIP_NO_TOKEN``)
    mean a verify command IS configured and the gate could not reach the
    repository. Both already log at WARNING because that is a broken
    deployment, not an operator choice. Closing a leaf on one of them made two
    false statements at once: that the leaf was satisfied, and that the
    operator had chosen to run nothing. The reason is compared rather than the
    status so a future skip reason cannot inherit the carve-out by default.

    Args:
        verdict: The gate result for the branch the leaf was cut from.
        base_branch: That branch, named in the evidence string.

    Returns:
        The evidence to store, or None when the no-op was not established.
    """
    if verdict.status == "passed":
        return f"verify passed on {base_branch}"
    if verdict.status == "failed" and verdict.leaf is not None and verdict.leaf.passed:
        return (
            f"the project verify command is red on {base_branch} and fails there "
            "for reasons that pre-date this task, so it was not held against it; "
            f"this task's OWN declared verification (`{verdict.leaf.command}`) "
            "passes on that same tree"
        )
    if verdict.status == "skipped" and verdict.reason in (
        _SKIP_NO_VERIFY_CMD,
        _SKIP_BENCH_MODE_DISABLED,
    ):
        return f"{verdict.reason}; harness exited clean on {base_branch}"
    return None


def _declared_paths_clause(
    declared: Sequence[str],
    paths: _DeclaredPathCheck | None,
    base_branch: str,
) -> str:
    """State what the declared-edit-location check established, including nothing.

    Appended to the reason ``mark_no_changes`` stores on the task, which the
    dashboard renders and MCP returns. A no-op backed by a checked list of
    files and a no-op backed by no check at all are worth different amounts of
    trust, and the surface that reports them has to say which one it is.
    Silence would read as the stronger of the two, which is the same shape of
    quiet overclaim the check exists to end.

    Args:
        declared: The leaf's declared edit locations, verbatim.
        paths: The check's three buckets, or None when no tree was fetched.
        base_branch: The branch that was inspected.

    Returns:
        One clause, always a statement about what IS known.
    """
    total = len(declared)
    if total == 0:
        return "this task declared no edit locations, so none could be checked"
    if paths is None:
        return (
            f"its {total} declared edit locations could not be checked, "
            "because no checkout of the branch was made"
        )
    if paths.checked == 0:
        return (
            f"none of its {total} declared edit locations could be resolved to "
            "a path, so none could be checked"
        )
    if paths.missing:
        # Unreachable while ``no_change_outcome`` refuses on a missing path
        # before it builds this string, and stated anyway. The alternative is a
        # sentence whose truth depends on a caller ordering somewhere else, and
        # this one is written into ``tasks.review_feedback`` for a human.
        return (
            f"{len(paths.missing)} of its {total} declared edit locations are "
            f"absent from {base_branch}"
        )
    clause = (
        f"all {len(paths.present)} of its declared edit locations exist "
        f"on {base_branch}"
    )
    if paths.unresolvable:
        clause += f", {len(paths.unresolvable)} more could not be resolved to a path"
    return clause


def _verify_outcome(
    passed: bool,
    output: str,
    plan_branch: str,
    verify_cmd: str,
    paths: _DeclaredPathCheck | None = None,
    returncode: int | None = None,
) -> _PlanVerifyResult:
    """Turn a ``run_verify`` result into a gate verdict, and log it.

    Shared by both backend paths deliberately: the only thing that legitimately
    differs between local and GitHub is how the plan branch reaches a working
    directory.  Once it is there, the verdict is computed AND LOGGED in
    exactly one place, so one path cannot drift into always reporting a pass
    -- or into reporting a real pass/fail with no trace in the log.

    Args:
        passed: The exit-status verdict from ``run_verify``.
        output: The command's combined output.
        plan_branch: The accumulated plan branch that was verified.  This
            gate has no task id, only a branch, so the branch is what makes
            the log line greppable against a live run.
        verify_cmd: The project's configured verification command.
        paths: The declared-edit-location check for the same tree, when a
            caller asked for one. Carried through rather than folded into the
            status: a branch can verify clean AND be missing a file the leaf
            declared, and those are the two facts that together produced the
            false success this argument exists for.
        returncode: The runner's own exit code, carried so a red base branch
            can be compared with a red head on HOW each failed. Defaulted to
            None so a caller that cannot supply one gets the pre-existing
            answer rather than a fabricated classification.

    Returns:
        A ``passed`` or ``failed`` result carrying truncated output.
    """
    if passed:
        logger.info("verify gate passed (branch=%s, cmd=`%s`)", plan_branch, verify_cmd)
    else:
        logger.warning(
            "verify gate failed (branch=%s, cmd=`%s`): %s",
            plan_branch,
            verify_cmd,
            _log_excerpt(output),
        )
    return _PlanVerifyResult(
        "passed" if passed else "failed",
        output[:_VERIFY_OUTPUT_MAX],
        paths=paths,
        returncode=returncode,
    )


async def _inspect_branch_tree(
    checkout_dir: str,
    plan_branch: str,
    verify_cmd: str | None,
    require_paths: Sequence[str],
    skip_reason: str,
    leaf_verify_cmd: str | None = None,
) -> _PlanVerifyResult:
    """Answer every question the gate has about an already-materialized tree.

    The single place both backend paths converge on once the branch is on
    disk, for the same reason ``_verify_outcome`` is: the only thing that
    legitimately differs between local and GitHub is HOW the branch gets to a
    working directory. Two copies of "check the declared paths, then run the
    command" is how one backend comes to check something the other does not.

    Doing all of it here is also what keeps this to ONE checkout. The
    declared-path check, the project command and the leaf's own command must see
    the SAME tree at the same instant; fetching the branch twice could observe
    two different states and decide a leaf's fate from a mixture of them.

    Args:
        checkout_dir: A checkout of ``plan_branch``.
        plan_branch: The branch under test, for the log lines.
        verify_cmd: The normalized verify command, or None when there is none
            to run and only the declared paths were asked about.
        require_paths: The leaf's declared edit locations; empty means the
            question was not asked.
        skip_reason: What to report when there is no command to run.
        leaf_verify_cmd: The leaf's OWN declared verification, already reduced
            to something shellable by ``shell_command_for_verification``. Run
            ONLY when the project command went red, because that is the only
            arm where the project command's answer cannot settle attribution.
            On a green project command the no-op is already established, and
            running this there could only REFUSE leaves that close today --
            a behaviour change on every path that reaches this gate.

    Returns:
        The gate verdict, carrying the path check when one was requested and
        the leaf run when one was made.
    """
    paths = (
        _check_declared_paths(checkout_dir, require_paths) if require_paths else None
    )
    if verify_cmd is None:
        logger.info("verify gate skipped: %s (branch=%s)", skip_reason, plan_branch)
        return _PlanVerifyResult("skipped", reason=skip_reason, paths=paths)
    run = await run_verify(checkout_dir, verify_cmd)
    passed, output = run
    result = _verify_outcome(
        passed, output, plan_branch, verify_cmd, paths, run_exit_code(run)
    )
    if result.status != "failed" or leaf_verify_cmd is None:
        return result
    leaf_passed, leaf_output = await run_verify(checkout_dir, leaf_verify_cmd)
    logger.info(
        "leaf verification %s on %s (`%s`): %s",
        "passed" if leaf_passed else "FAILED",
        plan_branch,
        leaf_verify_cmd,
        _log_excerpt(leaf_output),
    )
    return replace(
        result,
        leaf=_LeafVerifyRun(
            leaf_verify_cmd, leaf_passed, leaf_output[:_VERIFY_OUTPUT_MAX]
        ),
    )


@dataclass(frozen=True)
class _PlanVerifyAttribution:
    """What a RED plan branch turned out to mean at the whole-plan backstop.

    ``alarm`` is the decision, ``reported_status`` is what
    ``plan_integration_ready`` carries, and ``detail`` is the sentence that has
    to reach a human. Returned together because the caller needs all three in
    one breath, and because deriving ``alarm`` from ``reported_status`` at the
    call site is precisely how "red" and "this plan broke it" became one fact.
    """

    alarm: bool
    reported_status: str
    detail: str


def attribute_plan_verify_failure(
    head: _PlanVerifyResult, base: _PlanVerifyResult, base_branch: str
) -> _PlanVerifyAttribution:
    """Decide whether a red COMPLETED PLAN BRANCH is a regression this plan caused.

    The fourth and last seat of the class fixed at three others on 2026-08-26: a
    fact about the REPOSITORY or the BASE BRANCH reasoned about as a fact about
    THIS plan. ``on_plan_completed`` ran the project's verify command against the
    accumulated plan branch and, on red, published ``plan_verify_failed``
    describing a cross-task regression with no base comparison at all. On a
    repository whose default branch is red by design -- this project's own live
    rig is one -- that verdict fired on every completed plan and meant nothing.

    Two of the three parts of the shared rule apply here, and the missing one is
    the argument for why this is not a copy of the review seat. The project
    ``verify_cmd`` settles REGRESSION and the BASE BRANCH settles ATTRIBUTION,
    exactly as one layer up. There is no third step appealing to a leaf's OWN
    declared verification, because a completed plan branch carries several leaves
    and no single leaf's check speaks for the whole tree -- the same call
    ``attribute_wave_verify_failure`` makes for the same reason, one wave
    earlier. Step 1 settles it or nothing does.

    This stays ADVISORY. The integration PR is opened on every arm, before this
    existed and after: an operator who has learned that this event does not block
    integration must not one day find that it does.

    Deliberately NOT reached for a head ``error``. There the gate did not RUN, so
    there is no verdict to attribute and asking the base could only buy a second
    full clone and test run to compare against nothing.

    **A red base is no longer one answer.** Until 2026-08-27 the whole
    comparison was ``base.status == "failed"``, so a base that could not even
    COMPILE and a head that failed three assertions counted as the same
    failure. ``compare_failures`` asks the runner's own exit code instead, and
    the third arm below is what a changed failure MODE now buys.

    This seat ALARMS on ``FAILED_DIFFERENTLY``, and the wave gate one layer
    down deliberately does not park on it. The asymmetry is the point: this
    event is advisory and the integration PR opens on every arm, so the cost of
    alarming is an operator's attention, while the wave gate's action is a
    MEMOIZED park that nothing can ever clear.

    Args:
        head: What the command did on the accumulated plan branch, for the
            comparison. Its verdict is already known to be ``failed``; what is
            read here is HOW.
        base: What the same command did on the branch the plan was cut from.
        base_branch: Its name, for the sentence a human reads.

    Returns:
        The decision, in full.
    """
    if base.status == "passed":
        # The one arm that is byte-for-byte the old behaviour: green on the base
        # and red here, so work this plan merged is what broke it.
        return _PlanVerifyAttribution(
            alarm=True,
            reported_status="failed",
            detail=(
                f"CROSS-LEAF REGRESSION: the same command PASSES on "
                f"{base_branch}, so work this plan merged is what broke it."
            ),
        )
    if base.status == "failed":
        comparison = compare_failures(head.returncode, base.returncode)
        clause = base_failure_clause(
            comparison, base_branch, head.returncode, base.returncode
        )
        if comparison is FailureComparison.FAILED_DIFFERENTLY:
            # The plan branch is red, the base is red, and they are not red for
            # the same reason. Something this plan merged changed the failure
            # mode, which is the claim ``plan_verify_failed`` has always made.
            return _PlanVerifyAttribution(
                alarm=True,
                reported_status="failed",
                detail=f"CROSS-LEAF REGRESSION: {clause}.",
            )
        # Not shown to be a regression this plan caused. No event, and a
        # WARNING log line instead: ``plan_verify_failed`` means "a cross-task
        # regression was found" to every reader it has ever had, and publishing
        # it for a repository that was already red the same way is the untrue
        # claim this removes. ``INCOMPARABLE`` lands here too and must: an
        # unanswered question buys no alarm, and the clause says so in words
        # rather than claiming an identity nobody established.
        return _PlanVerifyAttribution(
            alarm=False,
            reported_status=_PLAN_VERIFY_UNATTRIBUTED,
            detail=f"{clause}, so it is not a regression this plan caused.",
        )
    # ``error`` and every skip: no ANSWER about the base branch. Fail closed,
    # exactly as before the comparison existed, and NAME the comparison that is
    # missing rather than implying it was made.
    why = base_comparison_unavailable(base_branch, base.status, base.reason)
    return _PlanVerifyAttribution(
        alarm=True,
        reported_status="failed",
        detail=(
            f"whether this failure pre-dates the plan could NOT be established, "
            f"because {why}, so it is reported as-is."
        ),
    )


class ReviewMixin:
    """PR-review and merge-approval half of the Orchestrator (see class Orchestrator)."""

    # Attributes provided by Orchestrator.__init__ (declared for mypy only).
    if TYPE_CHECKING:
        _tq: TaskQueue
        _bus: EventBus
        _git: Any
        _opus: Any
        _doc_indexer: Any
        _context_sync: Any
        _effective_settings: Any
        _llm_router: Any

        def _resolve_backend(self, repo_url: str) -> GitBackend:
            pass

    # A ref's repo is never backfilled from the PROJECT's repo_url.  The two are
    # independent: a fork PR lives in a different repository than the project,
    # so substituting the project slug would aim ``gh --repo`` at the wrong
    # repository and succeed doing it.  A repo-less ref is refused outright by
    # ``GitHubBackend._repo``, which is the single load-bearing guard.

    async def _fail_and_maybe_retry(
        self,
        task_id: str,
        task: dict[str, Any],
        project: dict[str, Any],
        feedback: str,
    ) -> None:
        """Fail a task, then requeue it or publish the terminal failure.

        Shared by the review-verdict path and the unparseable-ref path so both
        leave the task in a state the orchestration loop can move on from.

        Args:
            task_id: ID of the task being failed.
            task: The task row, read for its current ``attempt``.
            project: Project dict, read for ``max_retries``.
            feedback: Message stored as ``review_feedback`` and published.
        """
        await self._tq.fail_task(task_id, feedback)
        if task.get("implement_harness") == BRAIN_IMPLEMENTER:
            # A micro edit is never retried. There is no worker to send it back
            # to, and re-running the lane would rewrite the identical content,
            # find the index clean, and close as a no-op, which would report
            # "already correct" for a change its own verify gate had just
            # rejected. The caller estimated this as trivial and the estimate
            # was wrong, which is exactly the fact the rubric needs to stay
            # observable: it decides whether to dispatch this properly.
            logger.info(
                "task %s took the micro-edit lane; failing it terminally rather "
                "than retrying, so a mis-sized estimate stays visible",
                task_id,
            )
            self._bus.publish(
                {"type": "task_failed", "task_id": task_id, "feedback": feedback}
            )
            return
        if int(task["attempt"]) < int(project["max_retries"]):
            await self._tq.retry_task(task_id)
            self._bus.publish(
                {
                    "type": "task_retry",
                    "task_id": task_id,
                    "attempt": int(task["attempt"]) + 1,
                }
            )
        else:
            self._bus.publish(
                {
                    "type": "task_failed",
                    "task_id": task_id,
                    "feedback": feedback,
                }
            )

    def _review_error_streaks(self) -> dict[str, int]:
        """Per-task consecutive review-error counts, for this process only.

        An in-memory dict lazily attached to the instance, exactly like
        ``_branch_delete_failures`` and ``_repo_probe_failures`` in
        ``ReconcileMixin`` and for the same reasons: ``Orchestrator.__init__``
        lives outside this file, and a restart clearing the count is CORRECT
        because a restart may itself be what fixed the credential or the
        gateway the failures were caused by.

        Bounded in practice: an entry is removed when a review completes and
        when the cap fires, so only tasks that are STILL failing hold a key,
        and each is a single int.
        """
        streaks: dict[str, int] | None = getattr(self, "_review_errors", None)
        if streaks is None:
            streaks = {}
            self._review_errors = streaks
        return streaks

    async def _handle_review_error(
        self,
        task: dict[str, Any],
        project: dict[str, Any],
        log: Any,
        exc: Exception,
    ) -> None:
        """Decide what a failed review attempt means, and bound the retrying.

        Two classes, and only one of them may wait indefinitely.

        A genuine throttle parks ``opus_state`` before it re-raises, so the
        ``is_available()`` gate at the top of ``review_task`` short-circuits
        every following tick before a call is made. Waiting is therefore free,
        and failing the task for it would blame a worker for a subscription
        window and burn a retry that the wait returns for nothing.

        Everything else is bounded. An auth failure and a gateway 403/5xx are
        unavailabilities too, but they never park, so "wait for it to clear"
        meant a real provider call every ``loop_interval`` seconds forever
        while the task sat in REVIEWING -- which counts as active, so the plan
        could neither complete nor publish ``plan_stalled``. Past the cap the
        task is failed, which is what the unparseable-``pr_url`` arm one branch
        over already does with a review that cannot start: the plan has to be
        able to reach a terminal state.

        Args:
            task: The task row being reviewed.
            project: Its project row, read for ``max_retries``.
            log: The task-scoped logger ``review_task`` already built.
            exc: What the diff fetch or the brain call raised.
        """
        task_id = str(task["id"])
        streaks = self._review_error_streaks()
        if (
            isinstance(exc, ProviderRateLimitError)
            or not await self._opus.is_available()
        ):
            log.warning(
                "review is waiting on a throttled provider (%s); the task keeps "
                "its attempt and nothing is spent until the limit clears",
                exc,
            )
            return

        streak = streaks.get(task_id, 0) + 1
        if streak < REVIEW_ERROR_ATTEMPT_CAP:
            streaks[task_id] = streak
            log.warning(
                "review attempt %d of %d failed (%s: %s); retrying on the next pass",
                streak,
                REVIEW_ERROR_ATTEMPT_CAP,
                type(exc).__name__,
                exc,
            )
            return

        streaks.pop(task_id, None)
        # Worded for the FLOOR model that reads it next: ``core/worker_bible``
        # injects this string verbatim into the re-dispatched worker's prompt,
        # and a sentence that blamed the change would send that worker to fix a
        # defect nobody has observed. It says the REVIEWER did not run.
        feedback = (
            f"Review could not run: the reviewer failed on "
            f"{REVIEW_ERROR_ATTEMPT_CAP} consecutive attempts "
            f"({type(exc).__name__}: {exc}). The change itself was never "
            "judged, so nothing here says it is wrong."
        )
        log.warning("%s Failing the task so the plan can progress.", feedback)
        await self._fail_and_maybe_retry(task_id, task, project, feedback)

    async def _review_diff_for(
        self,
        backend: Any,
        ref: PullRequestRef,
        task: dict[str, Any],
        log: Any,
    ) -> tuple[str, str]:
        """Return the diff to review and a phrase naming what it covers.

        In single-branch (auto-delegate) mode every task pushes to one shared
        work branch, so the pull request accumulates every task's commits. A
        reviewer shown all of them failed every task after the first for
        touching files outside its scope, and ``core/outcome_recorder`` wrote
        that FAIL against a worker that had done its own task correctly. With a
        base sha recorded at dispatch the range can be bounded to this task's
        own commits. See
        ``docs/superpowers/plans/2026-08-14-review-scope-single-branch.md``.

        Args:
            backend: The resolved git backend.
            ref: The pull request being reviewed.
            task: The task row, for ``review_base_sha``.
            log: The task-scoped logger.

        Returns:
            ``(diff, scope)``. ``scope`` describes what the diff covers and is
            carried into the empty-diff decision, because "this pull request has
            no commits" and "this task added no commits of its own" are
            different facts about the same review.
        """
        base_sha = task.get("review_base_sha")
        pr_scope = f"the pull request {task['pr_url']}"
        if not base_sha:
            # NULL is the pre-column behavior and every two-tier row's normal
            # state: the per-task branch already carries only this task's work.
            return await backend.get_diff(ref), pr_scope

        scoped = getattr(backend, "get_diff_since", None)
        if scoped is None:
            # A backend that predates the capability. The degradation is safe,
            # but silence is not: the review would then be scoped differently
            # from what the task row says, with nothing recording it.
            log.warning(
                "backend %r cannot bound a diff by sha; reviewing the whole "
                "pull request instead of this task's own commits",
                getattr(backend, "name", backend),
            )
            return await backend.get_diff(ref), pr_scope
        return (
            await scoped(ref, base_sha),
            f"this task's own commits after {base_sha}",
        )

    async def _record_task_outcome(
        self,
        task: dict[str, Any],
        project: dict[str, Any],
        *,
        task_type: str | None,
        files_touched: int | None,
        loc_delta: int | None,
        outcome: str,
        failure_class: str | None,
    ) -> None:
        """Write ONE calibration row for one attempt at one leaf.

        A method rather than a closure inside ``review_task`` because two paths
        end an attempt and both have to describe the row the same way. The
        review-verdict path owned this list for as long as it was the only
        caller, and the cost of that was measured on 2026-08-26: an empty diff
        returns from ``review_task`` above the closure's own definition, so the
        one failure shape a calibration loop most wants to see -- a worker that
        produced nothing -- could not be recorded at all. Splitting the field
        list in two to fix that would only have moved the drift somewhere it
        takes longer to notice.

        Args:
            task: The task row the attempt belongs to.
            project: Its project row, for the attribution fallbacks.
            task_type: The leaf's task type from the plan graph, or None.
                ``summarize_outcomes`` groups by it, so None files the row under
                "unknown" rather than raising.
            files_touched: Files the attempt changed, or None when the size of
                the change was never measured. Never a guessed zero: see
                ``leaf_triage._unknown``.
            loc_delta: Net lines changed, on the same terms.
            outcome: ``pass``, ``fail``, or a value that deliberately votes
                neither way (see the supply-chain gate's ``blocked``).
            failure_class: A ``FailureClass`` value, or None for a non-failure.
                ``record_outcome`` derives ``counts_against_worker`` from THIS
                and nothing else, so it is also the attribution decision.
        """
        await record_outcome(
            self._tq._db,
            task_id=str(task["id"]),
            plan_id=task.get("plan_id"),
            project_id=project["id"],
            # Attribution follows the model that ACTUALLY implemented this
            # attempt. Crediting the original worker with an escalated
            # success teaches the calibration loop a lie.
            model_name=(
                task.get("implement_model")
                or project.get("agent_model")
                or project.get("model_name")
            ),
            harness=task.get("implement_harness") or project.get("harness"),
            task_type=task_type,
            files_touched=files_touched,
            loc_delta=loc_delta,
            context_tokens_est=None,
            attempt=int(task["attempt"]),
            outcome=outcome,
            failure_class=failure_class,
            emitter=getattr(self, "_emitter", None),
        )

    async def _decide_empty_pr_diff(
        self,
        task: dict[str, Any],
        project: dict[str, Any],
        plan: dict[str, Any] | None,
        log: Any,
        scope: str | None = None,
        task_type: str | None = None,
    ) -> None:
        """Decide what a pull request with no diff means, without asking the brain.

        The same fact-versus-verdict split the worker-reported ``no_changes``
        callback goes through, applied at the other end of the loop: the
        absence of a change is a fact, and what it MEANS is governance. So the
        evidence is the same evidence, the project's own verify command run
        against the branch the leaf was cut from, via ``no_change_outcome``
        (``resolve_no_change_run`` is the boolean wrapper over it, kept for
        callers that want only the answer).

        A review is NOT that evidence. An empty diff sent to the reviewer is a
        question about nothing, and a model that answers "pass" to it parked
        the task at the merge gate with a PR that would merge no change.

        Args:
            task: The task row being reviewed.
            project: Its project row.
            plan: The plan row, for the branch the leaf was cut from.
            log: The task-scoped logger ``review_task`` already built.
            scope: What was empty, as a phrase. Defaults to the pull request
                itself. In single-branch mode it is usually the task's own
                commit range instead, on a pull request that is full of other
                tasks' commits, and reporting THAT as "the pull request carries
                no diff" is a statement anyone can open the pull request and
                disprove.
            task_type: The leaf's task type, already resolved by the caller off
                the plan graph. Passed rather than re-derived: ``review_task``
                does that query before it ever reaches here, and a second one
                could read a graph that changed in between.
        """
        task_id = task["id"]
        subject = scope or f"the pull request {task['pr_url']}"
        log.warning(
            "review: %s carries no diff; deciding it as a fact "
            "rather than sending an empty change to the reviewer",
            subject,
        )
        decision = await self.no_change_outcome(task_id, project, plan)
        if decision.closed:
            return
        # ``why`` rather than one fixed sentence: the decision above declines
        # for four unrelated facts and only ONE of them is "the branch did not
        # verify clean". This string is stored on the task, published, and
        # injected into the next worker's prompt by the Bible, so a wrong one
        # sends a worker to fix a verification that never ran.
        feedback = (
            f"Review could not start: {subject} carries no diff, and {decision.why}."
        )
        await self.handle_declined_no_change(
            task, project, plan, decision, feedback, task_type=task_type
        )

    async def handle_declined_no_change(
        self,
        task: dict[str, Any],
        project: dict[str, Any],
        plan: dict[str, Any] | None,
        decision: NoChangeDecision,
        feedback: str,
        *,
        task_type: str | None,
    ) -> None:
        """Record and dispose of the attempt a DECLINED no-change just ended.

        The one description of what happens when an absent change is judged NOT
        to be a no-op. Two paths reach that judgement and they are the same
        event seen from different ends: an empty pull-request diff caught inside
        ``review_task``, and a worker that self-reported ``no_changes`` to
        ``api/internal.py``. Both mean the leaf produced nothing; only the first
        recorded anything or triaged anything, and the difference was invisible
        because each path reads as complete on its own.

        Measured live on 2026-08-26 on the second path: a leaf whose declared
        edit location was absent from the plan branch went 1 -> 2 -> 3 attempts
        with ``triage_decision`` NULL the whole way, and triage fired only on the
        one attempt that happened to come back through the review verdict.

        Two branches, and which one is taken is settled by
        ``decision.worker_attributable`` -- never by reading the reason text,
        which would start answering differently the day a sentence is reworded:

        - NOT attributable (the branch could not be resolved, the gate raised, a
          configured gate could not reach the repository): the same class as a
          reviewer that could not run. Fail and maybe retry, never triage, and
          never RECORD. ``record_outcome`` derives ``counts_against_worker`` from
          ``failure_class`` alone, so the only way to write a non-voting row is
          to name one of the three classes that mean something else
          (``provider_error``, ``worker_blocked``, ``needs_stronger_model``),
          which trades a false row in the calibration set for a false cause in
          the audit trail. And triage's worst answer, ``human``, is terminal and
          irreversible: spending it on a gateway blip gates a healthy leaf
          forever. The fault is not silent either way -- ``no_change_outcome``
          logs it at WARNING, the whole ``why`` is stored on
          ``tasks.review_feedback``, and the failure is published.
        - Attributable: record the attempt, then offer the leaf to adaptive
          triage. The BOUND on who gets triaged is not re-derived here; it lives
          in ``_triage_then_fail`` (``attempt >= 2``, one call per leaf lifetime)
          precisely so a leaf cannot buy a second brain call by failing through
          the other path.

        The evidence passed to triage is ``(0, 0, "")``, identical to what the
        review path has always passed for this shape, and honest on both: the
        change is zero files and zero lines because there IS no change, and
        there is no diff to show. ``leaf_triage._unknown`` reserves ``None`` for
        "nobody looked", which would be the false statement here.

        Args:
            task: The task row whose attempt just ended.
            project: Its project row.
            plan: The plan row, or None when the task has no plan graph.
            feedback: What is stored, published, and injected into the next
                worker's prompt. The CALLER writes it, because the two paths are
                honestly describing different observations -- "this pull request
                carries no diff" and "the worker reported no changes" -- and one
                fixed sentence for both would be false on one of them.
            task_type: The leaf's type from the plan graph, resolved by the
                caller (``graph_task_type``).
        """
        task_id = str(task["id"])
        await self.record_declined_no_change(
            task, project, decision, task_type=task_type
        )
        if not decision.worker_attributable:
            await self._fail_and_maybe_retry(task_id, task, project, feedback)
            return
        await self._triage_then_fail(task, project, plan, feedback, 0, 0, "")

    async def handle_worker_no_change(
        self,
        task: dict[str, Any],
        project: dict[str, Any],
        plan: dict[str, Any] | None,
        decision: NoChangeDecision,
        feedback: str,
        *,
        task_type: str | None,
    ) -> None:
        """``handle_declined_no_change`` for a task that is NOT parked in review.

        The callback entry point, and it exists for one reason: triage's
        rate-limit branch DEFERS by leaving the task exactly where it is, and
        "where it is" is not the same place on the two paths. From a review that
        is REVIEWING, an active state the next tick re-enters for free; from
        this callback it is IN_PROGRESS with the agent run already completed,
        which nothing ever looks at again. ``_settle_if_triage_deferred`` holds
        the whole account and the one implementation, so the sibling route added
        for a worker-reported ``failed`` cannot answer it differently.

        A ``no_changes`` worker has no pull request by definition, so REVIEWING
        is not available as a resting state here either: ``review_task`` returns
        immediately on a NULL ``pr_url``, which is the same wedge one state over.

        Args:
            task: The task row whose attempt just ended, read before this.
            project: Its project row.
            plan: The plan row, or None.
            decision: What ``no_change_outcome`` decided, in full.
            feedback: Stored, published, and given to the next worker.
            task_type: The leaf's type from the plan graph.
        """
        status_before = task["status"]
        await self.handle_declined_no_change(
            task, project, plan, decision, feedback, task_type=task_type
        )
        await self._settle_if_triage_deferred(task, project, feedback, status_before)

    async def handle_worker_run_failure(
        self,
        task: dict[str, Any],
        project: dict[str, Any],
        plan: dict[str, Any] | None,
        feedback: str,
        *,
        task_type: str | None,
    ) -> None:
        """Record and dispose of an attempt a WORKER-REPORTED failure just ended.

        The third route into the shared gate, and the one that carries the most
        traffic: ``failed`` is what both harness entrypoints report for every
        non-zero exit, so it is the commonest way a leaf ends an attempt. Until
        2026-08-26 it reached NEITHER half of the governance the other two
        routes reach. Measured live on ``adiatmaja/playground`` (plan
        ``c03b3ff6``, leaf 2): ``attempt`` 4 against ``max_retries`` 3,
        ``triage_decision`` NULL the whole way, zero triage lines in the
        orchestrator log for the plan, and no ``task_outcomes`` row at all.

        That is why a ``split`` decision had never been observed on a real
        repository across seven probes. The standing explanation was leaf
        sizing; the actual cause is that the failure shape adaptive splitting
        exists to answer could not reach the question.

        Worker-attributable, and the derivation is the module's own line rather
        than a new one. The reviewer-error and unparseable-``pr_url`` paths are
        excluded because neither says anything about the LEAF: the worker's
        output was never examined. A worker-reported failure is the opposite --
        the worker was handed the leaf, ran, and did not complete it. The one
        systematic cause that is about the endpoint instead of the worker is
        peeled off by the caller's ``is_provider_error`` check over the container
        log, exactly as it already is for the retry budget, and never arrives
        here. What remains is uncertain in its WHY and certain in its subject.

        The evidence passed to triage is ``(None, None, "")`` -- the OPPOSITE of
        the no-change route's measured ``(0, 0, "")``. Nothing counted anything
        on this path: no diff was fetched, ``agent_runs`` carries no file count,
        and a run that failed may well have committed and pushed before a later
        step aborted the script. ``leaf_triage._unknown`` reserves ``None`` for
        exactly that, and a zero here would tell the triage brain the worker
        wrote nothing, which is the strongest push it has toward ``escalate`` or
        ``human``.

        The calibration row is written on EVERY attempt, not only from the
        second, matching the review path: the triage bound is about spending a
        brain call, and a calibration set that only ever saw second attempts
        would be the same denominator hole one attempt over.

        Args:
            task: The task row whose attempt just ended, read before this.
            project: Its project row.
            plan: The plan row, or None when the task has no plan graph.
            feedback: Stored, published, and given to the next worker.
            task_type: The leaf's type from the plan graph.
        """
        status_before = task["status"]
        await self._record_task_outcome(
            task,
            project,
            task_type=task_type,
            files_touched=None,
            loc_delta=None,
            outcome="fail",
            failure_class=FailureClass.RUN_FAILED.value,
        )
        await self._triage_then_fail(task, project, plan, feedback, None, None, "")
        await self._settle_if_triage_deferred(task, project, feedback, status_before)

    async def _settle_if_triage_deferred(
        self,
        task: dict[str, Any],
        project: dict[str, Any],
        feedback: str,
        status_before: str,
    ) -> None:
        """Fail a callback-route task that triage left exactly where it was.

        The one description of the hazard EVERY worker-callback route has and
        the review route does not, held here rather than copied per route: the
        second copy is how one of them comes to strand a task the other settles.

        Triage's rate-limit branch DEFERS by deciding nothing and leaving the
        task in place. From ``review_task`` that place is REVIEWING, the only
        state that costs nothing to wait in -- no attempt consumed, no decision
        stamped, and REVIEWING counts as ACTIVE so the plan neither completes
        short of the leaf nor reports itself stalled. The next tick re-enters
        ``review_task`` and triages it once the limit clears.

        From a worker callback the task is IN_PROGRESS and its agent run was
        completed a few lines earlier. Nothing re-enters it: ``reconcile_runs``
        walks RUNNING runs only, so a completed run is never looked at again,
        and IN_PROGRESS counts as active, so the plan hangs forever with no
        error and no stall event. REVIEWING is not available as a resting state
        either -- a task with a NULL ``pr_url`` returns immediately from
        ``review_task``, which is the same wedge one state over.

        So the disposition is verified against the DATABASE rather than assumed
        from a return value. A task still sitting in the status it arrived in
        was deferred (or decided nothing) and takes the ordinary
        fail-and-maybe-retry path instead. That costs one retry on a throttle;
        the alternative is a plan that never finishes because a rate limit
        cleared five minutes later and nobody was listening.

        Args:
            task: The task row as it was read before the disposition.
            project: Its project row, for ``max_retries``.
            feedback: Stored, published, and given to the next worker.
            status_before: The status the task arrived in.
        """
        task_id = str(task["id"])
        settled = await self._tq.get_task(task_id)
        if settled is not None and settled["status"] == status_before:
            logger.warning(
                "Task %s was offered to triage from the worker callback and "
                "came back still %s, so triage deferred rather than deciding. "
                "Failing it normally: unlike a review, nothing re-enters a "
                "callback, so waiting here would strand the task and the plan.",
                task_id,
                status_before,
            )
            await self._fail_and_maybe_retry(task_id, task, project, feedback)

    async def review_task(self, task_id: str, project: dict[str, Any]) -> None:
        """Review a task PR with Opus and merge or retry accordingly."""

        task = await self._tq.get_task(task_id)
        if task is None:
            logger.warning("Task %s not found for review", task_id)
            return
        if task["status"] != TaskStatus.REVIEWING or task["pr_url"] is None:
            return

        log = task_logger(logger, plan_id=task.get("plan_id"), task_id=task_id)
        log.info("reviewing task (pr=%s)", task.get("pr_url"))

        if not await self._opus.is_available():
            await self._opus.queue_action(
                {"action": "review", "task_id": task_id, "project_id": project["id"]}
            )
            self._bus.publish({"type": "opus_queued", "action": "review"})
            return

        backend = self._resolve_backend(project["repo_url"])
        try:
            ref = PullRequestRef.from_url(task["pr_url"])
        except ValueError:
            # Fail the task rather than return.  review_task is re-entered for
            # every REVIEWING task on every loop tick and REVIEWING counts as
            # active, so a bare return wedged the plan short of COMPLETED
            # forever while suppressing plan_stalled (which requires
            # ``not active``).  The only symptom was one log line per tick.
            unparseable = (
                "Review could not start: the task's pr_url "
                f"{task['pr_url']!r} is not a recognized pull-request reference."
            )
            log.warning("%s Failing the task so the plan can progress.", unparseable)
            await self._fail_and_maybe_retry(task_id, task, project, unparseable)
            return

        # Resolve plan_text for this task from the plan's opus_plan task list.
        plan_text_for_review: str | None = None
        task_type_for_outcome: str | None = None
        # The leaf's OWN acceptance check, raw. The decomposer emits one, the
        # standard hard-requires it and F3 validates that it is runnable -- and
        # until 2026-08-26 nothing ever ran it. It is resolved here, with the
        # other two, off the same graph entry: a second resolution could pair a
        # row with a different leaf and judge this task by a sibling's contract.
        leaf_verification: Any = None
        plan = await self._tq.get_plan(task["plan_id"])
        if plan is not None:
            graph_tasks = parse_graph_tasks(plan)
            # One extra query per review, to get the rows in the order the plan
            # graph is aligned to. Acceptable: a review already clones the PR
            # head and calls the brain, so a single indexed SELECT is noise
            # beside it, and the alternative (deriving the slug from
            # ``branch_name``) resolves nothing in single-branch mode and
            # silently reviews the diff against no plan contract.
            ordered_rows = await self._tq.get_tasks_for_plan(task["plan_id"])
            task_slug = resolve_task_slug(
                task, build_graph_index(ordered_rows), graph_tasks
            )
            plan_task = slug_to_graph_task(graph_tasks).get(task_slug, {})
            plan_text_for_review = plan_task.get("plan_text")
            task_type_for_outcome = plan_task.get("task_type")
            leaf_verification = plan_task.get("verification")

        checkout: str | None
        with tempfile.TemporaryDirectory() as _checkout_dir:
            try:
                await backend.checkout(ref, _checkout_dir)
                checkout = _checkout_dir
            except Exception:  # noqa: BLE001 - degrade, never wedge review
                logger.exception(
                    "review: PR-head clone failed; falling back to diff-only review"
                )
                checkout = None

            # Bench condition C runs decomposition WITHOUT the verify gate, to
            # isolate whether the measured effect is decomposition or
            # verification. Double-gated; see core/bench_mode.py.
            bench_disabled = verify_gate_disabled()
            # An all-whitespace command is truthy, so without normalizing it
            # the branch below shells a blank command, gets exit 0, and logs
            # "verify gate passed" for a gate that ran nothing.  Normalized, it
            # falls through to the _SKIP_NO_VERIFY_CMD arm, which is the honest
            # report and the one this project already means by "" and None.
            verify_cmd = (
                None
                if bench_disabled
                else normalize_verify_cmd(project.get("verify_cmd"))
            )
            review: dict[str, Any] | None = None
            # Set only when a CONFIGURED gate did not run. A gate that is not
            # configured is not a skip anyone needs warning about; one that is
            # configured and could not run is the whole point of this variable.
            gate_skipped: str | None = None
            # The SAME facts as ``gate_skipped``, widened to cover all five
            # outcomes so ``_review_scope_statement`` never has to infer a pass
            # from the absence of a skip. Initialized to the no-command reason
            # because that is what the final ``else`` arm below means, and an
            # unset default would be a sixth state nothing produces.
            verify_state: str = _SKIP_NO_VERIFY_CMD
            # Set only when the project gate went RED and the failure was shown
            # to pre-date this task. Not a pass, and never rendered as one.
            unattributed: _UnattributedVerify | None = None
            radius: BlastRadius | None = None
            if verify_cmd and checkout is not None:
                head_run = await run_verify(checkout, verify_cmd)
                passed, gate_output = head_run
                verify_state = _GATE_PASSED if passed else _GATE_FAILED
                if passed:
                    log.info("verify gate passed (`%s`)", verify_cmd)
                else:
                    log.warning(
                        "verify gate failed (`%s`): %s",
                        verify_cmd,
                        _log_excerpt(gate_output),
                    )
                    # A red project command is not yet a verdict about THIS
                    # task: on a dependent chain it is routinely the bar only
                    # the complete feature can clear. Who it belongs to is
                    # decided next, against the branch this work was cut from.
                    attribution = await self._attribute_head_verify_failure(
                        task=task,
                        project=project,
                        plan=plan,
                        ref=ref,
                        checkout=checkout,
                        verify_cmd=verify_cmd,
                        gate_output=gate_output,
                        head_code=run_exit_code(head_run),
                        leaf_verification=leaf_verification,
                        log=log,
                    )
                    review = attribution.review
                    verify_state = attribution.verify_state
                    unattributed = attribution.unattributed
            elif verify_cmd and checkout is None:
                # WARNING, and carried onto the merge gate below. This is the
                # same class of fault _verify_plan_branch already logs at
                # WARNING for the plan gate: a project that CONFIGURED a
                # mechanical gate did not get one, because the PR head could
                # not be cloned. At INFO, and with nothing on the parked-PR
                # event, the human at the merge gate sees a clean PASS and has
                # no way to know the gate never ran.
                gate_skipped = _SKIP_CHECKOUT_UNAVAILABLE
                verify_state = _SKIP_CHECKOUT_UNAVAILABLE
                log.warning(
                    "verify gate skipped: %s (`%s`); the reviewer verdict is "
                    "the ONLY evidence for this task",
                    _SKIP_CHECKOUT_UNAVAILABLE,
                    verify_cmd,
                )
            elif bench_disabled:
                verify_state = _SKIP_BENCH_MODE_DISABLED
                log.info("verify gate skipped: %s", _SKIP_BENCH_MODE_DISABLED)
            else:
                log.info("verify gate skipped: %s", _SKIP_NO_VERIFY_CMD)

            # Only fetch the diff / call the brain if the gate did not already
            # fail the task; on gate failure the diff is unused (verdict is fail).
            diff = ""
            if review is None:
                try:
                    diff, scope = await self._review_diff_for(backend, ref, task, log)
                except Exception as exc:  # noqa: BLE001 - bounded, never per-tick
                    # `gh pr diff` (or the bare repo) over a network nobody
                    # controls, on the same unguarded path as the brain call
                    # below and wedging the plan the same way.
                    await self._handle_review_error(task, project, log, exc)
                    return
                if not diff.strip():
                    # An empty diff is a FACT, not a verdict. Both backends
                    # fetch it through a checked command, so a non-zero exit
                    # raises and "" can only mean the command succeeded and
                    # printed nothing.
                    #
                    # Handing that to the brain as "the change" is how a
                    # review of nothing became a PASS parked at the merge gate
                    # with "parked at merge gate awaiting approval". The
                    # governance for an empty diff already exists one layer
                    # down and is reused rather than re-decided here.
                    #
                    # ``scope`` is what the emptiness is ABOUT, and the two are
                    # different facts: a whole pull request with no commits, or
                    # a task that added none of its own to a branch that is full
                    # of other tasks' work. Reporting the second as the first is
                    # a statement anyone can open the pull request and disprove.
                    #
                    # A decision was reached, so the error streak is cleared
                    # here as well as on the verdict path below. The bound
                    # counts CONSECUTIVE failures; a task that carried an old
                    # streak into a later outage would be failed on the first
                    # blip after it.
                    self._review_error_streaks().pop(task_id, None)
                    await self._decide_empty_pr_diff(
                        task, project, plan, log, scope, task_type_for_outcome
                    )
                    return
                # A micro edit is reviewed at the re-review tier. On a micro
                # edit the mechanical gate carries nearly all the value: a
                # typo fix is not caught by a model reading a diff, it is
                # caught by `verify_cmd` running the repository's tests.
                #
                # This buys nothing on a STOCK install and the lane never
                # claims it does. `core/roles.py` maps both review call sites
                # to the `review` role, and `call_site_chain` returns the ROLE
                # CHAIN whenever one is configured, ignoring the call site, so
                # the shipped `review: [sonnet, haiku]` runs sonnet either
                # way. The tier is correct on an install that configures
                # per-call-site models, and the lane's real saving is the
                # container, the clone and the worker turn.
                tier = (
                    "rereview"
                    if task.get("implement_harness") == BRAIN_IMPLEMENTER
                    else "first"
                )
                # The reviewer holds the diff and a real checkout, and until now
                # it was never told how widely used the things in that diff are.
                # A change can be correct in every line the diff shows and still
                # make a property in an unshown block inert; nothing in the diff
                # says so, so the reviewer's green was structurally unable to
                # observe that defect class. Fails open: see
                # ``_blast_radius_for_review``.
                radius, blast_section = await _blast_radius_for_review(diff, checkout)
                try:
                    review = await self._opus.review_diff(
                        diff,
                        task["description"] or task["title"],
                        model=project.get("agent_model"),
                        effort=project.get("agent_model_effort"),
                        tier=tier,
                        plan_text=plan_text_for_review,
                        cwd=checkout,
                        blast_radius=blast_section,
                    )
                except Exception as exc:  # noqa: BLE001 - bounded, never per-tick
                    # The call this whole function exists to make, and it had
                    # no arm at all. Anything it raised escaped into
                    # ``process_plan_once`` (aborting the rest of THIS plan's
                    # task loop for the tick) and then into ``run_once``'s
                    # per-plan quarantine, leaving the task REVIEWING with its
                    # attempt unspent and the next tick re-entering here.
                    await self._handle_review_error(task, project, log, exc)
                    return
        # A verdict was produced, so whatever was failing has stopped. See the
        # matching reset on the empty-diff path above.
        self._review_error_streaks().pop(task_id, None)
        # Stripped. Everything that is not exactly "pass" falls through to the
        # failure path, which comments on the PR, retries, and writes a `fail`
        # row that counts against the worker in the calibration data. A model
        # answering "pass " or "Pass" is agreeing, and the review contract
        # (models/schemas.OpusReviewPayload) is not applied on this path.
        verdict = str(review["verdict"]).strip().lower()
        feedback = str(review.get("feedback", ""))

        # The verdict itself, not just the verify gate that may precede it.
        # Before this, a PASS was discoverable only through
        # GET /api/approvals/pending or the dashboard, and a FAIL was silent
        # too; a terminal-only operator had no way to tell a passing review
        # from a hung one.  Deliberately excludes the feedback body (model
        # prose, can be long); the verdict word and the PR url are enough to
        # make the line greppable against a run.
        if verdict == "pass":
            log.info("review verdict: pass (pr=%s)", task["pr_url"])
        else:
            log.warning("review verdict: %s (pr=%s)", verdict, task["pr_url"])

        # Outcome recording: compute diff stats once, define helper.
        #
        # ``None``, not ``diff_stats("")``, when the verify gate failed before
        # the diff was ever fetched. ``diff_stats("")`` is ``(0, 0)``, and zero
        # files touched is the signature of a worker that did nothing: it is
        # written into ``task_outcomes`` as a positive claim (the columns are
        # nullable, and ``context_tokens_est=None`` is passed right beside it
        # for exactly this "unknown" case) and it is handed to the triage brain
        # as evidence, where it pushes the decision toward escalate or human.
        # The gate failing establishes nothing at all about the size of the
        # change.
        gate_failed_before_diff = not diff
        files_touched, loc_delta = (
            (None, None) if gate_failed_before_diff else diff_stats(diff)
        )

        async def _record(outcome: str, failure_class: str | None) -> None:
            # The four call sites below are unchanged; what the row CONTAINS
            # now lives in ``_record_task_outcome``, because the empty-diff path
            # returns above this definition and has to write the same row.
            await self._record_task_outcome(
                task,
                project,
                task_type=task_type_for_outcome,
                files_touched=files_touched,
                loc_delta=loc_delta,
                outcome=outcome,
                failure_class=failure_class,
            )

        flagged = destructive_deletions(diff)
        if flagged and verdict == "fail":
            # Gate already failed; annotate so the reviewer's feedback includes
            # the specific files with large net deletions.
            feedback = f"Large net deletions in {flagged} — verify intentional. " + (
                feedback or ""
            )
        elif flagged and verdict == "pass":
            # Brain said PASS; treat the guard as advisory — surface the flagged
            # files as a warning in the feedback rather than overriding the
            # reviewer's verdict. The human can still reject at the approval gate.
            feedback = (
                f"[diff-guard] Warning: large net deletions in {flagged}. "
                "Brain review said PASS; confirm the deletions are intentional "
                "before merging. " + (feedback or "")
            )

        if verdict == "pass":
            # What this green actually covers, attached to the green itself.
            #
            # PASS only, and that is not an oversight. A FAIL never parks at the
            # merge gate, and its feedback is injected verbatim into the next
            # worker's prompt by ``core/worker_bible``; a sentence about what
            # the REVIEW observed is noise to a worker at best, and a floor
            # model reading "verify gate did not run" as an instruction at
            # worst.
            scope_statement = _review_scope_statement(
                checkout_available=checkout is not None,
                verify_state=verify_state,
                verify_cmd=verify_cmd,
                radius=radius,
                unattributed=unattributed,
            )
            # Into the STORED feedback, not only onto the event. The event
            # reaches whoever happened to be watching the stream;
            # ``tasks.review_feedback`` is what `praxis task`, MCP `poll_task`
            # and the dashboard render for a parked PR, and that is where the
            # person about to click approve is actually looking.
            feedback = (
                f"{feedback}\n\n{scope_statement}" if feedback else scope_statement
            )

            # Supply-chain gate: check for added dependencies and secrets.
            supply_chain = added_dependencies(diff) + detect_secrets(diff)
            if supply_chain:
                feedback = (
                    f"[supply-chain] Blocked: {supply_chain}. "
                    "Review added dependencies and secrets before merging. "
                    + (feedback or "")
                )
                # Not a pass. The reviewer said pass, this gate blocked the
                # merge anyway, and recording "pass" taught the calibration
                # loop that a diff nothing would merge was a clean success by
                # this model. Recording "fail" would be the opposite lie, so
                # the claim is withdrawn instead: ``fetch_recent_outcomes``
                # counts only 'pass' and qualifying 'fail' rows, so a distinct
                # value keeps the row auditable without voting either way.
                await _record("blocked", None)
                await self._tq.mark_passed(task_id, feedback)
                self._bus.publish(
                    {
                        "type": "task_supply_chain_gate",
                        "task_id": task_id,
                        "pr_url": task["pr_url"],
                        "findings": supply_chain,
                        "branch": task["branch_name"],
                    }
                )
                return

            # Judge the branch the merge ACTUALLY writes to, whenever we know it.
            # `backend.merge(ref)` targets `ref.base`, so gating on
            # `plans.plan_branch_name` asked the protected-branch carve-out about
            # a different branch entirely: in auto-delegate single-branch mode
            # dispatch reuses one caller-named work branch while basing it on the
            # project default, so the two diverge and an auto-merge straight into
            # `main` passed a gate whose whole purpose is to forbid exactly that.
            #
            # PARTIAL FIX, and the limit is structural. `PullRequestRef.from_url`
            # recovers a base only for a `praxis-local://` ref; a GitHub PR URL
            # does not encode one, so it yields `base=""`. Gating a GitHub PR on
            # that empty string would treat every base as protected and silently
            # disable auto-merge repo-wide, so GitHub keeps the old plan-branch
            # behavior and its half of the bug. Closing that half needs the PR's
            # real base, which means new surface: a `base_branch(ref)` method on
            # `GitBackend` backed by `gh pr view --json baseRefName`, or a base
            # column on `tasks` populated at dispatch.
            base_branch = ref.base or (plan.get("plan_branch_name") if plan else None)
            if auto_merge_eligible(project, base_branch):
                await _record("pass", None)
                await backend.merge(ref)
                await self._tq.mark_merged(task_id)
                await self._sync_plan_checkbox(task)
                self._bus.publish(
                    {
                        "type": "task_completed",
                        "task_id": task_id,
                        "pr_url": task["pr_url"],
                    }
                )
                # The third merge site, and it lands exactly what the other two
                # land: in single-branch mode N tasks share ONE pull request, so
                # merging it puts every sibling's work on the base branch too.
                # Without this they stayed PASSED on an already-merged PR, and
                # unlike the operator-driven path nobody typed a verb here, so
                # there was no one in the loop to notice. LAST, and never before
                # the task the merge was FOR is recorded, matching
                # ``approve_task_merge``.
                await self._sweep_merged_siblings(task)
                return
            # Default: park the reviewed PR for explicit human approval.
            await _record("pass", None)
            await self._tq.mark_passed(task_id, feedback)
            if gate_skipped:
                log.warning(
                    "parked at merge gate awaiting approval (pr=%s), but the "
                    "configured verify gate did NOT run: %s",
                    task["pr_url"],
                    gate_skipped,
                )
            elif unattributed is not None:
                # A separate arm, not folded into ``gate_skipped``: that
                # variable means "a configured gate could not run", and this
                # gate ran and went RED. Logging one as the other would be the
                # same overclaim in the opposite direction.
                log.warning(
                    "parked at merge gate awaiting approval (pr=%s), but the "
                    "project verify command is RED on this PR head; it fails "
                    "identically on %s, so it was not attributed to this task",
                    task["pr_url"],
                    unattributed.base_branch,
                )
            else:
                log.info(
                    "parked at merge gate awaiting approval (pr=%s)", task["pr_url"]
                )
            self._bus.publish(
                {
                    "type": "task_awaiting_merge",
                    "task_id": task_id,
                    "pr_url": task["pr_url"],
                    "verdict": verdict,
                    "review_summary": feedback,
                    "branch": task["branch_name"],
                    # None on the ordinary path. Non-null means the project's
                    # own mechanical gate did not run for this PASS, which the
                    # human approving the merge is entitled to see.
                    "verify_gate_skipped": gate_skipped,
                    # The same entitlement, stated positively and in full: what
                    # this PASS was based on, rather than only the one thing
                    # that was missing from it.
                    "review_scope": scope_statement,
                }
            )
            return

        # NULL, and it is the third answer this line has to be able to give.
        # ``_GATE_UNCOMPARED`` means the project command went red on the PR head
        # and whether that pre-dates this task could NOT be established, because
        # the base branch could not be ASKED. Both attributable classes below
        # count against the worker in ``failure_taxonomy``, so either one would
        # assert the very thing the stored feedback says was not established, and
        # ``fetch_recent_outcomes`` would hand it to the capability gate as
        # capability. There is no honest class to substitute: the three
        # non-voting ones each name a different CAUSE (``provider_error``,
        # ``worker_blocked``, ``needs_stronger_model``), which would trade a false
        # row in the calibration set for a false cause in the audit trail. A NULL
        # names no cause at all, and ``fetch_recent_outcomes`` requires
        # ``failure_class IN (...)`` for a ``fail`` row, so the row is written,
        # auditable and countable while voting neither way.
        #
        # Keyed on the STATE, not on the marker text: the marker is still in this
        # feedback (the gate really did fail) and must stay there, so a text test
        # cannot tell the two apart.
        if verify_state == _GATE_UNCOMPARED:
            fail_class = None
        elif _VERIFY_FAIL_MARKER in feedback:
            fail_class = FailureClass.VERIFY_FAIL.value
        else:
            fail_class = FailureClass.FIXABLE_IN_PLACE.value
        await _record("fail", fail_class)
        await backend.comment(ref, feedback)

        await self._triage_then_fail(
            task, project, plan, feedback, files_touched, loc_delta, diff
        )

    async def _triage_then_fail(
        self,
        task: dict[str, Any],
        project: dict[str, Any],
        plan: dict[str, Any] | None,
        feedback: str,
        files_touched: int | None,
        loc_delta: int | None,
        diff: str,
    ) -> None:
        """Offer a repeatedly-failing leaf to adaptive triage, then fail it.

        The adaptive-triage GATE, extracted so that being worker-attributable is
        the thing that decides who gets triaged, rather than which line of
        ``review_task`` happened to reach the failure. Only two paths call this:
        the review verdict, and an empty diff whose decline was about the
        worker. The reviewer-error path and the unparseable-``pr_url`` path call
        ``_fail_and_maybe_retry`` directly and deliberately, because neither says
        anything about the leaf -- triaging them would spend a brain call to
        reason about an infrastructure fault, and ``human`` would gate the leaf
        permanently over a gateway blip.

        The FIRST worker-attributable failure keeps the cheap retry-with-feedback
        path (ADaPT, arXiv 2311.05772: decompose only when the executor actually
        fails; one failure is not yet evidence about the leaf's size). From the
        SECOND on, ask the brain whether the leaf should be retried, split,
        escalated, or handed to a human. Bounded to one triage call per leaf
        lifetime by ``tasks.triage_decision``, and the bound is shared across
        both callers: a leaf triaged from one path must not buy a second call by
        failing through the other.

        The task is deliberately left REVIEWING across the brain call rather
        than failed first: ``_fail_and_maybe_retry`` is the only owner of the
        fail-then-maybe-retry transition, and pre-failing here would widen the
        existing crash window between that FAILED write and the retry requeue
        from two DB writes to a whole network round trip, turning a crash during
        triage into a silently terminal task that still had retries left. Every
        triage branch writes its own terminal or requeued state, and a REVIEWING
        task simply gets re-reviewed on the next tick.

        Args:
            task: The task row that failed.
            project: Its project row.
            plan: The plan row, or None when the task has no plan graph.
            feedback: What is stored, published, and injected into the next
                worker's prompt, and what the evidence pack reports as the
                reason this attempt failed.
            files_touched: Files changed by the failed attempt, or None when the
                size of the change is genuinely unknown. Never a guessed zero:
                see ``leaf_triage._unknown``.
            loc_delta: Net lines changed, on the same terms.
            diff: The failed attempt's diff.
        """
        task_id = str(task["id"])
        attempt = int(task["attempt"])
        already_triaged = bool(task.get("triage_decision"))
        if attempt >= 2 and not already_triaged and self._llm_router is not None:
            handled = await self._run_leaf_triage(
                task, project, plan, feedback, files_touched, loc_delta, diff
            )
            if handled:
                return

        await self._fail_and_maybe_retry(task_id, task, project, feedback)

    async def _triage_leaf(
        self, evidence: TriageEvidence, project_id: str | None
    ) -> TriageDecision:
        """Seam for the triage brain call, so tests can substitute it.

        Routed through the BRIDGE, not the bare router: this seat called
        ``LLMRouter.run`` directly and so was the one brain call in the process
        that could not park ``opus_state`` on a throttle.
        """
        return await triage_leaf(
            evidence,
            parking_brain_runner(self._opus, self._llm_router),
            project_id,
        )

    async def _run_leaf_triage(
        self,
        task: dict[str, Any],
        project: dict[str, Any],
        plan: dict[str, Any] | None,
        feedback: str,
        files_touched: int | None,
        loc_delta: int | None,
        diff: str,
    ) -> bool:
        """Triage a twice-failed leaf and act on the decision.

        Args:
            task: The task row being reviewed.
            project: Its project row.
            plan: The plan row, or None when the task has no plan graph.
            feedback: The reviewer's verdict text.
            files_touched: Files changed by the failed attempt, or None when
                the verify gate failed before any diff was fetched, so the
                size of the change is genuinely unknown.
            loc_delta: Net lines changed by the failed attempt, or None for the
                same reason.
            diff: The failed attempt's diff.

        Returns:
            True when this call took responsibility for the task's next state,
            so the caller must NOT fall through to the plain retry path. That
            includes deliberately leaving it REVIEWING for a later pass when
            the brain is throttled: deciding nothing is a decision about the
            next state too, and falling through would consume a retry for a
            limit that clears by itself. False keeps the old behavior (no plan
            graph or no settings to work against, or a split the graph
            refused).
        """
        settings = self._effective_settings
        if plan is None or settings is None:
            return False

        task_id = task["id"]

        graph_tasks = parse_graph_tasks(plan)
        graph_task_count = len(graph_tasks)
        # One extra query per triage, for the rows in the order the plan graph
        # is aligned to. Triage runs at most once per leaf and is about to make
        # a brain call, so the cost is irrelevant next to deciding a split from
        # the task description because the slug resolved nothing.
        ordered_rows = await self._tq.get_tasks_for_plan(task["plan_id"])
        task_slug = resolve_task_slug(
            task, build_graph_index(ordered_rows), graph_tasks
        )
        plan_task: dict[str, Any] = slug_to_graph_task(graph_tasks).get(task_slug, {})

        # Every scoping argument this seam has, passed. ``project_id=None``
        # here meant the per-project capability override could never apply and
        # the new ``projects.context_window`` column never reached triage, so an
        # operator who set the window fixed the dispatch gate and left triage
        # sizing leaves for an 8 K worker, with nothing saying so. ``harness``
        # is what makes the per-harness declaration tier reachable for a model
        # string nobody enumerated.
        profile = await settings.capability_profile(
            project_id=project.get("id"),
            model=project.get("model_name"),
            harness=project.get("harness"),
            project_context_window=project.get("context_window"),
        )
        ladder = await settings.implement_escalation()
        ceiling = await settings.max_leaves_per_plan()
        escalation_index = int(task.get("escalation_index") or 0)
        pair = next_escalation(ladder, escalation_index)
        # A split child may never split again (one generation), and escalation
        # needs an untried rung.
        is_split_child = task.get("parent_task_id") is not None

        evidence = TriageEvidence(
            task_slug=task_slug,
            leaf_type=str(
                task.get("leaf_type") or plan_task.get("leaf_type") or "generic"
            ),
            plan_text=str(plan_task.get("plan_text") or task["description"]),
            profile=profile,
            attempts=[
                {
                    "attempt": int(task["attempt"]),
                    "files_touched": files_touched,
                    "loc_delta": loc_delta,
                    "diff": diff,
                    # None, not 1. This attempt reaches triage from two paths:
                    # a verify command that exited non-zero (``run_verify``
                    # returns a bool, so the code is not known even then) and a
                    # reviewer verdict on a change that no verify command ever
                    # ran against. Stating 1 told the triage brain a
                    # verification had failed on the second path, where none
                    # had been attempted.
                    "verify_exit_code": None,
                    "verify_tail": feedback[-_VERIFY_OUTPUT_MAX:],
                    "review_reason": feedback,
                }
            ],
            difficulty_score=task.get("difficulty_score"),
            remaining_leaf_budget=(
                0 if is_split_child else max(int(ceiling) - graph_task_count, 0)
            ),
            escalation_available=pair is not None,
        )

        try:
            decision = await self._triage_leaf(evidence, project["id"])
        except ProviderRateLimitError as exc:
            # DEFER, do not decide. ``opus_state`` was already parked on the
            # way out (``OpusBridge.run``), so nothing here writes it.
            #
            # The leaf is left REVIEWING, which is the only state that costs
            # nothing: no attempt is consumed, no triage decision is stamped,
            # and REVIEWING counts as active, so the plan neither completes
            # short of this leaf nor reports itself stalled while it waits.
            # The next pass finds the brain parked and returns at
            # ``review_task``'s own availability check without spending a call;
            # the pass after the limit clears reviews and triages it exactly as
            # this one would have.
            #
            # The cost is one duplicate review round trip and a second
            # ``task_outcomes`` row for this attempt once the wait ends. That
            # is the price of not answering ``human``, which fails the leaf
            # terminally and which no clock undoes.
            log = task_logger(logger, plan_id=task.get("plan_id"), task_id=task_id)
            log.warning(
                "triage deferred: %s is rate limited, so the leaf stays in "
                "review and is triaged again once the limit clears (%s)",
                exc.provider,
                exc,
            )
            self._bus.publish(
                {
                    "type": "task_triage_deferred",
                    "task_id": task_id,
                    "provider": exc.provider,
                    "reason": str(exc),
                }
            )
            return True
        # Stamped BEFORE acting, so a decision that cannot be applied still
        # burns the one triage call this leaf gets.
        await self._tq.record_triage_decision(task_id, decision.decision)
        # Recorded HERE, next to the stamp, and for all four decisions.
        # ``task_split`` and ``task_escalated`` below are ACTION records emitted
        # only once the decision could be applied, so on their own the trail
        # answered "how many splits landed" while the calibration question is
        # "of N triages, how many were split". A ``retry`` was a spent brain
        # call with no trace at all, and a split the graph refused looked
        # identical to a triage that never happened.
        emitter = getattr(self, "_emitter", None)
        if emitter is not None and plan.get("id"):
            await emitter.emit(
                TaskTriagedEvent(
                    plan_id=str(plan["id"]),
                    task_id=str(task_id),
                    leaf_slug=task_slug,
                    decision=decision.decision,
                    attempt=int(task["attempt"]),
                )
            )

        if decision.decision == "split" and not is_split_child and decision.children:
            children = decision.children
            slugs = child_slugs(task_slug, len(children))
            emitter = getattr(self, "_emitter", None)
            log = task_logger(logger, plan_id=task.get("plan_id"), task_id=task_id)
            # The graph slug each child WOULD get, so every message and event
            # below names something an operator can look up. The children still
            # carry the brain's own ids at this point; rewiring replaces them.
            #
            # An id carried by TWO children is left out, not resolved to the
            # last of them: nothing makes the brain's ids unique, this map is
            # last-wins, and the gate below rejects that shape outright. The
            # findings then print the ambiguous id raw, which
            # _render_child_violations already handles, rather than naming one
            # arbitrary child as though the other did not exist. Same answer
            # execute_plan_decompose.normalize_slugs gives on the same input.
            child_id_counts = Counter(child.id for child in children)
            id_to_slug = {
                child.id: slug
                for child, slug in zip(children, slugs, strict=True)
                if child_id_counts[child.id] == 1
            }

            # F3 on the CORRECTION, not only on the hypothesis. Policy 1 of
            # docs/decomposition-standard.md makes the first decomposition a
            # hypothesis and observed failure the signal to split, which means
            # the split is the case where the brain has ALREADY demonstrated it
            # got the sizing wrong. The triage prompt renders the same
            # core/leaf_templates block the decompose prompt renders, but until
            # this ran nothing graded the answer: a child with no Acceptance
            # section, a "review it manually" verification, or twice the
            # profile's file budget went straight to a worker.
            verdict = validate_split_children(children, profile)
            if verdict.soft:
                log.warning(
                    "Split children for %s carry SOFT findings; dispatching "
                    "anyway, because soft findings are warnings: %s",
                    task_slug,
                    _render_child_violations(verdict.soft, id_to_slug),
                )
            if not verdict.dispatchable:
                # The same degradation the refused-rewiring branch below uses,
                # for the same reason: triage is an OPTIMIZATION over the plain
                # retry path, so an unusable answer must cost the optimization
                # and never the task. The decision stays stamped above, so this
                # leaf does not buy a second triage call on its next failure.
                log.warning(
                    "Refusing the triage split for %s: the children fail the "
                    "leaf standard (%s); falling back to the plain retry path",
                    task_slug,
                    _render_child_violations(verdict.hard, id_to_slug),
                )
                if emitter is not None:
                    for violation in verdict.hard:
                        await emitter.emit(
                            LeafRejectedEvent(
                                plan_id=plan["id"],
                                leaf_slug=id_to_slug.get(
                                    violation.task_id, violation.task_id
                                ),
                                rule_id=violation.rule,
                            )
                        )
                return False

            # Scored, never gated: see score_split_children for why a rejection
            # here would be strictly worse than dispatching the child.
            #
            # FAILS OPEN, and that is the whole contract. Scoring reads the
            # settings layer and the outcomes table to produce a dashboard flag,
            # while ``run_once`` has no per-plan try/except: an exception raised
            # here would abort the orchestration tick for EVERY plan on the
            # install in order to lose a flag. An unscored child is a state
            # dispatch already reads correctly (a NULL score means "not
            # flagged", never "safe"), so degrading to it costs strictly less
            # than the alternative.
            child_scores: dict[str, float] = {}
            try:
                scored = await score_split_children(
                    children,
                    profile,
                    settings,
                    db=self._tq._db,
                    model=project.get("model_name"),
                    project_id=project.get("id"),
                )
            except Exception:  # noqa: BLE001 - scoring must never wedge a task
                log.exception(
                    "Could not score the split children of %s; they are "
                    "inserted UNSCORED, so dispatch will not flag them",
                    task_slug,
                )
            else:
                child_scores = {
                    child.id: scored.scores[child.id].p_success for child in children
                }
                if emitter is not None:
                    for child in children:
                        leaf_score = scored.scores[child.id]
                        await emitter.emit(
                            LeafDifficultyScoredEvent(
                                plan_id=plan["id"],
                                leaf_slug=id_to_slug[child.id],
                                p_success=leaf_score.p_success,
                                features=leaf_score.features,
                                flagged=leaf_score.p_success < scored.flag_below,
                            )
                        )
            try:
                child_ids = await self._tq.insert_split_children(
                    plan["id"],
                    task_id,
                    task_slug,
                    children,
                    difficulty_scores=child_scores,
                )
            except (KeyError, ValueError):
                # insert_split_children fails closed on a graph it cannot
                # rewire (parent slug absent, slugs already taken) and writes
                # nothing when it does.  Triage is an optimization over the
                # retry path, so degrade to that path rather than let this
                # abort the whole orchestration tick for every plan.
                logger.exception(
                    "could not apply the triage split for task %s; "
                    "falling back to the plain retry path",
                    task_id,
                )
                return False
            await self._tq.supersede_task(task_id, "split", decision.reason)
            self._bus.publish(
                {
                    "type": "task_split",
                    "task_id": task_id,
                    "child_task_ids": child_ids,
                    "child_slugs": slugs,
                    "reason": decision.reason,
                }
            )
            if emitter is not None:
                await emitter.emit(
                    TaskSplitEvent(
                        plan_id=plan["id"],
                        parent_slug=task_slug,
                        child_slugs=slugs,
                        failure_evidence_ref=task_id,
                    )
                )
            return True

        if decision.decision == "escalate" and pair is not None:
            # next_escalation() reads its index as "rungs already burned", so
            # the rung just taken has to be counted or the ladder never moves.
            await self._tq.set_task_implementer(
                task_id, pair.harness, pair.model, escalation_index + 1
            )
            await self._tq.retry_task(task_id)
            self._bus.publish(
                {
                    "type": "task_escalated",
                    "task_id": task_id,
                    "from_model": project.get("model_name"),
                    "to_model": pair.model,
                    "to_harness": pair.harness,
                    "reason": decision.reason,
                }
            )
            emitter = getattr(self, "_emitter", None)
            if emitter is not None:
                await emitter.emit(
                    TaskEscalatedEvent(
                        plan_id=plan["id"],
                        leaf_slug=task_slug,
                        policy=f"{pair.harness}/{pair.model}",
                    )
                )
            return True

        if decision.decision == "retry":
            if decision.refined_prompt:
                await self._tq.append_progress_note(
                    task_id,
                    f"TRIAGE CORRECTION (act on this now):\n{decision.refined_prompt}",
                )
            await self._tq.retry_task(task_id)
            self._bus.publish(
                {
                    "type": "task_retry",
                    "task_id": task_id,
                    "attempt": int(task["attempt"]) + 1,
                    "triage": "retry",
                }
            )
            return True

        # "human", or a split/escalate the caller cannot honour: park terminal.
        await self._tq.fail_task(task_id, f"Triage: {decision.reason}\n\n{feedback}")
        self._bus.publish(
            {
                "type": "task_failed",
                "task_id": task_id,
                "feedback": decision.reason,
                "triage": decision.decision,
            }
        )
        return True

    async def handle_clarification(self, task_id: str, project: dict[str, Any]) -> None:
        """Answer a blocked worker's question, or park it for a human."""
        task = await self._tq.get_task(task_id)
        if task is None:
            return
        if (
            task["status"] != TaskStatus.NEEDS_CLARIFICATION
            or task.get("clarification_state") != ASKED
        ):
            return

        if not await self._opus.is_available():
            await self._opus.queue_action(
                {"action": "clarify", "task_id": task_id, "project_id": project["id"]}
            )
            self._bus.publish({"type": "opus_queued", "action": "clarify"})
            return

        # Resolve plan_text (same slug lookup as review_task)
        plan_text: str | None = None
        plan = await self._tq.get_plan(task["plan_id"])
        if plan is not None:
            graph_tasks = parse_graph_tasks(plan)
            # One extra query per clarification, for the rows in the order the
            # plan graph is aligned to. A clarification is already a brain
            # round trip; answering a blocked worker without the plan in front
            # of the brain is the expensive outcome, not this SELECT.
            ordered_rows = await self._tq.get_tasks_for_plan(task["plan_id"])
            task_slug = resolve_task_slug(
                task, build_graph_index(ordered_rows), graph_tasks
            )
            plan_text = (
                slug_to_graph_task(graph_tasks).get(task_slug, {}).get("plan_text")
            )

        # Fix 1: cap clarification rounds to avoid an unbounded brain/worker loop.
        max_retries: int = int(project.get("max_retries") or 3)
        if int(task["attempt"]) >= max_retries:
            await self._park_awaiting_human(
                task_id,
                task["clarification_question"],
                "Clarification round limit reached; needs a human.",
            )
            return

        try:
            result = await self._opus.answer_clarification(
                question=task["clarification_question"] or "",
                task_description=task["description"] or task["title"],
                plan_text=plan_text,
                model=project.get("agent_model"),
                effort=project.get("agent_model_effort"),
                project_id=project["id"],
            )
        except (ValueError, json.JSONDecodeError) as exc:
            # Fix 3: malformed brain output must not abort the loop pass.
            await self._park_awaiting_human(
                task_id,
                task["clarification_question"],
                f"Brain returned malformed response: {exc}",
            )
            return

        # Fix 2: re-fetch the task; a human /clarify may have resolved it during
        # the await.  Only proceed if the task is still in the "asked" state.
        refetched = await self._tq.get_task(task_id)
        if refetched is None:
            return
        if (
            refetched["status"] != TaskStatus.NEEDS_CLARIFICATION
            or refetched.get("clarification_state") != ASKED
        ):
            return

        threshold = float(project.get("confidence_threshold") or 0.7)
        resolved = bool(result.get("resolved")) and (
            float(result.get("confidence") or 0.0) >= threshold
        )
        answer = str(result.get("answer", ""))
        # An answer is what "resolved" MEANS here. record_clarification_answer
        # writes this into the worker's progress note under "ANSWER TO YOUR
        # EARLIER QUESTION (act on this now)", so an empty one redispatches a
        # blocked worker having told it that its question was answered. A reply
        # that claims resolution without one has not resolved anything, and
        # _park_awaiting_human is the honest branch.
        resolved = resolved and bool(answer.strip())

        if resolved:
            await self._tq.record_clarification_answer(
                task_id, answer, state=ANSWERED_BY_BRAIN
            )
            self._bus.publish(
                {
                    "type": "clarification_resolved",
                    "task_id": task_id,
                    "answer": answer,
                }
            )
        else:
            await self._park_awaiting_human(
                task_id,
                task["clarification_question"],
                answer,
            )

    async def _park_awaiting_human(
        self,
        task_id: str,
        question: str | None,
        brain_note: str,
    ) -> None:
        """Set clarification_state to awaiting_human and publish task_needs_clarification."""
        await self._tq._db.execute(
            "UPDATE tasks SET clarification_state = ? WHERE id = ?",
            (AWAITING_HUMAN, task_id),
        )
        self._bus.publish(
            {
                "type": "task_needs_clarification",
                "task_id": task_id,
                "question": question,
                "brain_note": brain_note,
            }
        )

    async def approve_task_merge(self, task_id: str, project: dict[str, Any]) -> None:
        """Merge a human-approved, review-passed task.

        Args:
            task_id: ID of the task to merge.
            project: Project dict (needs ``repo_url``).

        Raises:
            ValueError: If the task is missing, not in the PASSED state, or
                carries a ``pr_url`` no backend can parse.  This path is
                operator-driven, so a bad ref must surface as an error rather
                than let an approve click silently do nothing.
        """
        task = await self._tq.get_task(task_id)
        if task is None:
            msg = f"Task {task_id} not found"
            raise ValueError(msg)
        if task["status"] != TaskStatus.PASSED or task["pr_url"] is None:
            msg = f"Task {task_id} is not awaiting merge"
            raise ValueError(msg)

        backend = self._resolve_backend(project["repo_url"])
        try:
            ref = PullRequestRef.from_url(task["pr_url"])
        except ValueError as exc:
            msg = f"Task {task_id} has an unparseable pr_url {task['pr_url']!r}"
            raise ValueError(msg) from exc
        # Human approval: no auto_merge gate or protected-branch check applies here.
        await backend.merge(ref)
        await self._tq.mark_merged(task_id)
        await self._sync_plan_checkbox(task)
        self._bus.publish(
            {
                "type": "task_completed",
                "task_id": task_id,
                "pr_url": task["pr_url"],
            }
        )
        # LAST, and never before the task the operator actually named is
        # recorded: one merge can land several tasks, but the one that was
        # asked for must not depend on any of the others.
        await self._sweep_merged_siblings(task)

    async def _sweep_merged_siblings(self, merged_task: dict[str, Any]) -> None:
        """Take every OTHER task the same merge landed out of the merge gate.

        In auto-delegate (single-branch) mode every task pushes to ONE shared
        work branch, so N tasks share ONE pull request. Merging it puts all of
        their work on the base branch at once, but only the task the operator
        named was marked merged. The rest stayed PASSED, kept appearing in
        ``praxis pending``, and kept offering ``praxis merge <id>`` on a pull
        request GitHub had already merged. It converges if the operator repeats
        the verb once per task, because ``merge_pr`` re-reads PR state and
        accepts an already-merged PR, but every state in between asserts that a
        merged pull request still needs approval.

        Scope is three conditions and each one is load-bearing:

        - the same ``pr_url``: a different pull request was not merged by this
          call, and marking its tasks merged would claim a merge nobody made.
        - ``GATED_STATUSES``: a sibling still REVIEWING has not passed review,
          so recording MERGED would invent a verdict and satisfy every
          dependent leaf waiting on it.
        - the same PROJECT: a local ref is
          ``praxis-local://pr?branch=...&base=...`` and encodes no repository,
          so two local projects that share a branch name share the exact
          ``pr_url`` string. Keyed on the URL alone, merging one project's work
          would mark another project's task merged. The project is read from
          the merged task's OWN plan rather than from a caller-supplied dict,
          so no caller can widen the scope by passing the wrong project.

        Each sibling gets the same follow-through the primary got, because the
        checkbox sync and the ``task_completed`` event are what the plan
        document and every SSE consumer read; a status flipped without them
        leaves the row quiet rather than merged.

        This never raises. The merge has ALREADY happened on the remote by the
        time it runs, so an error here would report a failure for work that
        landed, and the operator's next move on a failure is to merge again.
        Each sibling is handled independently for the same reason: one row that
        cannot be recorded must not strand the rest.

        Args:
            merged_task: The task row whose pull request was just merged.
        """
        pr_url = merged_task.get("pr_url")
        if not pr_url:
            return
        # Materialized before the placeholders are counted: GATED_STATUSES is a
        # frozenset, so iterating it twice is not guaranteed to give the same
        # order and would bind the values to the wrong slots.
        gated = tuple(GATED_STATUSES)
        placeholders = ", ".join("?" for _ in gated)
        try:
            siblings = await self._tq._db.fetch_all(
                # `placeholders` is a run of `?` sized from a frozen set; every
                # value below is bound, never interpolated.
                f"""SELECT sibling.* FROM tasks AS sibling
                    JOIN plans AS sibling_plan ON sibling_plan.id = sibling.plan_id
                    WHERE sibling.pr_url = ?
                      AND sibling.id != ?
                      AND sibling.status IN ({placeholders})
                      AND sibling_plan.project_id = (
                          SELECT owner.project_id FROM plans AS owner
                          WHERE owner.id = ?
                      )""",  # nosec B608
                (
                    pr_url,
                    merged_task["id"],
                    *gated,
                    merged_task["plan_id"],
                ),
            )
        except Exception as exc:  # noqa: BLE001 - the merge already happened
            logger.error(
                "Merged %s but could not look up the tasks sharing it; they are "
                "still parked at the merge gate: %s",
                pr_url,
                exc,
            )
            return

        for sibling in siblings:
            sibling_id = str(sibling["id"])
            try:
                await self._tq.mark_merged(sibling_id)
                await self._sync_plan_checkbox(sibling)
                self._bus.publish(
                    {
                        "type": "task_completed",
                        "task_id": sibling_id,
                        "pr_url": pr_url,
                    }
                )
            except Exception as exc:  # noqa: BLE001 - one row, not the sweep
                logger.error(
                    "Task %s was landed by the merge of %s but could not be "
                    "recorded as merged; it is still parked at the merge "
                    "gate: %s",
                    sibling_id,
                    pr_url,
                    exc,
                )
            else:
                logger.info(
                    "Task %s left the merge gate with %s, which landed its work "
                    "along with task %s",
                    sibling_id,
                    pr_url,
                    merged_task["id"],
                )

    async def resolve_no_change_run(
        self,
        task_id: str,
        project: dict[str, Any],
        plan: dict[str, Any] | None,
    ) -> bool:
        """Return whether an empty diff was closed as a no-op.

        Thin wrapper over :meth:`no_change_outcome`, kept because the callback
        endpoint and every existing caller want the boolean and nothing else.

        Args:
            task_id: The task whose run produced no diff.
            project: Project dict (needs ``repo_url``, ``verify_cmd``).
            plan: The task's plan, for the branch it was cut from.

        Returns:
            True when the leaf was closed as a no-op.
        """
        return (await self.no_change_outcome(task_id, project, plan)).closed

    async def _graph_entry_for_task(
        self, task_id: str, plan: dict[str, Any] | None
    ) -> Mapping[str, Any] | None:
        """Return the plan-graph entry aligned to this task row, or None.

        The join lives here once because THREE facts are now read off the same
        entry from different places -- the declared edit locations, the leaf's
        ``task_type`` for the calibration row, and the plan text for the review
        prompt -- and the join is POSITIONAL, the one ``plan_graph`` argues is
        safe. Two copies of it is how one caller comes to align a row to a
        different leaf than another and answer confidently about the wrong one.

        Returns None for every shape that cannot be answered rather than
        raising, and each of them degrades to exactly the behavior that predates
        the caller: a plan with no graph, a row with no aligned entry, or a
        database that would not answer. Nothing read from here decides a leaf's
        fate on its own, so a fault here must not be able to fail one.

        Args:
            task_id: The task whose graph entry is wanted.
            plan: Its plan row, or None.

        Returns:
            The entry, verbatim as the brain wrote it, or None.
        """
        plan_id = (plan or {}).get("id")
        if not plan_id:
            return None
        graph_tasks = parse_graph_tasks(plan)
        if not graph_tasks:
            return None
        try:
            rows = await self._tq.get_tasks_for_plan(str(plan_id))
        except Exception:  # noqa: BLE001 - degrade to the pre-check behavior
            logger.warning(
                "Could not read the task rows of plan %s, so task %s's plan-graph "
                "entry could not be resolved",
                plan_id,
                task_id,
                exc_info=True,
            )
            return None
        return graph_entry_for_row(task_id, build_graph_index(rows), graph_tasks)

    async def graph_task_type(
        self, task_id: str, plan: dict[str, Any] | None
    ) -> str | None:
        """Return the leaf's ``task_type`` from the plan graph, or None.

        ``summarize_outcomes`` groups the calibration rows BY this column, so a
        row that omits it is filed under "unknown" and says nothing about which
        SHAPE of leaf a model cannot do. ``review_task`` resolves it off the
        graph before it reviews; the two paths that decide a no-change outside
        ``review_task`` have no such lookup of their own and would otherwise
        write a row the review path's row cannot be grouped with.

        Public because ``api/internal.py`` reaches it from outside the class,
        for the same reason ``no_change_outcome`` is.

        Args:
            task_id: The task whose leaf type is wanted.
            plan: Its plan row, or None.

        Returns:
            The declared task type, or None when the graph does not say. None
            is an ANSWER here ("the plan never declared one"), which is why it
            is not distinguished from a failed lookup: ``record_outcome`` files
            both the same way and neither is a claim about the leaf.
        """
        entry = await self._graph_entry_for_task(task_id, plan)
        if entry is None:
            return None
        task_type = entry.get("task_type")
        return str(task_type) if task_type else None

    async def record_declined_no_change(
        self,
        task: dict[str, Any],
        project: dict[str, Any],
        decision: NoChangeDecision,
        *,
        task_type: str | None,
    ) -> None:
        """Write the ONE calibration row a DECLINED no-change owes, or nothing.

        Three paths decide that an absent change is not a no-op -- the review
        path's empty PR diff, the worker callback in ``api/internal.py``, and
        the micro-edit lane in ``orchestrator_dispatch.py`` -- and all three
        owe the calibration set the same row. Only the first wrote one until
        2026-08-26, measured with a throwaway probe rather than read: both
        siblings reached a real ``NoChangeDecision(worker_attributable=True)``
        and left ``task_outcomes`` empty. A worker that produced nothing is the
        most informative failure a calibration loop can observe, and
        ``no_changes`` is the status BOTH harness entrypoints report, so the
        callback is where that shape is judged most often.

        The row's CONTENTS stay in ``_record_task_outcome``; what lives here is
        the one description of what a declined no-change means, so a fourth
        caller cannot invent a different class or a different measurement for
        the same event.

        ``worker_attributable`` decides whether anything is written at all, and
        the answer is taken from the DECISION rather than from the wording of
        its reason: a substring match over prose is the pattern this module
        rejects everywhere else, and it would start answering differently the
        day a sentence is reworded. A non-attributable decline records NOTHING
        rather than a row with a softer class, because ``record_outcome``
        derives ``counts_against_worker`` from ``failure_class`` ALONE: the only
        non-voting classes (``provider_error``, ``worker_blocked``,
        ``needs_stronger_model``) each name a different CAUSE, so writing one
        would trade a false row in the calibration set for a false cause in the
        audit trail -- the same harm moved one column across.

        There is deliberately no ``closed`` guard. All three callers reach this
        only on a decline, so a guard here would be dead code reading as a live
        one; the "a no-op records nothing" rule is theirs to hold and each of
        them is tested for it.

        Args:
            task: The task row whose attempt just ended. Attribution is read
                off it, so a caller whose path implemented the attempt itself
                must have recorded that first (see the micro-edit lane).
            project: Its project row, for the attribution fallbacks.
            decision: What ``no_change_outcome`` decided, in full.
            task_type: The leaf's type from the plan graph, resolved by the
                caller. Passed rather than derived here because ``review_task``
                already holds one taken before the review started, and a second
                lookup could read a graph that changed in between.
        """
        if not decision.worker_attributable:
            return
        # ZERO, not None, and the difference is the whole signal.
        # ``leaf_triage._unknown`` renders None as "unknown (not measured)":
        # "zero files touched is the signature of a worker that did nothing...
        # The brain is entitled to know the difference between 'nothing changed'
        # and 'nobody looked'." Every path reaching here has MEASURED the zero.
        # The review path fetched the diff through a checked command and got
        # nothing; both harness entrypoints report ``no_changes`` only when
        # ``git rev-list --count`` SUCCEEDED and returned 0 (an undeterminable
        # count deliberately stays ``failed``); and the micro-edit lane got a
        # clean index back from git after writing the file.
        await self._record_task_outcome(
            task,
            project,
            task_type=task_type,
            files_touched=0,
            loc_delta=0,
            outcome="fail",
            failure_class=FailureClass.NO_OUTPUT.value,
        )

    async def no_change_outcome(
        self,
        task_id: str,
        project: dict[str, Any],
        plan: dict[str, Any] | None,
    ) -> NoChangeDecision:
        """Decide whether a worker's empty diff is a no-op or a real failure.

        Returns the decision AND the reason for it. The reason exists because
        this returns False for four unrelated facts: the base branch could not
        be resolved, the gate FAILED, the gate ERRORED, or the gate skipped for
        a reason that establishes nothing. The caller writes its answer into
        ``tasks.review_feedback``, publishes it, and the Bible injects it into
        the next worker's prompt, so a caller that renders all four as "the
        branch did not verify clean" tells a worker to fix a verification that
        never ran and burns a retry on it.

        A worker that produces no diff is reporting a FACT, not a verdict:
        "the tree already satisfies this leaf". Deciding what that means is
        governance, so it happens here and not in the container.

        The measured shape this exists for: with a plan decomposed into "write
        the module" and "write its tests", task 1 routinely writes both files.
        Task 2 then has nothing to change, says so correctly, and used to be
        called ``failed``, retried three times to the identical correct answer,
        and take the whole plan down with the repository already in the state
        the spec asked for. That boundary crossing fired in four of four plans
        across both harnesses in run #5; the three that survived did so only
        because their workers happened to expand an existing file.

        The evidence used is the project's own verify command, run against the
        branch the leaf was cut FROM. If the tree there already passes, the
        leaf is genuinely a no-op. An infra error is treated as a real failure
        and falls through to the normal retry path, so this can never green a
        leaf whose work is actually missing.

        A RED project command is the one verdict that settles nothing on its
        own, and reading it as "the work is missing" was the defect corrected on
        2026-08-26. The worker changed nothing, so the branch verified IS the
        tree it was handed; the redness therefore pre-dates the attempt by
        construction, and on a dependent chain it is routinely the bar only the
        complete feature can clear. The leaf's OWN declared verification is run
        on that same checkout to settle it: a PASS establishes the no-op, a FAIL
        refutes it and is the only thing here that is evidence about the worker,
        and no declared check at all leaves the question open (fail closed, but
        do not charge).

        A gate skipped BECAUSE no ``verify_cmd`` is configured also closes the
        leaf. That is deliberate and it is the weakest link here: with no
        command there is no independent evidence, only the harness's clean
        exit. The alternative is worse and was measured, not guessed: retrying
        to the same answer and failing a plan whose work is already done. An
        install that wants the evidence configures ``verify_cmd``.

        That carve-out belongs to the REASON, not to ``skipped``: a gate that
        skipped because it could not reach the repository establishes nothing
        at all. ``_no_op_evidence`` is where the distinction lives, and it is
        also what keeps the stored reason from claiming a check that was never
        chosen and never ran.

        The verify command alone is NOT enough, and this was measured on
        2026-08-25. A leaf asked for a new eleven-module subpackage that did
        not exist and declared the repository's own suite as its acceptance
        check. The worker wrote nothing, ran that suite, watched 294
        pre-existing tests pass, and reported no changes. The gate then ran the
        same command on the base branch, got the same pass, and closed the leaf
        as terminally SATISFIED, releasing every dependent leaf onto work
        nobody had done. The command answers "is this repository healthy", not
        "was THIS leaf's work done", and for any leaf whose acceptance is not
        expressible as "the existing suite passes" a healthy repository makes
        every empty diff read as "already done".

        So the leaf's own declared edit locations are checked against the same
        tree. A declared path the branch does not carry REFUTES the no-op
        outright, whatever the command reported, and that refutation is
        deliberately taken BEFORE the gate verdict is read: it is the more
        specific fact and the only one that tells the next worker what to
        create.

        A leaf that declares NO edit locations gets the third answer rather
        than either of the first two. The check cannot run, so the decision is
        exactly what it was before, and the stored reason SAYS the check did
        not run rather than implying it passed. That matters because declaring
        nothing is the norm, not the exception: only the decomposition path
        produces ``files``, while the plan_spec path, the improvement loop and
        a direct dispatch that omitted them all arrive here declaring nothing.
        Refusing those would fail every such leaf, which is the measured
        alternative this method exists to avoid.

        A tree that could not be FETCHED is the same third answer for the same
        reason, and ``_verify_plan_branch`` is where that is enforced: when
        there was no command to run, a fetch made only to answer the declared
        paths reports the skip the gate always reported. The distinction that
        makes this consistent with the fail-closed rule, rather than a hole in
        it, is what the operator asked for. ``_no_op_evidence`` refuses on
        ``_SKIP_NO_CREDENTIAL_PROVIDER`` and ``_SKIP_NO_TOKEN`` because, in its
        own words, "a verify command IS configured and the gate could not reach
        the repository" -- closing there would claim the operator had chosen to
        run nothing. With no command configured that claim is simply true, so
        the premise the refusal rests on is absent and the refusal does not
        apply. Fail-closed governs a gate that was asked to run and could not;
        it says nothing about a gate nobody asked to run.

        Args:
            task_id: The task whose run produced no diff.
            project: Project dict (needs ``repo_url``, ``verify_cmd``).
            plan: The task's plan, for the branch it was cut from. ``None``
                falls back to the project's default branch.

        Returns:
            A :class:`NoChangeDecision`, which still unpacks as
            ``(closed, why)``. ``closed`` is True when the leaf was closed as a
            no-op; False when the caller should treat the run as a normal
            failure. ``why`` states which of the five facts produced that
            answer, in a form fit to show a human and a worker.
            ``worker_attributable`` says whether that fact is evidence about
            the WORKER's output, and only two shapes are:

            - a declared edit location the branch does not carry, which proves
              the work is absent whatever the command reported, and
            - the leaf's OWN declared verification, run on that branch, refuting
              the no-op.

            Everything else says nothing about the leaf: the gate failing to
            produce an answer at all (the branch could not be resolved, the
            clone/checkout/command raised, a configured gate could not reach the
            repository), and -- since 2026-08-26 -- the PROJECT command going
            red, which on this path is red on the very tree the worker was
            handed. All of them are the same class as a reviewer that could not
            run, so none may buy a triage call, whose worst answer (``human``)
            is terminal and irreversible.
        """
        repo_url = project.get("repo_url")
        base_branch = (plan or {}).get("plan_branch_name") or project.get(
            "default_branch"
        )
        if not repo_url or not base_branch:
            logger.warning(
                "Task %s reported no changes but its base branch could not be "
                "resolved (repo_url=%r, branch=%r); treating as a failure",
                task_id,
                repo_url,
                base_branch,
            )
            # Not worker-attributable: nothing was checked, so nothing here is
            # evidence about the worker. What is wrong is the project row.
            return NoChangeDecision(
                False,
                "the branch it was cut from could not be resolved "
                f"(repo_url={repo_url!r}, branch={base_branch!r}), so nothing "
                "could be checked",
            )

        # ONE read of the graph, two facts derived from it. Two lookups could
        # observe two states of a graph an adaptive split is free to rewrite,
        # and the declared paths and the declared verification are both about
        # the same leaf at the same instant or they are about nothing.
        #
        # Both declarations live in ``plans.opus_plan``, never on the task row:
        # ``tasks`` has no ``files`` column and ``agent_runs.files_touched`` is a
        # COUNT of what a worker changed, not a list of what it was asked to
        # change. The row is joined back POSITIONALLY (see
        # ``_graph_entry_for_task``), and every shape that cannot be answered --
        # no aligned entry, or an entry declaring nothing -- yields the empty
        # answer rather than raising: neither fact decides a leaf's fate alone,
        # so a fault here must not be able to fail one.
        entry = await self._graph_entry_for_task(task_id, plan)
        declared = declared_paths((entry or {}).get("files"))
        bench_disabled = verify_gate_disabled()
        verify_cmd = None if bench_disabled else project.get("verify_cmd")
        # Prose is never shelled: ``shell_command_for_verification`` accepts far
        # less than ``is_runnable_verification`` does, and errs toward "absent",
        # which never decides anything. Handing "the module imports cleanly" to a
        # shell yields ``the: command not found`` and would CHARGE a worker on
        # evidence Praxis fabricated -- a new false accusation for an old one.
        #
        # And a check that IS the project command is absent for the same reason
        # one step on: it is the bar this very call is about to show is red on
        # the branch the worker was handed, so re-running it can only restate
        # that. ``discriminating_leaf_command`` is the SSoT for both questions
        # and both seats ask it, so the rule cannot drift between them.
        leaf_check = discriminating_leaf_command(
            (entry or {}).get("verification"), verify_cmd
        )
        verdict = await self._verify_plan_branch(
            repo_url,
            base_branch,
            verify_cmd,
            # Without this the gate cannot tell bench mode's deliberate
            # disabling apart from an operator who configured no command, and
            # reports the second. That reason is stored on the task and rides
            # the no-op event, so a bench run's own records would say the
            # project had no verify command when it has one.
            disabled_reason=_SKIP_BENCH_MODE_DISABLED if bench_disabled else None,
            # Answered off the SAME checkout the command runs in, so the two
            # observations cannot come from two different states of the branch.
            require_paths=declared,
            # The third question, asked on that same checkout and only when the
            # project command goes red -- the one arm where its answer cannot be
            # about this task.
            leaf_verify_cmd=leaf_check,
        )
        paths = verdict.paths
        if paths is not None and paths.missing:
            # Taken before the gate verdict is read, and it outranks every one
            # of the verdict's own answers. A branch can verify perfectly clean
            # and still not carry a file this leaf was asked to write; that is
            # exactly the pair of facts that produced the measured false
            # success, and only this one names something the next worker can
            # act on.
            named = ", ".join(paths.missing)
            logger.warning(
                "Task %s reported no changes, but %d of its declared edit "
                "locations are absent from %s (%s); treating as a failure",
                task_id,
                len(paths.missing),
                base_branch,
                named,
            )
            # Worker-attributable, and the strongest form of it: the leaf was
            # asked to write these locations, the branch does not carry them,
            # and the worker changed nothing. Nothing about the deployment is
            # implicated, and this is the one decline that names something a
            # triage brain can reason about concretely.
            return NoChangeDecision(
                False,
                f"the branch it was cut from ({base_branch}) does not carry "
                f"edit locations this task declared ({named}), so the work is "
                "genuinely missing whatever the verify command reports",
                worker_attributable=True,
            )
        evidence = _no_op_evidence(verdict, base_branch)
        if evidence is None:
            logger.warning(
                "Task %s reported no changes, but %s did not establish it "
                "(status=%s, reason=%s); treating as a failure",
                task_id,
                base_branch,
                verdict.status,
                verdict.reason or "-",
            )
            # The line is whether the gate produced an answer ABOUT THIS LEAF,
            # not whether it produced an answer about the repository. Until
            # 2026-08-26 it was the second, and ``failed`` alone was enough.
            #
            # That was the same unsound inference cd0c127 removed from
            # ``review_task``, running in the opposite direction. The command
            # here runs on the branch the leaf was cut FROM, and the worker
            # changed NOTHING, so that branch IS the tree it was handed: a red
            # verdict is red identically on head and base by construction, which
            # is precisely the shape cd0c127 named ``_GATE_UNATTRIBUTED`` and
            # refused to charge. Two seats cannot answer one fact both ways.
            #
            # It is also the whole of the old attributable set, and it never
            # discriminated what it was read as discriminating: on a HEALTHY
            # repository the identical worker behaviour is CLOSED as a no-op
            # ("verify passed on <branch>"), so the only empty diffs this route
            # ever charged were the ones sitting on a red repository. The column
            # the capability loop reads as worker capability was being fed
            # repository health.
            #
            # The leaf's OWN declared verification is the discriminator, and it
            # is the same one cd0c127 chose for the same question. A leaf that
            # declares none leaves nothing here that is about it: cd0c127's step
            # 3, "the ABSENCE of a leaf check must not reinstate an attribution
            # that was just shown to be false". Not attributing is not passing --
            # the leaf still fails closed and still retries, it is simply not
            # charged and not offered a terminal triage answer.
            #
            # ``error`` and the surviving skips are the gate failing to answer at
            # all: a clone, checkout or command that raised, or a configured gate
            # that could not reach the repository. ``_no_op_evidence`` already
            # refuses to CLOSE a leaf on those because "a verify command IS
            # configured and the gate could not reach the repository"; the same
            # sentence is why they must not be triaged either.
            leaf_refuted = verdict.leaf is not None and not verdict.leaf.passed
            attributable = verdict.status == "failed" and leaf_refuted
            if verdict.status == "failed" and verdict.leaf is not None:
                # The DECLARED command's output, never the project command's.
                # This string is stored on the task, injected verbatim into the
                # next worker's prompt by the Bible and handed to the triage
                # brain; reporting a sibling's stack trace here is what poisoned
                # that evidence on the review path.
                why = (
                    f"this task's own declared verification "
                    f"(`{verdict.leaf.command}`) fails on the branch it was cut "
                    f"from ({base_branch}), so the work is genuinely missing: "
                    f"{_log_excerpt(verdict.leaf.output)}"
                )
            elif verdict.status == "failed":
                # Two states, and until 2026-08-27 both read as "declared
                # nothing". A leaf that declared the project command DID
                # declare a check; telling its author to write one sends them
                # to do the one thing that cannot help, and hides the fact they
                # could act on. Same phrases as the review seat, from
                # ``core/verify_gate``, so the two cannot drift.
                # NOT named ``declared``: that name is already bound in this
                # scope to the leaf's declared PATHS, and reusing it made two
                # unrelated facts share one word in a method whose whole
                # subject is telling facts apart.
                leaf_check_state = (
                    LEAF_CHECK_NONDISCRIMINATING
                    if restates_project_command(
                        (entry or {}).get("verification"), verify_cmd
                    )
                    else LEAF_CHECK_NONE
                )
                why = (
                    f"the project verify command is red on the branch it was cut "
                    f"from ({base_branch}), which is the tree this task was handed "
                    f"and says nothing about the work it was asked to do; "
                    f"{leaf_check_state}, so the no-op could not be established "
                    f"either way"
                )
            elif verdict.status == "error":
                why = (
                    f"the branch it was cut from ({base_branch}) could not be "
                    "verified at all (the clone, checkout or command raised), so "
                    "this could not be established as a no-op"
                )
            else:
                why = (
                    f"the verify gate on {base_branch} was skipped "
                    f"({verdict.reason or 'no reason recorded'}), which "
                    "establishes nothing either way"
                )
            return NoChangeDecision(False, why, worker_attributable=attributable)

        locations = _declared_paths_clause(declared, paths, base_branch)
        reason = (
            "No changes needed: the repository already satisfied this task "
            f"({evidence}; {locations})."
        )
        await self._tq.mark_no_changes(task_id, reason)
        self._bus.publish(
            {
                "type": "task_no_changes",
                "task_id": task_id,
                "base_branch": base_branch,
                "verify_status": verdict.status,
                # A bare "skipped" cannot say WHICH skip, and the two the gate
                # produces mean opposite things about how much this no-op is
                # worth trusting.
                "verify_reason": verdict.reason,
                # Published, not only stored: a no-op backed by a checked list
                # of edit locations and one backed by nothing are worth
                # different amounts of trust, and every read-only surface has
                # to be able to tell them apart without re-deriving it.
                "declared_paths_checked": 0 if paths is None else paths.checked,
                "declared_paths_total": len(declared),
                "reason": reason,
            }
        )
        logger.info("Task %s closed as a no-op: %s", task_id, reason)
        # A no-op is a SUCCESS and terminal. There is no failure to attribute.
        return NoChangeDecision(True, reason)

    async def approve_plan_integration(
        self, plan_id: str, project: dict[str, Any]
    ) -> str:
        """Merge the integration PR that carries a completed plan onto its base.

        This is the last link in the loop. Every task can be reviewed, merged
        to the plan branch and reported clean, and the change still not be on
        the base branch: the integration PR is what closes that gap, and until
        this existed nothing but a human with a browser could close it.

        Idempotent by design. Re-running against an already-integrated plan
        returns its URL rather than raising: "already done" must never read as
        an error, the same rule ``_abandoned_merge`` follows in the CLI.

        Args:
            plan_id: The completed plan to integrate.
            project: Project dict (needs ``repo_url``).

        Returns:
            The integration PR URL that was merged (or already merged).

        Raises:
            ValueError: If the plan is missing, has no integration PR, or
                carries a ``pr_url`` no backend can parse. Like task approval,
                this path is operator-driven and must never silently no-op.
        """
        plan = await self._tq.get_plan(plan_id)
        if plan is None:
            msg = f"Plan {plan_id} not found"
            raise ValueError(msg)
        pr_url = plan.get("integration_pr_url")
        if not pr_url:
            # Three causes, not two, and the third is the one that misleads:
            # on_plan_completed opens the PR and then records it in a separate
            # step whose failure is deliberately non-fatal, so a real open PR
            # can exist with this column NULL. Telling that operator the PR
            # "could not be opened" invites them to open a second one. The plan
            # status separates the cases we can distinguish from here.
            if str(plan.get("status") or "") != "completed":
                detail = f"it is {plan.get('status') or 'in an unknown state'}, not completed"
            else:
                detail = (
                    "the plan completed, so either the PR could not be opened or "
                    "it was opened and the URL could not be recorded; check the "
                    f"remote for an open PR from {plan.get('plan_branch_name') or 'the plan branch'}"
                )
            msg = f"Plan {plan_id} has no integration PR recorded ({detail})"
            raise ValueError(msg)
        if plan.get("integration_merged_at"):
            return str(pr_url)

        backend = self._resolve_backend(project["repo_url"])
        try:
            ref = PullRequestRef.from_url(str(pr_url))
        except ValueError as exc:
            msg = f"Plan {plan_id} has an unparseable integration PR url {pr_url!r}"
            raise ValueError(msg) from exc
        await backend.merge(ref)
        await self._tq.mark_plan_integrated(plan_id)
        self._bus.publish(
            {
                "type": "plan_integrated",
                "plan_id": plan_id,
                "project_id": project["id"],
                "pr_url": pr_url,
            }
        )
        return str(pr_url)

    async def reject_task_merge(
        self,
        task_id: str,
        project: dict[str, Any],
        feedback: str | None = None,
    ) -> None:
        """Reject a parked merge: comment, fail, and re-dispatch if attempts remain.

        Args:
            task_id: ID of the task to reject.
            project: Project dict (needs ``repo_url``, ``max_retries``).
            feedback: Optional rejection message posted as a PR comment.

        Raises:
            ValueError: If the task is missing, not in the PASSED state, or
                carries a ``pr_url`` no backend can parse.  Like approval, this
                path is operator-driven and must never silently no-op.
        """
        task = await self._tq.get_task(task_id)
        if task is None:
            msg = f"Task {task_id} not found"
            raise ValueError(msg)
        if task["status"] != TaskStatus.PASSED or task["pr_url"] is None:
            msg = f"Task {task_id} is not awaiting merge"
            raise ValueError(msg)

        message = feedback or "Merge rejected by user."
        backend = self._resolve_backend(project["repo_url"])
        try:
            ref = PullRequestRef.from_url(task["pr_url"])
        except ValueError as exc:
            msg = f"Task {task_id} has an unparseable pr_url {task['pr_url']!r}"
            raise ValueError(msg) from exc
        await backend.comment(ref, message)
        await self._tq.fail_task(task_id, message)
        if int(task["attempt"]) < int(project["max_retries"]):
            await self._tq.retry_task(task_id)
            self._bus.publish(
                {
                    "type": "task_retry",
                    "task_id": task_id,
                    "attempt": int(task["attempt"]) + 1,
                }
            )
        else:
            self._bus.publish(
                {"type": "task_failed", "task_id": task_id, "feedback": message}
            )

    async def _sync_plan_checkbox(self, task: dict[str, Any]) -> None:
        """Flip the task checkbox in the plan file inside the TARGET project repo.

        Clones the target repo to a temp dir, finds the plan file by its
        repo-relative path, flips the checkbox, commits, pushes, then removes
        the temp dir.  Falls back to a safe no-op (with a warning) when:
        - doc_indexer is unavailable, OR
        - the GitHub token cannot be resolved.

        All errors are caught so this never interrupts the main merge flow.
        """
        if self._doc_indexer is None:
            return

        # A per-repo GitHub token is resolved from git_ops' credential provider
        # once repo_url is known (below). If the provider is unavailable
        # (e.g. tests, stub objects), skip rather than corrupt the
        # orchestrator's own docs tree.
        provider = getattr(self._git, "_provider", None)
        if provider is None:
            logger.warning(
                "_sync_plan_checkbox: credential provider unavailable, "
                "checkbox sync requires a target-repo clone, skipped"
            )
            return

        try:
            title = task.get("title", "")
            if not title:
                return

            # Fetch plan → project to get repo_url.
            plan_id = task.get("plan_id")
            if plan_id is None:
                return
            plan = await self._tq.get_plan(plan_id)
            if plan is None:
                return
            project = await self._tq.get_project(plan["project_id"])
            if project is None:
                return
            repo_url: str = project["repo_url"]

            github_token = await provider.token_for_repo(repo_url)
            if not github_token:
                logger.warning(
                    "_sync_plan_checkbox: no token resolved for %s, skipped",
                    repo_url,
                )
                return

            # Find the plan file path (repo-relative) from doc_index.
            rows = await self._tq._db.fetch_all(
                "SELECT path FROM doc_index WHERE category = 'plan' ORDER BY updated_at DESC"
            )
            if not rows:
                return

            with tempfile.TemporaryDirectory() as tmp_dir:
                ws = tmp_dir
                clone_with_token(repo_url, ws, github_token)

                flipped = False
                # Counts the plan files that were actually FOUND in the clone.
                # Without it, "no unchecked item found" is also emitted when no
                # plan file was located at all, which is an absent search
                # reported as a negative result.
                searched = 0
                for row in rows:
                    # row["path"] is relative to the orchestrator's docs tree;
                    # use only the filename/tail to locate it inside the clone.
                    rel_path = row["path"]
                    candidate = Path(ws) / rel_path
                    if not candidate.exists():
                        # Try just the filename as a fallback.
                        candidate = next(
                            (
                                p
                                for p in Path(ws).rglob(Path(rel_path).name)
                                if p.is_file()
                            ),
                            None,
                        )
                        if candidate is None:
                            continue
                    searched += 1
                    text = candidate.read_text(encoding="utf-8")
                    updated = flip_checklist_item(text, title)
                    if updated != text:
                        candidate.write_text(updated, encoding="utf-8")
                        # Path relative to clone root for git add.
                        git_rel = str(candidate.relative_to(ws))
                        # commit_and_push returns False when the index was
                        # already clean, i.e. nothing was pushed. Discarding it
                        # and logging "Flipped" reports a push that did not
                        # happen; git_ops documents that the caller must be
                        # able to tell the two apart.
                        pushed = commit_and_push(
                            ws,
                            github_token,
                            f"docs: mark '{title}' complete",
                            paths=[git_rel],
                        )
                        if pushed:
                            logger.info(
                                "Flipped checkbox for '%s' in %s (target repo %s)",
                                title,
                                git_rel,
                                repo_url,
                            )
                        else:
                            logger.warning(
                                "Rewrote the checkbox for '%s' in %s but git had "
                                "nothing to commit, so nothing was pushed "
                                "(target repo %s)",
                                title,
                                git_rel,
                                repo_url,
                            )
                        flipped = True
                        break

                if not flipped and searched:
                    logger.debug(
                        "_sync_plan_checkbox: no unchecked item '%s' in the %d "
                        "plan file(s) found in the clone",
                        title,
                        searched,
                    )
                elif not flipped:
                    logger.warning(
                        "_sync_plan_checkbox: none of the %d indexed plan files "
                        "exists in the clone of %s, so no checkbox was searched "
                        "for '%s'",
                        len(rows),
                        repo_url,
                        title,
                    )

            await self._doc_indexer.scan()
            self._bus.publish({"type": "docs_refreshed"})
        except Exception as exc:  # noqa: BLE001 - non-fatal
            logger.warning(
                "_sync_plan_checkbox failed for task %s: %s", task.get("id"), exc
            )

    @staticmethod
    def _review_base_branch(
        ref: PullRequestRef,
        plan: dict[str, Any] | None,
        project: dict[str, Any],
    ) -> str | None:
        """Name the branch this pull request's work is being ADDED TO.

        Three sources, most specific first, and each one is already the answer
        somewhere else in this file:

        - ``ref.base``. Authoritative when it is there, because it is what
          ``backend.merge(ref)`` actually writes to. Only a ``praxis-local://``
          ref carries one; ``PullRequestRef.from_url`` sets ``base=""`` for a
          GitHub PR URL, which encodes no base at all.
        - ``plans.plan_branch_name``. What a two-tier task branch was cut from,
          and the fallback the auto-merge gate twenty lines up already uses for
          exactly the GitHub case above. It carries that gate's known limit
          with it: in single-branch mode dispatch bases the shared work branch
          on the project default, so the two diverge.
        - ``projects.default_branch``. What ``no_change_outcome`` falls back to
          for the same question, so the two seats cannot disagree about a plan
          that has no branch recorded.

        Returning the WRONG branch is the way this whole comparison goes
        silently wrong: "the same command fails there too" is only evidence when
        "there" is the tree this work was cut from. Every arm is therefore a
        branch something else in the system already treats as the base, never a
        guess.

        Args:
            ref: The parsed pull-request reference under review.
            plan: The task's plan row, or None.
            project: The project row.

        Returns:
            The branch name, or None when none of the three is recorded and
            nothing can be compared.
        """
        return (
            ref.base
            or (plan or {}).get("plan_branch_name")
            or project.get("default_branch")
            or None
        )

    async def _attribute_head_verify_failure(
        self,
        *,
        task: dict[str, Any],
        project: dict[str, Any],
        plan: dict[str, Any] | None,
        ref: PullRequestRef,
        checkout: str,
        verify_cmd: str,
        gate_output: str,
        head_code: int | None,
        leaf_verification: Any,
        log: Any,
    ) -> _VerifyAttribution:
        """Decide whether a red project verify command is THIS task's fault.

        The project ``verify_cmd`` is the right bar for a REGRESSION and the
        wrong bar for a leaf, and until 2026-08-26 it was used as both. It was
        measured twice on a dependent chain: leaf 1 of a Hindley-Milner plan
        wrote 322 lines of exactly its declared scope and FAILED, because the
        project's ``pytest`` collects an acceptance file importing a symbol that
        is LEAF 2's contract. The base branch failed the same command
        identically. So the gate charged a leaf with a failure that pre-existed
        on the branch it was cut from -- and capability-aware decomposition
        produces dependent chains precisely when it is doing its most valuable
        work, so the mechanism defeated itself exactly where it mattered. It
        poisoned the triage evidence too: the leaf arrived at triage carrying a
        stack trace about a sibling's contract.

        Three questions, in this order, and the order is the argument:

        1. **Does the same command fail on the base branch, and does it fail
           the same WAY?** If it PASSES there, the failure is NEW and this task
           is the only thing that changed, so the old behaviour stands
           unaltered. If the comparison could not be MADE (``error``, or a
           configured gate that could not reach the repository), this FAILS
           CLOSED and says why: an unanswered question must never buy a task a
           pass.

           Until 2026-08-27 a red base ended the question, because the whole
           comparison was ``base.status != "failed"`` -- status equality and
           nothing else. Measured live on plan ``4eb8ed70``: the head RAN the
           suite and three assertions failed, the base never ran a test at all
           (a collection ``ImportError``), and those two counted as identical,
           so a genuine leaf regression was excused as pre-existing.
           ``compare_failures`` asks the runner's OWN exit code, which is the
           only signal here that distinguishes two failures without parsing
           text -- see its docstring for the six measured noise sources that
           rule text comparison out, and for why an unknown runner degrades to
           ``INCOMPARABLE`` rather than to a wrong answer.
        2. **What did the leaf's OWN declared verification do?** The
           decomposer emits one, the standard hard-requires it and F3 validates
           that it is runnable -- and nothing ever ran it; it was a
           worker-prompt element only. It runs on the checkout the head command
           already ran in, never a second fetch, because two fetches can observe
           two states of the branch. A failure here IS evidence about this leaf
           and fails the task with the DECLARED command's output as the reason.
        3. **Was there a runnable one at all?** If not, attribution is settled
           by step 1 alone. The ABSENCE of a leaf check must not reinstate an
           attribution that was just shown to be false, and declaring nothing is
           the norm on every path but decomposition.

           With ONE exception, and it is the whole reason adaptive ``split``
           had never once been observed on a live run. A leaf whose declared
           check IS the project command reaches this step too, and it is not a
           leaf that declared nothing: it declared the WHOLE SUITE as its
           acceptance, which the decomposition standard WANTS from the final
           leaf of a dependent chain. Such a leaf had no positive signal, so
           every failure was non-attributable, so no ``task_outcomes`` row was
           written and triage -- where ``split`` is decided -- was never
           called. The largest leaf in every plan was structurally
           un-splittable and invisible to calibration. When step 1 shows the
           base failed a DIFFERENT way, that leaf is held to the bar IT named,
           and the bar is its own rather than one Praxis imposed.

           The bound matters as much as the arm. A leaf that declared nothing
           has made no acceptance claim, so nothing here may be held against
           it; and a leaf with a check that CAN discriminate never reaches this
           step, because that check is better evidence and keeps priority. So
           the population this arm can charge is exactly the population that
           named the project command itself.

        Not failing is NOT the same as passing, and nothing here treats it as
        such: the brain still reviews the diff, the human still gates the merge,
        and the returned ``_UnattributedVerify`` puts the whole fact into the
        stored ``review_feedback`` the merge gate renders.

        Args:
            task: The task row under review, for the log line only.
            project: Its project row (needs ``repo_url``, ``default_branch``).
            plan: Its plan row, or None.
            ref: The parsed pull-request reference.
            checkout: The PR-head checkout the head command just ran in.
            verify_cmd: The normalized project command that just failed.
            gate_output: What it printed, already the caller's evidence.
            head_code: The exit code the head run reported, or None when it
                carries no classification (a timeout). Threaded from the caller
                rather than re-derived, because a second run of the command
                could observe a different state of the branch.
            leaf_verification: The leaf's raw ``verification`` from the plan
                graph, untrusted brain output of any shape.
            log: The task-scoped logger.

        Returns:
            The decision, in full.
        """
        task_id = str(task["id"])
        base_branch = self._review_base_branch(ref, plan, project)
        repo_url = project.get("repo_url")
        if not repo_url or not base_branch:
            return self._verify_failure_stands(
                verify_cmd,
                gate_output,
                "the base branch could not be resolved "
                f"(repo_url={repo_url!r}, branch={base_branch!r})",
            )

        base = await self._verify_plan_branch(repo_url, base_branch, verify_cmd)
        if base.status == "passed":
            # The one arm that is byte-for-byte the old behaviour: the command
            # is green on the base and red here, so this task broke it.
            log.warning(
                "verify gate failure is attributed to this task: `%s` passes on "
                "%s and fails on the PR head",
                verify_cmd,
                base_branch,
            )
            return _VerifyAttribution(
                review={
                    "verdict": "fail",
                    "feedback": (
                        f"{_VERIFY_FAIL_MARKER} before review "
                        f"(`{verify_cmd}`). The same command PASSES on "
                        f"{base_branch}, so this change is what broke "
                        f"it:\n\n{gate_output}"
                    ),
                },
                verify_state=_GATE_FAILED,
            )
        if base.status != "failed":
            # ``error`` and every skip: the gate did not produce an ANSWER
            # about the base branch. Fail closed, exactly as before, and say
            # the comparison is missing rather than implying it was made.
            return self._verify_failure_stands(
                verify_cmd,
                gate_output,
                base_comparison_unavailable(base_branch, base.status, base.reason),
            )

        # HOW the two failed, not merely THAT both did. Computed once, here,
        # and threaded into every sentence below, so the log line, the stored
        # feedback and the merge-gate scope statement cannot disagree about a
        # comparison that was made exactly once.
        comparison = compare_failures(head_code, base.returncode)
        compared = base_failure_clause(
            comparison, base_branch, head_code, base.returncode
        )

        # Absent covers TWO shapes and the second is the one that composed into
        # a hole: a leaf declaring nothing runnable, and a leaf whose declared
        # check IS ``verify_cmd`` -- the command the two branches above have
        # just shown to be red on both trees. Running it again cannot
        # discriminate, so it must not license an attribution.
        leaf_check = discriminating_leaf_command(leaf_verification, verify_cmd)
        if leaf_check is None:
            # The two shapes told apart. ``restates_project_command`` derives it
            # by CALLING the same two functions the line above did, so the two
            # answers cannot drift.
            nondiscriminating = restates_project_command(leaf_verification, verify_cmd)
            if nondiscriminating and comparison is FailureComparison.FAILED_DIFFERENTLY:
                # THE arm this fix exists for. The leaf itself named the project
                # command as its acceptance, and the base branch has now been
                # shown to fail a DIFFERENT way, so there is no pre-existing
                # failure here to inherit the excuse. Charging it is what makes
                # the whole-suite leaf reachable by calibration and by triage,
                # which is where ``split`` is decided.
                log.warning(
                    "verify gate failure IS attributed to task %s: `%s` is also "
                    "this leaf's own declared acceptance, and %s",
                    task_id,
                    verify_cmd,
                    compared,
                )
                return _VerifyAttribution(
                    review={
                        "verdict": "fail",
                        "feedback": (
                            f"{_VERIFY_FAIL_MARKER} before review "
                            f"(`{verify_cmd}`), which this task declared as its "
                            f"OWN acceptance check. {compared[0].upper()}"
                            f"{compared[1:]}, so this failure is not the one "
                            f"already on {base_branch}:\n\n{gate_output}"
                        ),
                    },
                    verify_state=_GATE_FAILED,
                )
            log.warning(
                "verify gate failed for task %s (`%s`) but %s, and %s, so the "
                "failure is NOT attributed to it; the review continues and the "
                "merge gate is told",
                task_id,
                verify_cmd,
                compared,
                (
                    LEAF_CHECK_NONDISCRIMINATING
                    if nondiscriminating
                    else LEAF_CHECK_NONE
                ),
            )
            return _VerifyAttribution(
                review=None,
                verify_state=_GATE_UNATTRIBUTED,
                unattributed=_UnattributedVerify(
                    base_branch,
                    None,
                    comparison,
                    head_code,
                    base.returncode,
                    nondiscriminating=nondiscriminating,
                ),
            )

        leaf_passed, leaf_output = await run_verify(checkout, leaf_check)
        if not leaf_passed:
            log.warning(
                "verify gate failed for task %s and so did its OWN declared "
                "verification (`%s`): %s",
                task_id,
                leaf_check,
                _log_excerpt(leaf_output),
            )
            return _VerifyAttribution(
                review={
                    "verdict": "fail",
                    "feedback": (
                        f"{_VERIFY_FAIL_MARKER} before review. The project "
                        f"command (`{verify_cmd}`) also fails on {base_branch}, "
                        "so it was not held against this task. This task's OWN "
                        f"declared verification (`{leaf_check}`) was run "
                        f"instead, and it failed:\n\n{leaf_output}"
                    ),
                },
                verify_state=_GATE_FAILED,
            )
        log.warning(
            "verify gate failed for task %s (`%s`) but %s, and the task's own "
            "verification (`%s`) passed, so the failure is NOT attributed to "
            "it; the review continues and the merge gate is told",
            task_id,
            verify_cmd,
            compared,
            leaf_check,
        )
        return _VerifyAttribution(
            review=None,
            verify_state=_GATE_UNATTRIBUTED,
            unattributed=_UnattributedVerify(
                base_branch,
                leaf_check,
                comparison,
                head_code,
                base.returncode,
            ),
        )

    @staticmethod
    def _verify_failure_stands(
        verify_cmd: str, gate_output: str, why_no_comparison: str
    ) -> _VerifyAttribution:
        """Fail the task, and say the attribution could not be established.

        Every arm that reaches here is "the base branch could not be asked",
        never "the base branch answered". Those are opposite facts and the
        difference has to survive into the feedback, because this string is
        stored on the task AND injected verbatim into the next worker's prompt
        by ``core/worker_bible``: a worker told a comparison was made would be
        reading a claim nobody established.

        FAILING is unchanged and right: an unanswered question must never buy a
        task a pass. What changed on 2026-08-26 is the CALIBRATION row. The state
        returned is ``_GATE_UNCOMPARED`` rather than ``_GATE_FAILED``, and
        ``review_task`` writes that row with a NULL ``failure_class`` instead of
        ``VERIFY_FAIL``, which ``failure_taxonomy`` counts against the worker.
        This method's own feedback says, in words, that whether the failure
        pre-dates the task could NOT be established; a row asserting the worker's
        change failed verification asserts exactly that unestablished thing, and
        ``fetch_recent_outcomes`` then feeds it to the capability gate as
        capability. The row is still WRITTEN, so the attempt stays auditable and
        countable -- the same "withdraw the claim rather than state a false one"
        move the supply-chain gate makes with its ``blocked`` outcome, and the
        opposite of a silently missing row.

        Args:
            verify_cmd: The project command that failed on the PR head.
            gate_output: What it printed.
            why_no_comparison: Why the base branch could not answer.

        Returns:
            The unchanged failing verdict, with the missing comparison named.
        """
        return _VerifyAttribution(
            review={
                "verdict": "fail",
                "feedback": (
                    f"{_VERIFY_FAIL_MARKER} before review (`{verify_cmd}`). "
                    "Whether this failure pre-dates this task could NOT be "
                    f"established, because {why_no_comparison}, so it is "
                    f"reported as-is:\n\n{gate_output}"
                ),
            },
            verify_state=_GATE_UNCOMPARED,
        )

    async def _verify_plan_branch(
        self,
        repo_url: str,
        plan_branch: str,
        verify_cmd: str | None,
        disabled_reason: str | None = None,
        require_paths: Sequence[str] = (),
        leaf_verify_cmd: str | None = None,
    ) -> _PlanVerifyResult:
        """Run the project's verify command against the accumulated plan branch.

        Gets the plan-branch head into a temp dir and runs ``run_verify``.  How
        it gets there is the backend's business: a local bare repo is cloned
        straight off the filesystem through ``LocalGitBackend.checkout``, a
        GitHub repo is cloned with a token resolved via the git-ops credential
        provider, exactly like ``_sync_plan_checkbox``.

        Routing through the backend seam is what makes this gate real in local
        mode.  It used to resolve a token first and return ``skipped`` when
        there was none, and a local bare repo has no credential BY DESIGN
        (``preflight._preflight_local`` requires none; ``agent_manager``
        bind-mounts the repo).  So both callers -- the per-wave cross-leaf gate
        and the whole-plan backstop -- were dead for every local project, and
        neither can tell ``skipped`` from ``passed`` by control flow, so nothing
        said so.

        Returns a ``_PlanVerifyResult`` whose ``status`` is ``skipped`` only
        when there is genuinely nothing to run, ``passed`` / ``failed`` for the
        gate outcome, and ``error`` if any I/O raised.  All exceptions are
        caught so this never wedges the completion path.

        Args:
            repo_url: The project's repository URL or local bare-repo path.
            plan_branch: The accumulated plan branch to verify.
            verify_cmd: The project's configured verification command. Raw as
                read from the project row; normalized here rather than at the
                callers because this method is the single funnel for both of
                them (``no_change_outcome`` and ``on_plan_completed``), so
                one normalization cannot leave the other caller behind.
            disabled_reason: What to report when ``verify_cmd`` is absent
                because the CALLER suppressed it rather than because none is
                configured. Bench condition C passes ``None`` for a project
                that does have a command, and without this the skip reason
                would say the operator configured none. That reason is stored
                on the task and rides the no-op event, so it is a statement
                about the project, not a debug detail.
            require_paths: Edit locations a leaf declared, to be checked
                against the same tree this gate materializes. Empty (the
                default) leaves every existing caller byte-identical: no check
                is made and ``paths`` on the result stays None.

                Non-empty also makes the tree worth fetching on its own, so a
                project with NO verify command still gets the branch checked
                out and the declared paths answered. That is the one case where
                this method now does I/O it used to skip, and it is deliberate:
                with no command configured the harness's clean exit was the
                only evidence, and a declared file that is not there is better
                evidence than anything the gate had.

                That new fetch can FAIL, and when it does the gate reports the
                answer it gave before ``require_paths`` existed: ``skipped``
                with ``skip_reason`` and no path check. Asking a question we
                then could not answer must not be what converts a leaf into a
                failure. The three unfetchable faults would otherwise produce
                ``_SKIP_NO_CREDENTIAL_PROVIDER``, ``_SKIP_NO_TOKEN`` and
                ``error``, all three of which ``_no_op_evidence`` refuses, so a
                credential-less GitHub project with no verify command would go
                from closing its no-op leaves to failing every one of them.

                This applies ONLY when ``verify_cmd`` is None. With a command
                configured the same fetch failure keeps refusing, and must:
                there the operator asked for a check and it did not run.
            leaf_verify_cmd: The leaf's OWN declared verification, run on the
                SAME checkout and only when ``verify_cmd`` went red. Absent (the
                default) leaves every existing caller byte-identical: no second
                command is run and ``leaf`` on the result stays None.

        Returns:
            The gate verdict.
        """
        # ``""`` and ``None`` were already caught by the falsy check below.
        # ``"   "`` was not: it is truthy, so it reached the shell, exited 0,
        # and came back ``passed``.  For ``resolve_no_change_run`` that is the
        # worst shape of all, because a ``passed`` there closes a leaf with the
        # evidence string "verify passed on <branch>" for a command that never
        # ran.  Normalized, it reports ``skipped`` and says so.
        verify_cmd = normalize_verify_cmd(verify_cmd)
        skip_reason = disabled_reason or _SKIP_NO_VERIFY_CMD
        if verify_cmd is None and not require_paths:
            logger.info("verify gate skipped: %s (branch=%s)", skip_reason, plan_branch)
            return _PlanVerifyResult("skipped", reason=skip_reason)

        result = await self._fetch_and_inspect_branch(
            repo_url,
            plan_branch,
            verify_cmd,
            require_paths,
            skip_reason,
            leaf_verify_cmd,
        )
        # The tree never materialized AND there was no command to run, so the
        # fetch happened for ``require_paths`` alone. ``paths is None`` is the
        # exact test for that: ``require_paths`` is non-empty by here, so an
        # inspected tree always carries a check.
        #
        # Report the answer this gate gave before ``require_paths`` existed.
        # Asking a question we then could not answer must not, by itself,
        # convert a leaf that closed into a failure: the failure is a fact about
        # the DEPLOYMENT, not about the leaf. With a verify command configured
        # this does not fire, and it must not: there the same fetch failure
        # means a check the operator ASKED FOR did not run, which is the
        # fail-closed refusal ``_no_op_evidence`` makes on purpose.
        if verify_cmd is None and result.paths is None:
            logger.warning(
                "The branch %s could not be fetched, so %d declared edit "
                "location(s) went unchecked (%s); the gate's own answer is "
                "unchanged: %s",
                plan_branch,
                len(require_paths),
                result.reason or result.status,
                skip_reason,
            )
            return _PlanVerifyResult("skipped", reason=skip_reason)
        return result

    async def _fetch_and_inspect_branch(
        self,
        repo_url: str,
        plan_branch: str,
        verify_cmd: str | None,
        require_paths: Sequence[str],
        skip_reason: str,
        leaf_verify_cmd: str | None = None,
    ) -> _PlanVerifyResult:
        """Get the branch onto disk and answer whatever was asked about it.

        Split out of :meth:`_verify_plan_branch` so the "a fetch made only for
        ``require_paths`` must not change the gate's own answer" rule has ONE
        place to live. Inline, that rule would have to be repeated at each of
        the four points a fetch can fail (no credential provider, no token, the
        GitHub clone raising, the local checkout raising), and those four
        already produce three different verdicts between them.

        Args:
            repo_url: The project's repository URL or local bare-repo path.
            plan_branch: The branch to fetch.
            verify_cmd: The normalized verify command, or None when the fetch
                exists only to answer ``require_paths``.
            require_paths: Edit locations a leaf declared.
            skip_reason: What a missing ``verify_cmd`` should report.
            leaf_verify_cmd: The leaf's own declared verification, for the same
                checkout. Threaded rather than re-resolved per backend so one
                backend cannot come to run a check the other does not.

        Returns:
            The gate verdict. ``paths`` is set only when a tree was actually
            inspected, which is what lets the caller tell a fetch that failed
            from one that succeeded.
        """
        backend = self._resolve_backend(repo_url)
        if backend.name == "local":
            return await self._verify_local_plan_branch(
                backend,
                repo_url,
                plan_branch,
                verify_cmd,
                require_paths,
                skip_reason,
                leaf_verify_cmd,
            )

        provider = getattr(self._git, "_provider", None)
        if provider is None:
            # WARNING, not INFO, and deliberately unlike the no-verify_cmd skip
            # above.  Having no verify_cmd is an operator choice; having no way
            # to reach the repository is a broken deployment, and the gate that
            # was supposed to judge this branch did not run.  Logging that at
            # INFO is how a skip comes to read like a pass.
            logger.warning(
                "verify gate skipped: %s (branch=%s)",
                _SKIP_NO_CREDENTIAL_PROVIDER,
                plan_branch,
            )
            return _PlanVerifyResult("skipped", reason=_SKIP_NO_CREDENTIAL_PROVIDER)

        try:
            token = await provider.token_for_repo(repo_url)
            if not token:
                # A GitHub repo with no credential cannot be fetched at all, so
                # there is nothing to run against.  This is the documented
                # credential-less carve-out that also skips remote preflight;
                # it is NOT reachable for a local project any more, which is
                # the whole point of the branch above.
                # WARNING for the same reason as the credential-provider skip
                # above: a repository we cannot get a token for is a fault, not
                # a configuration choice.
                logger.warning(
                    "verify gate skipped: %s (repo=%s, branch=%s)",
                    _SKIP_NO_TOKEN,
                    repo_url,
                    plan_branch,
                )
                return _PlanVerifyResult("skipped", reason=_SKIP_NO_TOKEN)

            with tempfile.TemporaryDirectory() as checkout_dir:
                clone_with_token(repo_url, checkout_dir, token)
                checkout_branch(checkout_dir, plan_branch, token)
                result = await _inspect_branch_tree(
                    checkout_dir,
                    plan_branch,
                    verify_cmd,
                    require_paths,
                    skip_reason,
                    leaf_verify_cmd,
                )
        except Exception as exc:  # noqa: BLE001 - degrade, never wedge the loop
            logger.warning(
                "verify gate error (branch=%s, cmd=`%s`): %s",
                plan_branch,
                verify_cmd or "-",
                exc,
            )
            return _PlanVerifyResult("error")

        return result

    async def _verify_local_plan_branch(
        self,
        backend: GitBackend,
        repo_url: str,
        plan_branch: str,
        verify_cmd: str | None,
        require_paths: Sequence[str] = (),
        skip_reason: str = _SKIP_NO_VERIFY_CMD,
        leaf_verify_cmd: str | None = None,
    ) -> _PlanVerifyResult:
        """Run the verify command against a plan branch in a local bare repo.

        A bare repo on disk needs no token: the backend's ``checkout`` is a
        plain ``git clone <path> <dest>`` followed by ``git checkout
        <branch>``, and it raises when the branch is missing.

        A checkout that cannot be produced is an ``error``, never a ``skipped``.
        Both callers fail closed on ``error`` (park the wave / publish
        ``plan_verify_failed``) and neither reacts to a ``skipped`` at all, so
        returning ``skipped`` here would silently green the gate again.

        Args:
            backend: The already-resolved local backend.
            repo_url: The bare repo's path, for logging only.
            plan_branch: The accumulated plan branch to verify.
            verify_cmd: The project's configured verification command, or None
                when the branch is being fetched only to answer
                ``require_paths``.
            require_paths: Edit locations a leaf declared, checked against the
                same checkout. Empty means the question was not asked.
            skip_reason: What a missing ``verify_cmd`` should report.
            leaf_verify_cmd: The leaf's own declared verification, run in the
                same checkout when the project command goes red.

        Returns:
            The gate verdict: ``passed``, ``failed``, ``skipped`` (no command,
            paths only), or ``error``.
        """
        # ``LocalGitBackend.checkout`` reads only ``branch``.  ``base`` is set
        # to the same branch rather than left empty so that a future checkout
        # that did consult it would still resolve the branch under test.
        ref = PullRequestRef(backend="local", branch=plan_branch, base=plan_branch)
        try:
            with tempfile.TemporaryDirectory() as checkout_dir:
                await backend.checkout(ref, checkout_dir)
                result = await _inspect_branch_tree(
                    checkout_dir,
                    plan_branch,
                    verify_cmd,
                    require_paths,
                    skip_reason,
                    leaf_verify_cmd,
                )
        except Exception as exc:  # noqa: BLE001 - degrade, never wedge the loop
            logger.warning(
                "verify gate error (repo=%s, branch=%s, cmd=`%s`): %s",
                repo_url,
                plan_branch,
                verify_cmd or "-",
                exc,
            )
            return _PlanVerifyResult("error")

        return result

    async def _nothing_to_integrate_reason(
        self, repo_url: str, base: str, head: str, backend: GitBackend
    ) -> str | None:
        """Positively establish that there is no integration PR to open.

        Returns a reason ONLY for a fully answered lookup that settles the
        question. `gh pr create` refuses both cases below, and reporting a
        refusal the code could have predicted as `Integration PR open failed`
        misfiles a correct outcome as an error.

        Three facts qualify, and they are different facts:

        - **The branches resolve to the same SHA.** The plan branch has nothing
          of its own: every task closed as a no-op, so the repository already
          satisfied the spec. `gh pr create` says "No commits between ...".
        - **The plan branch is absent from the remote.** There is no head ref
          to open a PR from at all; `gh pr create` says "Head ref must be a
          branch". Single-branch (auto-delegate) mode reaches this routinely:
          the task PRs already target the base branch, so merging them IS the
          integration and the merge deletes the shared branch (walkthrough
          #12). What the absence MEANS is not asserted here, only the fact
          that there is no head to open a PR from.
        - **Base already contains the branch.** The plan branch merely TRAILS
          base: a two-tier plan whose leaves all closed ``no_changes`` or
          ``superseded`` puts no commit on it, and base then moves on, so the
          SHAs are no longer equal and neither fact above can fire. ``gh pr
          create`` refuses with "No commits between ..." and the caller wrote
          ``plans.error``, which is a ONE-WAY signal: the row read broken
          permanently for a plan that did everything right.

        The third fact is the only one that needs the BACKEND. ``ls-remote``
        reads refs and knows nothing about ancestry, and a bare repo has no
        ``gh``, so the question has to be asked through the seam that already
        abstracts those two worlds.

        ``remote_head_sha`` returns ``None`` for an absent branch and RAISES
        when the lookup itself fails, so ``None`` is an ANSWER, not a shrug.
        "Could not ask" arrives as the exception below and always falls
        through to the creation attempt: treating an unanswered lookup as a
        skip would stop opening integration PRs the first time the network
        hiccupped, and the plan would complete with no PR and no error, which
        is exactly the class of silent gap this loop keeps rediscovering.

        ``base`` is the anchor for BOTH facts, so it must be a real non-empty
        ``str`` or nothing is established and this falls through. That is not
        defensive clutter: "equal" is only meaningful for two answers, and any
        other object being equal to itself would make this skip integration
        for every plan while looking correct. It was measured doing exactly
        that against a loose test double before the check was tightened.

        Sufficient, not necessary, and deliberately so. Only a POSITIVE, fully
        answered check may change the flow, the same rule
        ``_existing_integration_pr`` follows: ``base_contains`` returning False
        or None falls through to the normal creation attempt and the old error
        path, which is the safe direction. The failure branch is deliberately
        NOT downgraded wholesale, and the refusal is deliberately NOT
        recognised by matching ``gh``'s "No commits between" text: credential,
        rate-limit and network failures reach that branch too, and they mean
        the plan's work really is stranded.

        Args:
            repo_url: The project's repository.
            base: The integration PR's base branch.
            head: The plan's branch.
            backend: The resolved git backend, for the ancestry question the
                ``ls-remote`` above cannot answer.

        Returns:
            A human-readable reason to log, or None when the question is not
            positively settled.
        """
        try:
            head_sha = await self._git.remote_head_sha(repo_url, head)
            base_sha = await self._git.remote_head_sha(repo_url, base)
        except Exception as exc:  # noqa: BLE001 - cannot ask is not an answer
            logger.warning(
                "Could not compare %s against %s to decide whether there is "
                "anything to integrate (%s); attempting the PR anyway",
                head,
                base,
                exc,
            )
            return None
        if not isinstance(base_sha, str) or not base_sha:
            return None
        if head_sha is None:
            return (
                f"branch={head} is not on the remote, so there is no head ref "
                f"to open an integration PR from; in single-branch mode "
                f"merging the task PRs integrates the work into base={base} "
                f"and deletes the branch"
            )
        if not isinstance(head_sha, str):
            return None
        if head_sha == base_sha:
            return (
                f"branch={head} is identical to base={base}, so there is no "
                f"diff to open a PR for"
            )
        # Only True changes the flow. False is an answer ("the branch really
        # does carry work base has not got") and None is the absence of one;
        # both fall through to the creation attempt, so an unreadable remote
        # can never turn into a silently skipped integration.
        contains = await backend.base_contains(base, head)
        if contains is True:
            return (
                f"base={base} already carries branch={head}, which trails it "
                f"and has no commit of its own to open a PR for"
            )
        return None

    async def _existing_integration_pr(
        self, repo_url: str, base: str, head: str
    ) -> str | None:
        """Positively check whether ``head`` already has an open PR against ``base``.

        Single-branch (auto-delegate) mode reuses one caller-named work
        branch for every task, so the plan's work branch (``head``) IS the
        worker's own PR head. By the time the plan completes, that PR already
        exists, and ``gh pr create --base <base> --head <head>`` fails
        outright (GitHub refuses a second open PR for the same
        base/head pair).

        This is detected BEFORE attempting creation, via ``gh pr list --head
        --base --state open`` (the same positive lookup the harness
        entrypoints already use to reuse a PR across worker retries -- see
        ``docker/opencode-agent/entrypoint.sh``), never by interpreting a
        creation failure afterwards: a caught failure there could just as
        easily be a real, different ``gh`` error (bad credentials, rate
        limit, network), and treating every failure as "already open" would
        hide it forever instead of surfacing it.

        Uses ``GitOps.repo_slug`` directly (a pure static method) rather than
        going through ``self._git.repo_slug`` so slug resolution never
        depends on how a test double happens to wire that attribute.

        Args:
            repo_url: The project's GitHub repository URL.
            base: The integration PR's base branch.
            head: The plan's work branch (the would-be PR head).

        Returns:
            The existing open PR's URL, or None when none exists OR the
            check itself could not be run (non-GitHub repo, no credential,
            ``gh`` failure). Either "None" case falls through to the normal
            best-effort creation attempt below, so a broken check never
            blocks it -- only a POSITIVE hit skips creation.
        """
        slug = GitOps.repo_slug(repo_url)
        if not slug:
            return None
        try:
            token = await self._git._token_for_repo(slug)
            code, stdout, _stderr = await self._git._run_command(
                [
                    "gh",
                    "pr",
                    "list",
                    "--repo",
                    slug,
                    "--head",
                    head,
                    "--base",
                    base,
                    "--state",
                    "open",
                    "--json",
                    "url",
                ],
                token=token,
            )
        except Exception:  # noqa: BLE001 - a broken check must not block creation
            return None
        if code != 0:
            return None
        results: list[Any] = []
        with contextlib.suppress(json.JSONDecodeError, TypeError):
            results = json.loads(stdout)
        if isinstance(results, list) and results and isinstance(results[0], dict):
            url = results[0].get("url")
            if isinstance(url, str):
                return url
        return None

    async def on_plan_completed(self, plan_id: str) -> None:
        """Open a best-effort integration PR and signal readiness, then draft a context sync."""
        plan = await self._tq.get_plan(plan_id)
        if plan is None:
            return
        project = await self._tq.get_project(plan["project_id"])
        if project is None:
            return

        log = task_logger(logger, plan_id=plan_id)

        plan_branch = plan.get("plan_branch_name")
        repo_url = project.get("repo_url")
        if plan_branch and repo_url:
            from orchestrator.core.git_ops import compare_url

            base = project.get("default_branch") or "main"

            # Whole-plan verify gate: run the project's verify command against
            # the accumulated plan branch BEFORE greening the integration PR.
            # Per-task gates are task-scoped, so a cross-task regression (an
            # additive change that breaks a pre-existing test in another leaf)
            # only surfaces against the fully merged plan branch.  All I/O here
            # degrades to a warning + a verify_status, never wedges the loop.
            # Bench condition C disables the mechanical gate at every level;
            # see core/bench_mode.py.
            plan_gate_disabled = verify_gate_disabled()
            gate_cmd = None if plan_gate_disabled else project.get("verify_cmd")
            verify_status = await self._verify_plan_branch(
                repo_url,
                plan_branch,
                gate_cmd,
                disabled_reason=(
                    _SKIP_BENCH_MODE_DISABLED if plan_gate_disabled else None
                ),
            )

            # A red plan branch is only evidence that this plan broke something
            # when the SAME command is green on the branch the plan was cut
            # from; see ``attribute_plan_verify_failure``. Asked here, next to
            # the run it is about, and with the SAME command the head ran, so
            # the two verdicts can never be about two different commands.
            #
            # Only a ``failed`` head reaches it. An ``error`` means the gate did
            # not run, so there is no verdict to attribute, and the arms below
            # keep their old behaviour for it exactly.
            attribution: _PlanVerifyAttribution | None = None
            if verify_status.status == "failed":
                base_verdict = await self._verify_plan_branch(repo_url, base, gate_cmd)
                attribution = attribute_plan_verify_failure(
                    verify_status, base_verdict, base
                )
                if not attribution.alarm:
                    log.warning(
                        "Plan verify gate is RED on plan %s's branch %s, but %s",
                        plan_id,
                        plan_branch,
                        attribution.detail,
                    )

            pr_url: str | None = None
            # WHICH of the four outcomes this plan got, and why. Two of them
            # carry no ``pr_url`` and mean opposite things, and until these
            # existed both published the identical payload below.
            integration_status: str
            integration_detail: str | None = None
            # The SEAM, not ``self._git``. Every other git operation on this
            # path already resolves a backend; the integration stage reaching
            # straight for ``GitOps`` is why a local project got the whole
            # governed loop except its last link: the slug was built by
            # splitting the repo URL on "github.com/", which for a filesystem
            # path is the path, and `gh pr create --repo /repos/demo` fails.
            backend = self._resolve_backend(repo_url)
            existing_pr = await self._existing_integration_pr(
                repo_url, base, plan_branch
            )
            if existing_pr:
                # Single-branch mode: the worker's own PR already IS the
                # integration PR. Reuse it rather than fail a second
                # `gh pr create` against the same (base, head) pair.
                pr_url = existing_pr
                integration_status = _INTEGRATION_REUSED
                log.info(
                    "integration PR skipped: branch=%s already has an open "
                    "PR against base=%s, reusing %s",
                    plan_branch,
                    base,
                    existing_pr,
                )
            elif nothing_to_integrate := await self._nothing_to_integrate_reason(
                repo_url, base, plan_branch, backend
            ):
                # Not a failure. Three shapes reach here, and none is an
                # error: a plan whose tasks all closed as no-ops leaves its
                # branch identical to base ("No commits between main and
                # plan/...", walkthrough #7), a single-branch plan whose
                # task PRs were merged has no branch left at all ("Head ref
                # must be a branch", walkthrough #12), and a two-tier plan
                # whose branch merely TRAILS a base that moved on is refused
                # for having no commits of its own. Attempting creation
                # anyway logged `Integration PR open failed` over a completely
                # correct outcome. This is the same fact-versus-verdict split
                # as `no_changes` one layer down: the absence is a fact, and
                # what it MEANS is decided here.
                integration_status = _INTEGRATION_NOTHING
                integration_detail = nothing_to_integrate
                log.info(
                    "nothing to integrate for plan %s: %s",
                    plan_id,
                    nothing_to_integrate,
                )
            else:
                try:
                    pr_url = await backend.open_integration_pr(
                        base=base,
                        head=plan_branch,
                        title=f"Integrate {plan_branch}",
                        body=(
                            "Auto-opened by Praxis: every task in this plan merged to "
                            f"`{plan_branch}`. Review and merge to `{base}` to integrate."
                        ),
                    )
                    integration_status = _INTEGRATION_OPENED
                except Exception as exc:  # noqa: BLE001 - reported, never fatal
                    integration_status = _INTEGRATION_FAILED
                    # States what was established and NOTHING ELSE. This used
                    # to assert that the work "is on the plan branch and has
                    # NOT reached the base branch", which is one of the causes
                    # rather than the finding: a branch that merely TRAILS base
                    # is refused by `gh pr create` for having no commits at
                    # all, and its work reached base long ago. Naming the check
                    # an operator can run beats guessing which case this was.
                    integration_detail = (
                        f"the integration pull request for branch={plan_branch} "
                        f"onto base={base} could not be opened ({exc}); check "
                        f"whether {plan_branch} carries commits {base} does not "
                        "have, and whether the credentials for this repository "
                        "still work"
                    )
                    logger.warning(
                        "Integration PR open failed for %s: %s", plan_id, exc
                    )
                    # Onto the PLAN ROW, not only into the log. The stage runs
                    # exactly once -- `process_plan_once` writes COMPLETED
                    # before calling this, and `get_runnable_plans` returns
                    # only PENDING and ACTIVE plans, so nothing re-enters here
                    # -- and `plans.error` is what `PlanResponse` and MCP
                    # `poll_plan` serve. Without it the stranding was
                    # discoverable only from one `docker logs` line.
                    try:
                        await self._tq.set_plan_error(plan_id, integration_detail)
                    except Exception:  # noqa: BLE001 - never wedge the loop
                        logger.warning(
                            "Failed to record the integration failure for plan %s",
                            plan_id,
                            exc_info=True,
                        )

            if pr_url:
                # Persist BEFORE publishing: the SSE event below is consumed
                # only by whoever is watching at that instant, and the log line
                # inside open_integration_pr is not a surface any user reaches.
                # The stored URL is what `praxis pending` and `merge-plan` read,
                # so a failure to store it must not be silent.
                try:
                    await self._tq.set_plan_integration_pr(plan_id, pr_url)
                except Exception as exc:  # noqa: BLE001 - never wedge the loop
                    logger.warning(
                        "Failed to record integration PR %s for plan %s: %s",
                        pr_url,
                        plan_id,
                        exc,
                    )

            # What this event REPORTS, which since the base comparison exists is
            # no longer the same fact as what the head gate DID. ``failed`` and
            # ``unattributed`` are both a red gate; only the first is this
            # plan's doing.
            reported_status = (
                attribution.reported_status
                if attribution is not None
                else verify_status.status
            )
            # Stated by the attribution, never re-derived from
            # ``reported_status``. Collapsing the two back into one membership
            # test is exactly the inference this fix removes.
            #
            # Honest caveat: across today's arms ``reported_status in
            # ("failed", "error")`` is still EQUIVALENT to ``attribution.alarm``,
            # so no test can currently distinguish the two and any guard
            # claiming to pin the separation would be inert. The separation is
            # future-proofing against the next arm, not a live discriminator.
            publish_alarm = (
                attribution.alarm
                if attribution is not None
                else verify_status.status in ("failed", "error")
            )
            if publish_alarm:
                # Fail closed: surface both a real verify failure AND an
                # infra error (clone/checkout/verify raised) on its own event so
                # the plan does not silently advance. Previously an ``error`` was
                # swallowed as a warning, which made the whole-plan backstop a
                # no-op whenever the plan-branch checkout failed. The integration
                # PR is still opened above so the failure is visible on a real PR.
                gate_output = verify_status.output or (
                    # Keyed on the STATUS, not on the emptiness of the output. A
                    # verify command is free to print nothing and exit non-zero
                    # (`test -f dist/bundle.js`), and that is a real cross-task
                    # regression; saying the gate RAISED sends the reader to a
                    # different fault with a different remedy.
                    "plan verify gate errored (clone/checkout/verify "
                    "raised); see orchestrator logs"
                    if verify_status.status == "error"
                    else "plan verify gate FAILED and the command "
                    "printed nothing; the exit status is the verdict"
                )
                self._bus.publish(
                    {
                        "type": "plan_verify_failed",
                        "project_id": project["id"],
                        "plan_id": plan_id,
                        "plan_branch": plan_branch,
                        "base_branch": base,
                        "status": reported_status,
                        # The attribution sentence FIRST, then what the command
                        # printed: the sentence says whose failure it is, the
                        # output says what failed, and an operator needs both.
                        # Prepended rather than carried in a new field so the
                        # one surface that renders this event -- the raw SSE
                        # stream -- cannot show the verdict without the reason.
                        "output": (
                            f"{attribution.detail}\n\n{gate_output}".strip()
                            if attribution is not None
                            else gate_output
                        ),
                        "pr_url": pr_url,
                    }
                )

            self._bus.publish(
                {
                    "type": "plan_integration_ready",
                    "project_id": project["id"],
                    "plan_id": plan_id,
                    "plan_branch": plan_branch,
                    "base_branch": base,
                    "pr_url": pr_url,
                    # Which of the four outcomes, stated rather than inferred.
                    # A consumer reading only ``pr_url`` cannot tell "the work
                    # is already on base, nothing to do" from "the pull request
                    # could not be opened and the work is stranded", and those
                    # are the two cases that most need telling apart.
                    "integration_status": integration_status,
                    # Why, in the words already logged for that outcome. None
                    # for the two statuses that carry a URL, where the URL is
                    # the whole answer.
                    "integration_detail": integration_detail,
                    "compare_url": compare_url(repo_url, base, plan_branch),
                    # ``_PLAN_VERIFY_UNATTRIBUTED`` is the one value of this
                    # field where the gate RAN, went RED, and no
                    # ``plan_verify_failed`` accompanies it. Neither of the
                    # obvious alternatives is honest: ``failed`` with no alarm
                    # silently breaks a pairing every reader relies on, and
                    # ``passed`` is the larger lie.
                    "verify_status": reported_status,
                }
            )

        if self._context_sync is None:
            return
        summary = f"Completed plan: {plan_branch or plan_id}"
        draft = await self._context_sync.draft(repo_url, summary)
        self._bus.publish(
            {
                "type": "context_draft_ready",
                "project_id": project["id"],
                "draft_id": draft["draft_id"],
            }
        )
