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

from orchestrator.core.llm_router import ProviderRateLimitError
from orchestrator.database import Database
from orchestrator.models.schemas import OpusStatus


if TYPE_CHECKING:
    from orchestrator.core.effective_settings import EffectiveSettings
    from orchestrator.core.llm_router import LLMRouter


logger = logging.getLogger(__name__)

#: How much of a rejected response an operator-facing message may quote. Long
#: enough to carry a refusal, a question or a permission request whole; short
#: enough that a rambling answer does not become the whole ``plans.error``.
_EXCERPT_LIMIT = 500


class BrainResponseError(ValueError):
    """The brain answered, but not with the JSON the call-site asked for.

    Subclasses ``ValueError`` deliberately: ``_extract_json`` has always raised
    ``ValueError`` (its own, plus ``json.JSONDecodeError``, which is one too),
    and several callers guard with a bare ``except ValueError``. Widening the
    type would have silently un-caught them.

    The two subclasses split that single failure by whether a retry can change
    the answer. Nothing about them inspects the ENGLISH of the response: the
    rule is "was there any JSON in it at all", which is a property of the
    format the prompt demanded and so cannot rot the way a keyword list does.

    Attributes:
        raw: The full response, kept whole for logs and diagnosis.
    """

    def __init__(self, message: str, raw: str) -> None:
        """Record the failure and the response that caused it.

        Args:
            message: What went wrong, for the exception's own ``str()``.
            raw: The complete raw response from the provider.
        """
        super().__init__(message)
        self.raw = raw

    @property
    def excerpt(self) -> str:
        """The first ``_EXCERPT_LIMIT`` characters of the raw response."""
        return self.raw[:_EXCERPT_LIMIT]


class BrainProseResponseError(BrainResponseError):
    """The response contained no JSON at all. PERMANENT.

    Every prompt on this class says "Respond with ONLY valid JSON". A response
    carrying neither a fenced block nor a ``{...}`` span is therefore not a
    formatting slip: it is a refusal, a question, or a permission request. None
    of those become JSON because the same prompt was sent again, so a caller
    that retries one is a caller that retries forever.
    """


class BrainMalformedJsonError(BrainResponseError):
    """A JSON span was found and the parser rejected it. TRANSIENT.

    The model was answering in the requested shape and truncated, over-quoted,
    or trailed a comma. Sampling alone can fix that, so this is worth a retry.
    """


#: Substrings that identify a subscription rate limit in a CLI's own output.
#: Read only by ``is_rate_limited`` below, which is what consumers must call:
#: the strings alone are half the rule, and a consumer that reimplements the
#: other half around them drifts silently.
RATE_LIMIT_SIGNATURES: tuple[str, ...] = (
    "rate limit",
    "usage limit",
    "too many requests",
)


def is_rate_limited(code: int, stdout: str, stderr: str) -> bool:
    """Whether a CLI's own output says the subscription is throttled.

    THE single predicate, deliberately not just the strings above.  Every
    consumer has to reach the same verdict, because a rate limit is a normal,
    self-healing state (``opus_state`` queues the brain call and resumes) while
    everything else on a non-zero exit is a real fault.  ``api/system.py``'s
    doctor round-trip once matched the signatures alone and disagreed with this
    module on three of four real Claude wordings, reporting a throttled
    subscription as a planner blocked by a hook and failing ``praxis init``,
    which exits with doctor's status code.

    Args:
        code: The CLI's exit code.  Required, not optional: the second clause
            below cannot be evaluated without it, and a caller that cannot pass
            it is a caller that will diverge.
        stdout: What the CLI printed to stdout.
        stderr: What the CLI printed to stderr.

    Returns:
        True when the output names a known limit signature, or when the CLI
        FAILED and said "limit" at all.  The second clause is the broad one on
        purpose: the wordings vary ("5-hour limit", "weekly limit exceeded",
        "quota limit"), and mistaking a throttle for a fault costs an operator
        a false red, while mistaking a fault for a throttle only delays it to
        the next tick.  It is gated on a non-zero exit so a successful answer
        that merely discusses limits is never read as one.
    """
    combined = f"{stdout} {stderr}".lower()
    return any(pattern in combined for pattern in RATE_LIMIT_SIGNATURES) or (
        code != 0 and "limit" in combined
    )


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

