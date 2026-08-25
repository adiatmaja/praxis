"""Resolve the context window of the model that will run a task.

The window has THREE states, not two: a number somebody declared, a number the
serving endpoint reported, and *unknown*. Erasing the third into a default is
what this module exists to prevent.

It used to be erased. ``_build_worker_bible`` asked LM Studio for the window of
whatever model the project named and wrote ``... or 8192`` after the call. For a
cloud harness (agy/Gemini, and the same latent defect for claude and codex) LM
Studio has never heard of the model, so the answer was always None and the
budget gate always ran at ``8192 * 0.4 = 3276`` tokens. Two live dispatches with
14 KB instruction bodies were failed two seconds in, carrying "context for this
task exceeds the local model's window; split the task", against a model whose
real window is on the order of a million tokens. Smaller specs had passed, so it
read as a size problem rather than a configuration one.

This is the same shape as two guards this project already got right:
``verify_gate.normalize_verify_cmd`` (a blank verify command is "not
configured", never a pass, because a blank shell exits 0) and ``bench/grade.py``
(an unrecognized report shape REFUSES to grade rather than writing a file of
``False``). A budget gate that cannot establish the window is in exactly that
position, and 8192 was its "blank shell exits 0".

Resolution order, highest first:

1. the project's own ``context_window`` column - the operator's escape hatch,
   and the only one that needs no code change and no restart;
2. a DECLARED window for that model name;
3. a DECLARED window for the harness, for a family whose model strings vary;
4. the LM Studio probe, attempted only when it could plausibly answer;
5. unknown.

**Harness identity is not the correctness mechanism, and must never become
one.** The obvious-looking fix here is "only probe local harnesses", and it is
wrong: OpenCode is a harness, not a model host, and an OpenCode project pointed
at a hosted OpenAI-compatible provider is a supported configuration whose model
LM Studio has never heard of either. Gating on the harness would fix agy and
silently re-break that. ``should_attempt_lm_studio_probe`` is therefore an
OPTIMIZATION - it skips a round trip that cannot succeed - and what makes every
case safe is step 5: the probe failing to come back with a number for this model
means unknown, whatever the harness, whatever the endpoint. Declared windows
(steps 1 to 3) are the primary mechanism for every API-served model on every
harness, not a cloud-only special case.

**"Failing to come back with a number" includes RAISING.** The probe parses a
payload from an endpoint this module has deliberately declined to classify, so
its exceptions are this module's to own: an unrecognized shape raises out of
``detect_context_limit`` rather than returning None, and left uncaught that is
not one failed dispatch but a plan that never dispatches again.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from orchestrator.core.agent_manager import detect_context_limit
from orchestrator.core.harnesses import should_attempt_lm_studio_probe


logger = logging.getLogger(__name__)


#: Windows shipped as defaults, merged UNDER whatever the settings file
#: declares, so an operator can correct or extend them with a file edit and a
#: ``docker compose restart`` (that file is mounted, never baked; its path
#: comes from ``settings_file.config_file_path``, never a literal).
#:
#: These are declarations, not measurements. They exist so the worker praxis
#: ships as its default works out of the box instead of resolving to "unknown"
#: until somebody writes a YAML block. A model missing here is not a bug: it
#: resolves to unknown, which skips the gate and says so. This is the layer an
#: operator running OpenCode against a HOSTED provider uses too - it is not a
#: cloud-harness special case, it is how any API-served model states its
#: window.
DEFAULT_DECLARED_MODEL_WINDOWS: dict[str, int] = {
    # Gemini 3.x via agy. Deliberately the conservative round million rather
    # than a vendor figure quoted from memory: the number only has to be right
    # enough that a real task is not refused, and this one is used as a
    # denominator, never as a promise to the model.
    "Gemini 3.7 Flash (High)": 1_000_000,
}

#: Per-harness fallback for a family whose model strings vary by tier and by
#: effort suffix ("Gemini 3.7 Flash (High)", "Gemini 3.7 Pro", ...). Consulted
#: only when the model itself is undeclared.
DEFAULT_DECLARED_HARNESS_WINDOWS: dict[str, int] = {
    "agy": 1_000_000,
}

#: The settings-file key an operator declares windows under::
#:
#:     context_windows:
#:       models:
#:         "Gemini 3.7 Flash (High)": 1000000
#:       harnesses:
#:         agy: 1000000
YAML_KEY = "context_windows"


def positive_window(value: Any) -> int | None:
    """Return ``value`` as a positive int, or None for anything else.

    Every candidate window passes through here, on BOTH seams that resolve one
    (``resolve_context_window`` at the gate and
    ``EffectiveSettings.capability_profile`` on the planning path), and the
    None it returns for a non-number is load-bearing rather than defensive: a
    YAML typo, a mocked settings object, or a NULL column must all read as "not
    declared" and fall through to the next source. Silently coercing them would
    put an invented number back in the one place this module exists to keep it
    out of.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value > 0 else None


@dataclass(frozen=True)
class DeclaredWindows:
    """Context windows declared in configuration, by model and by harness."""

    models: Mapping[str, int] = field(default_factory=dict)
    harnesses: Mapping[str, int] = field(default_factory=dict)

    def for_model(self, model_name: str | None) -> int | None:
        """Return the window declared for this model name, if any."""
        if not model_name:
            return None
        return positive_window(self.models.get(model_name))

    def for_harness(self, harness_id: str | None) -> int | None:
        """Return the window declared for this harness, if any."""
        if not harness_id:
            return None
        return positive_window(self.harnesses.get(harness_id))


