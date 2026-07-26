"""System-level control: shell execution, processes, power, clipboard."""

from __future__ import annotations

import os
import platform
import subprocess
from datetime import datetime
from typing import Literal

import psutil

from jarvis.tools.registry import tool

# Shell calls are capped rather than unbounded: a command that hangs would
# otherwise wedge the whole assistant, since tool execution is synchronous.
DEFAULT_TIMEOUT = 60
MAX_OUTPUT = 12000  # chars returned to the model before truncation


def _truncate(text: str) -> str:
    if len(text) <= MAX_OUTPUT:
        return text
    half = MAX_OUTPUT // 2
    omitted = len(text) - MAX_OUTPUT
    return f"{text[:half]}\n\n... <{omitted} characters omitted> ...\n\n{text[-half:]}"


@tool(destructive=True, category="system")
def run_powershell(command: str, timeout: int = DEFAULT_TIMEOUT) -> dict:
    """Run a PowerShell command and return its output.

    This is the general-purpose escape hatch: anything Windows can do from a
    terminal is reachable here. Prefer a dedicated tool when one exists, since
    they return cleaner structured data.

    Args:
        command: The PowerShell command to execute.
        timeout: Seconds to wait before giving up.
    """
    proc = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True,
        text=True,
        timeout=timeout,
        encoding="utf-8",
        errors="replace",
    )
    return {
        "exit_code": proc.returncode,
        "stdout": _truncate(proc.stdout or ""),
        "stderr": _truncate(proc.stderr or ""),
    }


@tool(destructive=True, category="system")
def run_command(command: str, cwd: str = "", timeout: int = DEFAULT_TIMEOUT) -> dict:
    """Run a shell command via cmd.exe and return its output.

    Args:
        command: The command line to execute.
        cwd: Working directory. Empty string uses the current directory.
        timeout: Seconds to wait before giving up.
    """
    proc = subprocess.run(
        command,
        shell=True,
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=cwd or None,
        encoding="utf-8",
        errors="replace",
    )
    return {
        "exit_code": proc.returncode,
        "stdout": _truncate(proc.stdout or ""),
        "stderr": _truncate(proc.stderr or ""),
    }


@tool(category="system")
def system_stats() -> dict:
    """Report CPU, memory, disk and GPU utilisation."""
    mem = psutil.virtual_memory()
    stats = {
        "cpu_percent": psutil.cpu_percent(interval=0.3),
        "cpu_cores": psutil.cpu_count(logical=False),
        "ram_used_gb": round(mem.used / 1024**3, 1),
        "ram_total_gb": round(mem.total / 1024**3, 1),
        "ram_percent": mem.percent,
        "uptime_hours": round(
            (datetime.now().timestamp() - psutil.boot_time()) / 3600, 1
        ),
        "disks": [],
    }

    for part in psutil.disk_partitions(all=False):
        try:
            usage = psutil.disk_usage(part.mountpoint)
        except (PermissionError, OSError):
            continue  # empty optical/card readers raise here
        stats["disks"].append(
            {
                "drive": part.device,
                "used_gb": round(usage.used / 1024**3, 1),
                "total_gb": round(usage.total / 1024**3, 1),
                "percent": usage.percent,
            }
        )

    try:
        proc = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True, text=True, timeout=5,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            name, util, used, total, temp = [
                p.strip() for p in proc.stdout.strip().splitlines()[0].split(",")
            ]
            stats["gpu"] = {
                "name": name,
                "utilisation_percent": int(util),
                "vram_used_mb": int(used),
                "vram_total_mb": int(total),
                "temperature_c": int(temp),
            }
    except (OSError, subprocess.TimeoutExpired, ValueError):
        pass  # no NVIDIA GPU, or driver not responding

    return stats


