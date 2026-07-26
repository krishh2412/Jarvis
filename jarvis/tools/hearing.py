"""Hearing: let JARVIS listen to what the computer itself is playing.

The microphone captures Sir's voice. This captures the machine's *output* — a
YouTube video, a song, a call, a game — so JARVIS can tell what is going on by
ear, not just by looking. It reuses the same faster-whisper model the voice
pipeline uses, so "what are they saying in this video?" becomes a real,
answerable question. Pair it with see_screen: look at the picture and listen to
the sound.

Capture is WASAPI loopback via the `soundcard` library, taken from whatever the
current default output device is — so it works on the USB headset, the onboard
speakers, or anything else Sir switches to, with no per-device setup. (PortAudio
in this build cannot do loopback, and the Realtek "Stereo Mix" only mirrors the
onboard output, missing a USB headset entirely — hence soundcard.)
"""

from __future__ import annotations

import numpy as np

from jarvis.tools.registry import tool

# One Whisper instance shared across calls, loaded on first use.
_transcriber = None
_CAPTURE_RATE = 48000  # loopback native rate; Whisper wants 16 kHz, we resample


def _get_transcriber():
    global _transcriber
    if _transcriber is None:
        from jarvis.audio.stt import Transcriber
        _transcriber = Transcriber()
        _transcriber.load()
    return _transcriber


def _capture_loopback(seconds: int) -> np.ndarray | None:
    """Record the default output device via WASAPI loopback. Mono float32."""
    import soundcard as sc

    speaker = sc.default_speaker()
    mic = sc.get_microphone(id=str(speaker.name), include_loopback=True)
    if mic is None:
        # Fall back to any loopback-capable device the OS exposes.
        loopbacks = [m for m in sc.all_microphones(include_loopback=True)
                     if getattr(m, "isloopback", False)]
        if not loopbacks:
            return None
        mic = loopbacks[0]
    with mic.recorder(samplerate=_CAPTURE_RATE, channels=1) as rec:
        data = rec.record(numframes=_CAPTURE_RATE * seconds)
    return data.reshape(-1).astype(np.float32)


@tool(category="hearing")
def hear_audio(seconds: int = 8) -> dict:
    """Listen to what the computer is currently playing and transcribe it.

    Use this to know what is being said in a video, song, call or anything else
    coming out of the speakers — the audio equivalent of see_screen. When Sir
    asks what is going on in a video, look with see_screen AND listen with this.

    Records for a few seconds, so expect it to take about that long. Returns the
    transcript of speech heard; instrumental music returns little or nothing.

    Args:
        seconds: How long to listen, 3 to 30. Longer catches more but takes longer.
    """
    from scipy.signal import resample_poly

    seconds = max(3, min(30, int(seconds)))

    try:
        mono = _capture_loopback(seconds)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"could not capture system audio: {type(exc).__name__}: {exc}"}
    if mono is None or mono.size == 0:
        return {"error": "no loopback capture device is available, so I cannot "
                         "hear what the PC is playing."}

    peak = float(np.max(np.abs(mono)))
    if peak < 0.002:
        return {"heard": "", "note": "near silence — nothing seems to be playing."}

    mono16 = resample_poly(mono, 16000, _CAPTURE_RATE)
    try:
        transcriber = _get_transcriber()
        text = transcriber.transcribe((np.clip(mono16, -1, 1) * 32768).astype(np.int16))
    except Exception as exc:  # noqa: BLE001
        return {"error": f"heard audio but could not transcribe it: {exc}"}

    return {
        "heard": text,
        "seconds": seconds,
        "level": round(peak, 3),
        "note": "" if text else "audio was playing but no clear speech was found "
                "(likely music, effects, or a language Whisper did not catch).",
    }
