"""Pydantic schemas and enums for orchestrator API contracts."""

from __future__ import annotations

import re
from enum import StrEnum
from sys import version_info
from typing import Literal, TypedDict

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from orchestrator.core.harnesses import REGISTRY, default_harness_id
from orchestrator.core.repo_url_policy import validate_repo_url as _validate_repo_url
from orchestrator.core.verify_gate import normalize_verify_cmd


if version_info < (3, 12):
    from typing_extensions import TypedDict


def sanitize_branch_ref(value: str | None) -> str | None:
    """Sanitize a caller-supplied branch into a safe git ref.

    Blocks path traversal, leading dashes, whitespace, and the dangerous
    ``..``/``//`` ref sequences so a hostile branch cannot escape the
    ``agent/``-style namespace or confuse git ref parsing.

    Shared by ``DispatchRequest`` and ``ExecutePlanRequest``.  Only the first
    used to have it, which is the same defect the ``repo_url`` policy had, on
    a field that reaches git's argv more directly: a caller-supplied branch
    becomes ``plans.plan_branch_name``, then ``BASE_BRANCH``, then
    ``PullRequestRef.base``, then a git argument.

    Args:
        value: The caller-supplied branch, or None.

    Returns:
        The stripped branch, or None when none was given.

    Raises:
        ValueError: If the branch is empty or is not a safe ref.
    """
    if value is None:
        return None
    candidate = value.strip()
    if not candidate:
        msg = "branch must not be empty when provided"
        raise ValueError(msg)
    if (
        not re.fullmatch(r"[A-Za-z0-9._/-]+", candidate)
        or candidate.startswith(("-", "/"))
        or candidate.endswith(("/", ".lock"))
        or ".." in candidate
        or "//" in candidate
    ):
        msg = "branch contains illegal characters or an unsafe ref pattern"
        raise ValueError(msg)
    return candidate


def validate_verify_cmd(value: str | None) -> str | None:
    """Refuse a ``verify_cmd`` that looks configured but contains no command.

    Shared by ``ProjectCreate`` and ``ProjectUpdate``. ``None`` and ``""`` are
    accepted UNCHANGED and keep their existing meaning of "not configured":
    ``None`` is the create-time default and, on a PATCH, is the "leave this
    field alone" signal (``update_project`` dumps with ``exclude_none=True``),
    while ``""`` is the only way an operator can clear a command that is
    already set. Rejecting either would break both flows.

    A non-empty, whitespace-only value is different in kind. It is not a way of
    saying "unconfigured", it is a value that reads as configured everywhere it
    is displayed and executes as nothing when run. A 422 naming the problem is
    the honest answer, and it stops the bad row ever reaching the database.

    Args:
        value: The submitted ``verify_cmd``.

    Returns:
        ``value`` unchanged when it is ``None``, ``""``, or a real command.

    Raises:
        ValueError: If the value is non-empty but has no non-whitespace
            character in it.
    """
    if value is None or value == "":
        return value
    # Same predicate the runtime gate uses, so the boundary can never disagree
    # with what the three read sites treat as "not configured".
    if normalize_verify_cmd(value) is None:
        msg = (
            "verify_cmd must contain a command; a whitespace-only value runs "
            "nothing and would report the verify gate as passed. Use an empty "
            "string to clear it, or omit the field to leave it unset."
        )
        raise ValueError(msg)
    return value


class TaskStatus(StrEnum):
    """Task lifecycle status."""

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    REVIEWING = "reviewing"
    PASSED = "passed"
    FAILED = "failed"
    MERGED = "merged"
    NEEDS_CLARIFICATION = "needs_clarification"
    SUPERSEDED = "superseded"
    #: The leaf's work was already present, so the worker correctly produced no
    #: diff. Terminal, and neither a success nor a failure: there is no PR to
    #: review and nothing to merge, but the repository IS in the state the leaf
    #: asked for. Distinct from SUPERSEDED, which means "replaced by split
    #: children" and would credit this leaf's outcome to a split that never
    #: happened.
    NO_CHANGES = "no_changes"


class PlanStatus(StrEnum):
    """Plan lifecycle status."""

    PENDING = "pending"
    ACTIVE = "active"
    COMPLETED = "completed"
    REJECTED = "rejected"
    FAILED = "failed"


