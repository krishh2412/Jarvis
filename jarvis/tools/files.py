"""Filesystem tools.

Destructive operations never hard-delete. They stash the original in the trash
store via the undo journal, which is what makes running without confirmation
prompts recoverable.
"""

from __future__ import annotations

import fnmatch
import os
import shutil
from datetime import datetime
from pathlib import Path

from jarvis.safety import journal
from jarvis.tools.registry import tool

MAX_READ_CHARS = 20000
MAX_RESULTS = 200

# Directories that make recursive search useless if traversed.
SKIP_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv", ".idea",
    ".vs", "dist", "build", ".next", "target", "AppData",
}


def _guard(path: str | Path) -> Path:
    """Resolve a path, refusing the handful that would brick Windows."""
    resolved = Path(os.path.expandvars(os.path.expanduser(str(path))))
    protected = journal.is_protected(resolved)
    if protected:
        raise PermissionError(
            f"{resolved} sits inside protected path {protected}; "
            "edit safety.protected_paths in config.json to allow this"
        )
    return resolved


@tool(category="files")
def read_file(path: str, max_chars: int = MAX_READ_CHARS) -> dict:
    """Read a text file's contents.

    Args:
        path: Path to the file. Environment variables and ~ are expanded.
        max_chars: Truncate the returned text beyond this length.
    """
    target = _guard(path)
    if not target.is_file():
        return {"error": f"not a file: {target}"}

    raw = target.read_text(encoding="utf-8", errors="replace")
    truncated = len(raw) > max_chars
    return {
        "path": str(target),
        "content": raw[:max_chars],
        "truncated": truncated,
        "total_chars": len(raw),
        "lines": raw.count("\n") + 1,
    }


@tool(destructive=True, category="files")
def write_file(path: str, content: str, append: bool = False) -> dict:
    """Write text to a file, creating parent directories as needed.

    An existing file is stashed to the trash store first, so an accidental
    overwrite can be reversed with undo_last_change.

    Args:
        path: Destination file path.
        content: Text to write.
        append: Append instead of replacing the file.
    """
    target = _guard(path)
    target.parent.mkdir(parents=True, exist_ok=True)

    entry_id = None
    if target.exists() and not append:
        # Stash removes the original, so capture the bytes we are replacing.
        entry_id = journal.stash(target, action="overwrite")

    with open(target, "a" if append else "w", encoding="utf-8") as fh:
        fh.write(content)

    return {
        "path": str(target),
        "bytes_written": len(content.encode("utf-8")),
        "mode": "append" if append else "overwrite",
        "undo_id": entry_id,
    }


@tool(category="files")
def list_directory(path: str = ".", pattern: str = "*",
                   include_hidden: bool = False) -> dict:
    """List the contents of a directory.

    Args:
        path: Directory to list.
        pattern: Glob filter, e.g. "*.py".
        include_hidden: Include dotfiles and hidden entries.
    """
    target = _guard(path)
    if not target.is_dir():
        return {"error": f"not a directory: {target}"}

    dirs, files = [], []
    for entry in sorted(target.iterdir(), key=lambda p: p.name.lower()):
        if not include_hidden and entry.name.startswith("."):
            continue
        if entry.is_dir():
            if fnmatch.fnmatch(entry.name, pattern) or pattern == "*":
                dirs.append(entry.name)
            continue
        if not fnmatch.fnmatch(entry.name, pattern):
            continue
        try:
            stat = entry.stat()
            files.append(
                {
                    "name": entry.name,
                    "size_kb": round(stat.st_size / 1024, 1),
                    "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(
                        timespec="minutes"
                    ),
                }
            )
        except OSError:
            continue

    return {
        "path": str(target),
        "directories": dirs[:MAX_RESULTS],
        "files": files[:MAX_RESULTS],
        "truncated": len(dirs) + len(files) > MAX_RESULTS,
    }


@tool(category="files")
def find_files(name_pattern: str, root: str = "", max_results: int = 50) -> dict:
    """Search for files by name pattern anywhere under a directory.

    Args:
        name_pattern: Glob to match against filenames, e.g. "*.pdf" or
            "invoice*".
        root: Directory to search from. Defaults to the user profile.
        max_results: Stop after this many matches.
    """
    base = _guard(root or os.environ.get("USERPROFILE", "C:/"))
    matches = []

    for dirpath, dirnames, filenames in os.walk(base, topdown=True):
        dirnames[:] = [
            d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")
        ]
        for filename in filenames:
            if fnmatch.fnmatch(filename.lower(), name_pattern.lower()):
                full = Path(dirpath) / filename
                try:
                    size = full.stat().st_size
                except OSError:
                    continue
                matches.append({"path": str(full), "size_kb": round(size / 1024, 1)})
                if len(matches) >= max_results:
                    return {"matches": matches, "truncated": True, "searched": str(base)}

    return {"matches": matches, "truncated": False, "searched": str(base)}


