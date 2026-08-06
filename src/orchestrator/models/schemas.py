"""Pydantic schemas and enums for orchestrator API contracts."""

from __future__ import annotations

import re
from enum import StrEnum
from sys import version_info
from typing import Literal, TypedDict

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from orchestrator.core.harnesses import REGISTRY, default_harness_id


if version_info < (3, 12):
    from typing_extensions import TypedDict


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
        """Reject dangerous git transports; allow only HTTPS or scp-style SSH.

        Allowed forms:
          - https://host/owner/repo[.git]
          - git@host:owner/repo[.git]

        Rejected (non-exhaustive):
          - ext::, file://, ssh://, git:// schemes
          - anything containing --upload-pack or --config (git option injection)
        """
        stripped = value.strip()
        dangerous_fragments = ("--upload-pack", "--config")
        for fragment in dangerous_fragments:
            if fragment in stripped:
                msg = f"repo_url must not contain '{fragment}'"
                raise ValueError(msg)
        # scp-style SSH: git@host:path
        if stripped.startswith("git@"):
            if ":" not in stripped.split("@", 1)[1]:
                msg = "invalid scp-style repo_url: expected git@host:path"
                raise ValueError(msg)
            return stripped
        # HTTPS only
        if stripped.startswith("https://"):
            rest = stripped[len("https://") :]
            if not rest or "/" not in rest:
                msg = "repo_url must be a valid https:// URL with a path"
                raise ValueError(msg)
            return stripped
        msg = (
            "repo_url must start with 'https://' or 'git@host:path'; "
            "other schemes (file://, ext::, ssh://, git://) are not allowed"
        )
        raise ValueError(msg)

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
    created_at: str


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
    spec_path: str | None = None
    plan_path: str | None = None
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


class DispatchRequest(BaseModel):
    """Request payload for MCP single-task dispatch."""

    repo_url: str
    instructions: str
    model: str
    harness: str | None = None
    branch: str | None = None
    """Base branch for the dispatched task. The worker always cuts a NEW
    agent/<slug> branch from this and opens a NEW PR; passing an existing PR's
    head here does NOT push follow-up commits onto that PR. Re-dispatching
    always creates a fresh PR. (Continue-on-PR mode is a planned follow-up.)"""
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

    @field_validator("repo_url")
    @classmethod
    def validate_repo_url(cls, value: str) -> str:
        """Reject anything that isn't an http(s) or SSH (git@) repo URL.

        A bad ``repo_url`` would otherwise create a project row that only fails
        later at container-spawn/clone time with an opaque git error.
        """
        candidate = value.strip()
        if not (
            candidate.startswith(("http://", "https://", "git@", "ssh://"))
            and len(candidate) > len("https://")
        ):
            msg = "repo_url must be an http(s) or git@/ssh:// repository URL"
            raise ValueError(msg)
        return candidate

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
        """Sanitize a caller-supplied branch into a safe git ref.

        Blocks path traversal, leading dashes, whitespace, and the dangerous
        ``..``/``//`` ref sequences so a hostile branch can't escape the
        ``agent/``-style namespace or confuse git ref parsing.
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


class ExecutePlanResponse(BaseModel):
    """Response for an accepted execute-plan (async decomposition in the loop).

    The endpoint returns immediately with ``status="decomposing"``. The brain
    decomposition runs asynchronously in the orchestration loop; tasks appear
    on the plan shortly after.
    """

    plan_id: str
    project_id: str
    dashboard_url: str
    status: str = "decomposing"


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
