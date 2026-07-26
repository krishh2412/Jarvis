"""Entry point for J.A.R.V.I.S.

Ties the three subsystems together: the Brain (Ollama plus the tool registry),
the native HUD window, and the voice pipeline. Run it with no arguments to get
the desktop app.

    python -m jarvis.main            desktop window
    python -m jarvis.main --console  text console, no audio hardware
    python -m jarvis.main --no-voice window without the microphone

The frozen JARVIS.exe calls :func:`main` directly.
"""

from __future__ import annotations

import argparse
import sys
from typing import Any

from jarvis.config import config, prepare_cuda


def _preflight() -> list[str]:
    """Check the things whose absence produces confusing failures later.

    Returns a list of human-readable warnings. None of these are fatal — the
    app degrades rather than refusing to start, since a missing wake-word model
    should not stop you typing at him.
    """
    import asyncio

    warnings: list[str] = []

    from jarvis.core.brain import Brain

    try:
        status = asyncio.run(Brain().health())
        if not status["ok"]:
            warnings.append(status.get("error") or "language model unavailable")
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"could not reach Ollama: {exc}")

    from jarvis.config import MODEL_DIR

    for name in ("kokoro-v1.0.onnx", "voices-v1.0.bin"):
        if not (MODEL_DIR / name).exists():
            warnings.append(
                f"speech model {name} missing — run scripts/fetch_models.py"
            )
            break

    return warnings


def _attach_voice(bridge: Any, brain: Any) -> Any | None:
    """Wire the voice pipeline into the UI bridge.

    Imported lazily and guarded: the audio stack pulls in CUDA, PortAudio and
    several hundred megabytes of model weights, any of which can be missing on
    a fresh machine. When that happens the window still opens and text chat
    still works — you simply get a note in the transcript instead of a crash
    on startup.
    """
    try:
        prepare_cuda()
        from jarvis.audio.voice import VoicePipeline
    except Exception as exc:  # noqa: BLE001
        bridge.push_event(
            {
                "type": "note",
                "text": f"Voice unavailable ({type(exc).__name__}). "
                        "Text input still works.",
            }
        )
        return None

    from jarvis.core.brain import speakable

    async def on_transcript(text: str) -> str | None:
        """Wake-word path: final transcript in, spoken reply out.

        Used only when JARVIS is triggered by "hey jarvis" outside voice mode.
        Voice mode drives the brain itself in the conversation loop below.
        """
        bridge.push_event({"type": "user", "text": text})

        async def forward(event: dict[str, Any]) -> None:
            bridge.push_event(event)

        reply = await brain.respond(text, forward)
        return speakable(reply)

    def report(future: Any, what: str) -> None:
        """Surface an async failure in the transcript instead of swallowing it."""

        def done(fut: Any) -> None:
            exc = fut.exception()
            if exc is not None:
                bridge.push_event(
                    {"type": "note", "text": f"{what}: {type(exc).__name__}: {exc}"}
                )
                bridge.set_state("idle")

        future.add_done_callback(done)

    try:
        pipeline = VoicePipeline(
            on_transcript=on_transcript,
            on_state=bridge.set_state,
            on_error=lambda exc: bridge.push_event(
                {"type": "note", "text": f"Voice: {exc}"}
            ),
            on_level=lambda level: bridge.push_event(
                {"type": "level", "value": level}
            ),
        )
    except Exception as exc:  # noqa: BLE001
        bridge.push_event(
            {"type": "note", "text": f"Voice pipeline failed to build: {exc}"}
        )
        return None

    # ---- hands-free voice mode --------------------------------------------
    # A single-flighted conversation loop: listen, answer, speak, repeat, until
    # the user leaves voice mode. It bypasses the wake word (disabled while
    # active) and drives the brain directly so the reply can be both shown big
    # on the overlay and spoken.
    voice_active = {"on": False}

    async def conversation() -> None:
        pipeline.set_wake_enabled(False)  # one mic owner at a time
        try:
            while voice_active["on"] and pipeline.running:
                bridge.set_state("listening")
                text = await pipeline.listen()
                if not voice_active["on"]:
                    break
                if not text:
                    continue  # silence or a cancelled capture; listen again
                bridge.push_event({"type": "voice_user", "text": text})
                bridge.set_state("thinking")

                async def forward(event: dict[str, Any]) -> None:
                    bridge.push_event(event)

                reply = await brain.respond(text, forward)
                spoken = speakable(reply)
                bridge.push_event({"type": "voice_reply", "text": spoken})
                if not voice_active["on"]:
                    break
                await pipeline.speak(spoken)
        finally:
            pipeline.set_wake_enabled(True)
            bridge.set_state("idle")

    def enter_voice() -> None:
        if voice_active["on"]:
            return
        if not pipeline.running:
            bridge.push_event(
                {"type": "note", "text": "Microphone is not available."}
            )
            bridge.push_event({"type": "voice_closed"})
            return
        voice_active["on"] = True
        report(bridge.submit_coro(conversation()), "Voice mode")

    def exit_voice() -> None:
        voice_active["on"] = False
        pipeline.cancel()  # break any pending listen immediately

    bridge.on_enter_voice = enter_voice
    bridge.on_exit_voice = exit_voice

    # The mic button in the chat view maps to entering/leaving voice mode.
    bridge.on_start_listening = enter_voice
    bridge.on_stop_listening = exit_voice

    # Begin wake-word listening straight away. If the model files are absent
    # this raises ModelMissing, which lands in the transcript as an actionable
    # note pointing at scripts/fetch_models.py.
    report(bridge.submit_coro(pipeline.start()), "Voice startup")
    return pipeline


def run_window(debug: bool = False, voice: bool = True) -> int:
    from jarvis.core.manager import BrainManager
    from jarvis.ui.window import create, start

    brain = BrainManager()
    window, bridge = create(brain=brain)

    for warning in _preflight():
        bridge.push_event({"type": "note", "text": warning})

    pipeline = _attach_voice(bridge, brain) if voice else None

    try:
        start(window, bridge, debug=debug)
    finally:
        if pipeline is not None:
            # stop() is async, but the bridge loop is already shutting down by
            # now, so drive it on a throwaway loop rather than submitting it.
            import asyncio

            try:
                asyncio.run(pipeline.stop())
            except Exception:  # noqa: BLE001
                pass
    return 0


def _ensure_machine_map() -> None:
    """Build the machine map in the background on first run, so JARVIS boots grounded.

    Cheap (~2s) and only when the map is absent. Runs off-thread so it never
    delays the window, and because the system prompt is rebuilt every turn the
    fresh map is picked up on the next message without a restart. Later refreshes
    happen on demand or in study mode.
    """
    import threading

    def _build() -> None:
        try:
            from jarvis.core import machine_map
            if machine_map.load() is None:
                machine_map.build()
        except Exception:  # noqa: BLE001 - grounding is a nicety, never fatal
            pass

    threading.Thread(target=_build, daemon=True).start()


def main() -> int:
    _ensure_machine_map()

    parser = argparse.ArgumentParser(
        prog="jarvis", description="Local JARVIS assistant"
    )
    parser.add_argument("--console", action="store_true",
                        help="text console instead of the window")
    parser.add_argument("--no-voice", action="store_true",
                        help="skip the microphone and voice pipeline")
    parser.add_argument("--debug", action="store_true",
                        help="open the webview devtools")
    args = parser.parse_args()

    if args.console:
        import asyncio

        from jarvis.cli import main as cli_main

        return asyncio.run(cli_main())

    return run_window(debug=args.debug, voice=not args.no_voice)


if __name__ == "__main__":
    sys.exit(main())
