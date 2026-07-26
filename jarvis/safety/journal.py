"""Undo journal for destructive filesystem operations.

The user chose full autonomy: no tool asks permission. That is workable only
because nothing is truly destroyed. Deletes and overwrites move the original
into a timestamped trash store and append a journal entry, so ``undo`` can put
it back. Entries expire after ``safety.trash_retention_days``.
"""

from __future__ import annotations

import json
import shutil
import threading
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from jarvis.config import DATA_DIR, TRASH_DIR, config

JOURNAL_FILE = DATA_DIR / "journal.jsonl"
_lock = threading.RLock()

Action = Literal["delete", "overwrite", "move"]


def _append(entry: dict[str, Any]) -> None:
    with open(JOURNAL_FILE, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        fh.flush()


def _read_all() -> list[dict[str, Any]]:
    if not JOURNAL_FILE.exists():
        return []
    entries = []
    with open(JOURNAL_FILE, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return entries


def _rewrite(entries: list[dict[str, Any]]) -> None:
    tmp = JOURNAL_FILE.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        for entry in entries:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    tmp.replace(JOURNAL_FILE)


def stash(path: str | Path, action: Action = "delete") -> str | None:
    """Move ``path`` into the trash store and journal it.

    Returns the journal entry id, or None if the path did not exist. Callers
    should treat a successful stash as "the original is gone" — that is the
    whole point — and rely on :func:`undo` to reverse it.
    """
    src = Path(path)
    if not src.exists():
        return None

    entry_id = uuid.uuid4().hex[:12]
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    bucket = TRASH_DIR / f"{stamp}-{entry_id}"
    bucket.mkdir(parents=True, exist_ok=True)
    dest = bucket / src.name

    with _lock:
        # copy2 + unlink rather than move: a cross-volume move on Windows can
        # fail partway and leave neither copy intact.
        if src.is_dir():
            shutil.copytree(src, dest, symlinks=True)
            shutil.rmtree(src, ignore_errors=False)
        else:
            shutil.copy2(src, dest)
            src.unlink()

        _append(
            {
                "id": entry_id,
                "ts": datetime.now().isoformat(timespec="seconds"),
                "action": action,
                "original": str(src.resolve() if src.parent.exists() else src),
                "stashed": str(dest),
                "is_dir": dest.is_dir(),
                "undone": False,
            }
        )
    return entry_id


def undo(entry_id: str | None = None) -> dict[str, Any]:
    """Restore a stashed item. With no id, reverses the most recent entry."""
    with _lock:
        entries = _read_all()
        pending = [e for e in entries if not e.get("undone")]
        if not pending:
            return {"ok": False, "error": "nothing to undo"}

        if entry_id:
            target = next((e for e in pending if e["id"] == entry_id), None)
            if target is None:
                return {"ok": False, "error": f"no pending entry {entry_id}"}
        else:
            target = pending[-1]

        stashed = Path(target["stashed"])
        original = Path(target["original"])
        if not stashed.exists():
            return {"ok": False, "error": "stashed copy is gone from the trash store"}
        if original.exists():
            return {"ok": False, "error": f"{original} exists again; refusing to clobber"}

        original.parent.mkdir(parents=True, exist_ok=True)
        if stashed.is_dir():
            shutil.copytree(stashed, original, symlinks=True)
            shutil.rmtree(stashed, ignore_errors=True)
        else:
            shutil.copy2(stashed, original)
            stashed.unlink()

        target["undone"] = True
        target["undone_ts"] = datetime.now().isoformat(timespec="seconds")
        _rewrite(entries)

    return {"ok": True, "restored": str(original), "id": target["id"]}


def history(limit: int = 20) -> list[dict[str, Any]]:
    """Recent journal entries, newest first."""
    return list(reversed(_read_all()))[:limit]


def purge_expired() -> int:
    """Drop trash buckets past the retention window. Returns count removed."""
    cutoff = datetime.now() - timedelta(days=config.safety.trash_retention_days)
    removed = 0
    with _lock:
        entries = _read_all()
        keep = []
        for entry in entries:
            try:
                ts = datetime.fromisoformat(entry["ts"])
            except (KeyError, ValueError):
                keep.append(entry)
                continue
            if ts < cutoff:
                bucket = Path(entry["stashed"]).parent
                shutil.rmtree(bucket, ignore_errors=True)
                removed += 1
            else:
                keep.append(entry)
        if removed:
            _rewrite(keep)
    return removed


def trash_size_mb() -> float:
    total = 0
    for path in TRASH_DIR.rglob("*"):
        if path.is_file():
            try:
                total += path.stat().st_size
            except OSError:
                continue
    return round(total / (1024 * 1024), 1)


def is_protected(path: str | Path) -> str | None:
    """Return the matching protected root if ``path`` sits inside one."""
    try:
        resolved = Path(path).resolve()
    except (OSError, ValueError):
        return None
    for root in config.safety.protected_paths:
        try:
            resolved.relative_to(Path(root).resolve())
        except (ValueError, OSError):
            continue
        return root
    return None
