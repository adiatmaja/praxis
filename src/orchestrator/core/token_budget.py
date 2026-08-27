"""Estimate context size and trim prioritized sections to a model's window.

The worker must never be handed more than its loaded context window can hold;
overflow causes silent server-side truncation or churny compaction (OpenCode). We estimate cheaply (chars/4), reserve headroom for the agent's own
reasoning + edits, and then FILL what is left in priority order, skipping any
section too big for the remaining room and carrying on to the next.
``floor`` sections are never dropped; if they alone overflow we raise.

That is best-fit packing, not a strict drop order, and the difference is
visible: a large high-priority section can be skipped while a smaller
lower-priority one is admitted after it. The docstring here used to say "drop
the lowest-priority sections until the rest fit", which describes a different
algorithm, and a reader who believed it would have concluded the packer was
broken. See ``fit_sections`` for why best-fit is the one that ships.

**Sections are not the whole prompt, and treating them as the whole prompt is
what this module got wrong until 2026-08-27.** Every dispatch also carries
``TASK_PROMPT`` (``core/agent_prompt``) into the same window, and may carry a
plan slice with it. Those are not ``Section`` objects, so the gate scored them
at zero: against a 4096-token window the budget is 1638 tokens and the fixed
scaffolding of the implementer prompt alone is over 1300 of them. The gate
therefore passed dispatches the serving endpoint then refused, and the engine
charged that refusal to the worker - a fact about the DEPLOYMENT recorded as a
fact about the worker's capability. ``out_of_band_tokens`` is how a caller
states that cost; ``worker_bible.build_bible`` supplies it on every assembly.

**What is charged is measured, and what is not measurable is NAMED rather than
reserved for.** See :data:`UNCOUNTED_CONTEXT`. The deliberate decision not to
hold back a guessed reserve for the harness's own system prompt is argued
there, because the argument is the load-bearing part.
"""

from __future__ import annotations

from dataclasses import dataclass


_CHARS_PER_TOKEN = 4

# Fraction of the context window reserved for the agent's own reasoning and
# edits.  The injected-context budget is the complement (1 - this value).
WORKER_RESERVE_FRACTION: float = 0.6

# Absolute ceiling on that reserve, in tokens.  A fraction is the right shape
# for a small window and the wrong shape for a large one: 60% of 8 192 is 4 915
# tokens of headroom, which a worker genuinely needs, while 60% of a million is
# 600 000 tokens nobody will ever use and 600 000 tokens of injected context the
# worker is refused.  What the reserve is FOR does not scale with the window: it
# holds the agent's own reasoning, its tool output, and the diffs it writes, and
# 32 768 tokens is roughly four times the largest Bible this project has ever
# assembled.  The reserve is therefore the SMALLER of the two, so every window
# at or below 54 613 tokens (0.6 x 54 613 = 32 768) is budgeted exactly as it
# was before this cap existed and only genuinely large windows change.
WORKER_RESERVE_CAP_TOKENS: int = 32_768


#: Context that reaches the worker's window but that Praxis cannot measure, and
#: therefore states instead of scoring. A passing verdict from this module is a
#: statement about the tokens PRAXIS puts in the window, never a promise that
#: the request will fit at the endpoint.
#:
#: **Why these are named and not reserved for.** The obvious move is to hold
#: back a fixed allowance for them. It was rejected, for three reasons that
#: point the same way. First, a reserve has no source of truth: the harness's
#: system prompt and tool schemas are the harness's business and change with
#: its version, and agy's differ from OpenCode's, so any figure written here
#: would be a guess that rots silently and that no field observation could
#: falsify. Second, a reserve charges in the direction that REFUSES work, and a
#: gate that fails a correctly-sized leaf with "split the task" is the exact
#: false signal ``core/context_window`` exists to have removed. Third, the
#: window already carries a reserve: ``WORKER_RESERVE_FRACTION`` holds back
#: room for the agent's own reasoning, its tool output and the diffs it writes,
#: which is the same envelope a harness preamble lives in, so a second reserve
#: would charge that region twice.
#:
#: The entrypoint manifest is here for a different reason: it is Praxis's own
#: text, but it is ASSEMBLED IN THE CONTAINER from ``command -v`` probes, so
#: the orchestrator has the shell source and not the output. Copying that text
#: into Python to score it would create a pair that drifts the moment the shell
#: changes, with nothing failing when it does.
UNCOUNTED_CONTEXT: tuple[str, ...] = (
    "the harness's own system prompt and tool schemas",
    "the target repository's AGENTS.md, which the harness combines with ours",
    "the agent entrypoint's runtime environment manifest",
)


