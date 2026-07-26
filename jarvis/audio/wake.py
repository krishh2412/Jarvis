"""Always-on wake word detection.

openWakeWord is used for one reason above all others: it ships a pretrained
"hey_jarvis" model, so there is nothing to train and nothing to license.

This is the only component that runs every second the machine is on, so it is
deliberately the cheapest thing in the stack — ONNX Runtime on the CPU, one
thread, 80 ms of audio at a time. It never touches CUDA; the GPU is reserved
for the models that only run when someone is actually talking.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

from jarvis.config import MODEL_DIR, config

# openWakeWord's feature frontend is trained at 16 kHz mono. Nothing else works.
SAMPLE_RATE = 16_000

# The mel frontend advances in 80 ms hops. Smaller chunks are accepted but
# re-run the feature models far more often for no gain in accuracy, which
# matters when the loop never stops. 1280 samples = 80 ms at 16 kHz.
CHUNK_SAMPLES = 1280

# Shared feature extractors every pretrained wake model depends on. They are
# downloaded alongside the wake models themselves.
FEATURE_MODELS = ("melspectrogram.onnx", "embedding_model.onnx")


class ModelMissing(RuntimeError):
    """A model file the voice stack needs is not on disk.

    Defined here rather than in a shared module because wake.py imports
    nothing else from ``jarvis.audio`` — stt.py and tts.py can both reach it
    without creating an import cycle.
    """


def _resolve_model(name: str) -> str:
    """Find the .onnx for a wake model given a name, a filename or a path.

    Pretrained models live inside the openwakeword package once
    ``download_models()`` has run; a custom model can be dropped in
    ``data/models`` and referenced by name instead.
    """
    import openwakeword

    direct = Path(name)
    if direct.suffix == ".onnx" and direct.is_file():
        return str(direct)

    package_dir = Path(openwakeword.__file__).resolve().parent / "resources" / "models"
    stem = direct.stem or name

    for directory in (MODEL_DIR, package_dir):
        if not directory.is_dir():
            continue
        # Pretrained files carry a version suffix ("hey_jarvis_v0.1.onnx"), so
        # match on prefix and take the highest version present.
        matches = sorted(
            p for p in directory.glob(f"{stem}*.onnx") if p.name not in FEATURE_MODELS
        )
        if matches:
            return str(matches[-1])

    raise ModelMissing(
        f"Wake word model '{name}' was not found in {MODEL_DIR} or {package_dir}. "
        "Run 'python scripts/fetch_models.py' to download the pretrained models."
    )


def _check_feature_models() -> None:
    import openwakeword

    package_dir = Path(openwakeword.__file__).resolve().parent / "resources" / "models"
    missing = [f for f in FEATURE_MODELS if not (package_dir / f).is_file()]
    if missing:
        raise ModelMissing(
            f"openWakeWord is missing its feature models ({', '.join(missing)}). "
            "Run 'python scripts/fetch_models.py' to download them."
        )


class WakeWord:
    """Streaming wake word detector.

    Feed it whatever frame size the microphone produces; it re-blocks the
    audio into the 80 ms chunks the model expects and reports the highest
    score seen. It owns no audio device — the pipeline hands it frames so a
    single input stream can serve wake detection, endpointing and barge-in.
    """

    def __init__(self, model: str | None = None, threshold: float | None = None) -> None:
        self.model_name = model or config.wake.model
        self.threshold = config.wake.threshold if threshold is None else threshold
        self.score = 0.0
        self._model = None
        self._pending = np.empty(0, dtype=np.int16)
        # Scores stay high for a moment after a hit; ignore that tail so one
        # "hey jarvis" cannot fire twice.
        self._refractory_chunks = 0

    @property
    def loaded(self) -> bool:
        return self._model is not None

    def load(self) -> None:
        """Build the ONNX session. Slow enough (~1 s) to be worth doing early."""
        if self._model is not None:
            return

        try:
            from openwakeword.model import Model
        except ImportError as exc:  # noqa: TRY003
            raise ModelMissing(
                "openwakeword is not installed. Install the requirements, then "
                "run 'python scripts/fetch_models.py'."
            ) from exc

        _check_feature_models()
        path = _resolve_model(self.model_name)

        # inference_framework must be stated: the default is tflite, which is
        # not a dependency of this project.
        self._model = Model(wakeword_models=[path], inference_framework="onnx")
        self.model_name = Path(path).stem

    def feed(self, frame: np.ndarray) -> float:
        """Push one frame of int16 mono audio. Returns the highest score in it."""
        if self._model is None:
            self.load()

        if frame.dtype != np.int16:
            frame = (np.clip(frame, -1.0, 1.0) * 32767).astype(np.int16)

        self._pending = np.concatenate((self._pending, frame.reshape(-1)))

        best = 0.0
        while self._pending.size >= CHUNK_SAMPLES:
            chunk = self._pending[:CHUNK_SAMPLES]
            self._pending = self._pending[CHUNK_SAMPLES:]

            if self._refractory_chunks > 0:
                self._refractory_chunks -= 1
                continue

            scores = self._model.predict(chunk)
            best = max(best, max(scores.values(), default=0.0))

        self.score = best
        return best

    def triggered(self, frame: np.ndarray) -> bool:
        """Convenience wrapper: True when this frame crosses the threshold."""
        fired = self.feed(frame) >= self.threshold
        if fired:
            # ~1.5 s of deafness, which is roughly how long the model's
            # internal buffer keeps reporting the phrase it just heard.
            self._refractory_chunks = 18
            self.reset()
        return fired

    def reset(self) -> None:
        """Clear the model's internal audio buffer between activations."""
        self._pending = np.empty(0, dtype=np.int16)
        if self._model is None:
            return
        reset = getattr(self._model, "reset", None)
        if callable(reset):
            reset()
