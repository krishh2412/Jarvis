"""Download the speech models JARVIS needs, once.

The LLM lives in Ollama and is pulled by setup.ps1. This handles the three
that ship as plain files: Kokoro (TTS), Whisper (STT) and openWakeWord.

Safe to re-run — anything already present is skipped.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jarvis.config import MODEL_DIR, config  # noqa: E402

KOKORO_BASE = (
    "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0"
)
KOKORO_FILES = [
    ("kokoro-v1.0.onnx", f"{KOKORO_BASE}/kokoro-v1.0.onnx", 310),
    ("voices-v1.0.bin", f"{KOKORO_BASE}/voices-v1.0.bin", 27),
]


def _download(url: str, dest: Path, approx_mb: int) -> bool:
    """Stream a file to disk with a progress line. Returns True on success."""
    import httpx

    if dest.exists() and dest.stat().st_size > 1024:
        print(f"  [skip] {dest.name} already present "
              f"({dest.stat().st_size / 1024**2:.0f} MB)")
        return True

    print(f"  [get ] {dest.name} (~{approx_mb} MB)")
    tmp = dest.with_suffix(dest.suffix + ".part")
    try:
        with httpx.stream("GET", url, follow_redirects=True, timeout=60) as response:
            response.raise_for_status()
            total = int(response.headers.get("content-length", 0))
            written = 0
            with open(tmp, "wb") as fh:
                for chunk in response.iter_bytes(chunk_size=1 << 20):
                    fh.write(chunk)
                    written += len(chunk)
                    if total:
                        pct = written * 100 // total
                        print(f"\r         {pct:3d}%  "
                              f"{written / 1024**2:6.1f} / {total / 1024**2:.1f} MB",
                              end="", flush=True)
            print()
        tmp.replace(dest)
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"\n  [fail] {dest.name}: {exc}")
        tmp.unlink(missing_ok=True)
        return False


def fetch_kokoro() -> bool:
    print("\nKokoro TTS")
    ok = True
    for name, url, size in KOKORO_FILES:
        ok &= _download(url, MODEL_DIR / name, size)
    return ok


def fetch_whisper() -> bool:
    """Instantiating the model triggers the HuggingFace download and caches it."""
    print(f"\nWhisper STT ({config.stt.model})")
    try:
        from faster_whisper import WhisperModel

        print("  [get ] downloading and converting (~500 MB for small)")
        # CPU + int8 here purely to avoid needing CUDA during setup; the
        # download is what matters and the cache is shared across devices.
        WhisperModel(config.stt.model, device="cpu", compute_type="int8")
        print("  [ok  ] cached")
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"  [fail] {exc}")
        return False


def fetch_wakeword() -> bool:
    print("\nopenWakeWord")
    try:
        import openwakeword.utils

        openwakeword.utils.download_models()
        print("  [ok  ] wake word models cached (includes 'hey_jarvis')")
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"  [fail] {exc}")
        return False


def main() -> int:
    print("Fetching speech models into", MODEL_DIR)
    results = {
        "Kokoro": fetch_kokoro(),
        "Whisper": fetch_whisper(),
        "openWakeWord": fetch_wakeword(),
    }

    print("\n" + "-" * 46)
    for name, ok in results.items():
        print(f"  {name:<14} {'ready' if ok else 'FAILED'}")

    failed = [n for n, ok in results.items() if not ok]
    if failed:
        print(f"\n{len(failed)} component(s) failed. Text chat will still work; "
              "voice needs these present.")
        return 1
    print("\nAll speech models ready.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
