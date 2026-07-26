"""The self-learning loop: episodic memory, reflection, and its notes.

This is the half that makes JARVIS get better on its own. It is not neural
retraining — the model's weights never change — but it is real learning in the
sense that matters here: he accumulates knowledge, corrections and skills in
files he reads back every time he starts, so next week he behaves better than
this week because of what happened in between.

Three moving parts live here:

1. **Episodic log** — an append-only, per-day record of what actually happened:
   every request, the actions it triggered, and the reply. Raw material for
   reflection. (Tool-level detail already lives in the audit log; this adds the
   request/reply narrative the audit log lacks.)

2. **Reflection** — after a session, the model reads the new episodic entries
   and distils them into three kinds of dated note: facts about the user, facts
   about the machine, and failures with their fixes. These are written to
   markdown under data/memory/ and loaded back into the prompt at startup.

3. **Note store** — the three markdown files, plus the open-questions and
   study-log files that study mode uses. Small, dated, human-readable, and
   condensed rather than deleted when they grow long.
"""

from __future__ import annotations

import json
import re
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from jarvis.config import DATA_DIR
from jarvis.core.text import normalise as _normalise

LEARNING_DIR = DATA_DIR / "learning"
MEMORY_DIR = DATA_DIR / "memory"

# Reflection's three output files, plus study mode's two.
USER_FACTS = MEMORY_DIR / "user_facts.md"
MACHINE_NOTES = MEMORY_DIR / "machine_notes.md"
FAILURES = MEMORY_DIR / "failures.md"
OPEN_QUESTIONS = MEMORY_DIR / "open_questions.md"
STUDY_LOG = MEMORY_DIR / "study_log.md"

# Marks how far reflection has already read, so it only considers new turns.
_REFLECT_MARKER = LEARNING_DIR / ".last_reflection"

_lock = threading.Lock()

# When a notes file passes this many lines, its older half is condensed by the
# model rather than dropped — nothing learned is thrown away, it just gets terser.
_CONDENSE_OVER = 120


# ---- episodic log ----------------------------------------------------------


def _episodic_path(day: datetime | None = None) -> Path:
    day = day or datetime.now()
    return LEARNING_DIR / f"episodic-{day.strftime('%Y-%m-%d')}.jsonl"


