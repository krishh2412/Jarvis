"""The voice loop: wake word, endpointed capture, transcription, spoken reply.

This module owns the microphone and nothing else. It does not know what a
Brain is and it never imports the UI — the coordinator injects a handler that
takes a transcript and returns something to say, which is what keeps this
testable with a lambda and a WAV file instead of a GPU and a person.

One input stream serves all three consumers. Wake detection, endpointing and
barge-in all want the same 16 kHz mono frames, and opening a device three
times on Windows is a good way to find out which driver claims exclusive mode.
Everything that touches audio runs on one worker thread; the async surface is
a thin shell that hands work to it and awaits the result, so callbacks fire on
the event loop and the audio path never waits on it.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import queue
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

import numpy as np

from jarvis.audio.stt import SAMPLE_RATE, Endpointer, SpeechGate, Transcriber
from jarvis.audio.tts import Speaker
from jarvis.audio.wake import ModelMissing, WakeWord
from jarvis.config import config

# ModelMissing is re-exported: it is the one error the coordinator has to
# handle ("run scripts/fetch_models.py") and it should not have to import
# three modules to catch it.
__all__ = ["STATES", "ModelMissing", "VoicePipeline", "audio_devices"]

# idle: waiting for the wake word, which is most of the time. listening:
# actively recording an utterance. thinking: the handler is working. speaking:
# audio is playing. The distinction between idle and listening is what lets the
# HUD show a live microphone only when one is genuinely open on the user.
STATES = ("idle", "listening", "thinking", "speaking")

# ~6 s of audio. If the handler takes longer than that the oldest frames are
# dropped rather than growing the queue without bound — stale audio is worse
# than no audio.
_QUEUE_FRAMES = 200

# Pre-roll kept ahead of the wake word so the first syllable of the command
# that follows it is never clipped.
_PREROLL_FRAMES = 10


def audio_devices() -> dict[str, list[dict]]:
    """List usable input and output devices, for diagnostics and settings UI."""
    import sounddevice as sd

    inputs, outputs = [], []
    for index, device in enumerate(sd.query_devices()):
        entry = {
            "index": index,
            "name": device["name"],
            "host_api": sd.query_hostapis(device["hostapi"])["name"],
            "default_samplerate": int(device["default_samplerate"]),
        }
        if device["max_input_channels"] > 0:
            inputs.append(entry)
        if device["max_output_channels"] > 0:
            outputs.append(entry)
    return {"input": inputs, "output": outputs}


def _for_speech(text: str) -> str:
    """Flatten markdown before it is read aloud.

    Borrowed from the brain rather than reimplemented, but imported lazily and
    optionally so this module still works without the LLM stack present.
    """
    try:
        from jarvis.core.brain import speakable
    except Exception:  # noqa: BLE001
        return text.strip()
    return speakable(text)


@dataclass
class _Request:
    """Work handed to the audio thread from the async side."""

    kind: str  # "speak" | "listen"
    text: str = ""
    interruptible: bool = True
    future: concurrent.futures.Future = field(
        default_factory=concurrent.futures.Future
    )


class VoicePipeline:
    """Wake word to spoken reply, driven by injected callbacks.

    Args:
        on_transcript: Given what the user said, returns what JARVIS should
            say back — or None to stay silent (useful when the caller wants to
            drive ``speak`` itself, streaming a reply as it is generated).
            May be sync or async.
        on_state: Called with one of ``idle``/``listening``/``thinking``/
            ``speaking`` on every transition.
        on_wake: Called the moment the wake word fires, before capture starts.
        on_interrupt: Called when the user barges in over a reply.
        on_error: Called with a human-readable message when a turn fails.
        barge_in: Disable on a speaker setup that echoes badly enough to
            trigger on JARVIS's own voice; there is no echo cancellation here.
    """

    def __init__(
        self,
        *,
        on_transcript: Callable[[str], Any | Awaitable[Any]] | None = None,
        on_state: Callable[[str], Any] | None = None,
        on_wake: Callable[[], Any] | None = None,
        on_interrupt: Callable[[], Any] | None = None,
        on_error: Callable[[str], Any] | None = None,
        on_level: Callable[[float], Any] | None = None,
        barge_in: bool = True,
    ) -> None:
        self.on_transcript = on_transcript
        self.on_state = on_state
        self.on_wake = on_wake
        self.on_interrupt = on_interrupt
        self.on_error = on_error
        # Fired with a 0..1 microphone level a few times a second while a mic
        # is open. The voice-mode meter uses it, and a flat zero is the tell
        # that the mic is muted or delivering silence.
        self.on_level = on_level
        self.barge_in = barge_in

        self.wake = WakeWord()
        self.stt = Transcriber()
        self.tts = Speaker()

        self.timings: dict[str, float] = {}
        self._state = "idle"
        self._wake_enabled = config.wake.enabled

        self._loop: asyncio.AbstractEventLoop | None = None
        self._stream = None
        self._worker: threading.Thread | None = None
        self._closing = threading.Event()
        # Set to abandon an in-progress capture promptly — used when the user
        # leaves voice mode while JARVIS is still listening.
        self._cancel = threading.Event()
        self._frames: queue.Queue[np.ndarray] = queue.Queue(maxsize=_QUEUE_FRAMES)
        self._requests: queue.Queue[_Request] = queue.Queue()
        self._preroll: deque[np.ndarray] = deque(maxlen=_PREROLL_FRAMES)
        self._gate = SpeechGate()
        self._last_level = 0.0

    # ---- lifecycle ---------------------------------------------------------

    @property
    def state(self) -> str:
        return self._state

    @property
    def running(self) -> bool:
        return self._worker is not None and self._worker.is_alive()

    async def warm_up(self) -> None:
        """Load all three models. Raises ModelMissing with what to run.

        Worth doing before the first "hey jarvis" — cold, the models add
        several seconds to the first reply.
        """
        await asyncio.to_thread(self._load_models)

    def _load_models(self) -> None:
        self.wake.load()
        self.stt.load()
        self.tts.load()

    async def start(self) -> None:
        """Load models, open the microphone and begin listening."""
        if self.running:
            return

        self._loop = asyncio.get_running_loop()
        self._closing.clear()
        await self.warm_up()
        await asyncio.to_thread(self._open_stream)

        self._worker = threading.Thread(target=self._run, name="voice", daemon=True)
        self._worker.start()
        self._set_state("idle")

    async def stop(self) -> None:
        """Stop listening and release the device. Safe to call twice."""
        self._closing.set()
        self.tts.stop()

        worker, self._worker = self._worker, None
        if worker is not None:
            await asyncio.to_thread(worker.join, 3.0)

        stream, self._stream = self._stream, None
        if stream is not None:
            await asyncio.to_thread(self._close_stream, stream)

        # Fail anything still queued rather than leaving an await hanging.
        while True:
            try:
                request = self._requests.get_nowait()
            except queue.Empty:
                break
            if not request.future.done():
                request.future.set_result(False if request.kind == "speak" else None)

        self._set_state("idle")

    def _open_stream(self) -> None:
        import sounddevice as sd

        frame_samples = int(SAMPLE_RATE * config.audio.frame_ms / 1000)

        def callback(indata, _frames, _time, status) -> None:
            if status:
                # Overflows are routine when the machine is busy and mean one
                # dropped frame, not a broken stream.
                pass
            frame = np.frombuffer(bytes(indata), dtype=np.int16).copy()
            try:
                self._frames.put_nowait(frame)
            except queue.Full:
                try:
                    self._frames.get_nowait()
                    self._frames.put_nowait(frame)
                except queue.Empty:
                    pass

        try:
            self._stream = sd.RawInputStream(
                samplerate=SAMPLE_RATE,
                blocksize=frame_samples,
                dtype="int16",
                channels=1,
                device=config.audio.input_device,
                callback=callback,
            )
            self._stream.start()
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(
                f"Could not open the microphone: {exc}. Check "
                "config.audio.input_device against the indices from "
                "jarvis.audio.voice.audio_devices()."
            ) from exc

    @staticmethod
    def _close_stream(stream) -> None:
        try:
            stream.stop()
            stream.close()
        except Exception:  # noqa: BLE001
            pass

    # ---- public actions ----------------------------------------------------

    async def speak(self, text: str, *, interruptible: bool = True) -> bool:
        """Say something. Returns False if the user cut in.

        Independent of the wake loop: with the pipeline stopped this just
        plays, with it running it goes through the audio thread so barge-in
        stays armed and the mic keeps being read.
        """
        if not self.running:
            return await asyncio.to_thread(self._speak, text, interruptible)

        request = _Request("speak", text=text, interruptible=interruptible)
        self._requests.put(request)
        return await asyncio.wrap_future(request.future)

    async def listen(self) -> str | None:
        """Capture and transcribe one utterance now, skipping the wake word.

        This is the push-to-talk path and the "answer the question he just
        asked" path.
        """
        if not self.running:
            raise RuntimeError("start() the pipeline before calling listen()")
        request = _Request("listen")
        self._requests.put(request)
        return await asyncio.wrap_future(request.future)

    def interrupt(self) -> None:
        """Cut off playback immediately. Safe from any thread."""
        self.tts.stop()

    def cancel(self) -> None:
        """Abandon any in-progress capture and stop speaking. Safe from any thread.

        Used when the user exits voice mode: a pending ``listen`` returns None
        promptly instead of waiting out the silence timeout.
        """
        self._cancel.set()
        self.tts.stop()

    def set_wake_enabled(self, enabled: bool) -> None:
        """Suspend or resume wake word detection without closing the device."""
        self._wake_enabled = enabled
        self.wake.reset()
        if self.running:
            self._set_state("idle")

    # ---- worker ------------------------------------------------------------

    def _run(self) -> None:
        while not self._closing.is_set():
            try:
                request = self._requests.get_nowait()
            except queue.Empty:
                request = None

            if request is not None:
                self._serve(request)
                continue

            try:
                frame = self._frames.get(timeout=0.1)
            except queue.Empty:
                continue

            # Keep the noise floor current while nothing is happening; it is
            # what barge-in later compares against.
            self._gate.observe(frame)
            self._preroll.append(frame)
            self._emit_level(frame)

            if not self._wake_enabled:
                continue

            try:
                if self.wake.triggered(frame):
                    self._turn()
            except Exception as exc:  # noqa: BLE001
                self._fail(f"Voice loop error: {exc}")
                self._set_state("idle")

    def _serve(self, request: _Request) -> None:
        try:
            if request.kind == "speak":
                result: Any = self._speak(request.text, request.interruptible)
            else:
                result = self._capture() or None
        except Exception as exc:  # noqa: BLE001
            self._fail(str(exc))
            result = False if request.kind == "speak" else None
        finally:
            self._set_state("idle")
        if not request.future.done():
            request.future.set_result(result)

    def _turn(self) -> None:
        """One full exchange, from wake word to the end of the reply."""
        started = time.perf_counter()
        self._dispatch(self.on_wake)

        text = self._capture()
        if not text:
            self._set_state("idle")
            return

        while text:
            self._set_state("thinking")
            asked = time.perf_counter()
            reply = self._ask(text)
            self.timings["reply_ms"] = (time.perf_counter() - asked) * 1000

            if not reply:
                break

            # A barge-in means the user is already talking, so take what they
            # said as the next turn instead of dropping back to the wake word.
            if self._speak(reply, interruptible=True):
                break
            text = self._capture(prime=list(self._gate.frames))

        self.timings["turn_ms"] = (time.perf_counter() - started) * 1000
        self._set_state("idle")

    def _capture(self, prime: list[np.ndarray] | None = None) -> str:
        """Record until the user stops talking, then transcribe."""
        self._set_state("listening")
        self._cancel.clear()
        endpointer = Endpointer()
        endpointer.prime(prime if prime is not None else list(self._preroll))
        self._preroll.clear()
        self._gate.reset()

        while not self._closing.is_set() and not self._cancel.is_set():
            try:
                frame = self._frames.get(timeout=0.5)
            except queue.Empty:
                continue
            self._emit_level(frame)
            if endpointer.feed(frame):
                break

        if self._cancel.is_set():
            return ""
        if endpointer.reason == "no_speech" or endpointer.speech_seconds < 0.3:
            return ""

        self._set_state("thinking")
        text = self.stt.transcribe(endpointer.audio)
        self.timings["stt_ms"] = self.stt.last_ms
        return text

    def _speak(self, text: str, interruptible: bool) -> bool:
        """Play a reply while watching for the user cutting in.

        Returns True if it finished, False if it was interrupted.
        """
        speech = _for_speech(text)
        if not speech:
            return True

        self._set_state("speaking")
        self._drain()
        self._gate.reset()

        playback = self.tts.speak(speech)
        watching = interruptible and self.barge_in

        while playback.active and not self._closing.is_set():
            try:
                frame = self._frames.get(timeout=0.05)
            except queue.Empty:
                continue
            if watching and self._gate.feed(frame):
                playback.stop()
                self._dispatch(self.on_interrupt)
                break

        playback.wait(2.0)
        self.timings["tts_first_audio_ms"] = playback.first_audio_ms
        if not playback.interrupted:
            # Drop the tail of our own voice from the mic before wake
            # detection resumes, or the room's echo gets scored.
            self._drain()
            self.wake.reset()
        return not playback.interrupted

    def _drain(self) -> None:
        while True:
            try:
                self._frames.get_nowait()
            except queue.Empty:
                return

    def _emit_level(self, frame: np.ndarray) -> None:
        """Push a 0..1 mic level to on_level, throttled to a few times a second.

        A sqrt curve makes quiet speech and room tone visible on the meter,
        which is the point: a bar that never moves means a dead microphone,
        and that has to be obvious at a glance.
        """
        if self.on_level is None:
            return
        now = time.perf_counter()
        if now - self._last_level < 0.07:
            return
        self._last_level = now
        samples = frame.astype(np.float32)
        rms = float(np.sqrt(np.mean(samples * samples))) if samples.size else 0.0
        level = min(1.0, (rms / 1500.0) ** 0.5)
        self._dispatch(self.on_level, level)

    # ---- callbacks ---------------------------------------------------------

    def _set_state(self, state: str) -> None:
        if state == self._state:
            return
        self._state = state
        self._dispatch(self.on_state, state)

    def _fail(self, message: str) -> None:
        print(f"[voice] {message}")
        self._dispatch(self.on_error, message)

    def _dispatch(self, callback: Callable | None, *args: Any) -> None:
        """Fire a callback on the event loop, sync or async, never blocking."""
        if callback is None:
            return
        if self._loop is None or self._loop.is_closed():
            try:
                callback(*args)
            except Exception as exc:  # noqa: BLE001
                print(f"[voice] callback failed: {exc}")
            return

        if asyncio.iscoroutinefunction(callback):
            asyncio.run_coroutine_threadsafe(callback(*args), self._loop)
        else:
            self._loop.call_soon_threadsafe(lambda: callback(*args))

    def _ask(self, text: str) -> str:
        """Hand the transcript to the coordinator and wait for the reply."""
        if self.on_transcript is None:
            return ""

        if asyncio.iscoroutinefunction(self.on_transcript):
            if self._loop is None:
                return ""
            future = asyncio.run_coroutine_threadsafe(
                self.on_transcript(text), self._loop
            )
            try:
                reply = future.result()
            except Exception as exc:  # noqa: BLE001
                self._fail(f"Handler failed: {exc}")
                return ""
        else:
            try:
                reply = self.on_transcript(text)
            except Exception as exc:  # noqa: BLE001
                self._fail(f"Handler failed: {exc}")
                return ""

        return reply if isinstance(reply, str) else ""