def describe_uncounted_context() -> str:
    """Return a one-line statement of what a passing verdict does not cover.

    Returns:
        The members of :data:`UNCOUNTED_CONTEXT` joined for a log line or an
        operator-facing message.
    """
    return "; ".join(UNCOUNTED_CONTEXT)


class ContextBudgetExceeded(Exception):  # noqa: N818
    """Raised when mandatory (floor) context alone exceeds the budget."""


def worker_budget(
    context_window: int,
    reserve_fraction: float = WORKER_RESERVE_FRACTION,
) -> int:
    """Return the tokens of injected context a window allows.

    The single source of truth for "how much of the window is ours", used by
    the Bible packer, the pre-dispatch difficulty scorer, and the decomposer's
    per-leaf budget, so all three can never disagree about the same model.

    Args:
        context_window: The model's context window in tokens.
        reserve_fraction: Fraction held back for the agent's own reasoning.

    Returns:
        The budget in tokens, never negative.
    """
    fractional_reserve = context_window * reserve_fraction
    if fractional_reserve <= WORKER_RESERVE_CAP_TOKENS:
        # Deliberately the original expression rather than
        # ``context_window - int(fractional_reserve)``: the two differ by one
        # token on most inputs (int() truncates each side separately), and this
        # branch exists to leave every already-exercised window untouched.
        budget = int(context_window * (1.0 - reserve_fraction))
    else:
        budget = context_window - WORKER_RESERVE_CAP_TOKENS
    # Clamped on the RESULT, not inside the cap branch where it first sat and
    # could never fire: reaching that branch needs
    # ``context_window * reserve_fraction > 32768``, which for any fraction at
    # or below 1 forces ``context_window > 32768``, so the subtraction is always
    # positive there. The reachable negative is on the OTHER branch - a
    # ``reserve_fraction`` above 1.0 (a caller or config error) makes
    # ``1 - reserve_fraction`` negative and the budget with it, and a negative
    # budget makes ``fit_sections`` raise ContextBudgetExceeded for every task
    # on the install. One guard, on the path that can actually take it.
    return max(budget, 0)


@dataclass
class Section:
    """One prioritized chunk of worker context.

    Attributes:
        name: Identifier (for logging/tests).
        text: The content.
        priority: Lower = more important; high-priority kept, low dropped first.
        floor: If True, never dropped (dropping it would make context useless).
    """

    name: str
    text: str
    priority: int
    floor: bool = False


def estimate_tokens(text: str) -> int:
    """Return a conservative token estimate (≈4 chars/token)."""
    return len(text) // _CHARS_PER_TOKEN


