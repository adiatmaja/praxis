"""Pydantic schemas and enums for orchestrator API contracts."""

from __future__ import annotations

import re
from enum import StrEnum
from sys import version_info
from typing import TypedDict

from pydantic import BaseModel, ConfigDict, Field, field_validator

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


class PlanStatus(StrEnum):
    """Plan lifecycle status."""

    PENDING = "pending"
    ACTIVE = "active"
    COMPLETED = "completed"
    REJECTED = "rejected"


class OpusStatus(StrEnum):
    """Claude Opus availability status."""

    AVAILABLE = "available"
    RATE_LIMITED = "rate_limited"
    RESUMING = "resuming"


class AgentRunStatus(StrEnum):
    """Aider agent run status."""

    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    STOPPED = "stopped"


class ProjectCreate(BaseModel):
    """Request payload for creating a project."""

    name: str
    repo_url: str
    model_name: str
    default_branch: str = "main"
    approval_gate: bool = True
    confidence_threshold: float = Field(default=0.7, ge=0.0, le=1.0)
    max_retries: int = Field(default=3, ge=1, le=10)
    max_improvement_cycles: int = Field(default=5, ge=1, le=20)
    lm_studio_url: str = "http://host.docker.internal:1234"
    agent_model: str | None = None
    agent_model_effort: str | None = None
    harness: str = Field(default_factory=default_harness_id)

    @field_validator("harness")
    @classmethod
    def validate_harness(cls, value: str) -> str:
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
        "model_name",
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


class ProjectUpdate(BaseModel):
    """Request payload for updating a project."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    model_name: str | None = None
    approval_gate: bool | None = None
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
    confidence_threshold: float
    max_retries: int
    max_improvement_cycles: int
    lm_studio_url: str
    agent_model: str | None = None
    agent_model_effort: str | None = None
    harness: str = "aider"
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
    name: str | None = None
    plan_path: str | None = None
    plan_text: str | None = None

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
