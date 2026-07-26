"""The native desktop window and the Python<->JavaScript bridge.

Architecture, because it is the part that is easy to get wrong:

* The HUD is a *string* of HTML handed to ``webview.create_window(html=...)``.
  Nothing is served, nothing listens on a socket, there is no URL.
* JavaScript reaches Python through pywebview's ``js_api`` object
  (:class:`JarvisAPI`) — every public method on it becomes
  ``pywebview.api.<name>()`` in the page.
* Python reaches JavaScript through ``window.evaluate_js`` calling the page's
  single ``window.__jarvis_dispatch(event)`` entry point.

Threading, likewise:

* The GUI loop owns the main thread — ``webview.start()`` never returns until
  the window closes.
* ``Brain.respond()`` is a coroutine, but pywebview invokes js_api methods on
  short-lived bridge threads. So one persistent asyncio loop runs in a daemon
  thread for the whole session and work is marshalled onto it with
  ``asyncio.run_coroutine_threadsafe``. One loop, created once: a fresh loop
  per message would strand the ``asyncio.Lock`` inside :class:`Brain` and lose
  the conversation.
* ``evaluate_js`` blocks until the page answers, so outbound events go through
  their own pump thread and never stall the asyncio loop.

The voice pipeline lives in ``jarvis/audio`` and is wired in from outside:
give :class:`JarvisBridge` an ``on_start_listening`` / ``on_stop_listening``
callback, and call :meth:`JarvisBridge.push_event` /
:meth:`JarvisBridge.submit_user_text` to drive the interface.
"""

from __future__ import annotations

import asyncio
import json
import queue
import threading
import uuid
from concurrent.futures import Future
from typing import Any, Callable

import webview

from jarvis.config import config
from jarvis.core import persona
from jarvis.ui.hud import hud_html

# Recognised UI states. Anything else is ignored rather than trusted.
STATES = ("idle", "listening", "thinking", "speaking")

VoiceHook = Callable[[], None]

_STOP = object()  # sentinel that retires the outbound pump thread


def _arg_preview(args: dict[str, Any]) -> str:
    """A one-line rendering of a call's arguments for the confirmation card."""
    parts = []
    for key, value in list(args.items())[:3]:
        text = str(value).replace("\n", " ")
        if len(text) > 70:
            text = text[:70] + "…"
        parts.append(f"{key}={text}")
    return "  ".join(parts)


# ---------------------------------------------------------------------------
# background asyncio loop
# ---------------------------------------------------------------------------


class _AsyncRunner:
    """One asyncio loop, one daemon thread, for the life of the window."""

    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._serve, name="jarvis-async", daemon=True
        )
        self._running = threading.Event()
        self._lock = threading.Lock()

    def start(self) -> None:
        with self._lock:
            if self._thread.is_alive():
                return
            self._thread.start()
        self._running.wait(timeout=5)

    def _serve(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.call_soon(self._running.set)
        try:
            self._loop.run_forever()
        finally:
            try:
                self._loop.run_until_complete(self._loop.shutdown_asyncgens())
            except Exception:  # noqa: BLE001 - shutdown is best effort
                pass
            self._loop.close()

    def submit(self, coro: Any) -> Future:
        """Schedule a coroutine on the loop from any thread."""
        self.start()
        return asyncio.run_coroutine_threadsafe(coro, self._loop)

    def stop(self) -> None:
        if not self._thread.is_alive():
            return
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=3)


# ---------------------------------------------------------------------------
# the bridge: everything Python-side owns
# ---------------------------------------------------------------------------


