"""A doctor hint must never send the operator to a command that does not exist.

Every RED row carries a remediation hint, and the hint is the whole point of
the row: the doctor is the front door to every problem, so a hint naming a
verb the CLI does not have converts a diagnosis into a dead end. This is not
hypothetical. The worker_endpoint hint told the operator to "switch preset
with `praxis config`" for as long as that check existed, and `praxis config`
only shows the model registry and sets role chains. It cannot change a worker
preset, so the one instruction attached to that failure led nowhere.

Nothing guarded the hints before this, which is exactly why the text drifted
away from the CLI without a single test going red.
"""
# ruff: noqa: S101

from __future__ import annotations

import re

import pytest

from cli.main import app
from orchestrator.core.doctor import CHECKS


#: `praxis <verb>` plus an optional second word, as it appears inside a hint.
#: The second word is captured so a command GROUP can be told from a runnable
#: command: `praxis config` is a group and does nothing but print help.
_PRAXIS_VERB = re.compile(r"praxis\s+([a-z][a-z0-9-]*)(?:\s+([a-z][a-z0-9-]*))?")


def _group_subcommands() -> dict[str, set[str]]:
    """Every command group, mapped to the subcommands it actually exposes."""
    groups: dict[str, set[str]] = {}
    for group in app.registered_groups:
        instance = getattr(group, "typer_instance", None)
        name = getattr(getattr(instance, "info", None), "name", None)
        if not isinstance(name, str) or not name:
            continue
        subs = set()
        for command in getattr(instance, "registered_commands", []):
            sub = command.name or (
                command.callback.__name__ if command.callback else ""
            )
            if sub:
                subs.add(sub.replace("_", "-"))
        groups[name] = subs
    return groups


def _registered_verbs() -> set[str]:
    """Every command name the Typer app actually exposes."""
    verbs = set()
    for command in app.registered_commands:
        name = command.name or (command.callback.__name__ if command.callback else "")
        # Typer turns an underscore in the function name into a dash.
        verbs.add(name.replace("_", "-"))
    for group in app.registered_groups:
        # `add_typer(config_app)` with no explicit name leaves `group.name` as
        # a Typer DefaultPlaceholder, not a string, so the real name has to
        # come off the sub-app itself. Reading `group.name` alone silently
        # yielded a verb set with no groups in it at all.
        instance = getattr(group, "typer_instance", None)
        name = getattr(getattr(instance, "info", None), "name", None)
        for candidate in (group.name, name):
            if isinstance(candidate, str) and candidate:
                verbs.add(candidate)
    return {v for v in verbs if v}


@pytest.mark.unit
def test_the_verb_extractor_finds_verbs_in_the_real_hints():
    """Guard the guard: a regex that matches nothing would pass vacuously."""
    found = [v for check in CHECKS for v in _PRAXIS_VERB.findall(check.hint)]
    assert found, "no `praxis <verb>` found in any hint; the regex is broken"


@pytest.mark.unit
def test_registered_verbs_are_discovered():
    """Guard the guard: an empty verb set would make the check below vacuous."""
    verbs = _registered_verbs()
    assert {"init", "doctor", "config", "pending"} <= verbs


@pytest.mark.unit
def test_there_is_at_least_one_command_group_to_check():
    """Guard the guard: no groups would make the group rule below vacuous."""
    assert _group_subcommands(), "no command groups discovered"


@pytest.mark.unit
@pytest.mark.parametrize("check", CHECKS, ids=lambda c: c.check_id)
def test_every_praxis_verb_named_in_a_hint_exists(check):
    verbs = _registered_verbs()
    for named, _sub in _PRAXIS_VERB.findall(check.hint):
        assert named in verbs, (
            f"the {check.check_id!r} hint tells the operator to run "
            f"`praxis {named}`, which is not a registered command. "
            f"Hint: {check.hint!r}"
        )


@pytest.mark.unit
@pytest.mark.parametrize("check", CHECKS, ids=lambda c: c.check_id)
def test_a_hint_never_sends_the_operator_to_a_bare_command_group(check):
    """Naming a GROUP with no subcommand is a dead end, not an instruction.

    This is the rule that catches the real worker_endpoint defect. `praxis
    config` passes an existence check, because the group is registered; run
    it and it prints its own help and changes nothing. The hint promised it
    would switch a worker preset, which no subcommand of it can do.
    """
    groups = _group_subcommands()
    for named, sub in _PRAXIS_VERB.findall(check.hint):
        if named not in groups:
            continue
        assert sub, (
            f"the {check.check_id!r} hint says `praxis {named}`, but {named!r} "
            f"is a command GROUP: running it only prints help. Name a "
            f"subcommand ({', '.join(sorted(groups[named])) or 'none exist'}) "
            f"or a different verb. Hint: {check.hint!r}"
        )
        assert sub in groups[named], (
            f"the {check.check_id!r} hint says `praxis {named} {sub}`, which "
            f"is not a subcommand of {named!r}. "
            f"Available: {', '.join(sorted(groups[named]))}"
        )
