"""Application and window control."""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from typing import Literal

from jarvis.tools.registry import tool

# Spoken names mapped onto what Windows actually needs to launch them.
# Anything not listed falls back to a Start Menu shortcut search.
APP_ALIASES: dict[str, str] = {
    "chrome": "chrome.exe",
    "google chrome": "chrome.exe",
    "edge": "msedge.exe",
    "firefox": "firefox.exe",
    "explorer": "explorer.exe",
    "file explorer": "explorer.exe",
    "notepad": "notepad.exe",
    "calculator": "calc.exe",
    "calc": "calc.exe",
    "paint": "mspaint.exe",
    "task manager": "taskmgr.exe",
    "control panel": "control.exe",
    "settings": "ms-settings:",
    "terminal": "wt.exe",
    "windows terminal": "wt.exe",
    "powershell": "powershell.exe",
    "cmd": "cmd.exe",
    "command prompt": "cmd.exe",
    "vscode": "code",
    "vs code": "code",
    "visual studio code": "code",
    "spotify": "spotify.exe",
    "discord": "discord.exe",
    "steam": "steam.exe",
    "word": "winword.exe",
    "excel": "excel.exe",
    "powerpoint": "powerpnt.exe",
    "outlook": "outlook.exe",
    "snipping tool": "snippingtool.exe",
}

START_MENU_DIRS = [
    Path(os.environ.get("APPDATA", "")) / "Microsoft/Windows/Start Menu/Programs",
    Path(os.environ.get("PROGRAMDATA", "")) / "Microsoft/Windows/Start Menu/Programs",
]


def _find_shortcut(name: str) -> str | None:
    """Look for a Start Menu .lnk whose name contains the request."""
    needle = name.lower()
    best: tuple[int, str] | None = None
    for base in START_MENU_DIRS:
        if not base.is_dir():
            continue
        for lnk in base.rglob("*.lnk"):
            stem = lnk.stem.lower()
            if needle == stem:
                return str(lnk)
            if needle in stem:
                # Prefer the shortest containing match: "Steam" over
                # "Steam Cleanup Utility".
                score = len(stem)
                if best is None or score < best[0]:
                    best = (score, str(lnk))
    return best[1] if best else None


@tool(destructive=True, category="apps")
def launch_app(name: str, arguments: str = "") -> dict:
    """Open an application by name.

    Resolves common spoken names ("chrome", "spotify") and otherwise searches
    the Start Menu.

    Args:
        name: Application name, executable, or full path.
        arguments: Optional command-line arguments to pass.
    """
    key = name.lower().strip()
    aliased = APP_ALIASES.get(key)

    def _run(path_or_cmd: str) -> None:
        subprocess.Popen(f'start "" "{path_or_cmd}" {arguments}'.rstrip(), shell=True)

    # 1. A full path given directly wins over everything.
    direct = Path(os.path.expandvars(os.path.expanduser(name)))
    if direct.exists():
        _run(str(direct))
        return {"launched": str(direct), "via": "path"}

    # 2. Protocol aliases (ms-settings:, and the like) open directly — there is
    #    no file and no shortcut to find.
    if aliased and ":" in aliased and not aliased.lower().endswith(".exe"):
        subprocess.Popen(f'start "" {aliased} {arguments}'.rstrip(), shell=True)
        return {"launched": aliased, "via": "protocol"}

    # 3. Start Menu shortcut FIRST. This is the reliable path for installed
    #    apps whose executable is NOT registered in Windows' App Paths — Steam,
    #    Spotify, Discord, Playnite and most games. `start steam.exe` fails with
    #    a "Windows cannot find steam.exe" dialog precisely because steam.exe is
    #    not on the PATH or in App Paths; the Steam.lnk shortcut always works.
    shortcut = _find_shortcut(name)
    if shortcut:
        if arguments:
            subprocess.Popen(f'start "" "{shortcut}" {arguments}', shell=True)
        else:
            os.startfile(shortcut)  # noqa: S606 - launching a shortcut is the point
        return {"launched": Path(shortcut).stem, "via": "start_menu"}

    # 4. Resolve an executable to a real path WITHOUT firing the shell's
    #    "cannot find" dialog: check the PATH and the App Paths registry (which
    #    is how `start chrome` works). Only launch if we actually found the file.
    exe = aliased or name
    resolved = _resolve_executable(exe)
    if resolved:
        _run(resolved)
        return {"launched": Path(resolved).name, "via": "resolved"}

    # 5. Look in the usual install locations for well-known apps, so a missing
    #    shortcut still does not stop us.
    guess = _guess_install_path(key, exe)
    if guess:
        _run(guess)
        return {"launched": guess, "via": "install_dir"}

    return {
        "error": f"could not find an application named '{name}'. It has no Start "
                 "Menu entry, is not on the PATH, and is not in a standard "
                 "install location. Give me the full path to its .exe and I will "
                 "open it — and tell me to remember it for next time."
    }


def _resolve_executable(exe: str) -> str | None:
    """Find an executable's real path via PATH then the App Paths registry.

    Returns None instead of guessing, so the caller never blindly hands an
    unresolvable name to the shell (which would pop the Windows error dialog).
    """
    import shutil

    found = shutil.which(exe)
    if found:
        return found

    candidate = exe if exe.lower().endswith(".exe") else exe + ".exe"
    try:
        import winreg
    except ImportError:
        return None

    subkey = (
        r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\\" + candidate
    )
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(hive, subkey) as handle:
                value, _ = winreg.QueryValueEx(handle, None)
        except OSError:
            continue
        path = (value or "").strip().strip('"')
        if path and Path(path).exists():
            return path
    return None