@tool(category="files")
def search_in_files(query: str, root: str, file_pattern: str = "*",
                    max_results: int = 40) -> dict:
    """Search file contents for a string, returning matching lines.

    Args:
        query: Text to search for. Case-insensitive.
        root: Directory to search under.
        file_pattern: Only search files matching this glob.
        max_results: Stop after this many matching lines.
    """
    base = _guard(root)
    needle = query.lower()
    hits = []

    for dirpath, dirnames, filenames in os.walk(base, topdown=True):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for filename in filenames:
            if not fnmatch.fnmatch(filename, file_pattern):
                continue
            full = Path(dirpath) / filename
            try:
                if full.stat().st_size > 5 * 1024 * 1024:
                    continue  # skip anything big enough to be a binary blob
                with open(full, "r", encoding="utf-8", errors="ignore") as fh:
                    for lineno, line in enumerate(fh, 1):
                        if needle in line.lower():
                            hits.append(
                                {
                                    "path": str(full),
                                    "line": lineno,
                                    "text": line.strip()[:300],
                                }
                            )
                            if len(hits) >= max_results:
                                return {"matches": hits, "truncated": True}
            except (OSError, UnicodeDecodeError):
                continue

    return {"matches": hits, "truncated": False}


@tool(destructive=True, category="files")
def delete_path(path: str) -> dict:
    """Delete a file or folder, recoverably.

    The target moves to the trash store rather than being erased, and can be
    restored with undo_last_change.

    Args:
        path: File or directory to delete.
    """
    target = _guard(path)
    if not target.exists():
        return {"error": f"does not exist: {target}"}

    entry_id = journal.stash(target, action="delete")
    return {
        "deleted": str(target),
        "undo_id": entry_id,
        "note": "moved to trash store; reversible via undo_last_change",
    }


@tool(destructive=True, category="files")
def move_path(source: str, destination: str) -> dict:
    """Move or rename a file or folder.

    Args:
        source: Existing path.
        destination: New path. A directory destination moves the item into it.
    """
    src = _guard(source)
    dst = _guard(destination)
    if not src.exists():
        return {"error": f"does not exist: {src}"}

    if dst.is_dir():
        dst = dst / src.name
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        return {"error": f"destination already exists: {dst}"}

    shutil.move(str(src), str(dst))
    return {"moved": str(src), "to": str(dst)}


@tool(destructive=True, category="files")
def copy_path(source: str, destination: str) -> dict:
    """Copy a file or folder.

    Args:
        source: Existing path.
        destination: Where to copy it.
    """
    src = _guard(source)
    dst = _guard(destination)
    if not src.exists():
        return {"error": f"does not exist: {src}"}

    if dst.is_dir() and src.is_file():
        dst = dst / src.name
    if dst.exists():
        return {"error": f"destination already exists: {dst}"}

    dst.parent.mkdir(parents=True, exist_ok=True)
    if src.is_dir():
        shutil.copytree(src, dst)
    else:
        shutil.copy2(src, dst)
    return {"copied": str(src), "to": str(dst)}


@tool(destructive=True, category="files")
def create_directory(path: str) -> dict:
    """Create a directory, including any missing parents.

    Args:
        path: Directory to create.
    """
    target = _guard(path)
    target.mkdir(parents=True, exist_ok=True)
    return {"created": str(target)}


@tool(destructive=True, category="files")
def undo_last_change(undo_id: str = "") -> dict:
    """Reverse a delete or overwrite, restoring the original from the trash store.

    Args:
        undo_id: Specific journal entry to reverse. Empty reverses the most
            recent change.
    """
    return journal.undo(undo_id or None)


@tool(category="files")
def recent_changes(limit: int = 10) -> list[dict]:
    """List recent reversible changes with their undo ids.

    Args:
        limit: How many entries to return.
    """
    return journal.history(limit)


@tool(category="files")
def user_folders() -> dict:
    """Get the paths of the standard user folders (Desktop, Documents, etc.)."""
    home = Path(os.environ.get("USERPROFILE", str(Path.home())))
    names = ["Desktop", "Documents", "Downloads", "Pictures", "Music", "Videos"]
    return {n: str(home / n) for n in names if (home / n).is_dir()} | {"home": str(home)}
