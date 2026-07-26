"""Switches the active brain between local JARVIS and cloud Claude.

The rest of the app holds a manager and calls ``respond`` / ``health`` /
``reset`` on it exactly as it did the single Brain, so nothing downstream has
to know which brain is answering. Switching is instant and each brain keeps its
own conversation, so flipping back and forth doesn't scramble either history.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from jarvis.config import config

EventHandler = Callable[[dict[str, Any]], Awaitable[None]]

# Per-assistant identity: the accent the HUD themes to, and the spoken name.
PROFILES: dict[str, dict[str, str]] = {
    "jarvis": {"name": "J.A.R.V.I.S.", "accent": "#4dd0e1", "label": "local"},
    "claude": {"name": "Claude", "accent": "#d97757", "label": "claude"},
}


class BrainManager:
    """Holds both brains; delegates to whichever is active."""

    def __init__(self) -> None:
        from jarvis.core.brain import Brain

        self._jarvis = Brain()
        self._claude: Any = None  # built lazily on first switch to Claude
        self.active = config.assistant.active if config.assistant.active in PROFILES else "jarvis"

    # ---- the brain surface the app expects --------------------------------

    @property
    def brain(self) -> Any:
        if self.active == "claude":
            if self._claude is None:
                from jarvis.core.claude_brain import ClaudeBrain
                self._claude = ClaudeBrain()
            return self._claude
        return self._jarvis

    async def respond(self, text: str, on_event: EventHandler | None = None) -> str:
        return await self.brain.respond(text, on_event)

    async def health(self) -> dict[str, Any]:
        status = await self.brain.health()
        status["assistant"] = self.active
        return status

    def reset(self) -> None:
        self.brain.reset()

    @property
    def history(self) -> list:
        return getattr(self.brain, "history", [])

    # ---- switching ---------------------------------------------------------

    def profile(self, name: str | None = None) -> dict[str, str]:
        return PROFILES.get(name or self.active, PROFILES["jarvis"])

    def switch(self, name: str) -> dict[str, Any]:
        """Change the active assistant. Returns its theme/identity for the UI."""
        name = (name or "").lower().strip()
        if name not in PROFILES:
            return {"ok": False, "error": f"unknown assistant '{name}'"}
        self.active = name
        # Persist the choice so it reopens in the same mode.
        config.assistant.active = name
        prof = dict(self.profile(name))
        prof["ok"] = True
        prof["active"] = name
        return prof

    def toggle(self) -> dict[str, Any]:
        return self.switch("claude" if self.active == "jarvis" else "jarvis")