def fit_sections(
    sections: list[Section],
    context_window: int | None,
    reserve_fraction: float = WORKER_RESERVE_FRACTION,
    *,
    out_of_band_tokens: int = 0,
) -> list[Section]:
    """Return the floor sections plus a best-fit pack of the rest.

    The floors are kept unconditionally. What is left of the budget is then
    filled by walking the droppable sections in ascending ``priority`` and
    taking each one that still fits, SKIPPING any that does not and continuing.
    So this is best-fit packing, not a strict tail-drop: a large high-priority
    section can be skipped and a smaller lower-priority one admitted after it.

    That is deliberate, and it is safe only because of how the caller ranks
    things. In ``worker_bible.build_bible`` every rank that carries an
    instruction is a FLOOR (goal, leaf contract, edit locations, acceptance,
    scope briefing, review feedback, handover); the droppable tail is narrative
    alone (neighbour interfaces, working agreement, caller context, repo
    memory). Nothing load-bearing is ever a candidate for skipping, so leaving
    budget unused to preserve a strict order would buy ordering purity and
    spend real context on nothing.

    ``tests/test_worker_bible_priority.py::
    test_a_large_high_priority_section_can_lose_to_a_smaller_low_priority_one``
    pins this end to end, and
    ``tests/test_token_budget.py::test_a_section_that_does_not_fit_is_skipped_
    not_a_stop_sign`` pins it here. Change the loop below to stop at the first
    section that does not fit and BOTH go red, which is the intended way to
    revisit this decision rather than discover it.

    Args:
        sections: Candidate sections.
        context_window: Model's context window in tokens, or None when nobody
            knows it. None SKIPS the gate: every section is kept and nothing is
            raised. That is a third state, not a lenient default. A window we
            cannot establish used to collapse onto a hardcoded 8192, which
            failed correctly-sized tasks against cloud models whose real window
            is two orders of magnitude larger, and reported it as the task's
            fault. Refusing to judge is the honest answer, and the caller says
            so out loud (see ``orchestrator_dispatch._build_worker_bible``).
        reserve_fraction: Fraction of the window reserved for the agent's own
            reasoning and edits, capped at ``WORKER_RESERVE_CAP_TOKENS``.
        out_of_band_tokens: Tokens Praxis will put in the SAME window that are
            not ``Section`` objects - the implementer prompt above all, plus any
            plan text sent with it. Charged against the budget before the floors
            are checked, because the endpoint charges them too. Zero means the
            caller is stating that nothing else is sent, which is a claim, not a
            default to fall back on: the seat that assembles the Bible supplies
            a real figure (see ``worker_bible.build_bible``).

    Returns:
        The kept sections, original order preserved.

    Raises:
        ContextBudgetExceeded: If ``out_of_band_tokens`` alone exceeds the
            budget, or if the floor sections do not fit what is left of it.
            The two carry different messages on purpose: only the second is a
            task-size problem a human could act on by splitting the task.
    """
    if context_window is None:
        return list(sections)
    budget = worker_budget(context_window, reserve_fraction)
    out_of_band = max(out_of_band_tokens, 0)
    if out_of_band > budget:
        # Deliberately NOT phrased as a size problem. No decomposition makes a
        # leaf smaller than the prompt wrapped around every leaf, so the only
        # action that helps is raising the worker's window, and the dispatcher's
        # own wording already tells a human to split the task.
        msg = (
            f"the prompt Praxis sends beside the Bible is {out_of_band} tok, "
            f"which alone exceeds the {budget} tok that this worker's "
            f"{context_window}-token context window allows. Splitting the task "
            "cannot shrink it; raise the worker's context window instead. Not "
            f"counted on top of this: {describe_uncounted_context()}"
        )
        raise ContextBudgetExceeded(msg)
    budget -= out_of_band
    floor_cost = sum(estimate_tokens(s.text) for s in sections if s.floor)
    if floor_cost > budget:
        msg = (
            f"floor context {floor_cost} tok exceeds budget {budget} tok "
            f"(the window's allowance less {out_of_band} tok of prompt Praxis "
            "sends beside the Bible)"
        )
        raise ContextBudgetExceeded(msg)

    kept = [s for s in sections if s.floor]
    remaining = budget - floor_cost
    for s in sorted((s for s in sections if not s.floor), key=lambda s: s.priority):
        cost = estimate_tokens(s.text)
        # `continue`, not `break`: see the docstring. A section too big for the
        # room left is skipped and the walk goes on, so the leftover budget can
        # still be spent on something smaller further down the ranking.
        if cost <= remaining:
            kept.append(s)
            remaining -= cost
    order = {id(s): i for i, s in enumerate(sections)}
    return sorted(kept, key=lambda s: order[id(s)])