class JarvisBridge:
    """Owns the window, the brain, the loop and the outbound event pump.

    This is the object other modules talk to. It is deliberately *not* the
    js_api object, so the surface JavaScript can reach stays small.
    """

    def __init__(self, brain: Any | None = None) -> None:
        self.brain = brain
        self.window: Any = None

        # Set by whoever wires in jarvis.audio. All are called on a bridge
        # thread, must not block for long, and may be None.
        self.on_start_listening: VoiceHook | None = None
        self.on_stop_listening: VoiceHook | None = None
        # Voice mode: the full-screen hands-free conversation. enter starts a
        # continuous listen/answer/speak loop; exit stops it.
        self.on_enter_voice: VoiceHook | None = None
        self.on_exit_voice: VoiceHook | None = None

        self._runner = _AsyncRunner()
        self._outbound: queue.Queue = queue.Queue()
        self._dom_ready = threading.Event()
        self._closing = threading.Event()
        self._pump = threading.Thread(
            target=self._drain, name="jarvis-ui-pump", daemon=True
        )
        self._state = "idle"
        self._inflight = 0
        self._count_lock = threading.Lock()
        self._brain_lock = threading.Lock()
        self._brain_error: str | None = None
        # Confirmations in flight: token -> (Event, result box). A tool thread
        # waits on the Event; the page's Approve/Deny click sets it.
        self._approvals: dict[str, tuple[threading.Event, dict[str, bool]]] = {}
        self._approval_lock = threading.Lock()

    # -- lifecycle ----------------------------------------------------------

    def attach(self, window: Any) -> None:
        self.window = window
        try:
            window.events.closed += self._on_closed
        except Exception:  # noqa: BLE001 - older pywebview event API
            pass
        if not self._pump.is_alive():
            self._pump.start()
        self._runner.start()
        # From here on, anything needing a yes asks through this window.
        from jarvis.safety import gate

        gate.set_approver(self.request_approval)

    def _on_closed(self) -> None:
        self.shutdown()

    def shutdown(self) -> None:
        if self._closing.is_set():
            return
        self._closing.set()
        # Release anything blocked on a confirmation before the page goes away,
        # or a tool thread waits out the full timeout against a dead window.
        with self._approval_lock:
            waiters = list(self._approvals.values())
        for event, box in waiters:
            box["granted"] = False
            event.set()
        try:
            from jarvis.safety import gate

            gate.set_approver(None)
        except Exception:  # noqa: BLE001
            pass
        self._outbound.put(_STOP)
        self._runner.stop()

    # -- confirmations -------------------------------------------------------

    def request_approval(self, tool: str, description: str,
                         args: dict[str, Any]) -> bool:
        """Ask the user to approve one action. Blocks the calling thread.

        Called from a tool worker thread by the safety gate, never from the GUI
        thread or the event loop. Returns False on timeout, on a closing window,
        or if the page never answers — the safe answer to an unanswered question.
        """
        if self._closing.is_set():
            return False

        token = uuid.uuid4().hex[:8]
        event = threading.Event()
        box = {"granted": False}
        with self._approval_lock:
            self._approvals[token] = (event, box)

        try:
            self.push_event({
                "type": "confirm",
                "token": token,
                "tool": tool,
                "description": description,
                "detail": _arg_preview(args),
            })
            answered = event.wait(timeout=max(5, config.safety.confirm_timeout))
        finally:
            with self._approval_lock:
                self._approvals.pop(token, None)

        granted = bool(answered and box["granted"])
        # Retire the card either way, so a timed-out prompt does not sit there
        # looking live.
        self.push_event({
            "type": "confirm_done",
            "token": token,
            "granted": granted,
            "timed_out": not answered,
        })
        return granted

    def resolve_approval(self, token: str, granted: bool) -> bool:
        """Deliver the user's answer. Returns False if nothing was waiting."""
        with self._approval_lock:
            entry = self._approvals.get(token)
        if entry is None:
            return False
        event, box = entry
        box["granted"] = bool(granted)
        event.set()
        return True

    # -- outbound: Python -> page -------------------------------------------

    def push_event(self, event: dict[str, Any]) -> None:
        """Queue an event for the page. Safe from any thread, never blocks.

        This is the entry point for the voice pipeline. Recognised ``type``
        values are ``state``, ``user``, ``partial``, ``reply``, ``tool_call``,
        ``tool_result``, ``error``, ``note``, ``link``, ``confirm``,
        ``confirm_done`` and ``clear`` — which is a superset of what
        :meth:`Brain.respond` emits, so a Brain event dict can be forwarded
        verbatim.
        """
        if self._closing.is_set():
            return
        if event.get("type") == "state":
            self._state = str(event.get("state", "idle"))
        self._outbound.put(event)

    def set_state(self, state: str) -> None:
        """Move the arc reactor and status readout to ``idle``/``listening``/
        ``thinking``/``speaking``."""
        if state not in STATES:
            return
        self.push_event({"type": "state", "state": state})

    @property
    def state(self) -> str:
        return self._state

    def _drain(self) -> None:
        # Events raised before the document has booted are held, not dropped:
        # the greeting and any startup diagnostics are queued well before the
        # page calls ready().
        while True:
            item = self._outbound.get()
            if item is _STOP:
                return
            self._dom_ready.wait()
            if self._closing.is_set():
                return
            self._evaluate(item)

    def _evaluate(self, event: dict[str, Any]) -> None:
        if self.window is None:
            return
        try:
            payload = json.dumps(event, default=str)
        except (TypeError, ValueError):
            payload = json.dumps({"type": "note", "text": "unserialisable event"})
        try:
            self.window.evaluate_js(f"window.__jarvis_dispatch({payload});0")
        except Exception as exc:  # noqa: BLE001 - window may be tearing down
            if not self._closing.is_set():
                print(f"[ui] evaluate_js failed: {exc}")

    # -- inbound: page / voice -> Python -------------------------------------

    def mark_dom_ready(self) -> None:
        self._dom_ready.set()

    def submit_coro(self, coro: Any) -> Future:
        """Schedule a coroutine on the bridge's event loop, from any thread.

        The voice pipeline is async but is driven from the js_api thread and
        from audio callbacks. Routing it through here keeps the whole app on
        one event loop — a second loop would give the Brain two conversation
        states and leak a thread per turn.
        """
        return self._runner.submit(coro)

    def submit_user_text(self, text: str, *, echo: bool = True) -> None:
        """Run one full turn for ``text``.

        Call this from the speech pipeline once a final transcript exists.
        ``echo=False`` skips re-rendering the user's line, for the case where
        the page already drew it optimistically.
        """
        text = (text or "").strip()
        if not text:
            return
        if echo:
            self.push_event({"type": "user", "text": text})
        with self._count_lock:
            self._inflight += 1
        self.set_state("thinking")
        self._runner.submit(self._turn(text))

    async def _turn(self, text: str) -> None:
        try:
            brain = self._ensure_brain()
            if brain is None:
                self.push_event(
                    {
                        "type": "error",
                        "message": self._brain_error
                        or "The reasoning core is unavailable.",
                    }
                )
                return
            await brain.respond(text, self._forward)
        except Exception as exc:  # noqa: BLE001 - never kill the loop thread
            self.push_event(
                {"type": "error", "message": f"{type(exc).__name__}: {exc}"}
            )
        finally:
            with self._count_lock:
                self._inflight = max(0, self._inflight - 1)
                idle = self._inflight == 0
            # Speech playback flips this to "speaking" itself; only settle back
            # to idle if nothing else claimed the state meanwhile.
            if idle and self._state == "thinking":
                self.set_state("idle")

    async def _forward(self, event: dict[str, Any]) -> None:
        """Brain event handler — the Brain's dicts are already the wire format."""
        self.push_event(event)

    def reset_conversation(self) -> None:
        brain = self.brain
        if brain is not None and hasattr(brain, "reset"):
            try:
                brain.reset()
            except Exception:  # noqa: BLE001
                pass
        self.push_event({"type": "clear"})
        self.push_event({"type": "note", "text": "session cleared"})

    def switch_assistant(self, name: str) -> dict[str, Any]:
        """Flip between the local JARVIS brain and cloud Claude, and re-theme."""
        brain = self.brain
        if brain is None or not hasattr(brain, "switch"):
            return {"ok": False, "error": "assistant switching unavailable"}
        result = brain.switch(name)
        if not result.get("ok"):
            self.push_event({"type": "note", "text": result.get("error", "switch failed")})
            return result

        # Re-theme the HUD and rename the assistant.
        self.push_event({
            "type": "assistant",
            "active": result["active"],
            "name": result["name"],
            "accent": result["accent"],
            "label": result.get("label", ""),
        })

        # Warn early if Claude was picked but no key is configured, so the user
        # isn't surprised when the first message bounces.
        if result["active"] == "claude":
            fut = self._runner.submit(brain.health())

            def _report(f: "Future") -> None:
                try:
                    status = f.result()
                except Exception:  # noqa: BLE001
                    return
                if not status.get("ok"):
                    self.push_event({"type": "note", "text": status.get("error", "")})

            fut.add_done_callback(_report)
        return result

    # -- brain --------------------------------------------------------------

    def _ensure_brain(self) -> Any | None:
        """Build the Brain on first use. Import cost is real; failure is not fatal."""
        with self._brain_lock:
            if self.brain is not None:
                return self.brain
            if self._brain_error is not None:
                return None
            try:
                from jarvis.core.brain import Brain

                self.brain = Brain()
            except Exception as exc:  # noqa: BLE001
                self._brain_error = (
                    f"The reasoning core failed to start: {type(exc).__name__}: {exc}"
                )
                return None
            return self.brain

    def warm_up(self) -> None:
        """Load the brain and report link status. Call off the GUI thread."""
        brain = self._ensure_brain()
        if brain is None:
            self.push_event({"type": "link", "ok": False, "label": "offline"})
            self.push_event({"type": "error", "message": self._brain_error or ""})
            return

        summary = self._tool_summary()
        if summary:
            self.push_event({"type": "note", "text": summary})

        try:
            status = self._runner.submit(brain.health()).result(timeout=20)
        except Exception as exc:  # noqa: BLE001
            self.push_event({"type": "link", "ok": False, "label": "offline"})
            self.push_event({"type": "error", "message": f"Health check failed: {exc}"})
            return

        ok = bool(status.get("ok"))
        self.push_event(
            {
                "type": "link",
                "ok": ok,
                "label": config.llm.model if ok else "offline",
            }
        )
        if not ok:
            self.push_event(
                {
                    "type": "error",
                    "message": status.get("error")
                    or "The language model is not reachable.",
                }
            )

    @staticmethod
    def _tool_summary() -> str:
        """Startup line, or "" when there is nothing worth saying."""
        try:
            from jarvis.tools import registry

            count = len(registry.REGISTRY)
        except Exception:  # noqa: BLE001
            return "tool registry unavailable"
        return f"{count} tools online" if count else ""

    # -- window controls -----------------------------------------------------

    def minimise(self) -> None:
        if self.window is not None:
            self.window.minimize()

    def close(self) -> None:
        self.shutdown()
        if self.window is not None:
            self.window.destroy()


