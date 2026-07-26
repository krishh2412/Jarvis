"""The machine map: JARVIS's structured model of this specific computer.

An assistant that guesses at your machine is worse than useless — it opens the
wrong app, looks in the wrong folder, assumes hardware you don't have. This
module builds a real, indexed picture of the actual PC and saves it as JSON that
the assistant reads back on every launch. Built once, refreshed on demand or on
a schedule.

Four things are worth knowing:

- **apps** — every installed application: its display name, the executable it
  launches, spoken aliases, and how to start it. This is what makes "open my
  browser" resolve to the right binary instead of a shell error.
- **folders** — where things actually live (Desktop, Downloads, projects), not
  where Windows says they should.
- **hardware** — CPU, RAM, GPU, disks, screen resolution. So JARVIS reasons
  about the real machine, not a generic one.
- **os** — version, default browser, installed shells and runtimes.

The build is deliberately fast: it reads Start Menu shortcuts and known folders,
never crawls the whole disk. A full scan on this machine is well under a second.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

import psutil

from jarvis.config import DATA_DIR

MAP_FILE = DATA_DIR / "memory" / "machine_map.json"

# Runtimes/shells worth knowing are present, so JARVIS never suggests using one
# that isn't installed. Probed with `which`, which is instant.
_RUNTIME_PROBES = {
    "python": ["python", "--version"],
    "pip": ["pip", "--version"],
    "node": ["node", "--version"],
    "npm": ["npm", "--version"],
    "git": ["git", "--version"],
    "powershell": ["powershell", "-NoProfile", "-Command", "$PSVersionTable.PSVersion.ToString()"],
    "pwsh": ["pwsh", "--version"],
    "docker": ["docker", "--version"],
    "ollama": ["ollama", "--version"],
    "cargo": ["cargo", "--version"],
    "go": ["go", "version"],
}

_START_MENU_DIRS = [
    Path(os.environ.get("APPDATA", "")) / "Microsoft/Windows/Start Menu/Programs",
    Path(os.environ.get("PROGRAMDATA", "")) / "Microsoft/Windows/Start Menu/Programs",
]

# Start Menu entries that are noise, not apps people ask for.
_APP_SKIP = {"uninstall", "readme", "read me", "help", "documentation", "website",
             "homepage", "changelog", "license", "release notes"}


# ---- hardware --------------------------------------------------------------


def _cpu_name() -> str:
    """The marketing CPU name ('Intel Core Ultra 7 265KF'), not the bare family."""
    try:
        import winreg
        key = r"HARDWARE\DESCRIPTION\System\CentralProcessor\0"
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key) as h:
            name, _ = winreg.QueryValueEx(h, "ProcessorNameString")
            return name.strip()
    except OSError:
        return platform.processor() or "unknown"


def _gpu() -> dict[str, Any] | None:
    try:
        proc = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total,driver_version",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            name, vram, driver = [p.strip() for p in
                                  proc.stdout.strip().splitlines()[0].split(",")]
            return {"name": name, "vram_gb": round(int(vram) / 1024, 1),
                    "driver": driver}
    except (OSError, subprocess.TimeoutExpired, ValueError):
        pass
    return None


def _screens() -> dict[str, Any]:
    import ctypes
    user32 = ctypes.windll.user32
    user32.SetProcessDPIAware()
    monitors: list[dict[str, int]] = []

    from ctypes import wintypes

    def _cb(hmon, _hdc, _rect, _param):
        info = _MonInfo()
        info.cbSize = ctypes.sizeof(_MonInfo)
        if user32.GetMonitorInfoW(hmon, ctypes.byref(info)):
            r = info.rcMonitor
            monitors.append({"width": r.right - r.left,
                             "height": r.bottom - r.top,
                             "primary": bool(info.dwFlags & 1)})
        return 1

    class _MonInfo(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                    ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]

    proc = ctypes.WINFUNCTYPE(
        wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC,
        ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)(_cb)
    user32.EnumDisplayMonitors(0, 0, proc, 0)
    return {"count": len(monitors), "displays": monitors}


def _hardware() -> dict[str, Any]:
    mem = psutil.virtual_memory()
    disks = []
    for part in psutil.disk_partitions(all=False):
        try:
            usage = psutil.disk_usage(part.mountpoint)
        except (PermissionError, OSError):
            continue
        disks.append({"drive": part.device.rstrip("\\"),
                      "total_gb": round(usage.total / 1024**3),
                      "free_gb": round(usage.free / 1024**3),
                      "fstype": part.fstype})
    out = {
        "cpu": _cpu_name(),
        "cpu_cores_physical": psutil.cpu_count(logical=False),
        "cpu_cores_logical": psutil.cpu_count(logical=True),
        "ram_gb": round(mem.total / 1024**3),
        "disks": disks,
        "screens": _screens(),
    }
    gpu = _gpu()
    if gpu:
        out["gpu"] = gpu
    return out


# ---- os --------------------------------------------------------------------


def _default_browser() -> str:
    """The default browser's friendly name, via the shell association API.

    The HKCU UrlAssociations\\UserChoice key is the documented location, but it
    is absent on some Windows 11 builds, so this asks the shell directly with
    AssocQueryString — the same lookup Explorer uses. argtypes are declared
    because the ASSOCSTR argument is an enum, not a pointer, and the default
    ctypes marshalling dereferences it and access-violates.
    """
    import ctypes
    from ctypes import wintypes

    try:
        fn = ctypes.windll.Shlwapi.AssocQueryStringW
    except (OSError, AttributeError):
        return "unknown"
    fn.restype = ctypes.c_long
    fn.argtypes = [wintypes.DWORD, ctypes.c_int, wintypes.LPCWSTR,
                   wintypes.LPCWSTR, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    ASSOCSTR_FRIENDLYAPPNAME = 4
    buf = ctypes.create_unicode_buffer(1024)
    size = wintypes.DWORD(1024)
    hr = fn(0, ASSOCSTR_FRIENDLYAPPNAME, "http", None, buf, ctypes.byref(size))
    return buf.value if hr == 0 and buf.value else "unknown"


def _runtimes() -> dict[str, str]:
    found: dict[str, str] = {}
    for name, cmd in _RUNTIME_PROBES.items():
        if shutil.which(cmd[0]) is None:
            continue
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=6)
            version = (proc.stdout or proc.stderr).strip().splitlines()
            first = version[0].strip() if version else "present"
            # A tool that is installed but not currently serving (e.g. Ollama
            # with no daemon up) prints a warning to its version query; record
            # that it is present rather than storing the warning as a version.
            found[name] = "present" if first.lower().startswith(("warning", "error")) else first
        except (OSError, subprocess.TimeoutExpired):
            found[name] = "present"
    return found


def _os_info() -> dict[str, Any]:
    return {
        "platform": platform.platform(),
        "release": platform.win32_ver()[0] if platform.system() == "Windows" else platform.release(),
        "version": platform.version(),
        "hostname": platform.node(),
        "user": os.environ.get("USERNAME", "unknown"),
        "default_browser": _default_browser(),
        "python": platform.python_version(),
        "runtimes": _runtimes(),
    }


# ---- apps ------------------------------------------------------------------


def _resolve_lnk_target(lnk: Path) -> str | None:
    """Read the .exe a Start Menu shortcut points at, if any."""
    try:
        import win32com.client
        shell = win32com.client.Dispatch("WScript.Shell")
        target = shell.CreateShortcut(str(lnk)).TargetPath
        if target and target.lower().endswith(".exe") and Path(target).exists():
            return target
    except Exception:  # noqa: BLE001 - COM is flaky; a missing target is fine
        pass
    return None


def _aliases(name: str, exe: str | None) -> list[str]:
    """Spoken names someone might use for an app."""
    aliases = {name.lower()}
    # Individual significant words: "Visual Studio Code" -> code, studio, visual.
    for word in name.lower().replace("-", " ").split():
        if len(word) > 2 and word not in {"the", "for", "and"}:
            aliases.add(word)
    if exe:
        aliases.add(Path(exe).stem.lower())
    aliases.discard("")
    return sorted(aliases)


def _apps() -> list[dict[str, Any]]:
    """Index installed apps from Start Menu shortcuts, resolving their targets."""
    seen: dict[str, dict[str, Any]] = {}
    for base in _START_MENU_DIRS:
        if not base.is_dir():
            continue
        for lnk in base.rglob("*.lnk"):
            stem = lnk.stem.strip()
            low = stem.lower()
            if not stem or any(skip in low for skip in _APP_SKIP):
                continue
            # First shortcut for a given name wins; user Start Menu is scanned
            # before ProgramData so per-user installs take precedence.
            if low in seen:
                continue
            exe = _resolve_lnk_target(lnk)
            seen[low] = {
                "name": stem,
                "exe": exe,
                "aliases": _aliases(stem, exe),
                "shortcut": str(lnk),
            }
    return sorted(seen.values(), key=lambda a: a["name"].lower())


# ---- folders ---------------------------------------------------------------


def _known_folders() -> dict[str, str]:
    """Standard user folders, resolved from the registry rather than assumed.

    Reads the real path each folder points at — a Downloads that has been moved
    to another drive is recorded where it actually is, not where Windows defaults.
    """
    out: dict[str, str] = {}
    try:
        import winreg
        key = (r"Software\Microsoft\Windows\CurrentVersion\Explorer"
               r"\Shell Folders")
        wanted = {"Desktop": "Desktop", "{374DE290-123F-4565-9164-39C4925E467B}": "Downloads",
                  "Personal": "Documents", "My Pictures": "Pictures",
                  "My Video": "Videos", "My Music": "Music"}
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as h:
            for reg_name, label in wanted.items():
                try:
                    value, _ = winreg.QueryValueEx(h, reg_name)
                    if value and Path(value).exists():
                        out[label] = value
                except OSError:
                    continue
    except OSError:
        pass
    return out


def _project_dirs() -> list[str]:
    """Find where code actually lives: directories containing project markers.

    Scans a shallow set of likely roots (home, and the top level of each fixed
    drive) looking for folders that hold a .git, package.json, or similar. Cheap
    and shallow on purpose — this learns where Sir keeps work, it does not crawl
    the disk.
    """
    markers = {".git", "package.json", "requirements.txt", "pyproject.toml",
               "Cargo.toml", "go.mod", ".venv", "node_modules"}
    roots: list[Path] = []
    home = Path.home()
    for candidate in (home, home / "source", home / "Documents",
                      home / "Projects", home / "dev"):
        if candidate.is_dir():
            roots.append(candidate)
    for part in psutil.disk_partitions(all=False):
        drive = Path(part.device)
        if part.fstype and drive.exists():
            for name in ("Projects", "Code", "Dev", "src", "repos", "Work"):
                if (drive / name).is_dir():
                    roots.append(drive / name)

    found: set[str] = set()
    for root in roots:
        try:
            for child in root.iterdir():
                if not child.is_dir():
                    continue
                try:
                    if any((child / m).exists() for m in markers):
                        found.add(str(child))
                except OSError:
                    continue
        except OSError:
            continue
    return sorted(found)


def _folders() -> dict[str, Any]:
    out: dict[str, Any] = {"known": _known_folders()}
    projects = _project_dirs()
    if projects:
        out["projects"] = projects
    return out


# ---- build / load / summarise ----------------------------------------------


def build(refresh: bool = True) -> dict[str, Any]:
    """Scan the machine and write the map to disk. Returns the map."""
    now = datetime.now().isoformat(timespec="seconds")
    existing = load() or {}
    machine_map = {
        "built": existing.get("built", now),
        "refreshed": now,
        "hardware": _hardware(),
        "os": _os_info(),
        "apps": _apps(),
        "folders": _folders(),
    }
    MAP_FILE.parent.mkdir(parents=True, exist_ok=True)
    MAP_FILE.write_text(json.dumps(machine_map, indent=2, ensure_ascii=False),
                        encoding="utf-8")
    return machine_map


def load() -> dict[str, Any] | None:
    """Read the saved map, or None if it has never been built."""
    if not MAP_FILE.exists():
        return None
    try:
        return json.loads(MAP_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


# Generic words people use for a *kind* of app rather than a specific one.
# Each maps to name fragments that identify apps of that kind; "browser" is
# special-cased to prefer the actual system default.
_CONCEPTS: dict[str, list[str]] = {
    "browser": ["brave", "chrome", "firefox", "edge", "opera"],
    "editor": ["code", "sublime", "notepad++", "notepad", "vim", "atom", "pycharm"],
    "code editor": ["code", "sublime", "pycharm", "notepad++"],
    "terminal": ["terminal", "powershell", "command prompt", "cmd", "wt"],
    "ide": ["visual studio", "pycharm", "intellij", "code"],
    "music": ["spotify", "music", "itunes", "foobar"],
    "video editor": ["davinci", "premiere", "capcut", "vegas", "shotcut"],
    "photo editor": ["photoshop", "gimp", "affinity", "paint"],
    "file manager": ["explorer", "files"],
}


def find_app(query: str, machine_map: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Fuzzy-match installed apps against a spoken query, best first.

    Handles both specific names ("spotify") and generic categories ("browser",
    "editor") — the latter via a small concept map, with "browser" preferring
    the system default browser recorded in the map.
    """
    machine_map = machine_map or load() or {}
    needle = query.lower().strip()

    # Concept queries: return apps of the requested kind, in preference order.
    concept = _CONCEPTS.get(needle)
    if concept:
        apps = machine_map.get("apps", [])
        prefer = concept[:]
        if needle == "browser":
            default = machine_map.get("os", {}).get("default_browser", "").lower()
            for frag in list(prefer):
                if frag in default:
                    prefer.remove(frag)
                    prefer.insert(0, frag)
        results: list[dict[str, Any]] = []
        for frag in prefer:
            for app in apps:
                hay = app["name"].lower() + " " + " ".join(app.get("aliases", []))
                if frag in hay and app not in results:
                    results.append(app)
        if results:
            return results[:8]

    scored: list[tuple[int, dict[str, Any]]] = []
    for app in machine_map.get("apps", []):
        name = app["name"].lower()
        aliases = app.get("aliases", [])
        if needle == name or needle in aliases:
            score = 100
        elif needle in name:
            score = 60 - len(name)
        elif any(needle in a for a in aliases):
            score = 40
        elif any(a in needle for a in aliases if len(a) > 3):
            score = 30
        else:
            score = 0
        if score:
            scored.append((score, app))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [app for _, app in scored[:8]]