def parse_declared_windows(raw: Any) -> DeclaredWindows:
    """Merge the ``context_windows`` YAML block over the shipped defaults.

    Args:
        raw: The raw value of the settings file's ``context_windows`` key.
            Anything that is not a mapping (absent, malformed, a mock) yields
            the shipped defaults unchanged.

    Returns:
        The merged declarations, every value validated as a positive int.
    """
    models = dict(DEFAULT_DECLARED_MODEL_WINDOWS)
    harnesses = dict(DEFAULT_DECLARED_HARNESS_WINDOWS)
    if isinstance(raw, Mapping):
        for key, target in (("models", models), ("harnesses", harnesses)):
            block = raw.get(key)
            if not isinstance(block, Mapping):
                continue
            for name, value in block.items():
                parsed = positive_window(value)
                if parsed is None:
                    logger.warning(
                        "Ignoring declared context window %s.%s.%r = %r: "
                        "not a positive integer",
                        YAML_KEY,
                        key,
                        name,
                        value,
                    )
                    continue
                target[str(name)] = parsed
    return DeclaredWindows(models=models, harnesses=harnesses)


@dataclass(frozen=True)
class ResolvedWindow:
    """A context window and where it came from.

    ``tokens is None`` means unknown, and ``source`` then says which harness
    and model could not be established. Callers must report the skip rather
    than substituting a number.
    """

    tokens: int | None
    source: str

    @property
    def known(self) -> bool:
        """True when a caller may run a budget gate against this window."""
        return self.tokens is not None


#: The value of ``ResolvedWindow.source`` when nothing could establish a window.
UNKNOWN_SOURCE = "unknown"


async def resolve_context_window(
    *,
    harness_id: str | None,
    model_name: str | None,
    project_override: Any = None,
    declared: Any = None,
    lm_studio_url: str | None = None,
) -> ResolvedWindow:
    """Resolve the worker's context window, or report it as unknown.

    Args:
        harness_id: The harness that will actually run the task (an escalated
            leaf carries its own, which is why the caller must resolve the
            implementer before calling this).
        model_name: The model that harness will run.
        project_override: The project row's ``context_window`` column. Wins
            over everything: it is the escape hatch for a model nobody has
            declared, and it needs no code change.
        declared: A :class:`DeclaredWindows`, or anything else (treated as no
            declarations at all).
        lm_studio_url: The worker endpoint this project will actually be given
            - the same value ``spawn_agent`` hands the container, so the probe
            can never ask a different endpoint than the worker will use. Empty
            means there is nothing to ask. It may well be a hosted provider
            rather than LM Studio, which is exactly why a probe that does not
            return a number for this model resolves to unknown.

    Returns:
        A :class:`ResolvedWindow`. ``tokens`` is None only when no source could
        answer; it is never a substituted default.
    """
    override = positive_window(project_override)
    if override is not None:
        return ResolvedWindow(override, "project override")

    if isinstance(declared, DeclaredWindows):
        by_model = declared.for_model(model_name)
        if by_model is not None:
            return ResolvedWindow(by_model, f"declared for model {model_name!r}")
        by_harness = declared.for_harness(harness_id)
        if by_harness is not None:
            return ResolvedWindow(by_harness, f"declared for harness {harness_id!r}")

    # Worth-a-try, not is-local: see ``should_attempt_lm_studio_probe``. The
    # block below is what carries the correctness, and it treats every
    # non-answer alike - LM Studio down, model not loaded, a hosted provider
    # that returned something unrecognizable at a URL that is not LM Studio at
    # all. All of them are "nobody knows", and none of them is 8192.
    if should_attempt_lm_studio_probe(harness_id, lm_studio_url):
        try:
            probed = positive_window(
                await detect_context_limit(lm_studio_url or "", model_name or "")
            )
        # BLE001 suppressed deliberately: an enumerated list is exactly how this
        # bug happened. ``detect_context_limit`` catches
        # ``(httpx.HTTPError, ValueError)`` and then calls ``payload.get(...)``,
        # so a foreign endpoint answering this path with a bare JSON array, a
        # string or ``null`` raises AttributeError straight through here, past
        # ``_build_worker_bible`` (which catches only ContextBudgetExceeded),
        # into ``run_once``'s per-plan guard - and the plan is then skipped on
        # THAT tick and every tick after it. A permanently non-dispatching plan,
        # logged as an AttributeError rather than as a failed probe.
        #
        # This module deliberately points that function at endpoints nobody has
        # classified (see ``should_attempt_lm_studio_probe``: guessing from a
        # hostname is the defect class being removed), so owning whatever they
        # return is this function's job, not the probe's. There is no exception
        # a probe can raise that means anything other than "nobody knows".
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Context-window probe against %s raised %s: %s. Treating the "
                "window as unknown.",
                lm_studio_url,
                type(exc).__name__,
                exc,
            )
            return ResolvedWindow(None, UNKNOWN_SOURCE)
        if probed is not None:
            return ResolvedWindow(probed, f"probed from {lm_studio_url}")

    return ResolvedWindow(None, UNKNOWN_SOURCE)
