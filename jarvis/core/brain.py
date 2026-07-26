"""The reasoning loop: Ollama chat plus multi-round tool calling."""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Awaitable, Callable

import ollama

from jarvis.config import config
from jarvis.core import persona
from jarvis.tools import registry

EventHandler = Callable[[dict[str, Any]], Awaitable[None]]

# Qwen3 and friends emit chain-of-thought in <think> blocks. It is useful for
# quality and actively harmful to read aloud, so it is stripped before speech.
_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_DANGLING_THINK_RE = re.compile(r"<think>.*", re.DOTALL | re.IGNORECASE)


def strip_thinking(text: str) -> str:
    text = _THINK_RE.sub("", text)
    text = _DANGLING_THINK_RE.sub("", text)
    return text.strip()


def speakable(text: str) -> str:
    """Flatten markdown that would otherwise be read aloud as punctuation."""
    text = strip_thinking(text)
    text = re.sub(r"```[\s\S]*?```", " (code omitted) ", text)
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"\*\*([^*]*)\*\*", r"\1", text)
    text = re.sub(r"(?<!\w)\*([^*]+)\*(?!\w)", r"\1", text)
    text = re.sub(r"^#{1,6}\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"^\s*[-*+]\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"\n{2,}", "\n", text)
    return text.strip()


