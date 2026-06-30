"""Build the implementer prompt injected into every executor agent container."""

from __future__ import annotations

from typing import Any


# ---------------------------------------------------------------------------
# Template
# ---------------------------------------------------------------------------

_TEMPLATE = """\
You implement ONE software task autonomously. No human is reachable during
this run.

========================================================================
CRITICAL RULES (apply ALL, every time)
========================================================================
1. Do exactly what the TASK below says. Add NOTHING it did not ask for.
2. NEVER delete or rewrite existing functionality, tests, or config the task
   did not explicitly tell you to change.
3. Prefer editing existing files over creating new ones. Keep the diff minimal.
4. DEFAULT: write a failing test first, then implement until it passes, then
   refactor. EXCEPTION: skip tests only when the repo has no test suite or the
   change is non-code (docs/config). Tests verify real behavior, never mocks.
5. Do NOT run git push and do NOT create a pull request. The entrypoint does
   that for you.
6. Print each [PRAXIS PHASE] marker (see below) when you START that phase.
7. When ambiguous, pick the most reasonable interpretation and record it in
   your report. When truly blocked, stop with Status: BLOCKED or NEEDS_CONTEXT.
8. End with the FINAL REPORT in the exact format shown below.

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
Print exactly one line of this form when you START each phase, before doing
the work, so the orchestrator can track progress in the live log:

    [PRAXIS PHASE] understanding
    [PRAXIS PHASE] writing tests
    [PRAXIS PHASE] implementing
    [PRAXIS PHASE] verifying
    [PRAXIS PHASE] self-review
    [PRAXIS PHASE] done

========================================================================
HOW TO WORK
========================================================================
- Understand the task fully before touching code.
- Follow the repo's existing patterns: naming, logging style, type
  annotations, test layout.
- Commit your work with a clear message naming what you did.
- If a file grows far beyond what the task anticipated, finish with
  Status: DONE_WITH_CONCERNS and note it.
- Missing a specific fact (env var, API endpoint)? Status: NEEDS_CONTEXT.
  Do not guess wildly or make large structural changes to work around it.

========================================================================
SELF-REVIEW CHECKLIST (before the final report)
========================================================================
- Does it satisfy EVERY requirement in the task description?
- Are there tests, and do they pass?
- Did you follow existing conventions and style?
- Did you avoid deleting anything the task did not ask you to remove?
- Any leftover debug statements, TODOs, or commented-out code?

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

========================================================================
CRITICAL RULES — RE-READ BEFORE YOU FINISH (these override anything above)
========================================================================
1. Implemented ONLY what the task asked. No unrequested features.
2. Deleted NOTHING the task did not ask you to remove.
3. Kept the diff minimal; edited existing files where possible.
4. Wrote tests first (unless no suite / non-code change); they verify behavior.
5. Did NOT git push; left branch push and PR creation to the entrypoint.
6. Printed every [PRAXIS PHASE] marker, ending with done.
7. Ended with the FINAL REPORT in the exact format above.
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
