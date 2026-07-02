"""Brain-call bridge (historically Opus-only, now provider-agnostic).

The name is legacy: since the Spec-3 LLM router, this module routes
plan/review/improve calls to whichever provider the call-site resolves to
(claude / codex / local). It is kept as ``opus_bridge`` to avoid churning
tests, docs, and operator muscle memory; treat "opus" here as "the brain".

Also handles Claude CLI rate-limit state management.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, cast

from orchestrator.database import Database
from orchestrator.models.schemas import OpusStatus


if TYPE_CHECKING:
    from orchestrator.core.effective_settings import EffectiveSettings
    from orchestrator.core.llm_router import LLMRouter


logger = logging.getLogger(__name__)

PLAN_PROMPT_TEMPLATE = """You are an AI project planner. Given a specification, break it into implementation tasks.

Repository: {repo_url}

Specification:
{spec}

Respond with ONLY valid JSON in this exact format:
{{
  "plan_summary": "one-line description",
  "plan_slug": "url-safe-slug",
  "tasks": [
    {{
      "title": "task name",
      "slug": "url-safe-task-slug",
      "description": "detailed implementation instructions for a coding agent",
      "depends_on": ["slug-of-dependency"]
    }}
  ]
}}

Rules:
- Each task should be independently implementable on its own git branch
- Use depends_on only when a task MUST read files created by another task
- Keep tasks focused - one feature or component per task
- Description should be detailed enough for an AI coding agent to implement without questions
"""

REVIEW_PROMPT_TEMPLATE = """You are a senior code reviewer. Review this PR diff for a task.

Task description: {task_description}

Plan / spec the change must satisfy:
{plan_text}

A clean checkout of the PR head is your current working directory; you may
inspect files with your tools to verify the diff in context. If git is
unavailable, review from the diff text alone - do NOT pass solely because you
could not verify.

Diff:
{diff}

Respond with ONLY valid JSON in this exact format:
{{
  "verdict": "pass" or "fail",
  "feedback": "summary of your review",
  "issues": ["list of specific issues if verdict is fail"]
}}

Pass if the code correctly implements the task and has no critical issues.
Fail if there are bugs, missing functionality, security problems, or it deletes
existing functionality/config the task did not ask to remove.
"""

IMPROVEMENT_PROMPT_TEMPLATE = """You are a senior software architect. Analyze this project for improvements.

Project summary:
{project_summary}

Respond with ONLY valid JSON in this exact format:
{{
  "confidence": 0.0 to 1.0,
  "reason": "why these improvements are worth doing",
  "proposed_tasks": [
    {{
      "title": "improvement name",
      "slug": "url-safe-slug",
      "description": "detailed implementation instructions"
    }}
  ]
}}

