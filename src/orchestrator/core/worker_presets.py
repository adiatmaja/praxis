"""Named (harness, model, endpoint) triples for one-choice worker setup.

Presets are convenience wiring over the existing settings layers: choosing one
sets three fields that already existed.  There is deliberately NO new resolution
logic here, so a preset can never disagree with what the orchestrator actually
does.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WorkerPreset:
    """One named worker configuration."""

    name: str
    label: str
    harness: str
    model: str
    endpoint: str
    requires: tuple[str, ...] = ()


def parse_presets(raw: list[Any]) -> list[WorkerPreset]:
    """Parse the ``worker_presets`` YAML block, skipping malformed entries.

    A typo in operator YAML must degrade to "that preset is missing", never
    stop the orchestrator from booting.  That applies to ``requires`` too: a
    scalar there is skipped rather than raising or being partly salvaged, since
    a preset kept with a requirement list that understates what it needs is a
    worse lie than a missing preset.
    """
    presets: list[WorkerPreset] = []
    for entry in raw or []:
        if not isinstance(entry, dict) or not entry.get("name"):
            logger.warning("skipping malformed worker preset entry: %r", entry)
            continue
        if not entry.get("harness") or not entry.get("model"):
            logger.warning(
                "skipping worker preset %r: harness and model are both required",
                entry.get("name"),
            )
            continue
        requires = entry.get("requires")
        # `requires: 5` raises on iteration and `requires: api_key` iterates
        # into one requirement per CHARACTER, so check the shape, not just
        # truthiness.  Absent or null is fine and means "no requirements".
        if requires is not None and not isinstance(requires, (list, tuple)):
            logger.warning(
                "skipping worker preset %r: requires must be a list, got %r",
                entry.get("name"),
                requires,
            )
            continue
        presets.append(
            WorkerPreset(
                name=str(entry["name"]),
                label=str(entry.get("label") or entry["name"]),
                harness=str(entry["harness"]),
                model=str(entry["model"]),
                endpoint=str(entry.get("endpoint") or ""),
                requires=tuple(str(r) for r in (requires or [])),
            )
        )
    return presets


def resolve_preset(presets: list[WorkerPreset], name: str) -> WorkerPreset:
    """Return the named preset.

    Raises:
        KeyError: With the known names in the message, so a typo is self-fixing.
    """
    for preset in presets:
        if preset.name == name:
            return preset
    known = ", ".join(p.name for p in presets) or "(none configured)"
    message = f"unknown worker preset {name!r}; known presets: {known}"
    raise KeyError(message)
