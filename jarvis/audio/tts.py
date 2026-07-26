"""Kokoro text to speech, streamed sentence by sentence.

Kokoro-82M synthesises faster than realtime on a 3060, but "faster than
realtime" still means a four-sentence reply takes about a second before a
single sample exists. Waiting for the whole thing is the largest avoidable
delay in the entire voice loop, so synthesis and playback are split across two
threads: one produces sentences into a queue, the other plays whatever has
arrived. JARVIS starts talking after the first clause and the rest is
generated behind him, under his own voice.

The same split is what makes barge-in possible. Playback writes small blocks
and checks a stop flag between them, so an interruption aborts the device and
discards the queue inside one block instead of at the end of the paragraph.
"""

from __future__ import annotations

import os
import queue
import re
import threading
import time
from typing import Iterator

import numpy as np

from jarvis.audio.wake import ModelMissing
from jarvis.config import MODEL_DIR, config, prepare_cuda

MODEL_FILE = "kokoro-v1.0.onnx"
VOICES_FILE = "voices-v1.0.bin"

# Written to the device in ~40 ms blocks so a stop lands inside 100 ms.
BLOCK_MS = 40

_SENTENCE_END = re.compile(r"(?<=[.!?…])\s+|\n+")
# Abbreviations and initials that end in a full stop but not a sentence.
_NOT_AN_END = re.compile(r"\b(?:mr|mrs|ms|dr|st|prof|sr|jr|vs|etc|e\.g|i\.e)\.$", re.I)


def sentences(text: str, max_chars: int = 220) -> Iterator[str]:
    """Split a reply into speakable chunks.

    Sentence boundaries first, because they are where a natural pause already
    exists and a seam between two synthesis calls is inaudible. Anything still
    over ``max_chars`` is cut at a comma so the first-audio delay stays bounded
    even when the model produces a wall of text.
    """
    buffer = ""
    for piece in _SENTENCE_END.split(text.strip()):
        piece = piece.strip()
        if not piece:
            continue
        buffer = f"{buffer} {piece}".strip() if buffer else piece
        # Hold on to fragments that are too short to synthesise well, and to
        # anything that only looked like a sentence end.
        if len(buffer) < 12 or _NOT_AN_END.search(buffer):
            continue
        yield from _split_long(buffer, max_chars)
        buffer = ""
    if buffer:
        yield from _split_long(buffer, max_chars)


def _split_long(text: str, max_chars: int) -> Iterator[str]:
    while len(text) > max_chars:
        cut = text.rfind(",", 0, max_chars)
        if cut < max_chars // 3:
            cut = text.rfind(" ", 0, max_chars)
        if cut <= 0:
            break
        yield text[: cut + 1].strip()
        text = text[cut + 1:].strip()
    if text:
        yield text


class Playback:
    """Handle on one in-flight utterance.

    Returned rather than blocking so the pipeline can keep reading the
    microphone while JARVIS talks, which is the whole basis of barge-in.
    """

    def __init__(self) -> None:
        self._stop = threading.Event()
        self._done = threading.Event()
        self.interrupted = False
        # Time from speak() to the first sample hitting the device — the
        # latency the user actually perceives.
        self.first_audio_ms = 0.0

    @property
    def active(self) -> bool:
        return not self._done.is_set()

    def stop(self) -> None:
        """Ask playback to abort. Returns immediately; audio dies within ~40 ms."""
        self.interrupted = True
        self._stop.set()

    def wait(self, timeout: float | None = None) -> bool:
        """Block until finished. True if it played to the end, False if cut off."""
        self._done.wait(timeout)
        return not self.interrupted