@tool(category="system")
def list_processes(sort_by: Literal["cpu", "memory", "name"] = "memory",
                   limit: int = 15) -> list[dict]:
    """List running processes, heaviest first.

    Args:
        sort_by: Ranking metric.
        limit: How many processes to return.
    """
    procs = []
    for proc in psutil.process_iter(["pid", "name", "memory_info", "cpu_percent"]):
        try:
            info = proc.info
            procs.append(
                {
                    "pid": info["pid"],
                    "name": info["name"],
                    "memory_mb": round(info["memory_info"].rss / 1024**2, 1)
                    if info["memory_info"] else 0.0,
                    "cpu_percent": info["cpu_percent"] or 0.0,
                }
            )
        except (psutil.NoSuchProcess, psutil.AccessDenied, AttributeError):
            continue

    key = {"cpu": "cpu_percent", "memory": "memory_mb", "name": "name"}[sort_by]
    procs.sort(key=lambda p: p[key], reverse=(sort_by != "name"))
    return procs[:limit]


@tool(destructive=True, category="system")
def kill_process(name_or_pid: str, force: bool = False) -> dict:
    """Terminate a process by name or PID.

    Args:
        name_or_pid: Process name (e.g. "chrome.exe") or numeric PID. A name
            kills every matching process.
        force: Skip the graceful terminate and kill immediately.
    """
    killed, failed = [], []
    targets: list[psutil.Process] = []

    if name_or_pid.isdigit():
        try:
            targets.append(psutil.Process(int(name_or_pid)))
        except psutil.NoSuchProcess:
            return {"error": f"no process with PID {name_or_pid}"}
    else:
        needle = name_or_pid.lower().removesuffix(".exe")
        for proc in psutil.process_iter(["pid", "name"]):
            pname = (proc.info["name"] or "").lower().removesuffix(".exe")
            if pname == needle:
                targets.append(proc)

    if not targets:
        return {"error": f"no process matching '{name_or_pid}'"}

    for proc in targets:
        try:
            label = f"{proc.name()} ({proc.pid})"
            proc.kill() if force else proc.terminate()
            killed.append(label)
        except (psutil.NoSuchProcess, psutil.AccessDenied) as exc:
            failed.append(f"{proc.pid}: {type(exc).__name__}")

    psutil.wait_procs(targets, timeout=3)
    return {"killed": killed, "failed": failed}


@tool(destructive=True, category="system")
def power_action(action: Literal["lock", "sleep", "shutdown", "restart",
                                 "sign_out"]) -> str:
    """Lock, sleep, restart, shut down or sign out of Windows.

    Shutdown and restart are issued with a 10 second delay so they can be
    cancelled with `run_command("shutdown /a")` if the request was misheard.

    Args:
        action: Which power action to perform.
    """
    commands = {
        "lock": "rundll32.exe user32.dll,LockWorkStation",
        "sleep": "rundll32.exe powrprof.dll,SetSuspendState 0,1,0",
        "shutdown": "shutdown /s /t 10",
        "restart": "shutdown /r /t 10",
        "sign_out": "shutdown /l",
    }
    subprocess.Popen(commands[action], shell=True)
    if action in ("shutdown", "restart"):
        return f"{action} scheduled in 10 seconds; 'shutdown /a' aborts it"
    return f"{action} issued"


@tool(category="system")
def get_datetime() -> dict:
    """Get the current local date, time, timezone and day of week."""
    now = datetime.now().astimezone()
    return {
        "iso": now.isoformat(timespec="seconds"),
        "spoken": now.strftime("%-I:%M %p on %A, %-d %B %Y")
        if platform.system() != "Windows"
        else now.strftime("%I:%M %p on %A, %d %B %Y").lstrip("0"),
        "timezone": str(now.tzinfo),
    }


@tool(category="system")
def read_clipboard() -> str:
    """Read the current contents of the Windows clipboard."""
    import pyperclip
    return _truncate(pyperclip.paste() or "")


@tool(destructive=True, category="system")
def write_clipboard(text: str) -> str:
    """Replace the Windows clipboard contents.

    Args:
        text: Text to place on the clipboard.
    """
    import pyperclip
    pyperclip.copy(text)
    return f"copied {len(text)} characters to clipboard"


@tool(category="system")
def environment_info() -> dict:
    """Report OS version, hostname, username and Python version."""
    return {
        "os": platform.platform(),
        "hostname": platform.node(),
        "user": os.environ.get("USERNAME", "unknown"),
        "python": platform.python_version(),
        "cpu": platform.processor(),
    }