class OpusStatus(StrEnum):
    """Claude Opus availability status."""

    AVAILABLE = "available"
    RATE_LIMITED = "rate_limited"
    RESUMING = "resuming"


class AgentRunStatus(StrEnum):
    """Harness agent run status (any harness: OpenCode or agy)."""

    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    STOPPED = "stopped"


class CapabilityProfile(BaseModel):
    """Declared and learned capability profile for a local worker model.

    Used by the capability-aware plan review to decide which tasks the local
    model can handle and which to flag ``needs_stronger_model``.
    """

    model_name: str
    parameter_count_b: float
    context_window: int
    strengths: str = ""
    weaknesses: str = ""
    max_task_complexity: str = "medium"
    max_files_touched: int = 5
    max_loc_delta: int = 300
    max_checklist_items: int = 12
    max_dep_depth: int = 4
    escalate_task_types: list[str] = Field(default_factory=list)


class LeafType(StrEnum):
    """Fixed task shapes a decomposed leaf may take.

    Free-form decomposition invites free-form ambiguity; fixed templates
    outperform open-ended planning (Agentless arXiv 2407.01489, CodeR
    arXiv 2406.01304).  Each type declares which ``plan_text`` sections are
    mandatory; see ``core/leaf_templates.REQUIRED_SECTIONS``.
    """

    BUGFIX_REPRO = "bugfix_repro"
    FUNCTION_ADD = "function_add"
    ENDPOINT_ADD = "endpoint_add"
    REFACTOR_RENAME = "refactor_rename"
    TEST_ADD = "test_add"
    CONFIG_CHANGE = "config_change"
    DOC_CHANGE = "doc_change"
    GENERIC = "generic"


LEAF_SCHEMA_VERSION = 2


class LeafChecklistItem(BaseModel):
    """Single checklist step within a leaf task."""

    text: str


class LeafTask(BaseModel):
    """Decomposed leaf task produced by the capability-aware plan review.

    Accepts extra fields (``extra="allow"``) so the brain can attach
    arbitrary metadata without breaking the parser.  Missing optional
    fields derive sensible defaults from ``title`` via an after-validator.
    """

    model_config = ConfigDict(extra="allow")

    schema_version: int = LEAF_SCHEMA_VERSION
    id: str
    title: str
    description: str = ""
    plan_text: str = ""
    depends_on: list[str] = Field(default_factory=list)
    checklist: list[LeafChecklistItem] = Field(default_factory=list)
    needs_stronger_model: bool = False
    files: list[str] = Field(default_factory=list)
    task_type: str | None = None
    estimated_loc: int | None = None
    verification: str | None = None
    leaf_type: LeafType = LeafType.GENERIC
    neighbor_contracts: str | None = None

    @model_validator(mode="after")
    def _apply_title_defaults(self) -> LeafTask:
        if not self.description:
            self.description = self.title
        if not self.plan_text:
            self.plan_text = self.description
        if not self.checklist:
            self.checklist = [LeafChecklistItem(text=self.title)]
        return self


class TriageDecision(BaseModel):
    """The brain's verdict on a leaf that failed twice.

    Contract from the Usable Praxis spec section 2.2.  Malformed output gets
    one re-ask with the validation errors (same pattern as F3's informed
    rounds); a second failure falls back to ``human``.  Fail closed, never
    guess.
    """

    model_config = ConfigDict(extra="forbid")

    decision: Literal["retry", "split", "escalate", "human"]
    reason: str
    children: list[LeafTask] | None = None
    refined_prompt: str | None = None

    @model_validator(mode="after")
    def _check_children_match_decision(self) -> TriageDecision:
        if self.decision == "split":
            if not self.children:
                message = "children are required when decision == 'split'"
                raise ValueError(message)
            if not 2 <= len(self.children) <= 4:
                message = (
                    "a split must produce between 2 and 4 children, "
                    f"got {len(self.children)}"
                )
                raise ValueError(message)
        elif self.children:
            message = (
                "children are only allowed when decision == 'split', "
                f"not '{self.decision}'"
            )
            raise ValueError(message)
        return self