class Speaker:
    """Kokoro synthesis plus an interruptible output stream."""

    def __init__(self) -> None:
        self.voice = config.tts.voice
        self.provider = config.tts.provider
        self.sample_rate = config.tts.sample_rate
        self._kokoro = None
        self._lock = threading.Lock()
        self._current: Playback | None = None

    @property
    def loaded(self) -> bool:
        return self._kokoro is not None

    def load(self) -> None:
        """Build the ONNX session, preferring CUDA and falling back to CPU."""
        if self._kokoro is not None:
            return

        model_path = MODEL_DIR / MODEL_FILE
        voices_path = MODEL_DIR / VOICES_FILE
        missing = [p.name for p in (model_path, voices_path) if not p.is_file()]
        if missing:
            raise ModelMissing(
                f"Kokoro is missing {', '.join(missing)} in {MODEL_DIR}. "
                "Run 'python scripts/fetch_models.py' to download it."
            )

        # onnxruntime-gpu resolves cuBLAS/cuDNN at session creation, and those
        # only exist in the nvidia-* wheels on this machine.
        prepare_cuda()

        try:
            from kokoro_onnx import Kokoro
        except ImportError as exc:  # noqa: TRY003
            raise ModelMissing(
                "kokoro-onnx is not installed. Install the requirements, then "
                "run 'python scripts/fetch_models.py'."
            ) from exc

        for provider in (self.provider, "CPUExecutionProvider"):
            # kokoro-onnx picks its execution provider from the environment
            # rather than an argument.
            os.environ["ONNX_PROVIDER"] = provider
            try:
                self._kokoro = Kokoro(str(model_path), str(voices_path),
                                      **self._espeak_kwargs())
                self.provider = provider
                break
            except Exception as exc:  # noqa: BLE001
                if provider == "CPUExecutionProvider":
                    raise ModelMissing(f"Kokoro failed to load: {exc}") from exc
                print(f"[tts] {provider} unavailable ({exc}); using CPU. "
                      "Synthesis stays faster than realtime but the first "
                      "sentence will take longer.")

        print(f"[tts] kokoro {self.voice} on {self.provider}")

    @staticmethod
    def _espeak_kwargs() -> dict:
        """Point Kokoro's phonemiser at the bundled espeak-ng.

        There is no system espeak-ng on a stock Windows box, so the loader
        wheel supplies both the DLL and its data. Older kokoro-onnx builds
        find it themselves; if the config type is absent, let them.
        """
        try:
            import espeakng_loader
            from kokoro_onnx import EspeakConfig
        except ImportError:
            return {}
        return {
            "espeak_config": EspeakConfig(
                lib_path=str(espeakng_loader.get_library_path()),
                data_path=str(espeakng_loader.get_data_path()),
            )
        }

    def synthesise(self, text: str) -> np.ndarray:
        """Render one chunk of text to float32 samples. Blocking."""
        if self._kokoro is None:
            self.load()

        # The bm_* presets are British; telling espeak otherwise gives an
        # American reading of dates and place names in a British voice.
        lang = "en-gb" if self.voice.startswith("b") else "en-us"
        try:
            samples, rate = self._kokoro.create(
                text, voice=self.voice, speed=config.tts.speed, lang=lang
            )
        except Exception as exc:  # noqa: BLE001
            raise ModelMissing(
                f"Kokoro could not synthesise with voice '{self.voice}': {exc}"
            ) from exc
        self.sample_rate = int(rate)
        return np.asarray(samples, dtype=np.float32)

    def speak(self, text: str) -> Playback:
        """Start speaking. Returns immediately with a handle on the playback."""
        playback = Playback()
        clean = text.strip()
        if not clean:
            playback._done.set()
            return playback

        with self._lock:
            if self._current is not None and self._current.active:
                self._current.stop()
            self._current = playback

        chunks: queue.Queue[np.ndarray | None] = queue.Queue(maxsize=2)
        started = time.perf_counter()

        threading.Thread(
            target=self._synth_worker, args=(clean, chunks, playback),
            name="tts-synth", daemon=True,
        ).start()
        threading.Thread(
            target=self._play_worker, args=(chunks, playback, started),
            name="tts-play", daemon=True,
        ).start()
        return playback

    def say(self, text: str) -> bool:
        """Speak and block until done. True if it finished uninterrupted."""
        return self.speak(text).wait()

    def stop(self) -> None:
        """Cut off whatever is playing now."""
        with self._lock:
            if self._current is not None:
                self._current.stop()

    @property
    def speaking(self) -> bool:
        return self._current is not None and self._current.active

    # ---- workers -----------------------------------------------------------

    def _synth_worker(
        self, text: str, chunks: queue.Queue, playback: Playback
    ) -> None:
        """Render sentence by sentence, one ahead of the player."""
        try:
            for sentence in sentences(text):
                if playback._stop.is_set():
                    break
                audio = self.synthesise(sentence)
                # The bounded queue is the backpressure: at most one sentence
                # is rendered ahead, so an interruption wastes almost no GPU
                # time and the memory cost stays flat on long replies.
                while not playback._stop.is_set():
                    try:
                        chunks.put(audio, timeout=0.1)
                        break
                    except queue.Full:
                        continue
        except Exception as exc:  # noqa: BLE001
            print(f"[tts] synthesis failed: {exc}")
        finally:
            chunks.put(None)

    def _play_worker(
        self, chunks: queue.Queue, playback: Playback, started: float
    ) -> None:
        import sounddevice as sd

        stream = None
        block = None
        try:
            while not playback._stop.is_set():
                try:
                    audio = chunks.get(timeout=0.1)
                except queue.Empty:
                    continue
                if audio is None:
                    break

                if stream is None:
                    stream = sd.OutputStream(
                        samplerate=self.sample_rate,
                        channels=1,
                        dtype="float32",
                        device=config.audio.output_device,
                    )
                    stream.start()
                    block = int(self.sample_rate * BLOCK_MS / 1000)
                    playback.first_audio_ms = (time.perf_counter() - started) * 1000

                for offset in range(0, audio.size, block):
                    if playback._stop.is_set():
                        break
                    stream.write(audio[offset:offset + block].reshape(-1, 1))
        except Exception as exc:  # noqa: BLE001
            print(f"[tts] playback failed: {exc}")
        finally:
            if stream is not None:
                try:
                    # abort(), not stop(): stop() drains the device buffer,
                    # which is exactly the tail of JARVIS we are trying to get
                    # rid of when the user cuts in.
                    if playback.interrupted:
                        stream.abort()
                    else:
                        stream.stop()
                    stream.close()
                except Exception:  # noqa: BLE001
                    pass
            # Drop anything already rendered so it cannot leak into the next
            # utterance.
            while True:
                try:
                    chunks.get_nowait()
                except queue.Empty:
                    break
            playback._done.set()