def summary(machine_map: dict[str, Any] | None = None, max_apps: int = 40) -> str:
    """A compact, human-readable digest for the system prompt.

    Small on purpose: the full map is large and would blow the context and the
    prompt cache. This is the shape of the machine, plus the names of the apps
    that exist — enough to reason and to know what to look up in detail.
    """
    machine_map = machine_map or load()
    if not machine_map:
        return ""

    hw = machine_map.get("hardware", {})
    os_i = machine_map.get("os", {})
    folders = machine_map.get("folders", {})
    apps = machine_map.get("apps", [])

    lines = ["## This machine (from the machine map)"]

    gpu = hw.get("gpu", {})
    disks = ", ".join(f"{d['drive']} {d['free_gb']}/{d['total_gb']} GB free"
                      for d in hw.get("disks", []))
    lines.append(
        f"- Hardware: {hw.get('cpu', '?')}, {hw.get('ram_gb', '?')} GB RAM"
        + (f", {gpu.get('name')} ({gpu.get('vram_gb')} GB)" if gpu else "")
    )
    if disks:
        # Flagged as a snapshot on purpose: free space changes by the hour, and
        # an assistant that quotes a week-old figure as the current one is worse
        # than one that checks. Specifications may be recited; measurements are taken.
        lines.append(f"- Disks (free space as of the last scan — call system_stats "
                     f"for current figures): {disks}")
    screens = hw.get("screens", {})
    if screens.get("displays"):
        res = ", ".join(f"{d['width']}x{d['height']}" for d in screens["displays"])
        lines.append(f"- Displays: {screens.get('count')} ({res})")
    lines.append(f"- OS: {os_i.get('platform', '?')}; default browser "
                 f"{os_i.get('default_browser', '?')}")
    runtimes = os_i.get("runtimes", {})
    if runtimes:
        lines.append("- Runtimes: " + ", ".join(sorted(runtimes)))

    known = folders.get("known", {})
    if known:
        lines.append("- Folders: " + ", ".join(f"{k} at {v}" for k, v in known.items()))
    if folders.get("projects"):
        lines.append("- Project folders: " + ", ".join(folders["projects"][:12]))

    if apps:
        # Deliberately NOT listing every app name here — 150+ names is ~1k tokens
        # of standing prompt for little gain. The count tells the model what is
        # available; find_installed_app / machine_overview fetch specifics on
        # demand, which keeps the prompt lean and the context budget intact.
        names = [a["name"] for a in apps]
        lines.append(
            f"- {len(names)} applications are installed and indexed. To launch or "
            "check one, use find_installed_app to resolve the real name, or "
            "machine_overview to see them all; refresh_machine_map if one is missing."
        )
    return "\n".join(lines)
