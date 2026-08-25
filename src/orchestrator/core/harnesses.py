"""Implementation-harness registry: behavior + presentation metadata.

Single source of truth for which Docker image runs a project's tasks and for
the user-facing "About" content (description, pros/cons, when-to-pick).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


#: How a harness receives a thinking-effort signal.
#:
#: ``request_option`` - praxis controls it directly in the request/provider
#:   config, so it MUST be stated explicitly (see :mod:`orchestrator.core.thinking`:
#:   an absent key hands the level to the server, whose default is not stable
#:   and has inverted twice on the configured endpoint).
#: ``model_name``     - the effort is encoded in the model string itself
#:   (e.g. "Gemini 3.5 Flash (High)"); there is no separate knob to set.
#: ``none``           - the harness exposes no effort control at all.
EFFORT_CHANNELS: frozenset[str] = frozenset({"request_option", "model_name", "none"})


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
    #: Whether this harness CAN be driven through an OpenAI-compatible endpoint
    #: at all. It is a "may", never an "is": OpenCode answers True and is just
    #: as happily pointed at a hosted provider as at a local LM Studio, so this
    #: flag can tell you a probe is pointless (agy speaks only to Google) but
    #: never that a probe will succeed. It governs whether an endpoint is
    #: REQUIRED (``api/doctor``, ``api/system``) and whether an LM Studio probe
    #: is worth attempting (``should_attempt_lm_studio_probe`` below).
    supports_local_llm: bool = True
    does_own_git: bool = True
    effort_channel: str = "none"
    reports_tokens: bool = False
    notes: tuple[str, ...] = field(default_factory=tuple)


REGISTRY: dict[str, HarnessSpec] = {
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
            "builds one oversized request, so it survives big repos that would "
            "otherwise silently truncate. Adds auto-compaction on top."
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
            "Compaction triggers off the model's known context length; for a "
            "custom local provider set `limit.context` so it fires correctly",
        ),
        maturity="active",
        recommended=True,
        does_own_git=False,
        effort_channel="request_option",
        reports_tokens=False,
        notes=(
            "Praxis's entrypoint runs `git add -A && git commit` after the "
            "agent because OpenCode edits files but does not commit.",
        ),
    ),
    "agy": HarnessSpec(
        id="agy",
        display_name="Antigravity (Gemini)",
        image="agy-agent:latest",
        description=(
            "Google's Antigravity (agy) CLI agent, powered by Gemini models. "
            "Edits files headless and uses portable OAuth credentials — no "
            "OS-keychain lock-in. Gemini-only: does not talk to LM Studio or "
            "any OpenAI-compatible endpoint."
        ),
        uniqueness=(
            "First-party Google agent using Gemini natively via OAuth; brings "
            "Gemini's large context window and code reasoning without requiring "
            "a separate API key beyond the user's existing Google account."
        ),
        when_to_pick=(
            "When the project is already on Google Cloud / Gemini and you want "
            "first-party model quality without proxying through LM Studio. "
            "Requires a one-time interactive `agy` login into a Docker "
            "credentials volume "
            "(GEMINI_CREDS_VOLUME); see docs/deployment.md."
        ),
        pros=(
            "First-party Gemini agent — best model quality for Gemini users",
            "Large context window (Gemini 3.x models)",
            "Login once into a Docker volume; setup is identical on every OS",
            "Non-interactive `--dangerously-skip-permissions --mode accept-edits`",
        ),
        cons=(
            "Gemini-only: cannot use local open-weight models via LM Studio",
            "No API-key auth: needs an interactive `agy` session to seed the volume",
            "Does not auto-commit — entrypoint stages and commits changes",
            "Newer / less battle-tested in headless CI than OpenCode",
        ),
        maturity="experimental",
        recommended=False,
        does_own_git=False,
        supports_local_llm=False,
        effort_channel="model_name",
        reports_tokens=True,
        notes=(
            "One-time setup: chown the praxis-gemini-creds volume to the agent "
            "user, then start an interactive `agy` session against it, which is "
            "what triggers the OAuth flow: there is no `agy login` "
            "subcommand. The orchestrator "
            "mounts that volume read-write at ~/.gemini in each container. See "
            "docs/deployment.md for the exact cross-platform commands.",
        ),
    ),
}


def default_harness_id() -> str:
    """The harness assigned to projects that don't specify one.

    OpenCode is the default because its agentic loop reads files in bounded
    chunks and auto-compacts, so it survives long-running / large tasks.
    """

    return "opencode"


def get_harness(harness_id: str) -> HarnessSpec:
    """Return the spec for ``harness_id`` or raise ``KeyError``."""

    return REGISTRY[harness_id]


def may_use_lm_studio(harness_id: str | None) -> bool:
    """Return True when this harness CAN be driven by an OpenAI-compatible endpoint.

    A capability, not a fact about any particular project. OpenCode answers
    True whether the operator pointed it at a local LM Studio or at a hosted
    provider; agy answers False because it speaks only to Google and no
    endpoint will ever change that.

    An UNREGISTERED id answers False: nothing is known about how it is served,
    and a probe on a guess can only produce an answer nobody can trust.
    """

    spec = REGISTRY.get(harness_id or default_harness_id())
    if spec is None:
        return False
    return spec.supports_local_llm


def should_attempt_lm_studio_probe(
    harness_id: str | None, endpoint_url: str | None
) -> bool:
    """Return True when asking LM Studio for a context window is worth a try.

    The single predicate shared by ``agent_manager.spawn_agent`` and
    ``core/context_window``, so the two cannot hold different wrong rules. It
    used to be a hardcoded ``harness_id != "agy"`` in one of them and nothing
    at all in the other.

    Read the name literally: WORTH A TRY. This is an optimization that avoids a
    round trip that cannot succeed, and it is NOT the correctness mechanism.
    Harness identity does not determine whether a model is locally served: an
    OpenCode project pointed at a hosted OpenAI-compatible provider answers
    True here, the probe hits an endpoint that is not LM Studio, and
    ``detect_context_limit`` returns None. What makes that safe is the rule
    downstream, not this predicate: a probe that does not come back with a
    number means UNKNOWN, and unknown skips the budget gate rather than
    substituting one. Any attempt to make this function decide correctness -
    by pattern-matching hostnames, say - would be a new guess in the exact
    place a guess caused the defect.

    Args:
        harness_id: The harness that will run the task, or None for the default.
        endpoint_url: The worker endpoint this project will actually be given.
            Empty or None means there is nothing to ask.

    Returns:
        True only when there is an endpoint AND the harness could be served
        through one.
    """

    if not endpoint_url:
        return False
    return may_use_lm_studio(harness_id)


def list_harnesses() -> list[dict[str, Any]]:
    """Return all specs as serializable dicts (recommended first)."""

    specs = sorted(REGISTRY.values(), key=lambda s: (not s.recommended, s.id))
    return [asdict(spec) for spec in specs]
