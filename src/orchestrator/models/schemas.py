"""Pydantic schemas and enums for orchestrator API contracts."""

from __future__ import annotations

from enum import StrEnum
from pydantic import BaseModel, Field, field_validator
from typing_extensions import TypedDict


class TaskStatus(StrEnum):
    """Task lifecycle status."""

    pending = "pending"
    in_progress = "in_progress"
    reviewing = "reviewing"
    passed = "passed"
    failed = "failed"
    merged = "merged"


class PlanStatus(StrEnum):
    """Plan lifecycle status."""

    pending = "pending"
    active = "active"
    completed = "completed"
    rejected = "rejected"


class OpusStatus(StrEnum):
    """Claude Opus availability status."""

    available = "available"
    rate_limited = "rate_limited"
    resuming = "resuming"


class AgentRunStatus(StrEnum):
    """Aider agent run status."""

    running = "running"
    completed = "completed"
    failed = "failed"
    stopped = "stopped"


class ProjectCreate(BaseModel):
    """Request payload for creating a project."""

    name: str
    repo_url: str
    model_name: str
    default_branch: str = "main"
    approval_gate: bool = True
    confidence_threshold: float = Field(default=0.7, ge=0.0, le=1.0)
    max_retries: int = 3
    max_improvement_cycles: int = 5
    lm_studio_url: str = "http://host.docker.internal:1234"


class ProjectUpdate(BaseModel):
    """Request payload for updating a project."""

    name: str | None = None
    repo_url: str | None = None
    model_name: str | None = None
    default_branch: str | None = None
    approval_gate: bool | None = None
    confidence_threshold: float | None = Field(default=None, ge=0.0, le=1.0)
    max_retries: int | None = None
    max_improvement_cycles: int | None = None
    lm_studio_url: str | None = None


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

    id: int
    user_id: int
    name: str
    repo_url: str
    model_name: str
    default_branch: str
    approval_gate: bool
    confidence_threshold: float
    max_retries: int
    max_improvement_cycles: int
    lm_studio_url: str
    created_at: str


class PlanResponse(BaseModel):
    """Plan response payload."""

    id: int
    project_id: int
    spec: str
    opus_plan: str | None = None
    plan_branch_name: str | None = None
    source: str
    confidence: float | None = None
    confidence_reason: str | None = None
    status: PlanStatus
    created_at: str


class TaskResponse(BaseModel):
    """Task response payload."""

    id: int
    plan_id: int
    title: str
    description: str
    branch_name: str | None = None
    pr_url: str | None = None
    status: TaskStatus
    attempt: int
    review_feedback: str | None = None
    created_at: str
    updated_at: str


class AgentRunResponse(BaseModel):
    """Agent run response payload."""

    id: int
    task_id: int
    container_id: str | None = None
    status: AgentRunStatus
    logs: str
    started_at: str
    finished_at: str | None = None


class OpusStateResponse(BaseModel):
    """Current Opus state response payload."""

    id: int
    status: OpusStatus
    rate_limited_at: str | None = None
    resume_at: str | None = None
    queued_actions: str


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