Blast radius - how far the things this diff changes reach:
{blast_radius}

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

#: What stands in for the blast-radius section when nothing was measured.
#:
#: A neutral LINE, never an empty heading and never silence. The measurement is
#: skipped whenever the PR head could not be checked out and dropped whenever
#: the walk raised, and in both cases a blank section reads as "we looked and
#: the change is contained" -- which is the exact misleading green the section
#: was added to remove.
NO_BLAST_RADIUS_LINE = (
    "Not measured for this review (no checkout was available, or the "
    "measurement failed). Do not read that as evidence the change is contained."
)

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

CLARIFICATION_PROMPT_TEMPLATE = """\
A local coding model was implementing a task and stopped to ask a question.
Answer it ONLY if the answer is determinable from the task and plan context
below. If the answer requires information not present here (a human decision,
a missing credential, an undocumented business rule), do NOT guess.

Task description:
{task_description}

Plan context:
{plan_text}

The worker's question:
{question}

Respond with a single JSON object and nothing else:
{{"resolved": <true|false>, "answer": "<the answer, or why you cannot answer>", "confidence": <0.0-1.0>}}
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

    async def _park_rate_limited(self, provider: str = "claude") -> None:
        """Write the throttle to ``opus_state``.

        THE only writer of the ``available -> rate_limited`` TRANSITION, which
        is the narrow claim and the accurate one: ``is_available`` writes
        ``status`` in the other direction when the limit expires, and
        ``queue_action`` writes ``queued_actions``. Nothing else moves the row
        INTO the parked state.

        Both routes to a rate limit end here: the legacy ``_run_claude`` path
        via :meth:`_check_and_handle_rate_limit`, and the router path via
        :meth:`_run_routed`. Keeping the write in one body is the point of the
        split -- the defect this fixes was two paths disagreeing about whether
        the state got written at all, and a third path that also wrote would
        recreate it in a new shape.

        The state is a SINGLE global gate on every brain call, so a throttle
        reported by any subscription CLI parks all of them. That is broader
        than ideal (a throttled ``codex`` parks a healthy ``claude``) and is a
        deliberate limit: per-provider state needs a schema change, and the
        reference configuration points every routed seat at one provider
        anyway, so in practice one throttle really does mean all of them.

        Args:
            provider: Which provider reported the throttle, for the log line.
                Not stored: the row has no column for it.
        """
        now = datetime.now(UTC)
        resume_at = now + timedelta(hours=5, minutes=1)
        await self._db.execute(
            """UPDATE opus_state
               SET status = ?, rate_limited_at = ?, resume_at = ?
               WHERE id = 1""",
            (OpusStatus.RATE_LIMITED, now.isoformat(), resume_at.isoformat()),
        )
        logger.warning(
            "Brain provider %s is rate limited. Queuing brain calls until %s",
            provider,
            resume_at.isoformat(),
        )

    async def _check_and_handle_rate_limit(
        self,
        code: int,
        stdout: str,
        stderr: str,
    ) -> bool:
        """Detect a throttle in a legacy CLI result and park the state if so.

        Args:
            code: The CLI's exit code.
            stdout: What the CLI printed to stdout.
            stderr: What the CLI printed to stderr.

        Returns:
            True when the output named a throttle (and the state was parked).
        """
        if not is_rate_limited(code, stdout, stderr):
            return False
        await self._park_rate_limited()
        return True

    async def _run_routed(
        self,
        call_site: str,
        prompt: str,
        project_id: str | None,
        cwd: str | None = None,
    ) -> str:
        """Run a brain call through the router, parking the state on a throttle.

        The router is the path a stock install takes: ``main.py`` always wires
        it, so every routed call-site here reached a provider without ever
        touching ``opus_state``. The queue-and-resume branch at the top of
        ``plan_and_activate`` reads that row and so could never fire, which
        made a five-hour subscription wait indistinguishable from a broken
        planner.

        Only :class:`~orchestrator.core.llm_router.ProviderRateLimitError`
        parks. That type is raised solely from the router's CLI arm, so a
        ``local`` (LM Studio) endpoint being unreachable or returning 429
        cannot park the global gate: an endpoint outage ends when somebody
        restarts it, not on the five-hour clock this state models. Auth
        failures and empty answers do not park either -- both need a human, and
        waiting them out never ends them.

        Args:
            call_site: The routed call-site name.
            prompt: The full prompt text.
            project_id: Project whose per-call-site overrides apply.
            cwd: Working directory for the provider, when it takes one.

        Returns:
            The provider's raw response.

        Raises:
            Exception: Whatever the router raised, re-raised unchanged. The
                caller's classification is unaffected; only the state row is a
                side effect.
        """
        router = self._router
        if router is None:  # pragma: no cover - callers check first
            message = "no LLM router is configured"
            raise RuntimeError(message)
        try:
            return await router.run(call_site, prompt, project_id, cwd=cwd)
        except ProviderRateLimitError as exc:
            await self._park_rate_limited(exc.provider)
            raise

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
        """Parse the brain's answer, classifying failure by whether JSON exists.

        Args:
            raw: The provider's complete response.

        Returns:
            The decoded JSON object.

        Raises:
            BrainMalformedJsonError: A JSON span was present and unparseable.
            BrainProseResponseError: No JSON span was present at all.
        """
        code_block = re.search(r"```(?:json)?\s*\n(.*?)\n```", raw, re.DOTALL)
        if code_block is not None:
            try:
                return cast(dict[str, Any], json.loads(code_block.group(1)))
            except json.JSONDecodeError as exc:
                message = f"Fenced JSON block did not parse: {exc}"
                raise BrainMalformedJsonError(message, raw) from exc

        json_object = re.search(r"\{.*\}", raw, re.DOTALL)
        if json_object is not None:
            try:
                return cast(dict[str, Any], json.loads(json_object.group(0)))
            except json.JSONDecodeError as exc:
                message = f"JSON span did not parse: {exc}"
                raise BrainMalformedJsonError(message, raw) from exc

        message = f"Could not extract JSON from response: {raw[:200]}"
        raise BrainProseResponseError(message, raw)

    async def plan_spec(
        self,
        spec: str,
        repo_url: str,
        model: str | None = None,
        effort: str | None = None,
        project_id: str | None = None,
        cwd: str | None = None,
    ) -> dict[str, Any]:
        """Decompose a specification into a task graph.

        Args:
            spec: The specification text.
            repo_url: The repository the work targets, named in the prompt.
            model: Explicit model override for the legacy CLI path.
            effort: Explicit effort override for the legacy CLI path.
            project_id: Project whose per-call-site overrides apply.
            cwd: A readable checkout of ``repo_url`` to run the provider in.
                Without one the model is asked to reason about a path it cannot
                open, which is how a planner comes to answer in prose.

        Returns:
            The decoded plan graph.
        """
        prompt = PLAN_PROMPT_TEMPLATE.format(spec=spec, repo_url=repo_url)
        router: LLMRouter | None = getattr(self, "_router", None)
        if router is not None:
            raw = await self._run_routed("plan_spec", prompt, project_id, cwd=cwd)
        else:
            raw = await self._run_claude(prompt, model, effort, cwd=cwd)
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
        blast_radius: str | None = None,
    ) -> dict[str, Any]:
        """Review a diff against the task and plan it must satisfy.

        Args:
            diff: The change under review.
            task_description: What the task asked for.
            model: Explicit model override for the legacy CLI path.
            effort: Explicit effort override for the legacy CLI path.
            project_id: Project whose per-call-site overrides apply.
            tier: ``"first"`` or ``"rereview"``, selecting the call site.
            plan_text: The plan contract the change must satisfy.
            cwd: A clean checkout of the PR head to run the provider in.
            blast_radius: A rendered
                :func:`orchestrator.core.blast_radius.render_blast_radius`
                section, or None. OPTIONAL on purpose: the measurement fails
                open, so every caller must be able to omit it, and omitting it
                renders ``NO_BLAST_RADIUS_LINE`` rather than an empty heading.

        Returns:
            The decoded review verdict.
        """
        prompt = REVIEW_PROMPT_TEMPLATE.format(
            diff=diff,
            task_description=task_description,
            plan_text=(plan_text or "(no plan text was provided)"),
            blast_radius=(blast_radius or NO_BLAST_RADIUS_LINE),
        )
        router: LLMRouter | None = getattr(self, "_router", None)
        if router is not None:
            call_site = (
                "review_diff_rereview" if tier == "rereview" else "review_diff_first"
            )
            raw = await self._run_routed(call_site, prompt, project_id, cwd=cwd)
        else:
            raw = await self._run_claude(prompt, model, effort, cwd=cwd)
        return self._extract_json(raw)

    async def answer_clarification(
        self,
        question: str,
        task_description: str,
        plan_text: str | None = None,
        model: str | None = None,
        effort: str | None = None,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        """Attempt to answer a blocked worker's question from task/plan context."""
        prompt = CLARIFICATION_PROMPT_TEMPLATE.format(
            question=question,
            task_description=task_description,
            plan_text=(plan_text or "(no plan text was provided)"),
        )
        router: LLMRouter | None = getattr(self, "_router", None)
        if router is not None:
            raw = await self._run_routed("answer_clarification", prompt, project_id)
        else:
            raw = await self._run_claude(prompt, model, effort)
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
            raw = await self._run_routed("analyze_improvements", prompt, project_id)
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
                # The ledger is emptied with the same write that lifts the
                # throttle. Nothing REPLAYS a queued action (see
                # ``queue_action``); the work resumes because the plan and task
                # rows the actions describe are still pending and the loop
                # re-reads them. Leaving the entries behind would make
                # ``/api/status`` report work waiting on a brain that is no
                # longer waiting, permanently.
                await self._db.execute(
                    """UPDATE opus_state
                       SET status = ?, queued_actions = '[]'
                       WHERE id = 1""",
                    (OpusStatus.AVAILABLE,),
                )
                logger.info("Brain rate limit expired, now available")
                return True
        return False

    async def queue_action(self, action: dict[str, Any]) -> None:
        """Record that a brain call was deferred because the brain is parked.

        A LEDGER, not a work list: nothing reads it back to re-run anything
        (``get_queued_actions`` has no production caller). The replay is the
        orchestration loop finding the plan still PENDING, or the task still
        REVIEWING, on the pass after the limit clears.

        Idempotent, and that is load-bearing rather than tidy. The callers run
        once per orchestration pass for as long as the brain stays parked, and
        at the shipped five-second interval a five-hour subscription throttle
        is about 3600 passes: a plain append would rewrite an ever-growing JSON
        blob 3600 times per waiting plan and report a meaningless
        ``queued_count`` on ``/api/status``. Nothing noticed before because the
        branch that calls this could not fire on the router path at all.

        Args:
            action: The deferred call, e.g.
                ``{"action": "plan", "plan_id": ..., "project_id": ...}``.
        """
        state = await self.get_opus_state()
        queued = json.loads(state["queued_actions"])
        if action in queued:
            return
        queued.append(action)
        await self._db.execute(
            "UPDATE opus_state SET queued_actions = ? WHERE id = 1",
            (json.dumps(queued),),
        )

    async def get_queued_actions(self) -> list[dict[str, Any]]:
        """Read the ledger of deferred brain calls.

        No production caller: this is a read for tests and diagnosis. The
        ledger is not a work list and nothing replays what it holds; see
        :meth:`queue_action`.

        Returns:
            The deferred actions, oldest first.
        """
        state = await self.get_opus_state()
        return cast(list[dict[str, Any]], json.loads(state["queued_actions"]))

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
            raw = (
                (await self._run_routed("classify_doc", prompt, project_id))
                .strip()
                .lower()
            )
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