# Where well-known apps install when they are not registered anywhere findable.
# {var} is expanded against the environment.
_INSTALL_HINTS: dict[str, list[str]] = {
    "steam": [r"{ProgramFiles(x86)}\Steam\steam.exe", r"{ProgramFiles}\Steam\steam.exe"],
    "epic": [r"{ProgramFiles(x86)}\Epic Games\Launcher\Portal\Binaries\Win64\EpicGamesLauncher.exe"],
    "epic games": [r"{ProgramFiles(x86)}\Epic Games\Launcher\Portal\Binaries\Win64\EpicGamesLauncher.exe"],
    "discord": [r"{LOCALAPPDATA}\Discord\Update.exe"],
    "spotify": [r"{APPDATA}\Spotify\Spotify.exe", r"{LOCALAPPDATA}\Microsoft\WindowsApps\Spotify.exe"],
}


def _guess_install_path(key: str, exe: str) -> str | None:
    """Check standard install locations for a well-known app."""
    def _expand(template: str) -> str:
        out = template
        for var in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA", "APPDATA"):
            out = out.replace("{" + var + "}", os.environ.get(var, ""))
        return out

    for candidate in _INSTALL_HINTS.get(key, []):
        path = _expand(candidate)
        if path and Path(path).exists():
            return path
    return None


@tool(category="apps")
def list_windows(visible_only: bool = True) -> list[dict]:
    """List open windows with their titles and owning processes.

    Args:
        visible_only: Skip windows with no title or that are hidden.
    """
    import win32con
    import win32gui
    import win32process
    import psutil

    windows = []

    def callback(hwnd: int, _: object) -> bool:
        if visible_only and not win32gui.IsWindowVisible(hwnd):
            return True
        title = win32gui.GetWindowText(hwnd)
        if visible_only and not title.strip():
            return True
        try:
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            proc_name = psutil.Process(pid).name()
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
            proc_name = "unknown"
        placement = win32gui.GetWindowPlacement(hwnd)
        state = {
            win32con.SW_SHOWMINIMIZED: "minimised",
            win32con.SW_SHOWMAXIMIZED: "maximised",
        }.get(placement[1], "normal")
        windows.append(
            {"hwnd": hwnd, "title": title, "process": proc_name, "state": state}
        )
        return True

    win32gui.EnumWindows(callback, None)
    return windows


def _resolve_window(title: str) -> int | None:
    """Find a window handle by fuzzy title match, preferring exact hits."""
    import win32gui

    needle = title.lower()
    exact: list[int] = []
    partial: list[tuple[int, int]] = []

    def callback(hwnd: int, _: object) -> bool:
        if not win32gui.IsWindowVisible(hwnd):
            return True
        text = win32gui.GetWindowText(hwnd)
        if not text:
            return True
        lowered = text.lower()
        if lowered == needle:
            exact.append(hwnd)
        elif needle in lowered:
            partial.append((len(text), hwnd))
        return True

    win32gui.EnumWindows(callback, None)
    if exact:
        return exact[0]
    if partial:
        return min(partial)[1]
    return None


@tool(destructive=True, category="apps")
def control_window(
    title: str,
    action: Literal["focus", "minimise", "maximise", "restore", "close"],
) -> dict:
    """Focus, minimise, maximise, restore or close a window by title.

    Args:
        title: Window title, or any distinctive part of it.
        action: What to do with the window.
    """
    import win32con
    import win32gui

    hwnd = _resolve_window(title)
    if hwnd is None:
        return {"error": f"no visible window matching '{title}'"}

    resolved_title = win32gui.GetWindowText(hwnd)

    if action == "close":
        win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
    elif action == "minimise":
        win32gui.ShowWindow(hwnd, win32con.SW_MINIMIZE)
    elif action == "maximise":
        win32gui.ShowWindow(hwnd, win32con.SW_MAXIMIZE)
    elif action == "restore":
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
    else:  # focus
        # A minimised window has to be restored before it can take focus.
        if win32gui.IsIconic(hwnd):
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        try:
            win32gui.SetForegroundWindow(hwnd)
        except Exception:  # noqa: BLE001
            # Windows blocks foreground stealing unless the caller owns the
            # foreground. Nudging it topmost then back is the usual workaround.
            win32gui.SetWindowPos(
                hwnd, win32con.HWND_TOPMOST, 0, 0, 0, 0,
                win32con.SWP_NOMOVE | win32con.SWP_NOSIZE,
            )
            win32gui.SetWindowPos(
                hwnd, win32con.HWND_NOTOPMOST, 0, 0, 0, 0,
                win32con.SWP_NOMOVE | win32con.SWP_NOSIZE,
            )

    return {"window": resolved_title, "action": action}


@tool(category="apps")
def active_window() -> dict:
    """Get the title and process of the currently focused window."""
    import win32gui
    import win32process
    import psutil

    hwnd = win32gui.GetForegroundWindow()
    title = win32gui.GetWindowText(hwnd)
    try:
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        proc = psutil.Process(pid)
        return {"title": title, "process": proc.name(), "pid": pid,
                "exe": proc.exe()}
    except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
        return {"title": title, "process": "unknown"}


@tool(category="apps")
def installed_apps(filter_text: str = "", limit: int = 60) -> list[str]:
    """List installed applications found in the Start Menu.

    Args:
        filter_text: Only return names containing this text.
        limit: Maximum number of names to return.
    """
    names: set[str] = set()
    needle = filter_text.lower()
    for base in START_MENU_DIRS:
        if not base.is_dir():
            continue
        for lnk in base.rglob("*.lnk"):
            if needle and needle not in lnk.stem.lower():
                continue
            names.add(lnk.stem)
    return sorted(names)[:limit]
