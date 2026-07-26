"""Audio volume and media transport control."""

from __future__ import annotations

import time
from typing import Literal

from jarvis.tools.registry import tool


def _endpoint():
    """Grab the default audio output endpoint via Core Audio."""
    from ctypes import cast, POINTER
    from comtypes import CLSCTX_ALL
    from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume

    devices = AudioUtilities.GetSpeakers()
    interface = devices.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
    return cast(interface, POINTER(IAudioEndpointVolume))


@tool(category="media")
def get_volume() -> dict:
    """Get the current system output volume and mute state."""
    volume = _endpoint()
    return {
        "percent": round(volume.GetMasterVolumeLevelScalar() * 100),
        "muted": bool(volume.GetMute()),
    }


@tool(destructive=True, category="media")
def set_volume(percent: int) -> dict:
    """Set the system output volume to an ABSOLUTE level.

    Use this whenever a target level is named: "set the volume to 30", "turn it
    down to 30", "volume at half", "put it on 20". The phrase "down TO 30" means
    a destination of 30 — that is this tool, not adjust_volume.

    Args:
        percent: Target volume from 0 to 100.
    """
    level = max(0, min(100, int(percent)))
    volume = _endpoint()
    volume.SetMasterVolumeLevelScalar(level / 100.0, None)
    # Setting a level above zero implies you want to hear it.
    if level > 0 and volume.GetMute():
        volume.SetMute(0, None)
    return {"percent": level}


@tool(destructive=True, category="media")
def adjust_volume(delta: int) -> dict:
    """Change the volume RELATIVE to whatever it is now, by a number of points.

    Use this only when no target level is named: "turn it up a bit", "louder",
    "quieter", "down a notch". "Down BY 30" is this tool.

    Do NOT use it for "turn it down TO 30" — that names a destination, so it is
    set_volume(30). Getting this wrong silently lands on the wrong level: at a
    current volume of 30, adjust_volume(-30) mutes the machine.

    Args:
        delta: Percentage points to change by. Negative lowers.
    """
    volume = _endpoint()
    current = round(volume.GetMasterVolumeLevelScalar() * 100)
    target = max(0, min(100, current + int(delta)))
    volume.SetMasterVolumeLevelScalar(target / 100.0, None)
    return {"from": current, "to": target}


@tool(destructive=True, category="media")
def set_mute(muted: bool) -> dict:
    """Mute or unmute the system audio output.

    Args:
        muted: True to mute, False to unmute.
    """
    volume = _endpoint()
    volume.SetMute(1 if muted else 0, None)
    return {"muted": muted}


@tool(destructive=True, category="media")
def media_control(
    action: Literal["play_pause", "next", "previous", "stop"],
) -> dict:
    """Send a media transport key, controlling whatever app has media focus.

    Works with Spotify, YouTube in a browser, VLC and anything else that
    registers for the Windows media keys.

    Args:
        action: Transport action to send.
    """
    import pyautogui

    keys = {
        "play_pause": "playpause",
        "next": "nexttrack",
        "previous": "prevtrack",
        "stop": "stop",
    }
    pyautogui.press(keys[action])
    return {"sent": action}


@tool(category="media")
def audio_sessions() -> list[dict]:
    """List applications currently playing audio, with their volumes."""
    from pycaw.pycaw import AudioUtilities

    sessions = []
    for session in AudioUtilities.GetAllSessions():
        if session.Process is None:
            continue
        try:
            sessions.append(
                {
                    "process": session.Process.name(),
                    "pid": session.Process.pid,
                    "volume_percent": round(session.SimpleAudioVolume.GetMasterVolume() * 100),
                    "muted": bool(session.SimpleAudioVolume.GetMute()),
                }
            )
        except (OSError, AttributeError):
            continue
    return sessions


# ---- display brightness (DDC/CI) ------------------------------------------
#
# Laptop panels expose brightness through WMI (WmiMonitorBrightnessMethods).
# Desktop monitors do NOT — that call returns "Not supported". The path that
# works for an external monitor is DDC/CI, a control channel that rides the
# display cable itself, reached through the native dxva2.dll API. It talks to
# the monitor's own hardware, so it works regardless of GPU or connection, and
# it addresses each physical monitor separately.


def _dxva2():
    """dxva2.dll with argtypes declared.

    Declaring the signatures is not optional here: a physical-monitor HANDLE is
    a 64-bit pointer, and ctypes' default of passing arguments as C ``int``
    truncates it to 32 bits. Reads sometimes survive that by luck;
    SetMonitorBrightness reliably fails with a truncated handle, which looks
    exactly like "the monitor refused the change".
    """
    import ctypes
    from ctypes import wintypes

    dxva2 = ctypes.windll.dxva2
    dw_p = ctypes.POINTER(wintypes.DWORD)
    dxva2.GetMonitorBrightness.argtypes = [wintypes.HANDLE, dw_p, dw_p, dw_p]
    dxva2.GetMonitorBrightness.restype = wintypes.BOOL
    dxva2.SetMonitorBrightness.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    dxva2.SetMonitorBrightness.restype = wintypes.BOOL
    dxva2.DestroyPhysicalMonitor.argtypes = [wintypes.HANDLE]
    return dxva2