def log_turn(request: str, actions: list[dict[str, Any]], reply: str,
             assistant: str = "jarvis") -> None:
    """Record one completed turn: what was asked, what ran, what was answered.

    Args:
        request: The user's message.
        actions: One dict per tool call this turn — {tool, ok, error?}.
        reply: The assistant's final spoken reply.
        assistant: Which brain answered ("jarvis" or "claude").
    """
    entry = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "assistant": assistant,
        "request": request[:2000],
        "actions": actions,
        "reply": reply[:2000],
        "had_error": any(not a.get("ok", True) for a in actions),
    }
    LEARNING_DIR.mkdir(parents=True, exist_ok=True)
    line = json.dumps(entry, ensure_ascii=False, default=str)
    with _lock:
        try:
            with open(_episodic_path(), "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
                fh.flush()
        except OSError:
            pass  # learning must never take the assistant down with it


def _read_episodic_since(marker: datetime, days: int = 3) -> list[dict[str, Any]]:
    """All episodic turns newer than `marker`, across the last few day-files."""
    turns: list[dict[str, Any]] = []
    for delta in range(days):
        path = _episodic_path(datetime.now() - timedelta(days=delta))
        if not path.exists():
            continue
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    turn = json.loads(line)
                    ts = datetime.fromisoformat(turn.get("ts", ""))
                except (json.JSONDecodeError, ValueError):
                    continue
                if ts > marker:
                    turns.append(turn)
        except OSError:
            continue
    turns.sort(key=lambda t: t.get("ts", ""))
    return turns


def _load_marker() -> datetime:
    try:
        return datetime.fromisoformat(_REFLECT_MARKER.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return datetime.now() - timedelta(days=1)


def _save_marker(when: datetime) -> None:
    LEARNING_DIR.mkdir(parents=True, exist_ok=True)
    try:
        _REFLECT_MARKER.write_text(when.isoformat(timespec="seconds"), encoding="utf-8")
    except OSError:
        pass


# ---- note files ------------------------------------------------------------


_HEADERS = {
    USER_FACTS: "# What I've learned about Sir\n\nDated notes distilled from our "
                "sessions: his preferences, habits, projects, and schedule.\n",
    MACHINE_NOTES: "# What I've learned about this machine\n\nDated corrections "
                   "and quirks discovered in use, beyond the machine map.\n",
    FAILURES: "# Failures and their fixes\n\nWhat broke, why, and what actually "
              "worked instead. Read before repeating a past mistake.\n",
    OPEN_QUESTIONS: "# Open questions\n\nThings I do not yet know and should find "
                    "out. Study mode researches these.\n",
    STUDY_LOG: "# Study log\n\nWhat I taught myself, and when.\n",
}


def _ensure(path: Path) -> None:
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_HEADERS.get(path, f"# {path.stem}\n"), encoding="utf-8")


_BULLET_RE = re.compile(r"^-\s*(?:\d{4}-\d{2}-\d{2}\s*:\s*)?(.*)$")


def _existing_notes(path: Path) -> set[str]:
    """Normalised text of every bullet already in a notes file."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return set()
    out: set[str] = set()
    for line in lines:
        match = _BULLET_RE.match(line.strip())
        if match and match.group(1).strip():
            out.add(_normalise(match.group(1)))
    return out


def append_notes(path: Path, notes: list[str]) -> int:
    """Append dated bullet notes to a file, skipping ones already present.

    De-duplication compares whole notes, not substrings: a note that merely
    contains an existing one is new information ("Spotify is installed" versus
    "Spotify is installed but not on the Start Menu") and must not be dropped.
    Notes repeated within a single batch are collapsed too, which a scan of the
    file alone would miss.
    """
    notes = [n.strip() for n in notes if n.strip()]
    if not notes:
        return 0
    _ensure(path)
    with _lock:
        seen = _existing_notes(path)
        today = datetime.now().strftime("%Y-%m-%d")
        added = 0
        with open(path, "a", encoding="utf-8") as fh:
            for note in notes:
                key = _normalise(note)
                if not key or key in seen:
                    continue
                seen.add(key)
                fh.write(f"- {today}: {note}\n")
                added += 1
    if added:
        _maybe_condense(path)
    return added


def read_notes(path: Path, max_chars: int = 1500) -> str:
    """The tail of a notes file, for injecting into the prompt."""
    if not path.exists():
        return ""
    text = path.read_text(encoding="utf-8").strip()
    if len(text) <= max_chars:
        return text
    return text[-max_chars:]


# ---- reflection (model-driven) ---------------------------------------------


_REFLECTION_PROMPT = """\
You are the reflective memory of JARVIS, a personal assistant. Below is a log of \
recent interactions with Sir (the user) on his PC — what he asked, which tools ran, \
whether they succeeded, and what was replied.

Read it and extract durable lessons worth remembering next session. Be strict: only \
things that will still matter in a week. Ignore one-off chatter and successful \
routine actions.

Return ONLY a JSON object with exactly these three keys, each a list of short, \
self-contained strings (empty list if nothing qualifies):

- "user_facts": preferences, habits, project names, schedule, how Sir likes things \
done. E.g. "Sir keeps active projects under D:\\Projects".
- "machine_notes": corrections or quirks about THIS machine discovered in use. E.g. \
"Spotify is not installed via Start Menu; launch by exe path".
- "failures": things that broke and what worked instead, as a lesson. E.g. \
"open_website failed on 'youtube music' — it is music.youtube.com, not an app".

No prose, no markdown, no code fences. Just the JSON object.

RECENT LOG:
"""


def _log_digest(turns: list[dict[str, Any]], limit: int = 60) -> str:
    """Compact the episodic turns into text the model can reflect over."""
    lines: list[str] = []
    for turn in turns[-limit:]:
        acts = turn.get("actions", [])
        act_str = ", ".join(
            a.get("tool", "?") + ("" if a.get("ok", True) else f"(FAILED: {a.get('error', '')[:80]})")
            for a in acts
        ) or "(no tools)"
        lines.append(
            f"[{turn.get('ts', '')}] asked: {turn.get('request', '')[:200]}\n"
            f"   tools: {act_str}\n"
            f"   replied: {turn.get('reply', '')[:200]}"
        )
    return "\n".join(lines)


def reflect(force: bool = False) -> dict[str, Any]:
    """Read new episodic turns and distil dated notes. Model-driven.

    Returns a summary of what was written. Safe to call with nothing new — it
    simply reports that there was nothing to reflect on.
    """
    marker = datetime.min if force else _load_marker()
    turns = _read_episodic_since(marker)
    if not turns:
        return {"reflected": 0, "note": "no new interactions to reflect on"}

    digest = _log_digest(turns)
    try:
        import ollama
        from jarvis.config import config
        response = ollama.Client(host=config.llm.host).chat(
            model=config.llm.model,
            messages=[{"role": "user", "content": _REFLECTION_PROMPT + digest}],
            options={"temperature": 0.2, "num_ctx": config.llm.num_ctx},
            think=False,
            keep_alive=config.llm.keep_alive,
        )
        raw = response.get("message", {}).get("content", "") or ""
    except Exception as exc:  # noqa: BLE001
        return {"error": f"reflection model call failed: {exc}", "turns_seen": len(turns)}

    parsed = _extract_json(raw)
    if parsed is None:
        return {"error": "could not parse reflection output", "raw": raw[:400],
                "turns_seen": len(turns)}

    written = {
        "user_facts": append_notes(USER_FACTS, parsed.get("user_facts", [])),
        "machine_notes": append_notes(MACHINE_NOTES, parsed.get("machine_notes", [])),
        "failures": append_notes(FAILURES, parsed.get("failures", [])),
    }
    _save_marker(datetime.now())
    return {"reflected": len(turns), "notes_written": written}


def _extract_json(text: str) -> dict[str, Any] | None:
    """Pull the first JSON object out of a model reply, fences or not."""
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```", 2)[1] if "```" in text[3:] else text
        text = text.lstrip("json").strip("`").strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1 or end < start:
        return None
    try:
        obj = json.loads(text[start:end + 1])
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        return None


# ---- condensing ------------------------------------------------------------


def _maybe_condense(path: Path) -> None:
    """When a notes file grows long, compress its older half rather than lose it."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    bullets = [ln for ln in lines if ln.startswith("- ")]
    if len(bullets) <= _CONDENSE_OVER:
        return

    header = "\n".join(ln for ln in lines if not ln.startswith("- "))
    keep = bullets[-_CONDENSE_OVER // 2:]
    old = bullets[:-_CONDENSE_OVER // 2]
    try:
        import ollama
        from jarvis.config import config
        prompt = (
            "Condense these dated notes into fewer, merged bullet points without "
            "losing any distinct fact. Keep the '- YYYY-MM-DD: ' style, merge "
            "duplicates and near-duplicates, drop nothing important. Return only "
            "the bullet lines.\n\n" + "\n".join(old)
        )
        response = ollama.Client(host=config.llm.host).chat(
            model=config.llm.model,
            messages=[{"role": "user", "content": prompt}],
            options={"temperature": 0.2, "num_ctx": config.llm.num_ctx},
            think=False,
        )
        condensed = (response.get("message", {}).get("content", "") or "").strip()
        condensed_lines = [ln for ln in condensed.splitlines() if ln.strip().startswith("-")]
        if not condensed_lines:
            return
    except Exception:  # noqa: BLE001
        return  # condensing is best-effort; leave the file as-is on failure

    with _lock:
        path.write_text(header.rstrip() + "\n\n"
                        + "\n".join(condensed_lines + keep) + "\n", encoding="utf-8")


# ---- startup context -------------------------------------------------------


def startup_block() -> str:
    """Learned notes to splice into the system prompt at boot.

    User facts and failure lessons — the two that change behaviour. Machine
    notes are folded into the machine-map summary elsewhere. Kept short so the
    prompt cache stays warm.
    """
    facts = read_notes(USER_FACTS, 1200)
    fails = read_notes(FAILURES, 1000)
    blocks: list[str] = []
    if facts and facts.count("- ") > 0:
        blocks.append("## What you've learned about " + _title() + " (from reflection)\n"
                      + _bullets_only(facts))
    if fails and fails.count("- ") > 0:
        blocks.append("## Past failures — do not repeat these\n" + _bullets_only(fails))
    return "\n\n".join(blocks)


def _bullets_only(text: str) -> str:
    return "\n".join(ln for ln in text.splitlines() if ln.strip().startswith("- "))


def _title() -> str:
    from jarvis.config import config
    return config.user_title