# ---------------------------------------------------------------------------
# js_api — the entire surface JavaScript can reach
# ---------------------------------------------------------------------------


class JarvisAPI:
    """Exposed to the page as ``pywebview.api``.

    Every public method here is callable from JavaScript, so the class holds
    nothing but thin delegations to :class:`JarvisBridge`. Methods return
    JSON-serialisable dicts and never raise across the bridge.
    """

    def __init__(self, bridge: JarvisBridge) -> None:
        self._bridge = bridge

    # -- boot ---------------------------------------------------------------

    def ready(self) -> dict[str, Any]:
        """Called once by the page when the document has booted.

        Returns the boot payload and unblocks the outbound event pump.
        """
        self._bridge.mark_dom_ready()
        brain = self._bridge.brain
        profile = brain.profile() if hasattr(brain, "profile") else {}
        active = getattr(brain, "active", "jarvis")
        return {
            "greeting": persona.greeting(),
            "state": self._bridge.state,
            "subtitle": profile.get("label", "local"),
            "user_title": config.user_title,
            "accent": profile.get("accent", config.ui.theme_accent),
            "assistant": active,
            "assistant_name": profile.get("name", "J.A.R.V.I.S."),
        }

    # -- conversation --------------------------------------------------------

    def send_message(self, text: str) -> dict[str, Any]:
        """Run a typed message through the brain.

        Returns immediately; the reply arrives as a pushed ``reply`` event.
        The page has already drawn the user's line, so it is not echoed back.
        """
        text = (text or "").strip()
        if not text:
            return {"ok": False, "error": "empty message"}
        try:
            self._bridge.submit_user_text(text, echo=False)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        return {"ok": True}

    def reset_conversation(self) -> dict[str, Any]:
        self._bridge.reset_conversation()
        return {"ok": True}

    def switch_assistant(self, name: str) -> dict[str, Any]:
        """Switch between "jarvis" (local) and "claude" (cloud)."""
        return self._bridge.switch_assistant(name)

    # -- confirmations -------------------------------------------------------

    def resolve_confirmation(self, token: str, granted: bool) -> dict[str, Any]:
        """Answer a pending Approve/Deny card. The only path to an approval.

        Called by the page when the user clicks a button on the confirmation
        card. Nothing else — and in particular no tool the model can invoke —
        can reach this.
        """
        ok = self._bridge.resolve_approval(str(token or ""), bool(granted))
        return {"ok": ok}

    # -- voice (stubs; jarvis.audio is wired in by the coordinator) ----------

    def start_listening(self) -> dict[str, Any]:
        """Arm the microphone. No-op until a voice hook is attached."""
        hook = self._bridge.on_start_listening
        self._bridge.set_state("listening")
        if hook is None:
            self._bridge.push_event(
                {"type": "note", "text": "voice pipeline not connected"}
            )
            self._bridge.set_state("idle")
            return {"ok": False, "error": "no voice pipeline", "state": "idle"}
        try:
            hook()
        except Exception as exc:  # noqa: BLE001
            self._bridge.set_state("idle")
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        return {"ok": True, "state": "listening"}

    def stop_listening(self) -> dict[str, Any]:
        """Disarm the microphone. No-op until a voice hook is attached."""
        hook = self._bridge.on_stop_listening
        self._bridge.set_state("idle")
        if hook is None:
            return {"ok": False, "error": "no voice pipeline", "state": "idle"}
        try:
            hook()
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        return {"ok": True, "state": "idle"}

    def toggle_listening(self) -> dict[str, Any]:
        if self._bridge.state == "listening":
            return self.stop_listening()
        return self.start_listening()

    def enter_voice_mode(self) -> dict[str, Any]:
        """Begin the hands-free conversation loop behind the voice overlay."""
        hook = self._bridge.on_enter_voice
        if hook is None:
            self._bridge.push_event(
                {"type": "note",
                 "text": "Voice is not connected. Check the microphone."}
            )
            return {"ok": False, "error": "no voice pipeline"}
        try:
            hook()
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        return {"ok": True}

    def exit_voice_mode(self) -> dict[str, Any]:
        """Leave voice mode and return to the chat view."""
        hook = self._bridge.on_exit_voice
        if hook is not None:
            try:
                hook()
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        self._bridge.set_state("idle")
        return {"ok": True}

    # -- window --------------------------------------------------------------

    def minimise(self) -> dict[str, Any]:
        self._bridge.minimise()
        return {"ok": True}

    def close(self) -> dict[str, Any]:
        self._bridge.close()
        return {"ok": True}

    def log(self, message: str) -> dict[str, Any]:
        """Console escape hatch for debugging the page from Python's stdout."""
        print(f"[hud] {message}")
        return {"ok": True}


