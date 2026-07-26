"""The skill library: reusable procedures JARVIS writes for himself.

The first time JARVIS works out how to do a multi-step job — "post my daily
standup", "clear the download folder of installers older than a week", "set up
my coding workspace" — he can save the solution as a named Python skill. Next
time the same request comes in, he runs the skill instead of reasoning it out
from scratch. If a skill fails, he repairs the file rather than working around
it. That is the difference between an assistant that relearns everything every
day and one that compounds.

A skill is a plain Python file in the project's ``skills/`` directory:

    \"\"\"When to use: clear old installers from Downloads.\"\"\"

    def run(ctx, days: int = 7):
        result = ctx.call("search_files", directory=ctx.folders["Downloads"],
                          pattern="*.exe")
        ...
        return {"removed": count}

``ctx`` is the bridge to everything JARVIS can already do: ``ctx.call(tool, **args)``
runs any registered tool (through the same audit + safety gate as normal use), and
``ctx.folders`` / ``ctx.machine`` expose the machine map. Skills compose existing
tools; they do not reinvent them.

Safety: a skill is generated code, so ``run_skill`` is classified irreversible by
the gate (confirmed before running unless the level is "full"). Before a skill is
ever saved or run its source is scanned against the catastrophic denylist, and
execution is bounded by a timeout.
"""

from __future__ import annotations

import json
import re
import threading
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from jarvis.config import ROOT, config

SKILLS_DIR = ROOT / "skills"
INDEX_FILE = SKILLS_DIR / "_index.json"

# A skill name has to be a safe module-ish identifier — it becomes a filename.
_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,48}$")

# Python-level patterns too dangerous to allow in generated code, on top of the
# shell command denylist in config. These wipe files directly, bypassing the
# tool layer and its journal, so they are refused before a skill is saved.
_CODE_DENYLIST = [
    r"shutil\.rmtree\s*\(\s*['\"]?[a-zA-Z]:[\\/]?['\"]?\s*\)",  # rmtree of a drive root
    r"os\.remove\s*\(\s*['\"][a-zA-Z]:[\\/]",                    # remove at a drive root
    r"format\s*\(\s*['\"]?[a-zA-Z]:",
    r"\bwinreg\b.*Delete",
    r"subprocess.*shutdown\s+/",
    r"os\.system\s*\(\s*['\"]\s*(format|del\s+/|rd\s+/s)",
]

_DEFAULT_TIMEOUT = 30


class SkillError(Exception):
    """Raised for a bad name, unsafe code, or a missing skill."""


# ---- the execution context handed to a skill -------------------------------


class SkillContext:
    """What a running skill is allowed to touch: tools, and the machine map.

    Every ``call`` goes through the normal registry, so a skill is audited and
    gated exactly like a hand-written tool sequence — it cannot escape the
    safety rails just by being code.
    """

    def __init__(self) -> None:
        from jarvis.core import machine_map
        self._machine = machine_map.load() or {}
        self.machine = self._machine
        self.folders = self._machine.get("folders", {}).get("known", {})

    def call(self, tool: str, **args: Any) -> Any:
        """Run a registered JARVIS tool by name and return its result."""
        from jarvis.tools import registry
        return registry.execute(tool, args)


# ---- the library ------------------------------------------------------------