@dataclass
class Brain:
    """Holds conversation state and drives the model."""

    client: ollama.AsyncClient = field(init=False)
    history: list[dict[str, Any]] = field(default_factory=list)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)

    def __post_init__(self) -> None:
        self.client = ollama.AsyncClient(host=config.llm.host)
        registry.load_all()

    # ---- conversation state ------------------------------------------------

    def _messages(self) -> list[dict[str, Any]]:
        return [{"role": "system", "content": persona.system_prompt()}, *self.history]

    def _options(self) -> dict[str, Any]:
        return {
            "temperature": config.llm.temperature,
            "num_ctx": config.llm.num_ctx,
            "num_predict": config.llm.num_predict,
        }

    def _trim(self) -> None:
        """Drop the oldest exchanges once history outgrows the window.

        Trims from the front but never leaves a tool result orphaned from the
        assistant message that requested it — models reject that shape.
        """
        limit = config.llm.history_turns * 2
        if len(self.history) <= limit:
            return
        excess = len(self.history) - limit
        while excess < len(self.history) and self.history[excess]["role"] == "tool":
            excess += 1
        self.history = self.history[excess:]

    def reset(self) -> None:
        self.history.clear()

    # ---- generation --------------------------------------------------------

    async def respond(
        self, user_text: str, on_event: EventHandler | None = None
    ) -> str:
        """Run one full turn: think, call tools as needed, return the reply."""

        async def emit(kind: str, **payload: Any) -> None:
            if on_event is not None:
                await on_event({"type": kind, **payload})

        async with self._lock:
            self.history.append({"role": "user", "content": user_text})
            self._trim()

            tools = registry.schemas()
            final_text = ""
            consecutive_failures = 0  # for the reflection-on-failure scaffolding
            turn_actions: list[dict[str, Any]] = []  # for the episodic log
            prev_sig: tuple | None = None  # last round's tool-call signature
            repeat_hits = 0                # consecutive identical rounds

            for round_index in range(config.llm.max_tool_rounds):
                try:
                    response = await self.client.chat(
                        model=config.llm.model,
                        messages=self._messages(),
                        tools=tools,
                        options=self._options(),
                        keep_alive=config.llm.keep_alive,
                        think=not config.llm.disable_thinking,
                    )
                except Exception as exc:  # noqa: BLE001
                    error = self._explain_connection_error(exc)
                    await emit("error", message=error)
                    return error

                message = response.get("message", {}) or {}
                content = strip_thinking(message.get("content") or "")
                tool_calls = message.get("tool_calls") or []

                # Record the assistant turn verbatim so tool results have
                # something to attach to.
                self.history.append(
                    {
                        "role": "assistant",
                        "content": message.get("content") or "",
                        **({"tool_calls": tool_calls} if tool_calls else {}),
                    }
                )

                if not tool_calls:
                    final_text = content
                    break

                round_failed = 0
                for call in tool_calls:
                    fn = call.get("function", {}) or {}
                    name = fn.get("name", "")
                    args = fn.get("arguments") or {}
                    if isinstance(args, str):
                        try:
                            args = json.loads(args)
                        except json.JSONDecodeError:
                            args = {}

                    await emit("tool_call", tool=name, args=args,
                               category=self._category(name))

                    # Tools are synchronous and some block on I/O; keep the
                    # event loop free so the UI stays responsive.
                    result = await asyncio.to_thread(registry.execute, name, args)

                    failed = isinstance(result, dict) and "error" in result
                    turn_actions.append({
                        "tool": name,
                        "ok": not failed,
                        **({"error": str(result.get("error"))[:200]} if failed else {}),
                    })
                    if failed:
                        round_failed += 1

                    await emit("tool_result", tool=name, result=result)
                    self.history.append(
                        {
                            "role": "tool",
                            "content": json.dumps(result, default=str)[:8000],
                            "name": name,
                        }
                    )

                # Reflection scaffolding: a model that stops to ask "what went
                # wrong, what should I try instead" recovers far better than one
                # that keeps firing the same failing call. Only nudge when a
                # whole round failed, and repeatedly, so a single recoverable
                # error does not derail an otherwise fine turn.
                if round_failed and round_failed == len(tool_calls):
                    consecutive_failures += 1
                    if consecutive_failures >= 2:
                        self.history.append({
                            "role": "user",
                            "content": (
                                "[Two attempts just failed. Before trying again, "
                                "think about WHY: was the tool wrong for the job, "
                                "the arguments wrong, or the thing simply absent? "
                                "Change your approach — a different tool, different "
                                "arguments, or checking your assumption first — "
                                "rather than repeating the same call. If it truly "
                                "cannot be done, say so plainly.]"
                            ),
                        })
                        consecutive_failures = 0
                else:
                    consecutive_failures = 0

                # Loop-breaker for calls that *succeed* but get nowhere. The
                # failure nudge above never fires when a tool returns cleanly, so
                # a model that keeps asking the same tool the same thing — the
                # wrong tool for the job, say — would spin until the round cap.
                # Detect an identical round and intervene: nudge once toward a
                # different tool, then force a tool-free answer if it persists.
                sig = self._calls_signature(tool_calls)
                if sig == prev_sig:
                    repeat_hits += 1
                else:
                    repeat_hits = 0
                prev_sig = sig

                if repeat_hits >= 2:
                    final_text = await self._force_answer()
                    self.history.append({"role": "assistant", "content": final_text})
                    break
                if repeat_hits == 1:
                    self.history.append({
                        "role": "user",
                        "content": (
                            "[That is the same tool call as last time and it "
                            "returns the same thing — it is not getting you "
                            "closer. That tool is likely the wrong one for this. "
                            "Look for a DIFFERENT tool whose name fits the request "
                            "better, or answer with what you already have. Do not "
                            "repeat that call.]"
                        ),
                    })

                self._trim()
            else:
                final_text = (
                    f"I appear to be going in circles, {config.user_title}. "
                    "I have stopped before making it worse."
                )
                self.history.append({"role": "assistant", "content": final_text})

            await emit("reply", text=final_text, speech=speakable(final_text))

            # Episodic memory: record the completed turn so the reflection pass
            # has something to learn from later. Never let logging break a reply.
            try:
                from jarvis.core import learning
                learning.log_turn(user_text, turn_actions, final_text, assistant="jarvis")
            except Exception:  # noqa: BLE001
                pass

            return final_text

    async def stream_reply(self, user_text: str) -> AsyncIterator[str]:
        """Token stream for a turn that needs no tools. Used for quick chatter."""
        self.history.append({"role": "user", "content": user_text})
        buffer = ""
        async for chunk in await self.client.chat(
            model=config.llm.model,
            messages=self._messages(),
            stream=True,
            options=self._options(),
            keep_alive=config.llm.keep_alive,
            think=not config.llm.disable_thinking,
        ):
            piece = chunk.get("message", {}).get("content", "")
            if piece:
                buffer += piece
                yield piece
        self.history.append({"role": "assistant", "content": buffer})
        self._trim()

    # ---- helpers -----------------------------------------------------------

    @staticmethod
    def _calls_signature(tool_calls: list[dict[str, Any]]) -> tuple:
        """A hashable fingerprint of a round's tool calls (names + arguments).

        Two rounds with the same signature are the model asking for the exact
        same thing again — the shape of a loop.
        """
        sig = []
        for call in tool_calls:
            fn = call.get("function", {}) or {}
            args = fn.get("arguments") or {}
            if isinstance(args, str):
                args_repr = args
            else:
                try:
                    args_repr = json.dumps(args, sort_keys=True, default=str)
                except (TypeError, ValueError):
                    args_repr = str(args)
            sig.append((fn.get("name", ""), args_repr))
        return tuple(sorted(sig))

    async def _force_answer(self) -> str:
        """One last model call with no tools, to make it answer instead of loop."""
        self.history.append({
            "role": "user",
            "content": (
                "[Stop calling tools now. Using only what you already have, give "
                f"me your best plain answer, {config.user_title}, or tell me "
                "honestly that you could not do it and why.]"
            ),
        })
        try:
            response = await self.client.chat(
                model=config.llm.model,
                messages=self._messages(),
                options=self._options(),
                keep_alive=config.llm.keep_alive,
                think=False,
            )
            text = strip_thinking(response.get("message", {}).get("content") or "")
            if text:
                return text
        except Exception:  # noqa: BLE001
            pass
        return (f"I could not complete that cleanly, {config.user_title}, and "
                "stopped before making it worse.")

    @staticmethod
    def _category(tool_name: str) -> str:
        entry = registry.REGISTRY.get(tool_name)
        return entry.category if entry else "general"

    @staticmethod
    def _explain_connection_error(exc: Exception) -> str:
        text = str(exc).lower()
        if "connect" in text or "refused" in text or "connection" in text:
            return (
                f"I cannot reach the language model, {config.user_title}. "
                "Ollama does not appear to be running — start it and try again."
            )
        if "not found" in text or "no such model" in text:
            return (
                f"The model {config.llm.model} is not installed, "
                f"{config.user_title}. Run setup.ps1 to pull it."
            )
        return f"Something went wrong reaching the model, {config.user_title}: {exc}"

    async def health(self) -> dict[str, Any]:
        """Check that Ollama is up and the configured model is present."""
        try:
            listing = await self.client.list()
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"Ollama unreachable: {exc}"}

        installed = [m.get("model", m.get("name", "")) for m in listing.get("models", [])]
        wanted = config.llm.model
        # Ollama reports "qwen3:8b"; a config of "qwen3" should still match.
        present = any(m == wanted or m.startswith(wanted.split(":")[0]) for m in installed)
        return {
            "ok": present,
            "model": wanted,
            "installed": installed,
            "error": None if present else f"model '{wanted}' not pulled",
        }
