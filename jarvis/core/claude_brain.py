"""The cloud brain: Claude (Anthropic API) with the same tools as JARVIS.

This is the "you" the user can switch to — Claude driving the same PC-control
tools, memory and screen vision, but speaking in Claude's own voice. It mirrors
the local Brain's surface (``respond``/``health``/``reset``) so the UI and voice
pipeline treat the two interchangeably.

Dormant until selected: nothing here runs, and no key is required, unless the
user switches to Claude mode.
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from jarvis.config import config
from jarvis.core import persona
from jarvis.tools import registry

EventHandler = Callable[[dict[str, Any]], Awaitable[None]]


@dataclass
class ClaudeBrain:
    """Anthropic-backed brain with a manual tool-use loop."""

    history: list[dict[str, Any]] = field(default_factory=list)
    _client: Any = field(default=None, init=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)

    def __post_init__(self) -> None:
        registry.load_all()

    # ---- client ------------------------------------------------------------

    def _ensure_client(self) -> Any:
        if self._client is not None:
            return self._client
        import anthropic

        # The SDK resolves ANTHROPIC_API_KEY from the environment (config.py
        # loads it from .env at import time). A missing key raises here, which
        # health() surfaces as an actionable message.
        self._client = anthropic.AsyncAnthropic()
        return self._client

    @staticmethod
    def _has_key() -> bool:
        return bool(os.environ.get("ANTHROPIC_API_KEY", "").strip())

    # ---- state -------------------------------------------------------------

    def reset(self) -> None:
        self.history.clear()

    def _trim(self) -> None:
        limit = config.llm.history_turns * 2
        if len(self.history) > limit:
            self.history = self.history[-limit:]

    # ---- generation --------------------------------------------------------

    async def respond(
        self, user_text: str, on_event: EventHandler | None = None
    ) -> str:
        """One full turn: call Claude, run any tools it requests, return the reply."""

        async def emit(kind: str, **payload: Any) -> None:
            if on_event is not None:
                await on_event({"type": kind, **payload})

        if not self._has_key():
            msg = ("Claude mode needs an Anthropic API key. Add ANTHROPIC_API_KEY "
                   "to a .env file next to the app, then switch back to Claude.")
            await emit("error", message=msg)
            return msg

        async with self._lock:
            client = self._ensure_client()
            tools = registry.anthropic_schemas()

            # Working message list for this turn: prior text turns + the new
            # user message. Tool blocks accumulate here within the turn.
            messages: list[dict[str, Any]] = [
                {"role": m["role"], "content": m["content"]} for m in self.history
            ]
            messages.append({"role": "user", "content": user_text})

            request: dict[str, Any] = {
                "model": config.claude.model,
                "max_tokens": config.claude.max_tokens,
                "system": persona.claude_prompt(),
                "tools": tools,
                "messages": messages,
            }
            if not config.claude.thinking:
                request["thinking"] = {"type": "disabled"}

            final_text = ""
            for _ in range(config.claude.max_tool_rounds):
                try:
                    response = await client.messages.create(**request)
                except Exception as exc:  # noqa: BLE001
                    error = self._explain_error(exc)
                    await emit("error", message=error)
                    return error

                if response.stop_reason == "refusal":
                    final_text = ("I can't help with that one — it tripped a safety "
                                  "check on my end.")
                    break

                # Record the assistant turn (content blocks) so tool results
                # have something to attach to.
                messages.append({"role": "assistant", "content": response.content})

                tool_uses = [b for b in response.content if b.type == "tool_use"]
                text_parts = [b.text for b in response.content if b.type == "text"]

                if response.stop_reason != "tool_use" or not tool_uses:
                    final_text = " ".join(t.strip() for t in text_parts if t).strip()
                    break

                results: list[dict[str, Any]] = []
                for block in tool_uses:
                    name = block.name
                    args = block.input if isinstance(block.input, dict) else {}
                    await emit("tool_call", tool=name, args=args,
                               category=self._category(name))
                    result = await asyncio.to_thread(registry.execute, name, args)
                    await emit("tool_result", tool=name, result=result)
                    results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": json.dumps(result, default=str)[:8000],
                    })
                messages.append({"role": "user", "content": results})
            else:
                final_text = ("I've gone back and forth on this several times without "
                              "getting there — stopping so I don't make it worse.")

            self.history.append({"role": "user", "content": user_text})
            self.history.append({"role": "assistant", "content": final_text})
            self._trim()

            from jarvis.core.brain import speakable
            await emit("reply", text=final_text, speech=speakable(final_text))
            return final_text

    # ---- helpers -----------------------------------------------------------

    @staticmethod
    def _category(tool_name: str) -> str:
        entry = registry.REGISTRY.get(tool_name)
        return entry.category if entry else "general"

    @staticmethod
    def _explain_error(exc: Exception) -> str:
        import anthropic

        if isinstance(exc, anthropic.AuthenticationError):
            return ("That Anthropic API key was rejected. Check ANTHROPIC_API_KEY "
                    "in your .env file.")
        if isinstance(exc, anthropic.RateLimitError):
            return "Anthropic is rate-limiting the key right now — give it a moment."
        if isinstance(exc, anthropic.APIConnectionError):
            return "I can't reach Anthropic — check the internet connection."
        return f"Something went wrong talking to Claude: {exc}"

    async def health(self) -> dict[str, Any]:
        """Cheap readiness check: is a key present? (No API call, no cost.)"""
        if not self._has_key():
            return {
                "ok": False,
                "model": config.claude.model,
                "error": "no ANTHROPIC_API_KEY (add one to .env for Claude mode)",
            }
        return {"ok": True, "model": config.claude.model, "error": None}