def _load_index() -> dict[str, Any]:
    if not INDEX_FILE.exists():
        return {}
    try:
        return json.loads(INDEX_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _save_index(index: dict[str, Any]) -> None:
    SKILLS_DIR.mkdir(parents=True, exist_ok=True)
    INDEX_FILE.write_text(json.dumps(index, indent=2, ensure_ascii=False),
                          encoding="utf-8")


def _skill_path(name: str) -> Path:
    return SKILLS_DIR / f"{name}.py"


def _scan_code(code: str) -> str | None:
    """Return the offending pattern if code trips a safety denylist, else None."""
    for pattern in config.safety.command_denylist + _CODE_DENYLIST:
        try:
            if re.search(pattern, code, re.IGNORECASE):
                return pattern
        except re.error:
            continue
    return None


def save(name: str, description: str, code: str) -> dict[str, Any]:
    """Write a skill file and index it. Overwrites an existing skill of the name.

    The code must define a top-level ``def run(ctx, ...)``. Its source is scanned
    for catastrophic operations before it is allowed to land on disk.
    """
    name = name.strip().lower()
    if not _NAME_RE.match(name):
        raise SkillError(f"invalid skill name '{name}': use lowercase letters, "
                         "digits and underscores, starting with a letter")
    if "def run(" not in code:
        raise SkillError("a skill must define a top-level function 'def run(ctx, ...)'")
    hit = _scan_code(code)
    if hit is not None:
        raise SkillError(f"skill refused: its code matches the safety denylist "
                         f"(pattern {hit!r})")

    # Verify it at least parses before committing it.
    try:
        compile(code, f"<skill:{name}>", "exec")
    except SyntaxError as exc:
        raise SkillError(f"skill has a syntax error: {exc}") from exc

    SKILLS_DIR.mkdir(parents=True, exist_ok=True)
    _skill_path(name).write_text(code, encoding="utf-8")

    index = _load_index()
    entry = index.get(name, {})
    index[name] = {
        "description": description.strip(),
        "created": entry.get("created", datetime.now().isoformat(timespec="seconds")),
        "updated": datetime.now().isoformat(timespec="seconds"),
        "runs": entry.get("runs", 0),
        "failures": entry.get("failures", 0),
    }
    _save_index(index)
    return {"saved": name, "description": description.strip()}


def delete(name: str) -> bool:
    name = name.strip().lower()
    index = _load_index()
    existed = name in index
    index.pop(name, None)
    _save_index(index)
    path = _skill_path(name)
    if path.exists():
        path.unlink()
        existed = True
    return existed


def list_skills() -> list[dict[str, Any]]:
    """Every skill with its description and run stats, for the model to choose from."""
    index = _load_index()
    return [
        {"name": name, "description": meta.get("description", ""),
         "runs": meta.get("runs", 0), "failures": meta.get("failures", 0)}
        for name, meta in sorted(index.items())
    ]


def get_source(name: str) -> str | None:
    path = _skill_path(name.strip().lower())
    return path.read_text(encoding="utf-8") if path.exists() else None


def index_block() -> str:
    """A compact skill index for the system prompt: names and when to use them."""
    skills = list_skills()
    if not skills:
        return ""
    lines = ["## Your skills (reusable procedures you have written)",
             "Before solving a multi-step task from scratch, check whether one of "
             "these already does it and run_skill it instead. If one fails, repair "
             "it with save_skill rather than working around it."]
    for s in skills:
        note = f" (failed {s['failures']}x)" if s.get("failures") else ""
        lines.append(f"- {s['name']}: {s['description']}{note}")
    return "\n".join(lines)


def _bump(name: str, *, failed: bool) -> None:
    index = _load_index()
    if name in index:
        index[name]["runs"] = index[name].get("runs", 0) + 1
        if failed:
            index[name]["failures"] = index[name].get("failures", 0) + 1
        _save_index(index)


def run(name: str, kwargs: dict[str, Any] | None = None,
        timeout: int = _DEFAULT_TIMEOUT) -> dict[str, Any]:
    """Load and execute a skill's ``run(ctx, **kwargs)`` in a bounded worker.

    Returns {"result": ...} on success or {"error": ..., "traceback": ...} on
    failure, so the model can read a failure and repair the skill.
    """
    name = name.strip().lower()
    source = get_source(name)
    if source is None:
        raise SkillError(f"no skill named '{name}'. Known: "
                         f"{', '.join(s['name'] for s in list_skills()) or 'none'}")

    # Re-scan on run: the file could have been edited by hand since it was saved.
    hit = _scan_code(source)
    if hit is not None:
        _bump(name, failed=True)
        return {"error": f"skill '{name}' matches the safety denylist "
                         f"(pattern {hit!r}) and was not run"}

    namespace: dict[str, Any] = {}
    try:
        exec(compile(source, f"<skill:{name}>", "exec"), namespace)  # noqa: S102
    except Exception as exc:  # noqa: BLE001
        _bump(name, failed=True)
        return {"error": f"skill failed to load: {exc}",
                "traceback": traceback.format_exc()}

    fn = namespace.get("run")
    if not callable(fn):
        _bump(name, failed=True)
        return {"error": f"skill '{name}' has no callable run(ctx, ...)"}

    ctx = SkillContext()
    box: dict[str, Any] = {}

    def _worker() -> None:
        try:
            box["result"] = fn(ctx, **(kwargs or {}))
        except Exception as exc:  # noqa: BLE001
            box["error"] = f"{type(exc).__name__}: {exc}"
            box["traceback"] = traceback.format_exc()

    thread = threading.Thread(target=_worker, daemon=True)
    thread.start()
    thread.join(timeout)

    if thread.is_alive():
        _bump(name, failed=True)
        return {"error": f"skill '{name}' exceeded {timeout}s and was abandoned"}

    if "error" in box:
        _bump(name, failed=True)
        return {"error": box["error"], "traceback": box.get("traceback", ""),
                "hint": "read the traceback, then repair the skill with save_skill."}

    _bump(name, failed=False)
    return {"result": box.get("result")}
