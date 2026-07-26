"""Speech to text, and the voice-activity plumbing that decides when to stop.

Two jobs live here because they are two halves of the same question — "has he
finished talking, and what did he say?".

faster-whisper runs the transcription on CUDA through CTranslate2. The CUDA
runtime arrives as pip wheels rather than a system install, so
``config.prepare_cuda()`` has to put those DLLs on the search path *before*
CTranslate2 is imported, or the load fails with an opaque Windows error. If
CUDA still refuses, we drop to CPU and say so rather than taking the assistant
down with us.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import numpy as np

from jarvis.audio.wake import ModelMissing
from jarvis.config import config, prepare_cuda

# Whisper resamples anything else, and the wake word model demands 16 kHz, so
# the whole capture path runs at one rate.
SAMPLE_RATE = 16_000

# Whisper fills silence with its training data's boilerplate. These are the
# phrases it invents when handed a recording of a room, and they are never
# something a user actually said to a machine.
_HALLUCINATIONS = frozenset(
    {
        "you", "thank you.", "thanks for watching!", "thank you for watching.",
        "bye.", ".", "...", "so", "okay.", "[blank_audio]",
        "subtitles by the amara.org community",
    }
)


def _frame_bytes(frame: np.ndarray) -> bytes:
    """webrtcvad wants raw little-endian int16, nothing else."""
    if frame.dtype != np.int16:
        frame = (np.clip(frame, -1.0, 1.0) * 32767).astype(np.int16)
    return frame.reshape(-1).tobytes()


def _rms(frame: np.ndarray) -> float:
    if frame.size == 0:
        return 0.0
    samples = frame.astype(np.float32)
    return float(np.sqrt(np.mean(samples * samples)))


@dataclass
class Endpointer:
    """Collects one utterance and decides when the speaker is done.

    webrtcvad alone is twitchy — it flags a chair creak as speech — so a
    capture only counts as started once a few consecutive frames agree, and
    only ends after ``endpoint_silence`` of quiet *following* real speech.
    """

    silence: float = field(default_factory=lambda: config.wake.endpoint_silence)
    max_seconds: float = field(default_factory=lambda: config.wake.max_utterance)
    # How long to wait for the user to start at all before giving up. They
    # said the wake word; if nothing follows, they were talking to someone else.
    lead_in: float = 3.0

    def __post_init__(self) -> None:
        import webrtcvad

        self._vad = webrtcvad.Vad(config.audio.vad_aggressiveness)
        self._frame_ms = config.audio.frame_ms
        self._frames: list[np.ndarray] = []
        self._speech_frames = 0
        self._silence_frames = 0
        self._elapsed_ms = 0
        self.started = False
        self.reason = ""

    # Thresholds in frames, derived once from the configured frame size.
    @property
    def _silence_needed(self) -> int:
        return max(1, int(self.silence * 1000 / self._frame_ms))

    def feed(self, frame: np.ndarray) -> bool:
        """Add a frame. Returns True once the utterance is complete."""
        self._frames.append(frame)
        self._elapsed_ms += self._frame_ms

        is_speech = self._vad.is_speech(_frame_bytes(frame), SAMPLE_RATE)

        if is_speech:
            self._speech_frames += 1
            self._silence_frames = 0
            # Three agreeing frames (~90 ms) before we believe it.
            if self._speech_frames >= 3:
                self.started = True
        else:
            self._speech_frames = 0
            if self.started:
                self._silence_frames += 1

        if self.started and self._silence_frames >= self._silence_needed:
            self.reason = "silence"
            return True
        if not self.started and self._elapsed_ms >= self.lead_in * 1000:
            self.reason = "no_speech"
            return True
        if self._elapsed_ms >= self.max_seconds * 1000:
            self.reason = "timeout"
            return True
        return False

    def prime(self, frames: list[np.ndarray]) -> None:
        """Seed the buffer with pre-roll so the first syllable is not clipped."""
        self._frames.extend(frames)

    @property
    def audio(self) -> np.ndarray:
        if not self._frames:
            return np.empty(0, dtype=np.int16)
        return np.concatenate([f.reshape(-1) for f in self._frames]).astype(np.int16)

    @property
    def speech_seconds(self) -> float:
        """Total captured length, trailing silence excluded."""
        trailing = self._silence_frames * self._frame_ms / 1000
        return max(0.0, self._elapsed_ms / 1000 - trailing)


@dataclass
class SpeechGate:
    """Fires only on sustained, loud-enough speech. Used for barge-in.

    A cough, a keyboard clatter or a muttered "mm-hm" must not abort JARVIS
    mid-sentence, so two conditions have to hold together: webrtcvad at its
    strictest calls it speech, and the frame is clearly above the room's noise
    floor, which is learned continuously from the quiet frames.
    """

    required_ms: int = field(default_factory=lambda: config.audio.bargein_speech_ms)
    # Multiple of the learned noise floor a frame must exceed. Without echo
    # cancellation this is also what stops JARVIS from interrupting himself
    # through the speakers — raise it if that happens.
    energy_ratio: float = 3.0
    energy_floor: float = 220.0

    def __post_init__(self) -> None:
        import webrtcvad

        # Aggressiveness 3 regardless of config: for barge-in a missed
        # interruption is far cheaper than a false one.
        self._vad = webrtcvad.Vad(3)
        self._frame_ms = config.audio.frame_ms
        self._run_ms = 0
        self._noise = self.energy_floor
        self.frames: list[np.ndarray] = []

    def feed(self, frame: np.ndarray) -> bool:
        """Returns True once speech has been sustained for long enough."""
        level = _rms(frame)
        loud = level > max(self._noise * self.energy_ratio, self.energy_floor)
        speech = loud and self._vad.is_speech(_frame_bytes(frame), SAMPLE_RATE)

        if speech:
            self._run_ms += self._frame_ms
            self.frames.append(frame)
        else:
            # Slow attack, so a single quiet frame mid-word does not reset the
            # run, but the floor still tracks the room over a few seconds.
            self._noise = 0.95 * self._noise + 0.05 * level
            self._run_ms = 0
            self.frames.clear()

        return self._run_ms >= self.required_ms

    def observe(self, frame: np.ndarray) -> None:
        """Update the noise floor without arming the gate. Called while idle."""
        self._noise = 0.95 * self._noise + 0.05 * _rms(frame)

    def reset(self) -> None:
        self._run_ms = 0
        self.frames.clear()


class Transcriber:
    """faster-whisper wrapper with a CPU fallback that never crashes the app."""

    def __init__(self) -> None:
        self.device = config.stt.device
        self.compute_type = config.stt.compute_type
        self.last_ms = 0.0
        self._model = None

    @property
    def loaded(self) -> bool:
        return self._model is not None

    def load(self) -> None:
        """Load the model, degrading from CUDA to CPU rather than failing."""
        if self._model is not None:
            return

        # Must happen before CTranslate2 is imported: it resolves cuBLAS and
        # cuDNN at import time on Windows.
        prepare_cuda()

        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:  # noqa: TRY003
            raise ModelMissing(
                "faster-whisper is not installed. Install the requirements, "
                "then run 'python scripts/fetch_models.py'."
            ) from exc

        wanted = config.stt.device
        try:
            self._model = WhisperModel(
                config.stt.model,
                device=wanted,
                compute_type=config.stt.compute_type,
            )
            self.device = wanted
            self.compute_type = config.stt.compute_type
        except Exception as exc:  # noqa: BLE001
            if wanted != "cuda":
                raise self._explain(exc) from exc
            print(f"[stt] CUDA unavailable ({exc}); falling back to CPU int8. "
                  "Transcription will be slower.")
            try:
                self._model = WhisperModel(
                    config.stt.model, device="cpu", compute_type="int8"
                )
            except Exception as cpu_exc:  # noqa: BLE001
                raise self._explain(cpu_exc) from cpu_exc
            self.device = "cpu"
            self.compute_type = "int8"

        print(f"[stt] {config.stt.model} on {self.device} ({self.compute_type})")

    def transcribe(self, audio: np.ndarray) -> str:
        """Transcribe one utterance of 16 kHz mono audio."""
        if self._model is None:
            self.load()

        if audio.dtype == np.int16:
            samples = audio.astype(np.float32) / 32768.0
        else:
            samples = audio.astype(np.float32)

        # Below ~0.3 s there is nothing to recognise and Whisper will invent
        # something rather than return empty.
        if samples.size < SAMPLE_RATE * 0.3:
            return ""

        started = time.perf_counter()
        segments, _ = self._model.transcribe(
            samples,
            language=config.stt.language,
            beam_size=config.stt.beam_size,
            without_timestamps=True,
            # History conditioning makes a standalone command inherit the tone
            # of the last one, which is how you get invented follow-ups.
            condition_on_previous_text=False,
        )
        text = " ".join(segment.text.strip() for segment in segments).strip()
        self.last_ms = (time.perf_counter() - started) * 1000

        if text.lower().strip() in _HALLUCINATIONS:
            return ""
        return text

    @staticmethod
    def _explain(exc: Exception) -> ModelMissing:
        text = str(exc).lower()
        if "connect" in text or "resolve" in text or "download" in text or "404" in text:
            return ModelMissing(
                f"The Whisper model '{config.stt.model}' is not cached and cannot "
                "be downloaded. Run 'python scripts/fetch_models.py' while online."
            )
        return ModelMissing(f"Whisper failed to load: {exc}")
