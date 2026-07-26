"""Append-only audit log of every tool JARVIS invokes.

Running with no confirmation prompts means the log is the only record of what
happened. It is written synchronously and flushed on every call, so a crash
mid-action still leaves a trace of what was attempted.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone
from typing import Any

from jarvis.config import LOG_DIR

_lock = threading.Lock()
_MAX_FIELD = 2000  # truncate huge payloads so the log stays greppable


def _log_path() -> str:
    day = datetime.now().strftime("%Y-%m-%d")
    return str(LOG_DIR / f"audit-{day}.jsonl")


def _clip(value: Any) -> Any:
    """Shrink oversized values so one screenshot cannot bloat the log."""
    if isinstance(value, str) and len(value) > _MAX_FIELD:
        return value[:_MAX_FIELD] + f"... <{len(value) - _MAX_FIELD} more chars>"
    if isinstance(value, dict):
        return {k: _clip(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clip(v) for v in value[:50]]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return repr(value)[:_MAX_FIELD]


def record(
    tool: str,
    args: dict[str, Any],
    result: Any = None,
    error: str | None = None,
    duration_ms: float | None = None,
    destructive: bool = False,
) -> None:
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        "tool": tool,
        "destructive": destructive,
        "args": _clip(args),
        "ok": error is None,
    }
    if duration_ms is not None:
        entry["ms"] = round(duration_ms, 1)
    if error is not None:
        entry["error"] = _clip(error)
    else:
        entry["result"] = _clip(result)

    line = json.dumps(entry, ensure_ascii=False, default=str)
    with _lock:
        try:
            with open(_log_path(), "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
                fh.flush()
        except OSError:
            # Auditing must never take the assistant down with it.
            pass


def tail(limit: int = 50) -> list[dict[str, Any]]:
    """Most recent entries from today's log, newest last."""
    try:
        with open(_log_path(), "r", encoding="utf-8") as fh:
            lines = fh.readlines()[-limit:]
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


class Timer:
    """Context manager measuring wall time in milliseconds."""

    def __enter__(self) -> "Timer":
        self._start = time.perf_counter()
        self.ms = 0.0
        return self

    def __exit__(self, *exc: object) -> None:
        self.ms = (time.perf_counter() - self._start) * 1000
