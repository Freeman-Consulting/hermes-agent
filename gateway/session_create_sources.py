"""Allowed ``session.create`` source labels.

``runtime.describe`` advertises this table and the canonical validator enforces it.
Keep the mapping here so those two cannot drift.
"""
from typing import Literal, get_args

CreateSource = Literal["cli", "tui", "gui", "acp", "tool", "oneshot"]
CREATE_SOURCES = get_args(CreateSource)
# Creation label -> agent surface. ``tool`` and ``oneshot`` run as the CLI but keep
# their own stored label so human pickers can hide them.
_SURFACE_PLATFORM = {
    "cli": "cli",
    "tui": "tui",
    "gui": "desktop",
    "acp": "acp",
    "tool": "cli",
    "oneshot": "cli",
}
SURFACES = {label: _SURFACE_PLATFORM[label] for label in CREATE_SOURCES}
