"""Provider-agnostic LLM call routing for orchestrator brain call-sites."""

from __future__ import annotations

import asyncio
import shutil
from collections.abc import Awaitable, Callable


class UnknownProviderError(Exception):
    """Raised when a call-site config names a provider with no builder."""


class ProviderAuthError(Exception):
    """Raised when a provider CLI is installed but not (validly) authenticated.

    Distinct from a transient failure so the orchestration loop can pause and
    surface a login instruction instead of retrying the task to exhaustion.
    """

    def __init__(self, provider: str, login_hint: str) -> None:
        self.provider = provider
        self.login_hint = login_hint
        super().__init__(f"{provider} is not authenticated. Run: {login_hint}")


class ProviderOutputError(RuntimeError):
    """Raised when a provider exits cleanly but yields no usable output."""


# Per-provider command the human must run to (re)authenticate. Surfaced in
# ProviderAuthError and the dashboard so login stays an explicit manual step.
LOGIN_HINTS: dict[str, str] = {
    "claude": "claude login",
    "codex": "codex login",
    "agy": "agy (run interactively to sign in)",
}

# Substrings that mean "the CLI ran but the session is dead". Critical for
# `codex`, which exits 0 while printing a 401 to stderr — returncode alone
# would let a failed-auth call pass through as success.
_AUTH_FAIL_SIGNATURES: tuple[str, ...] = (
    "refresh token was revoked",
    "please log out and sign in",
    "401 unauthorized",
    "not authenticated",
    "please sign in",
    "no credentials",
)

# agy takes the prompt as a positional argv value (not stdin); guard against
# the OS command-line length limit (Windows WinError 206 above ~32K chars).
_AGY_ARGV_LIMIT = 30000


def _looks_like_auth_failure(stderr: str) -> bool:
    low = stderr.lower()
    return any(sig in low for sig in _AUTH_FAIL_SIGNATURES)


# Default {provider, model, effort} per call-site (the model-tiering policy).
CALL_SITE_DEFAULTS: dict[str, dict[str, str | None]] = {
    # Planning from a spec is a structured spec->task-graph transformation, not
    # open-ended authoring (Praxis always starts from an existing spec/plan). A
    # mid-tier model suffices; Opus is reserved for analyze_improvements, the one
    # seat that reasons open-ended (decide what to build next with no artifact).
    "plan_spec": {"provider": "claude", "model": "claude-sonnet-4-6", "effort": None},
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
    # Decomposition is a structured transformation of an already-authored plan
    # into a constraint-valid DAG, not open-ended deep reasoning; Sonnet is
    # sufficient and far cheaper/faster. Opus is reserved for the judgment-heavy
    # seats (plan_spec authoring, analyze_improvements). See capability-engine
    # framing: use the highest-cost model only where capability truly demands it.
    "plan_review": {
        "provider": "claude",
        "model": "claude-sonnet-4-6",
        "effort": None,
    },
    "answer_clarification": {
        "provider": "claude",
        "model": "claude-sonnet-4-6",
        "effort": "medium",
    },
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
            argv += ["--effort", effort]
        return argv
    if provider == "agy":
        # Gemini CLI: `--print` takes the prompt as its argument value, NOT
        # stdin (piping yields "flag needs an argument: -p"). The prompt is
        # therefore embedded in argv here; LLMRouter.run guards its length.
        argv = ["agy", "--print", prompt]
        if model:
            argv += ["--model", model]
        return argv
    if provider == "codex":
        # GPT CLI: `codex exec` reads the prompt from stdin when no positional
        # is given (LLMRouter.run pipes it), matching the claude path.
        argv = ["codex", "exec"]
        if model:
            argv += ["--model", model]
        return argv
    message = f"Unknown or non-CLI provider: {provider}"
    raise UnknownProviderError(message)


Resolver = Callable[[str, str | None], Awaitable[list[dict]]]


class LLMRouter:
    """Resolve a call-site to an ordered chain and execute with fallback."""

    def __init__(
        self,
        resolve_chain: Resolver,
        lm_studio_url: str = "",
        event_bus: object | None = None,
    ) -> None:
        self._resolve_chain = resolve_chain
        self._lm_studio_url = lm_studio_url
        self._event_bus = event_bus

    async def run(
        self,
        call_site: str,
        prompt: str,
        project_id: str | None,
        cwd: str | None = None,
    ) -> str:
        from orchestrator.core.provider_errors import is_unavailability

        chain = await self._resolve_chain(call_site, project_id)
        last_exc: BaseException | None = None
        for index, cfg in enumerate(chain):
            try:
                return await self._execute_one(cfg, prompt, cwd)
            except Exception as exc:  # noqa: BLE001 - re-raised below
                if not is_unavailability(exc) or index == len(chain) - 1:
                    raise
                last_exc = exc
                nxt = chain[index + 1]
                if self._event_bus is not None and hasattr(self._event_bus, "publish"):
                    self._event_bus.publish(
                        {
                            "type": "model_fallback",
                            "call_site": call_site,
                            "from_model": cfg.get("model") or cfg.get("provider"),
                            "to_model": nxt.get("model") or nxt.get("provider"),
                            "reason": type(exc).__name__,
                        }
                    )
        if last_exc is not None:
            raise last_exc
        message = f"no models configured for call-site {call_site}"
        raise ProviderOutputError(message)

    async def _execute_one(self, cfg: dict, prompt: str, cwd: str | None) -> str:
        provider = cfg["provider"]
        if provider == "local":
            return await self._run_local(prompt, cfg.get("model") or "")
        # agy receives the prompt as a positional argv value; every other CLI
        # provider receives it on stdin (avoids the argv length limit).
        prompt_in_argv = provider == "agy"
        if prompt_in_argv and len(prompt) > _AGY_ARGV_LIMIT:
            message = (
                f"prompt too large for agy argv ({len(prompt)} chars); agy cannot "
                "accept large prompts on this provider"
            )
            raise ProviderOutputError(message)
        argv = build_argv(provider, cfg.get("model") or "", cfg.get("effort"), prompt)
        # Resolve argv[0] to its full path: on Windows these CLIs are .CMD/.EXE
        # npm/installer shims that create_subprocess_exec cannot find by bare
        # name (raises WinError 2). shutil.which honors PATHEXT.
        resolved = shutil.which(argv[0])
        if resolved is None:
            raise ProviderAuthError(
                provider, LOGIN_HINTS.get(provider, f"install {provider}")
            )
        argv[0] = resolved
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
        )
        stdin_input = None if prompt_in_argv else prompt.encode()
        stdout, stderr = await proc.communicate(input=stdin_input)
        err = stderr.decode()
        # Auth check first: codex exits 0 while printing a 401 to stderr, so a
        # returncode-only check would silently accept a failed-auth response.
        if _looks_like_auth_failure(err):
            raise ProviderAuthError(
                provider, LOGIN_HINTS.get(provider, f"re-authenticate {provider}")
            )
        if proc.returncode:
            message = f"{provider} failed (exit {proc.returncode}): {err.strip()}"
            raise RuntimeError(message)
        out = stdout.decode().strip()
        if not out:
            # agy's --print renders only to an interactive TTY and yields no
            # capturable stdout when run non-interactively (exit 0, empty out).
            message = f"{provider} returned empty output (exit 0)." + (
                " agy --print writes only to an interactive terminal and "
                "cannot be captured non-interactively."
                if provider == "agy"
                else ""
            )
            raise ProviderOutputError(message)
        return out

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