def _physical_monitors() -> list:
    """Enumerate physical monitors as dxva2 handles. Caller must destroy them."""
    import ctypes
    from ctypes import wintypes

    class PHYSICAL_MONITOR(ctypes.Structure):
        _fields_ = [
            ("hPhysicalMonitor", wintypes.HANDLE),
            ("szPhysicalMonitorDescription", wintypes.WCHAR * 128),
        ]

    user32 = ctypes.windll.user32
    dxva2 = ctypes.windll.dxva2
    handles: list = []

    def _callback(hmon, _hdc, _rect, _param):
        count = wintypes.DWORD()
        if dxva2.GetNumberOfPhysicalMonitorsFromHMONITOR(
            hmon, ctypes.byref(count)
        ) and count.value:
            arr = (PHYSICAL_MONITOR * count.value)()
            if dxva2.GetPhysicalMonitorsFromHMONITOR(hmon, count.value, arr):
                handles.extend(arr)
        return 1

    proc = ctypes.WINFUNCTYPE(
        wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC,
        ctypes.POINTER(wintypes.RECT), wintypes.LPARAM,
    )(_callback)
    user32.EnumDisplayMonitors(0, 0, proc, 0)
    return handles


@tool(category="media")
def get_brightness() -> dict:
    """Get the current brightness of each connected monitor, as a percentage.

    Works on desktop monitors via DDC/CI, not just laptop panels.
    """
    import ctypes
    from ctypes import wintypes

    dxva2 = _dxva2()
    monitors = _physical_monitors()
    if not monitors:
        return {"error": "no monitors found"}

    out = []
    for index, pm in enumerate(monitors):
        mn, cur, mx = wintypes.DWORD(), wintypes.DWORD(), wintypes.DWORD()
        ok = dxva2.GetMonitorBrightness(
            pm.hPhysicalMonitor, ctypes.byref(mn),
            ctypes.byref(cur), ctypes.byref(mx),
        )
        if ok and mx.value > mn.value:
            span = mx.value - mn.value
            out.append({
                "monitor": index,
                "percent": round((cur.value - mn.value) / span * 100),
            })
        else:
            out.append({"monitor": index, "percent": None,
                        "note": "does not report brightness over DDC/CI"})
        dxva2.DestroyPhysicalMonitor(pm.hPhysicalMonitor)
    return {"monitors": out}


@tool(destructive=True, category="media")
def set_brightness(percent: int, monitor: int = -1) -> dict:
    """Set monitor brightness by percentage, via DDC/CI.

    Args:
        percent: Target brightness from 0 to 100.
        monitor: Which monitor (0 is the first). -1 sets every monitor.
    """
    import ctypes
    from ctypes import wintypes

    dxva2 = _dxva2()
    level = max(0, min(100, int(percent)))
    monitors = _physical_monitors()
    if not monitors:
        return {"error": "no monitors found"}

    changed, skipped = [], []
    for index, pm in enumerate(monitors):
        if monitor != -1 and index != monitor:
            dxva2.DestroyPhysicalMonitor(pm.hPhysicalMonitor)
            continue
        mn, cur, mx = wintypes.DWORD(), wintypes.DWORD(), wintypes.DWORD()
        if dxva2.GetMonitorBrightness(
            pm.hPhysicalMonitor, ctypes.byref(mn),
            ctypes.byref(cur), ctypes.byref(mx),
        ):
            target = mn.value + round((mx.value - mn.value) * level / 100)
            # Some monitor controllers reject a DDC/CI write that arrives too
            # soon after the preceding one, so retry with a short pause before
            # giving up on that monitor.
            ok = False
            for attempt in range(4):
                if dxva2.SetMonitorBrightness(pm.hPhysicalMonitor, target):
                    ok = True
                    break
                time.sleep(0.12)
            (changed if ok else skipped).append(index)
        else:
            skipped.append(index)
        dxva2.DestroyPhysicalMonitor(pm.hPhysicalMonitor)

    if not changed:
        return {"error": "no monitor accepted a brightness change (DDC/CI may "
                         "be disabled in the monitor's on-screen menu)",
                "skipped": skipped}
    result = {"percent": level, "monitors_changed": changed}
    if skipped:
        result["skipped"] = skipped
    return result


@tool(destructive=True, category="media")
def set_app_volume(process_name: str, percent: int) -> dict:
    """Set the volume of one application without touching the system volume.

    Args:
        process_name: Executable name, e.g. "spotify.exe".
        percent: Target volume from 0 to 100.
    """
    from pycaw.pycaw import AudioUtilities

    needle = process_name.lower().removesuffix(".exe")
    level = max(0, min(100, int(percent)))
    changed = []

    for session in AudioUtilities.GetAllSessions():
        if session.Process is None:
            continue
        name = session.Process.name().lower().removesuffix(".exe")
        if name == needle:
            session.SimpleAudioVolume.SetMasterVolume(level / 100.0, None)
            changed.append(session.Process.name())

    if not changed:
        return {"error": f"no audio session for '{process_name}'"}
    return {"applications": changed, "percent": level}