class ProjectCreate(BaseModel):
    """Request payload for creating a project."""

    name: str
    repo_url: str
    model_name: str | None = None
    default_branch: str = "main"
    approval_gate: bool = True
    confidence_threshold: float = Field(default=0.7, ge=0.0, le=1.0)
    max_retries: int = Field(default=3, ge=1, le=10)
    max_improvement_cycles: int = Field(default=5, ge=1, le=20)
    lm_studio_url: str = "http://host.docker.internal:1234"
    auto_merge: bool = False
    verify_cmd: str | None = None
    agent_model: str | None = None
    agent_model_effort: str | None = None
    harness: str | None = None
    #: Worker context window in tokens. None means "not declared", which falls
    #: through to the settings file's declaration and then to the LM Studio
    #: probe (``core/context_window``). Set it for a model nobody has declared,
    #: so the pre-dispatch budget gate can run instead of being skipped.
    context_window: int | None = Field(default=None, gt=0)

    @field_validator("harness")
    @classmethod
    def validate_harness(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if value not in REGISTRY:
            allowed = ", ".join(sorted(REGISTRY))
            msg = f"harness must be one of: {allowed}"
            raise ValueError(msg)
        return value

    @field_validator("repo_url")
    @classmethod
    def validate_repo_url(cls, value: str) -> str:
        """Apply the single shared policy (``core/repo_url_policy``).

        A local filesystem path passes here and is judged by
        ``Settings.allow_local_repo_paths`` at the endpoint, which is the only
        layer that can see runtime settings.
        """
        return _validate_repo_url(value)

    @field_validator(
        "name",
        "repo_url",
        "default_branch",
        "lm_studio_url",
    )
    @classmethod
    def validate_required_nonempty(cls, value: str) -> str:
        trimmed = value.strip()
        if not trimmed:
            msg = "value must not be empty"
            raise ValueError(msg)
        return trimmed

    @field_validator("model_name")
    @classmethod
    def validate_optional_model_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        trimmed = value.strip()
        if not trimmed:
            msg = "value must not be empty"
            raise ValueError(msg)
        return trimmed

    @field_validator("verify_cmd")
    @classmethod
    def validate_create_verify_cmd(cls, value: str | None) -> str | None:
        """Apply the shared policy (:func:`validate_verify_cmd`)."""
        return validate_verify_cmd(value)


class ProjectUpdate(BaseModel):
    """Request payload for updating a project."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    model_name: str | None = None
    approval_gate: bool | None = None
    auto_merge: bool | None = None
    verify_cmd: str | None = None
    confidence_threshold: float | None = Field(default=None, ge=0.0, le=1.0)
    max_retries: int | None = Field(default=None, ge=1, le=10)
    max_improvement_cycles: int | None = Field(default=None, ge=1, le=20)
    lm_studio_url: str | None = None
    agent_model: str | None = None
    agent_model_effort: str | None = None
    harness: str | None = None
    #: See ``ProjectCreate.context_window``. ``exclude_none`` on the update
    #: path means omitting it leaves the stored value alone; there is no way
    #: to clear it back to NULL through this endpoint, exactly as for every
    #: other nullable column here.
    context_window: int | None = Field(default=None, gt=0)

    @field_validator("harness")
    @classmethod
    def validate_optional_harness(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if value not in REGISTRY:
            allowed = ", ".join(sorted(REGISTRY))
            msg = f"harness must be one of: {allowed}"
            raise ValueError(msg)
        return value

    @field_validator(
        "name",
        "model_name",
        "lm_studio_url",
    )
    @classmethod
    def validate_optional_nonempty(cls, value: str | None) -> str | None:
        if value is None:
            return None
        trimmed = value.strip()
        if not trimmed:
            msg = "value must not be empty"
            raise ValueError(msg)
        return trimmed

    @field_validator("verify_cmd")
    @classmethod
    def validate_update_verify_cmd(cls, value: str | None) -> str | None:
        """Apply the shared policy (:func:`validate_verify_cmd`).

        Deliberately NOT folded into ``validate_optional_nonempty`` above:
        that one rejects ``""`` too, and ``""`` is the only way to clear an
        already-configured verify command.
        """
        return validate_verify_cmd(value)


class PlanCreate(BaseModel):
    """Request payload for creating a plan from a specification."""

    spec: str

    @field_validator("spec")
    @classmethod
    def validate_spec(cls, value: str) -> str:
        trimmed = value.strip()
        if not trimmed:
            msg = "spec must not be empty"
            raise ValueError(msg)
        return trimmed


class ProjectResponse(BaseModel):
    """Project response payload."""

    id: str
    user_id: str
    name: str
    repo_url: str
    model_name: str
    default_branch: str
    approval_gate: bool
    auto_merge: bool = False
    verify_cmd: str | None = None
    confidence_threshold: float
    max_retries: int
    max_improvement_cycles: int
    lm_studio_url: str
    agent_model: str | None = None
    agent_model_effort: str | None = None
    harness: str = Field(default_factory=default_harness_id)
    #: The window this PROJECT declares, or None when it declares none.
    #:
    #: None does NOT mean the budget gate was skipped, and an earlier version of
    #: this comment said it did. Every agy project resolves through a declared
    #: window with this column NULL, so the field cannot answer that question and
    #: must not be read as if it could. What answers it is the
    #: ``context_budget_skipped`` event and the ``context_window`` /
    #: ``context_window_source`` fields on ``agent_dispatched``, both per
    #: DISPATCH, which is the only scope at which the question is meaningful:
    #: an escalated leaf can run on a different harness than its project names.
    context_window: int | None = None
    created_at: str


def _max_planning_attempts() -> int:
    """The engine's planning-retry cap, read from its single definition.

    Imported inside the factory rather than at module scope because
    ``core/orchestrator.py`` imports THIS module, so a top-level import would
    be a cycle. Same dodge ``core/llm_router.py`` already uses for
    ``opus_bridge``, and for the same reason.

    Returns:
        ``core.orchestrator.MAX_PLANNING_ATTEMPTS``.
    """
    from orchestrator.core.orchestrator import MAX_PLANNING_ATTEMPTS

    return MAX_PLANNING_ATTEMPTS


class PlanResponse(BaseModel):
    """Plan response payload."""

    id: str
    project_id: str
    opus_plan: str | None = None
    plan_branch_name: str | None = None
    source: str
    confidence: float | None = None
    confidence_reason: str | None = None
    status: PlanStatus
    error: str | None = None
    #: How many times planning has been attempted and failed. Exposed because
    #: a plan that is retrying looks exactly like a plan that is decomposing
    #: from the outside: both are pending with no tasks. The count is the only
    #: thing that tells them apart, and it is what says how close the plan is
    #: to the bound that stops the retry.
    plan_attempts: int = 0
    #: The cap those attempts count against, so a reader can say how close the
    #: plan is to terminal. Served rather than mirrored: a client that keeps its
    #: own copy of this number prints "attempt 4/3" the day the engine's cap
    #: moves, and a denominator that says the plan is already dead is worse than
    #: no denominator at all. Every client reads it with a fallback, because a
    #: server older than this field simply does not send it.
    max_planning_attempts: int = Field(default_factory=_max_planning_attempts)
    spec_path: str | None = None
    plan_path: str | None = None
    #: The PR that carries this plan from its plan branch onto the project's
    #: base branch, and the time it landed. Exposed because a plan reported
    #: `completed` with no URL anywhere is indistinguishable, from the outside,
    #: from a plan whose work reached the base branch.
    integration_pr_url: str | None = None
    integration_merged_at: str | None = None
    #: DERIVED, not a column. Two projections of one
    #: ``plan_reachability.derive_stalled_by_failure_state`` call: the pending
    #: leaves that can never be dispatched, and the terminally FAILED tasks
    #: holding them there. No migration backs either, because both are a pure
    #: function of ``opus_plan`` plus the live task rows, and persisting them
    #: would create a second answer that goes stale the moment a task moves.
    #:
    #: They are DIFFERENT sets and both are needed, which is why this is not
    #: one list. The first gives a reader the count ("1 task blocked by a
    #: failure"); the second gives the recovery verb its argument, and only the
    #: second is a legal one -- ``POST /api/tasks/{id}/retry`` answers 409 for
    #: every status but ``failed``, so a surface holding only the first would
    #: print a ``praxis retry`` line that cannot work.
    #:
    #: Default empty, and every client reads them with a fallback: a server
    #: older than this field simply does not send them, and the absence must
    #: render as it did before the field existed rather than as "not stalled",
    #: which is the same answer here but would not be if the polarity inverted.
    stalled_task_ids: list[str] = []
    stalled_blocked_by_task_ids: list[str] = []
    created_at: str


class TaskResponse(BaseModel):
    """Task response payload."""

    id: str
    plan_id: str
    title: str
    description: str
    branch_name: str
    pr_url: str | None = None
    status: TaskStatus
    attempt: int
    review_feedback: str | None = None
    needs_stronger_model: bool = False
    escalation_state: str | None = None
    escalated_to: str | None = None
    checklist: list[dict] | None = None
    progress_note: str | None = None
    clarification_question: str | None = None
    clarification_answer: str | None = None
    clarification_state: str | None = None
    created_at: str
    updated_at: str


class AgentRunResponse(BaseModel):
    """Agent run response payload."""

    id: str
    task_id: str
    container_id: str
    status: AgentRunStatus
    logs: str
    started_at: str
    finished_at: str | None = None


class OpusStateResponse(BaseModel):
    """Current Opus state response payload."""

    status: OpusStatus
    rate_limited_at: str | None = None
    resume_at: str | None = None
    queued_count: int


class OpusTaskItem(TypedDict):
    """Opus-generated plan task contract."""

    title: str
    slug: str
    description: str
    depends_on: list[str]


class OpusPlanPayload(BaseModel):
    """Payload returned by Opus plan generation."""

    plan_summary: str
    plan_slug: str
    tasks: list[OpusTaskItem]


class OpusReviewPayload(BaseModel):
    """Payload returned by Opus review."""

    verdict: str
    feedback: str
    issues: list[str]

    @field_validator("verdict")
    @classmethod
    def validate_verdict(cls, value: str) -> str:
        if value not in {"pass", "fail"}:
            msg = "verdict must be either 'pass' or 'fail'"
            raise ValueError(msg)
        return value


class OpusImprovementTaskItem(TypedDict):
    """Opus-generated improvement task contract."""

    title: str
    slug: str
    description: str


class OpusImprovementPayload(BaseModel):
    """Payload returned by Opus autonomous improvement."""

    confidence: float = Field(ge=0.0, le=1.0)
    reason: str
    proposed_tasks: list[OpusImprovementTaskItem]


class DocResponse(BaseModel):
    """Doc index entry response payload."""

    path: str
    category: str
    title: str | None = None
    branch: str | None = None
    done_count: int = 0
    total_count: int = 0
    classified_by: str = "marker"
    updated_at: str | None = None


class SettingsEditableEntry(BaseModel):
    """Single editable setting with override status."""

    value: str | None
    overridden: bool


class SettingsReadonly(BaseModel):
    """Read-only system settings (never includes secrets)."""

    host: str
    port: int
    database_url: str


class SettingsResponse(BaseModel):
    """Response payload for GET /api/settings."""

    editable: dict[str, SettingsEditableEntry]
    readonly: SettingsReadonly


class MicroEdit(BaseModel):
    """A single-file change the BRAIN authored, for the micro-edit lane.

    Present on a dispatch means "do not spawn a worker for this; commit it
    yourself and govern it exactly as if you had". The lane skips the worker,
    never the governance: the verify gate, the review, the merge gate and the
    outcome row all still run. See
    ``docs/superpowers/specs/2026-08-21-micro-edit-lane.md``.

    ``content`` is the file's FULL new content rather than a patch or an
    old/new pair. A patch can fail to apply and an old/new pair can match in
    more than one place, and both failure modes arrive after the lane has
    already been chosen; full content cannot be ambiguous about what the file
    is supposed to end up as.
    """

    path: str
    """Repository-relative path of the one file to write."""
    content: str
    """The file's full new content, written verbatim."""
    commit_message: str
    """The commit subject. Written by the brain, so it says what and why."""

    @field_validator("path", "commit_message")
    @classmethod
    def validate_non_blank(cls, value: str) -> str:
        if not value.strip():
            msg = "field must not be empty"
            raise ValueError(msg)
        return value

    @field_validator("path")
    @classmethod
    def validate_relative_path(cls, value: str) -> str:
        """Reject a path that is absolute or climbs out of the repository.

        The lane checks this again against the real workspace, where the
        answer is authoritative. Rejecting here as well means the caller is
        told at the API boundary, with a 422 it can act on, instead of at
        dispatch time as a failed task.
        """
        candidate = value.strip().replace("\\", "/")
        if candidate.startswith("/") or ":" in candidate.split("/")[0]:
            msg = "path must be relative to the repository root"
            raise ValueError(msg)
        if any(part == ".." for part in candidate.split("/")):
            msg = "path must not climb out of the repository"
            raise ValueError(msg)
        return candidate


class DispatchRequest(BaseModel):
    """Request payload for MCP single-task dispatch."""

    repo_url: str
    instructions: str
    model: str
    harness: str | None = None
    branch: str | None = None
    """Base branch, or the work branch itself - which one depends on the mode
    active when the task is dispatched.

    In the DEFAULT (non-auto-delegate) mode, this is a BASE branch: the
    worker cuts a NEW ``agent/<slug>`` branch from it and opens a NEW PR;
    passing an existing PR's head here does NOT push follow-up commits onto
    that PR, and re-dispatching always creates a fresh PR. (Continue-on-PR
    mode for this arm is a planned follow-up.)

    In auto-delegate (single-branch) mode, this IS the work branch itself:
    the worker (or the micro-edit lane) commits directly onto ``branch``, no
    new branch is cut and no new PR opened per task. A second dispatch naming
    the same branch stacks its commit on top of the first's; both are
    preserved. Omit ``branch`` to let Praxis name one itself
    (``plan/mcp-<slug>``)."""
    name: str | None = None
    plan_path: str | None = None
    plan_text: str | None = None
    context: str | None = None
    """Curated, task-relevant context for the worker (memory, conventions,
    architecture notes). Scrubbed of secrets and size-capped server-side. NOT
    a place for secret values - those are redacted on arrival."""
    local_context: str | None = None
    """Minimum-blocking, secret-scrubbed manifest of NON-COMMITTED context the
    worker cannot see from a git clone (gitignored config shapes, user-scope
    conventions). Self-contained inline text, never a "read file X" pointer.
    Threaded onto each leaf's droppable repo_memory Bible slot. Include env var
    NAMES/shapes over live values; the worker writes code, it does not run it."""
    expected_base_sha: str | None = None
    """Optional origin base sha the caller believes it is dispatching against.
    When set, the server rejects the dispatch if it does not match the current
    ``origin/<branch>`` head (defense-in-depth against dispatching stale code
    when local commits were never pushed). Read-only remote compare."""
    files: list[str] | None = None
    """Edit locations for the worker (decomposition-standard rank 2), the same
    shape as ``LeafTask.files``. A direct dispatch has no decomposition step to
    supply this, so without it the worker's edit-locations pack section is
    simply empty. Optional; omit to leave that section empty, as before."""
    verification: str | None = None
    """Acceptance check for the worker (decomposition-standard rank 3 slot).
    Falls back to the project's ``verify_cmd`` when omitted, same as a
    decomposed leaf that declares no check of its own."""
    neighbor_contracts: str | None = None
    """Signatures of direct neighbors the worker should not break
    (decomposition-standard rank 4, optional), the same shape as
    ``LeafTask.neighbor_contracts``."""
    micro_edit: MicroEdit | None = None
    """Take the MICRO-EDIT LANE instead of dispatching a worker.

    When set, no container is spawned: the orchestrator commits this file to
    the work branch itself and then governs it exactly like a worker's change
    (verify gate, review, merge gate, outcome row). ``instructions`` is still
    required and still becomes the task's description, because it is what the
    review judges the change against.

    v1 requires auto-delegate mode, and requires ``branch``: the lane's
    reasoning about which commits belong to which task is built on one shared
    caller-named work branch. The rubric for when to use it lives in the mode
    contract (``praxis://guide/orchestration``), not in the engine: size is
    only reliably knowable AFTER a change is made and the lane must be chosen
    before, so the estimate belongs to whoever wrote the task."""

    @field_validator("repo_url")
    @classmethod
    def validate_repo_url(cls, value: str) -> str:
        """Apply the single shared policy (``core/repo_url_policy``).

        This used to be a looser, prefix-only copy that accepted ``http://``,
        ``ssh://`` and an embedded ``--upload-pack=``; it is now the same
        policy ``ProjectCreate`` enforces.
        """
        return _validate_repo_url(value)

    @field_validator("instructions", "model")
    @classmethod
    def validate_non_empty(cls, value: str) -> str:
        if not value.strip():
            msg = "field must not be empty"
            raise ValueError(msg)
        return value

    @field_validator("branch")
    @classmethod
    def validate_branch(cls, value: str | None) -> str | None:
        """Apply the single shared branch policy (:func:`sanitize_branch_ref`)."""
        return sanitize_branch_ref(value)


class DispatchResponse(BaseModel):
    """Response for a dispatched single-task plan."""

    task_id: str
    plan_id: str
    project_id: str
    status: str
    dashboard_url: str
    warnings: list[str] = Field(default_factory=list)


class ExecutePlanRequest(BaseModel):
    """Request to ingest an externally-authored plan for capability-aware execution."""

    repo_url: str
    plan: str
    model: str
    harness: str | None = None
    branch: str | None = None
    context: str | None = None
    local_context: str | None = None
    """Minimum-blocking, secret-scrubbed manifest of NON-COMMITTED context the
    worker cannot see from a git clone (gitignored config shapes, user-scope
    conventions). Self-contained inline text, never a "read file X" pointer.
    Threaded onto each leaf's droppable repo_memory Bible slot. Include env var
    NAMES/shapes over live values; the worker writes code, it does not run it."""
    expected_base_sha: str | None = None
    """Optional origin base sha the caller believes it is dispatching against.
    Rejected server-side if it does not match ``origin/<branch>`` head."""

    @field_validator("repo_url")
    @classmethod
    def validate_repo_url(cls, value: str) -> str:
        """Apply the single shared policy (``core/repo_url_policy``).

        This schema had NO validator at all and accepted ``ext::sh -c ...``,
        ``git://`` and an embedded ``--upload-pack=``.
        """
        return _validate_repo_url(value)

    @field_validator("branch")
    @classmethod
    def validate_branch(cls, value: str | None) -> str | None:
        """Apply the single shared branch policy (:func:`sanitize_branch_ref`).

        This schema had no branch validator either, so
        ``branch="--upload-pack=/bin/sh"`` was accepted here and refused by
        its sibling.
        """
        return sanitize_branch_ref(value)


class ExecutePlanResponse(BaseModel):
    """Response for an accepted execute-plan (async decomposition in the loop).

    The endpoint returns immediately with the status the plan ROW holds, which
    is ``pending``. It used to answer ``"decomposing"``, which asserted work
    that had not started (decomposition begins on a later orchestration tick,
    and only if the loop is running) and named a value outside
    ``CANONICAL_PLAN_STATUSES``, so a caller polling for it would never see it
    again from ``poll_plan``. The brain decomposition runs asynchronously;
    tasks appear on the plan shortly after.
    """

    plan_id: str
    project_id: str
    dashboard_url: str
    # The endpoint always sets this explicitly, so the default is unreachable.
    # It agrees with the endpoint anyway: a default that contradicts every
    # response is a claim waiting for the day someone constructs one of these
    # without passing a status.
    status: str = PlanStatus.PENDING.value


class GitStateResponse(BaseModel):
    """Origin HEAD state for a project's base branch (dashboard visualization)."""

    base: str
    sha: str | None = None
    short_sha: str | None = None
    subject: str | None = None
    committed_at: str | None = None
    available: bool = True
    detail: str | None = None


class RegisteredModel(BaseModel):
    """One entry in the model registry."""

    name: str
    provider: str
    model: str = ""
    effort: str | None = None


class RoleChains(BaseModel):
    """Ordered fallback chains keyed by role name."""

    chains: dict[str, list[str]]
