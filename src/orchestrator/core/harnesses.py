"""Implementation-harness registry: behavior + presentation metadata.

Single source of truth for which Docker image runs a project's tasks and for
the user-facing "About" content (description, pros/cons, when-to-pick).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class HarnessSpec:
    """A selectable implementation harness."""

    id: str
    display_name: str
    image: str
    description: str
    uniqueness: str
    when_to_pick: str
    pros: tuple[str, ...]
    cons: tuple[str, ...]
    maturity: str
    recommended: bool = False
    supports_local_llm: bool = True
    does_own_git: bool = True
    notes: tuple[str, ...] = field(default_factory=tuple)


REGISTRY: dict[str, HarnessSpec] = {
    "aider": HarnessSpec(
        id="aider",
        display_name="Aider",
        image="aider-agent:latest",
        description=(
            "A lean, terminal-first pair-programming agent. Edits multiple "
            "files via diffs and commits to git automatically. Praxis's "
            "original harness (OpenCode is now the default)."
        ),
        uniqueness=(
            "Native, first-class git integration: every change is an "
            "auto-commit, so the orchestrator's branch/PR flow needs no extra "
            "wiring. Smallest, fastest image."
        ),
        when_to_pick=(
            "Best for focused, well-scoped tasks that comfortably fit one "
            "context window, on small-to-medium repos, when you want minimal "
            "moving parts and fast runs. Pre-decompose larger work first."
        ),
        pros=(
            "Auto-commits — zero git glue required",
            "Mature and stable (in production since 2023)",
            "Lightweight image, fast cold start",
            "Excellent diff/edit quality on scoped tasks",
        ),
        cons=(
            "Single-shot --message: NO context compaction — each task must fit "
            "one context window",
            "On overflow it sends everything raw; if the model server (e.g. LM "
            "Studio) silently truncates, Aider reports SUCCESS on a partial "
            "view (verified) — no error, no flag",
            "Less autonomous on large multi-step tasks",
            "No built-in sandboxed code execution or browsing",
        ),
        maturity="stable",
        recommended=False,
    ),
    "opencode": HarnessSpec(
        id="opencode",
        display_name="OpenCode",
        image="opencode-agent:latest",
        description=(
            "A provider-agnostic, terminal-first coding agent (the most "
            "popular open-source Claude Code alternative). Runs headless via "
            "`opencode run` and works with any OpenAI-compatible endpoint."
        ),
        uniqueness=(
            "Truly model-agnostic with a polished agent loop and strong tool "
            "use; the de facto community default. Mixes/swaps providers freely."
        ),
        when_to_pick=(
            "Default choice. Best for long-running, large, or open-ended tasks: "
            "its agentic loop reads files in bounded, paginated chunks and never "
            "builds one oversized request, so it survives big repos where Aider "
            "would silently truncate. Adds auto-compaction on top."
        ),
        pros=(
            "Bounded, paginated tool reads — never dumps a whole file into one "
            "request, so it sidesteps silent context-truncation (verified)",
            "Auto-compacts conversation context (~95% capacity) for long "
            "multi-turn tasks",
            "Largest community / most active development",
            "Works with any OpenAI-compatible local model; headless `opencode "
            "run` suits automation",
        ),
        cons=(
            "Does not auto-commit — entrypoint must stage & commit changes",
            "Larger image than Aider",
            "Compaction triggers off the model's known context length; for a "
            "custom local provider set `limit.context` so it fires correctly",
        ),
        maturity="active",
        recommended=True,
        does_own_git=False,
        notes=(
            "Praxis's entrypoint runs `git add -A && git commit` after the "
            "agent because OpenCode edits files but does not commit.",
        ),
    ),
    "openhands": HarnessSpec(
        id="openhands",
        display_name="OpenHands",
        image="openhands-agent:latest",
        description=(
            "A fully autonomous software-engineering agent (formerly "
            "OpenDevin) that runs in a sandboxed runtime able to execute code, "
            "browse, and edit end-to-end. Strongest headless/CI story."
        ),
        uniqueness=(
            "Goes beyond editing: it can run the code it writes, inspect "
            "output, and iterate autonomously across many steps in a sandbox."
        ),
        when_to_pick=(
            "For large, open-ended, multi-step tasks where the agent benefits "
            "from running tests/commands and self-correcting. Pick when task "
            "complexity justifies the heavier runtime."
        ),
        pros=(
            "Most autonomous — executes & verifies its own changes",
            "Best for complex, exploratory, multi-file work",
            "Model-agnostic via LiteLLM (OpenAI-compatible local models work)",
            "Strong headless batch mode",
        ),
        cons=(
            "Heaviest image and slowest runs",
            "Needs a sandbox runtime (local runtime, or a mounted Docker "
            "socket for the container runtime)",
            "Higher token usage from multi-step loops",
            "Does not auto-commit — entrypoint must stage & commit changes",
        ),
        maturity="active",
        does_own_git=False,
        notes=(
            'Runs headless via `python -m openhands.core.main -t "$TASK" '
            "--override-with-envs`. Praxis uses the local runtime to avoid "
            "Docker-in-Docker; if unavailable, mount /var/run/docker.sock.",
        ),
    ),
}


def default_harness_id() -> str:
    """The harness assigned to projects that don't specify one.

    OpenCode is the default because its agentic loop reads files in bounded
    chunks and auto-compacts, so it survives long-running / large tasks. Aider
    is single-shot with no compaction and silently truncates on overflow.
    """

    return "opencode"


def get_harness(harness_id: str) -> HarnessSpec:
    """Return the spec for ``harness_id`` or raise ``KeyError``."""

    return REGISTRY[harness_id]


def list_harnesses() -> list[dict[str, Any]]:
    """Return all specs as serializable dicts (recommended first)."""

    specs = sorted(REGISTRY.values(), key=lambda s: (not s.recommended, s.id))
    return [asdict(spec) for spec in specs]
