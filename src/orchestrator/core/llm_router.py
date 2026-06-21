"""Provider-agnostic LLM call routing for orchestrator brain call-sites."""

from __future__ import annotations


class UnknownProviderError(Exception):
    """Raised when a call-site config names a provider with no builder."""


# Default {provider, model, effort} per call-site (the model-tiering policy).
CALL_SITE_DEFAULTS: dict[str, dict[str, str | None]] = {
    "plan_spec": {"provider": "claude", "model": "claude-opus-4-8", "effort": "high"},
    "review_diff_first": {"provider": "claude", "model": "claude-sonnet-4-6", "effort": None},
    "review_diff_rereview": {"provider": "claude", "model": "claude-haiku-4-5", "effort": None},
    "analyze_improvements": {"provider": "claude", "model": "claude-opus-4-8", "effort": "high"},
    "classify_doc": {"provider": "claude", "model": "claude-haiku-4-5", "effort": None},
    "brainstorm_run_turn": {"provider": "claude", "model": "claude-sonnet-4-6", "effort": None},
    "brainstorm_generate_plan": {"provider": "claude", "model": "claude-sonnet-4-6", "effort": None},
    "context_sync": {"provider": "claude", "model": "claude-haiku-4-5", "effort": None},
    "derive_tasks": {"provider": "local", "model": "", "effort": None},
}


def build_argv(provider: str, model: str, effort: str | None, prompt: str) -> list[str]:
    """Build the subprocess argv for a CLI provider. 'local' is not a CLI."""
    if provider == "claude":
        argv = ["claude", "-p", prompt, "--output-format", "text"]
        if model:
            argv += ["--model", model]
        if effort:
            argv += ["--reasoning-effort", effort]
        return argv
    if provider == "agy":  # Gemini CLI — verify one-shot flag during impl
        argv = ["agy", "-p", prompt]
        if model:
            argv += ["--model", model]
        return argv
    if provider == "codex":  # GPT CLI — verify one-shot flag during impl
        argv = ["codex", "exec", prompt]
        if model:
            argv += ["--model", model]
        return argv
    message = f"Unknown or non-CLI provider: {provider}"
    raise UnknownProviderError(message)
