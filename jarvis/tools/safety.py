"""Tools for the safety gate: reading and setting the confirmation level.

There is deliberately no "approve" tool here. Confirmation is asked of the user
directly, by the interface, while the tool call waits — see jarvis/safety/gate.py.
Giving the model a way to approve an action made the confirmation worth nothing,
since the model held both the question and the answer.
"""

from __future__ import annotations

from typing import Literal

from jarvis.config import config
from jarvis.safety import gate
from jarvis.tools.registry import tool


@tool(category="safety", destructive=True)
def set_safety_level(level: Literal["full", "standard", "strict"]) -> dict:
    """Change how much JARVIS confirms before doing risky things.

    - full: no confirmations at all (catastrophic commands still hard-blocked).
    - standard: confirm only irreversible/dangerous actions (the default).
    - strict: confirm every destructive action.

    Args:
        level: The new safety level.
    """
    config.safety.level = level
    try:
        config.save()
    except Exception:  # noqa: BLE001
        pass
    return {"safety_level": level}


@tool(category="safety")
def safety_status() -> dict:
    """Report the current safety level and whether confirmations can be asked."""
    return {
        "level": config.safety.level,
        "can_ask_for_confirmation": gate.has_approver(),
        "denylist_patterns": len(config.safety.command_denylist),
    }
