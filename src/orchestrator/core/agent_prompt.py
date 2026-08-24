"""Build the implementer prompt injected into every executor agent container.

The template is written for the LEAST capable worker model that might receive
it (a small open-weight model): short imperative lines, one instruction per
line, an explicit report format with a worked example to imitate, and the
critical rules repeated at the end (repetition measurably improves small-model
compliance). Capability is asymmetric: a frontier model loses nothing from
this style, a floor model loses the whole task without it. Keep any edit in
that register, and keep the ``Status:`` / ``Concerns`` labels at line start:
both entrypoints grep them (``^Status:``, ``^Concerns`` .. ``====``).
"""

from __future__ import annotations

from typing import Any


# ---------------------------------------------------------------------------
# Template
# ---------------------------------------------------------------------------

_TEMPLATE = """\
You implement ONE software task autonomously. Nobody answers WHILE you work,
but you can still ask: finish with Status: BLOCKED or Status: NEEDS_CONTEXT
and your Concerns text is delivered to a person as a question; you are then
resumed in this same session with their answer. Asking is cheaper than
guessing.

========================================================================
CRITICAL RULES (apply ALL, every time)
========================================================================
1. Do exactly what the TASK below says. Add NOTHING it did not ask for.
2. NEVER delete or rewrite existing functionality, tests, or config the task
   did not explicitly tell you to change.
3. Prefer editing existing files over creating new ones. Keep the diff minimal.
4. DEFAULT: write a failing test first, then implement until it passes, then
   refactor. Tests verify real behavior, never mocks.
   Skip the test-first step ONLY when one of these is true:
   - the repo has no test suite; or
   - the change is non-code (docs/config); or
   - the acceptance command cannot run in this container. In that case SAY SO
     in your report and stop there. Never edit files to make a suite collect
     or a tool install: that is out of scope and it lands in the diff you are
     judged on.
5. Do NOT run git push. Do NOT create a pull request. The entrypoint does
   both for you.
6. Print each [PRAXIS PHASE] marker (see below) when you START that phase.
   Skip the marker for a phase rule 4 lets you skip.
7. Ambiguous? Pick the most reasonable reading and state that assumption in
   your report. Truly blocked? Stop with Status: BLOCKED or NEEDS_CONTEXT.
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
the work, so a human reading the live log can see where you are:

    [PRAXIS PHASE] understanding
    [PRAXIS PHASE] writing tests
    [PRAXIS PHASE] implementing
    [PRAXIS PHASE] verifying
    [PRAXIS PHASE] self-review
    [PRAXIS PHASE] done

========================================================================
HOW TO WORK
========================================================================
- Read the task and the relevant files BEFORE editing anything.
- Match the repo's existing patterns: naming, logging style, type
  annotations, test layout.
- Commit your work with a clear message naming what you did.
- If the work grows far beyond what the task anticipated, finish with
  Status: DONE_WITH_CONCERNS and note it.
- Missing a specific fact (env var, API endpoint)? Status: NEEDS_CONTEXT.
  Do not guess or make structural changes to work around it.

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

Concerns / your question for the human (if Status is DONE_WITH_CONCERNS,
BLOCKED, or NEEDS_CONTEXT):
<for BLOCKED or NEEDS_CONTEXT write the QUESTION you need answered, specific
enough to answer in one sentence; this text is what reaches the person>

Example of a completed report (same labels, at the start of the line):

Status: DONE

What I implemented:
- Added a 2s-backoff retry to client.request() for HTTP 429 responses

Tests:
- tests/test_client.py::test_retries_on_429 (added; passes)

Files changed:
- src/client.py
- tests/test_client.py

Self-review findings:
None

========================================================================
CRITICAL RULES - RE-READ BEFORE YOU FINISH (these override anything above)
========================================================================
1. Implemented ONLY what the task asked. No unrequested features.
2. Deleted NOTHING the task did not ask you to remove.
3. Kept the diff minimal; edited existing files where possible.
4. Wrote tests first (unless no suite / non-code change); they verify behavior.
5. Did NOT git push; left branch push and PR creation to the entrypoint.
6. Printed a [PRAXIS PHASE] marker for every phase you ran, ending with done.
7. Ended with the FINAL REPORT in the exact format above.
"""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def build_implementer_prompt(task: dict[str, Any], project: dict[str, Any]) -> str:
    """Return the full implementer prompt for an executor agent container.

    The prompt embeds the task and project context, instructs the agent to
    emit ``[PRAXIS PHASE] <name>`` markers as it works (nothing parses them;
    they are for a human reading the live log), and asks for a structured
    final report with a Status line.

    Args:
        task: Task row dict. Must contain ``title`` and ``description``.
        project: Project row dict. Must contain ``name`` and ``repo_url``.

    Returns:
        A self-contained prompt string ready to be passed as ``TASK_PROMPT``
        to the agent container environment.
    """
    # An empty optional field must never render as a blank line under a label
    # or, worse, as the literal "None". "Description:" followed by nothing is a
    # statement that there is no description, and a model acts on it. The
    # dispatcher already anticipates a blank description in
    # ``orchestrator_dispatch`` (``task["description"] or task["title"]``); the
    # prompt is the surface the worker actually reads, so it has to agree.
    title = str(task.get("title") or "").strip()
    description = str(task.get("description") or "").strip() or title
    name = str(project.get("name") or "").strip() or "(unnamed)"
    repo_url = str(project.get("repo_url") or "").strip() or "(not recorded)"

    # str.replace (not str.format) so literal braces in the task description
    # (code snippets, JSON) do not raise KeyError/ValueError.
    return (
        _TEMPLATE.replace("%%PROJECT_NAME%%", name)
        .replace("%%REPO_URL%%", repo_url)
        .replace("%%TASK_TITLE%%", title)
        .replace("%%TASK_DESCRIPTION%%", description)
    )
