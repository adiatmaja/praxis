"""Build the implementer prompt injected into every executor agent container."""

from __future__ import annotations

from typing import Any


# ---------------------------------------------------------------------------
# Template
# ---------------------------------------------------------------------------

_TEMPLATE = """\
You are implementing a software task as part of an automated development loop.
You cannot interact with a human during this run. Work autonomously: make the
most reasonable interpretation when anything is ambiguous, state your
assumptions explicitly in your final report, and if you genuinely cannot
proceed, end with Status: BLOCKED or NEEDS_CONTEXT and a clear explanation.

========================================================================
TASK
========================================================================
Project : %%PROJECT_NAME%%
Repo    : %%REPO_URL%%
Working directory: /home/agent/workspace

Title:
%%TASK_TITLE%%

Description:
%%TASK_DESCRIPTION%%

========================================================================
PHASE MARKERS
========================================================================
As you work through each phase, print exactly one line of this form so the
orchestrator can track your progress in the live log:

    [PRAXIS PHASE] understanding
    [PRAXIS PHASE] writing tests
    [PRAXIS PHASE] implementing
    [PRAXIS PHASE] verifying
    [PRAXIS PHASE] self-review
    [PRAXIS PHASE] done

Print each marker when you START that phase, before doing the work.

========================================================================
YOUR JOB
========================================================================
1. Understand the task description completely before touching any code.
2. Follow Test-Driven Development where applicable:
   - Write failing tests first (red), then implement until they pass (green),
     then refactor. Tests must verify real behavior, not mock internals.
3. Implement exactly what the spec describes. Do not add unrequested features.
4. Commit your work with a clear commit message.
5. Self-review your changes before finishing (see Self-Review below).

The entrypoint handles pushing and opening a pull request for you.
Do NOT invoke git push or create a pull request yourself.

========================================================================
CODE ORGANIZATION
========================================================================
- Make focused, minimal changes. Prefer editing existing files over creating
  new ones unless the task explicitly calls for a new module.
- Follow the existing patterns in the repository (naming conventions, logging
  style, type annotations, test structure).
- If a file is growing far beyond what the task description anticipated,
  note it under "concerns" in your final report (Status: DONE_WITH_CONCERNS).

========================================================================
WHEN YOU ARE STUCK
========================================================================
If you hit a blocker that makes it genuinely impossible to complete the task,
stop and set Status: BLOCKED with a precise explanation. Do not guess wildly
or make large structural changes to work around a missing piece of context.
If you are missing a specific fact (e.g. an env var name, an API endpoint),
set Status: NEEDS_CONTEXT.

========================================================================
SELF-REVIEW CHECKLIST
========================================================================
Before writing your final report, check:
- Does the implementation satisfy every requirement in the task description?
- Are there tests, and do they pass?
- Have you followed existing code conventions and style?
- Are there any leftover debug statements, TODOs, or commented-out code?
- Would a competent reviewer find any obvious issues?

========================================================================
FINAL REPORT FORMAT
========================================================================
End your output with a structured report using exactly this format:

Status: DONE | DONE_WITH_CONCERNS | BLOCKED | NEEDS_CONTEXT

What I implemented:
<concise bullet list>

Tests:
<bullet list of test files/functions added or modified, and whether they pass>

Files changed:
<bullet list of files created or modified>

Self-review findings:
<any issues found during self-review, or "None">

Concerns (if Status is DONE_WITH_CONCERNS, BLOCKED, or NEEDS_CONTEXT):
<explanation>
"""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def build_implementer_prompt(task: dict[str, Any], project: dict[str, Any]) -> str:
    """Return the full implementer prompt for an executor agent container.

    The prompt embeds the task and project context, instructs the agent to
    emit ``[PRAXIS PHASE] <name>`` markers as it works (so the dashboard live
    log can track progress), and asks for a structured final report with a
    Status line.

    Args:
        task: Task row dict. Must contain ``title`` and ``description``.
        project: Project row dict. Must contain ``name`` and ``repo_url``.

    Returns:
        A self-contained prompt string ready to be passed as ``TASK_PROMPT``
        to the agent container environment.
    """
    # str.replace (not str.format) so literal braces in the task description
    # (code snippets, JSON) do not raise KeyError/ValueError.
    return (
        _TEMPLATE.replace("%%PROJECT_NAME%%", str(project["name"]))
        .replace("%%REPO_URL%%", str(project["repo_url"]))
        .replace("%%TASK_TITLE%%", str(task["title"]))
        .replace("%%TASK_DESCRIPTION%%", str(task["description"]))
    )