# ---------------------------------------------------------------------------
# construction / entry point
# ---------------------------------------------------------------------------


def create(brain: Any | None = None) -> tuple[Any, JarvisBridge]:
    """Create the window and its bridge without starting the GUI loop.

    Returns ``(window, bridge)``. Hand the bridge to whatever owns the voice
    pipeline before calling :func:`start`.
    """
    bridge = JarvisBridge(brain=brain)
    window = webview.create_window(
        "J.A.R.V.I.S.",
        html=hud_html(),
        js_api=JarvisAPI(bridge),
        width=config.ui.window_width,
        height=config.ui.window_height,
        min_size=(760, 520),
        frameless=config.ui.frameless,
        # easy_drag would make the whole surface a drag handle, which eats
        # clicks on the transcript. The titlebar carries pywebview-drag-region
        # instead.
        easy_drag=False,
        resizable=config.ui.resizable,
        on_top=config.ui.on_top,
        minimized=config.ui.start_minimised,
        background_color="#080c10",
        text_select=True,
    )
    bridge.attach(window)
    return window, bridge


def _icon_path() -> str | None:
    """The app icon, if generated. Resolves next to the exe when frozen."""
    from jarvis.config import ROOT

    for candidate in (ROOT / "assets" / "jarvis.ico", ROOT / "assets" / "jarvis.png"):
        if candidate.is_file():
            return str(candidate)
    return None


def start(
    window: Any,
    bridge: JarvisBridge,
    *,
    debug: bool = False,
    warm_up: bool = True,
) -> None:
    """Run the GUI loop. Blocks the main thread until the window closes."""

    def _after_start() -> None:
        if warm_up:
            bridge.warm_up()

    kwargs: dict[str, Any] = {"debug": debug}
    icon = _icon_path()
    if icon is not None:
        # pywebview 5.x accepts an icon on start(); guard in case a future
        # version drops it so a missing kwarg never stops the app launching.
        kwargs["icon"] = icon

    try:
        try:
            webview.start(_after_start, **kwargs)
        except TypeError:
            kwargs.pop("icon", None)
            webview.start(_after_start, **kwargs)
    finally:
        bridge.shutdown()


def run(brain: Any | None = None, *, debug: bool = False) -> None:
    """Create the window and run it. The simple path for ``jarvis.main``."""
    window, bridge = create(brain=brain)
    start(window, bridge, debug=debug)


if __name__ == "__main__":  # pragma: no cover - manual smoke test
    run(debug=True)
