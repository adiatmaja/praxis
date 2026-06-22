"""Provider-agnostic LLM call routing for orchestrator brain call-sites."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable


class UnknownProviderError(Exception):
    """Raised when a call-site config names a provider with no builder."""


# Default {provider, model, effort} per call-site (the model-tiering policy).
CALL_SITE_DEFAULTS: dict[str, dict[str, str | None]] = {
    "plan_spec": {"provider": "claude", "model": "claude-opus-4-8", "effort": "high"},
    "review_diff_first": {
        "provider": "claude",
        "model": "claude-sonnet-4-6",
        "effort": None,
    },
    "review_diff_rereview": {
        "provider": "claude",
        "model": "claude-haiku-4-5",
        "effort": None,
    },
    "analyze_improvements": {
        "provider": "claude",
        "model": "claude-opus-4-8",
        "effort": "high",
    },
    "classify_doc": {"provider": "claude", "model": "claude-haiku-4-5", "effort": None},
    "brainstorm_run_turn": {
        "provider": "claude",
        "model": "claude-sonnet-4-6",
        "effort": None,
    },
    "brainstorm_generate_plan": {
        "provider": "claude",
        "model": "claude-sonnet-4-6",
        "effort": None,
    },
    "context_sync": {"provider": "claude", "model": "claude-haiku-4-5", "effort": None},
    "derive_tasks": {"provider": "local", "model": "", "effort": None},
}


def build_argv(
    provider: str, model: str, effort: str | None, prompt: str = ""
) -> list[str]:
    """Build the subprocess argv for a CLI provider. 'local' is not a CLI.

    The prompt is fed to the process via stdin (see ``LLMRouter.run``), never
    embedded in argv: a large prompt (e.g. a full PR diff) overflows the OS
    command-line length limit (Windows raises WinError 206 above ~32K chars).
    The ``prompt`` parameter is retained for backward compatibility only.
    """
    if provider == "claude":
        argv = ["claude", "-p", "--output-format", "text"]
        if model:
            argv += ["--model", model]
        if effort:
            argv += ["--reasoning-effort", effort]
        return argv
    if provider == "agy":  # Gemini CLI — verify one-shot flag during impl
        argv = ["agy", "-p"]
        if model:
            argv += ["--model", model]
        return argv
    if provider == "codex":  # GPT CLI — verify one-shot flag during impl
        argv = ["codex", "exec"]
        if model:
            argv += ["--model", model]
        return argv
    message = f"Unknown or non-CLI provider: {provider}"
    raise UnknownProviderError(message)


Resolver = Callable[[str, str | None], Awaitable[dict]]


class LLMRouter:
    """Resolve a call-site to {provider, model, effort} and execute it."""

    def __init__(self, resolve: Resolver, lm_studio_url: str = "") -> None:
        self._resolve = resolve
        self._lm_studio_url = lm_studio_url

    async def run(self, call_site: str, prompt: str, project_id: str | None) -> str:
        cfg = await self._resolve(call_site, project_id)
        provider = cfg["provider"]
        if provider == "local":
            return await self._run_local(prompt, cfg.get("model") or "")
        argv = build_argv(provider, cfg.get("model") or "", cfg.get("effort"))
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate(input=prompt.encode())
        if proc.returncode:
            message = (
                f"{provider} failed (exit {proc.returncode}): {stderr.decode().strip()}"
            )
            raise RuntimeError(message)
        return stdout.decode().strip()

    async def _run_local(self, prompt: str, model: str) -> str:
        import httpx

        url = self._lm_studio_url.rstrip("/") + "/v1/chat/completions"
        body: dict[str, object] = {
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0,
        }
        if model:
            body["model"] = model
        async with httpx.AsyncClient(timeout=120) as http:
            resp = await http.post(url, json=body)
            resp.raise_for_status()
            return str(resp.json()["choices"][0]["message"]["content"]).strip()