Rules:
- Set confidence to 0.0 if no meaningful improvements are needed
- Only propose improvements that materially improve quality, security, or functionality
- Do not propose cosmetic or stylistic changes
"""


class OpusBridge:
    """Interface to Claude Opus via the `claude -p` CLI."""

    def __init__(
        self,
        db: Database,
        default_model: str | None = None,
        default_effort: str | None = None,
        effective_settings: EffectiveSettings | None = None,
        router: LLMRouter | None = None,
    ) -> None:
        self._db = db
        self._default_model = default_model
        self._default_effort = default_effort
        self._effective_settings = effective_settings
        self._router = router

    async def _resolve_model(self, override: str | None) -> str | None:
        """Return effective model: explicit override > effective_settings > default."""
        if override:
            return override
        if self._effective_settings is not None:
            return await self._effective_settings.agent_model()
        return self._default_model

    async def _resolve_effort(self, override: str | None) -> str | None:
        """Return effective effort: explicit override > effective_settings > default."""
        if override:
            return override
        if self._effective_settings is not None:
            return await self._effective_settings.agent_model_effort()
        return self._default_effort

    async def _run_claude_raw(
        self,
        prompt: str,
        model: str | None = None,
        effort: str | None = None,
        cwd: str | None = None,
    ) -> tuple[int, str, str]:
        resolved_model = await self._resolve_model(model)
        resolved_effort = await self._resolve_effort(effort)
        # Pass the prompt via stdin, not as an argv element: a large prompt
        # (e.g. a full PR diff) overflows the OS command-line length limit
        # (Windows raises WinError 206 above ~32K chars).
        args = ["claude", "-p", "--output-format", "text"]
        if resolved_model:
            args += ["--model", resolved_model]
        if resolved_effort:
            args += ["--effort", resolved_effort]
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
        )
        stdout, stderr = await proc.communicate(input=prompt.encode())
        return (
            proc.returncode or 0,
            stdout.decode().strip(),
            stderr.decode().strip(),
        )

    async def _check_and_handle_rate_limit(
        self,
        code: int,
        stdout: str,
        stderr: str,
    ) -> bool:
        combined = f"{stdout} {stderr}".lower()
        rate_limited = any(
            pattern in combined
            for pattern in ("rate limit", "usage limit", "too many requests")
        ) or (code != 0 and "limit" in combined)
        if not rate_limited:
            return False

        now = datetime.now(UTC)
        resume_at = now + timedelta(hours=5, minutes=1)
        await self._db.execute(
            """UPDATE opus_state
               SET status = ?, rate_limited_at = ?, resume_at = ?
               WHERE id = 1""",
            (OpusStatus.RATE_LIMITED, now.isoformat(), resume_at.isoformat()),
        )
        logger.warning("Opus rate limited. Will resume at %s", resume_at.isoformat())
        return True

    async def _run_claude(
        self,
        prompt: str,
        model: str | None = None,
        effort: str | None = None,
        cwd: str | None = None,
    ) -> str:
        code, stdout, stderr = await self._run_claude_raw(prompt, model, effort, cwd)
        if await self._check_and_handle_rate_limit(code, stdout, stderr):
            message = "Opus rate limited"
            raise RuntimeError(message)
        if code != 0:
            message = f"claude -p failed (exit {code}): {stderr}"
            raise RuntimeError(message)
        return stdout

    def _extract_json(self, raw: str) -> dict[str, Any]:
        code_block = re.search(r"```(?:json)?\s*\n(.*?)\n```", raw, re.DOTALL)
        if code_block is not None:
            return cast(dict[str, Any], json.loads(code_block.group(1)))

        json_object = re.search(r"\{.*\}", raw, re.DOTALL)
        if json_object is not None:
            return cast(dict[str, Any], json.loads(json_object.group(0)))

        message = f"Could not extract JSON from response: {raw[:200]}"
        raise ValueError(message)

    async def plan_spec(
        self,
        spec: str,
        repo_url: str,
        model: str | None = None,
        effort: str | None = None,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        prompt = PLAN_PROMPT_TEMPLATE.format(spec=spec, repo_url=repo_url)
        router: LLMRouter | None = getattr(self, "_router", None)
        if router is not None:
            raw = await router.run("plan_spec", prompt, project_id)
        else:
            raw = await self._run_claude(prompt, model, effort)
        return self._extract_json(raw)

    async def review_diff(
        self,
        diff: str,
        task_description: str,
        model: str | None = None,
        effort: str | None = None,
        project_id: str | None = None,
        tier: str = "first",
        plan_text: str | None = None,
        cwd: str | None = None,
    ) -> dict[str, Any]:
        prompt = REVIEW_PROMPT_TEMPLATE.format(
            diff=diff,
            task_description=task_description,
            plan_text=(plan_text or "(no plan text was provided)"),
        )
        router: LLMRouter | None = getattr(self, "_router", None)
        if router is not None:
            call_site = (
                "review_diff_rereview" if tier == "rereview" else "review_diff_first"
            )
            raw = await router.run(call_site, prompt, project_id, cwd=cwd)
        else:
            raw = await self._run_claude(prompt, model, effort, cwd=cwd)
        return self._extract_json(raw)

    async def analyze_improvements(
        self,
        project_summary: str,
        model: str | None = None,
        effort: str | None = None,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        prompt = IMPROVEMENT_PROMPT_TEMPLATE.format(project_summary=project_summary)
        router: LLMRouter | None = getattr(self, "_router", None)
        if router is not None:
            raw = await router.run("analyze_improvements", prompt, project_id)
        else:
            raw = await self._run_claude(prompt, model, effort)
        return self._extract_json(raw)

    async def get_opus_state(self) -> dict[str, Any]:
        state = await self._db.fetch_one("SELECT * FROM opus_state WHERE id = 1")
        if state is None:
            message = "Opus state row is missing"
            raise RuntimeError(message)
        return state

    async def is_available(self) -> bool:
        state = await self.get_opus_state()
        if state["status"] == OpusStatus.AVAILABLE:
            return True
        if state["status"] == OpusStatus.RATE_LIMITED and state["resume_at"]:
            resume_at = datetime.fromisoformat(state["resume_at"])
            if datetime.now(UTC) >= resume_at:
                await self._db.execute(
                    "UPDATE opus_state SET status = ? WHERE id = 1",
                    (OpusStatus.AVAILABLE,),
                )
                logger.info("Opus rate limit expired, now available")
                return True
        return False

    async def queue_action(self, action: dict[str, Any]) -> None:
        state = await self.get_opus_state()
        queued = json.loads(state["queued_actions"])
        queued.append(action)
        await self._db.execute(
            "UPDATE opus_state SET queued_actions = ? WHERE id = 1",
            (json.dumps(queued),),
        )

    async def get_queued_actions(self) -> list[dict[str, Any]]:
        state = await self.get_opus_state()
        return cast(list[dict[str, Any]], json.loads(state["queued_actions"]))

    async def clear_queued_actions(self) -> None:
        await self._db.execute(
            "UPDATE opus_state SET queued_actions = '[]' WHERE id = 1"
        )

    CLASSIFY_PROMPT = (
        "Classify this markdown document as exactly one word: 'spec', 'plan', or 'other'. "
        "A spec describes WHAT to build; a plan is a step-by-step implementation checklist. "
        "Reply with only the single word.\n\n---\n{text}"
    )

    async def classify_doc(self, text: str, project_id: str | None = None) -> str:
        """Classify ambiguous markdown via Haiku; returns spec|plan|other."""
        prompt = self.CLASSIFY_PROMPT.format(text=text[:4000])
        router: LLMRouter | None = getattr(self, "_router", None)
        if router is not None:
            raw = (await router.run("classify_doc", prompt, project_id)).strip().lower()
        else:
            raw = (
                (await self._run_claude(prompt, model="claude-haiku-4-5"))
                .strip()
                .lower()
            )
        # 1. Exact match after stripping surrounding punctuation/whitespace.
        cleaned = raw.strip(" '\".:!\n")
        if cleaned in ("spec", "plan", "other"):
            return cleaned
        # 2. Word-boundary scan; if both appear, prefer the last-mentioned
        #    (the model's conclusion tends to come last).
        last_pos: dict[str, int] = {}
        for category in ("spec", "plan"):
            matches = list(re.finditer(rf"\b{category}\b", raw))
            if matches:
                last_pos[category] = matches[-1].start()
        if last_pos:
            return max(last_pos, key=lambda c: last_pos[c])
        return "other"
