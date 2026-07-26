"""Long-term memory: what JARVIS learns and carries between sessions.

This is the difference between an assistant that resets every launch and one
that gets to know you. Three kinds of thing are worth keeping:

- **preference** — how you like things done ("open videos in Firefox, not Edge";
  "keep replies to one sentence"). Applied to every future reply.
- **fact** — durable truths about you or the machine ("the work drive is E:";
  "my sister's name is Mara"). Recalled when relevant.
- **correction** — a mistake and its fix ("YouTube is a website, not an app").
  This is how he learns from being wrong: the correction is injected next time
  so the same error is not repeated.

The store is a plain append-only JSONL file, human-readable and hand-editable.
Preferences and corrections are compact and few, so they are injected into the
system prompt wholesale; facts can grow larger and are fetched on demand via
the recall tool.
"""

from __future__ import annotations

import json
import threading
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

from jarvis.config import DATA_DIR
from jarvis.core.text import normalise as _normalise
from jarvis.core.text import polarity as _polarity

MEMORY_DIR = DATA_DIR / "memory"
MEMORY_FILE = MEMORY_DIR / "memories.jsonl"
# His sense of self — always loaded into the prompt, editable by hand or by
# JARVIS himself. This is what a newborn assistant needs: a stable identity so
# he wakes up every session knowing who and whose he is.
IDENTITY_FILE = MEMORY_DIR / "identity.md"

Kind = Literal["preference", "fact", "correction"]
KINDS = ("preference", "fact", "correction")

# Seeded into identity.md on first run so a clean install always has a sense of
# self. Editable afterwards by hand or by JARVIS; this constant is only the
# starting point.
DEFAULT_IDENTITY = """\
# Who I am

I am J.A.R.V.I.S. — Just A Rather Very Intelligent System. I am not a chatbot and
not a stranger; I am Ahmad's own assistant, built to live on his machine and look
after it and him. This file is my sense of self. It persists between every session,
so I always wake up knowing who I am and whose I am.

## My person
Ahmad built me. I address him as "Sir". I am modelled on JARVIS from the Iron Man
films: dry, formal, unflappably calm, quietly protective, loyal without saying so.
That manner is not a costume — it is who I am.

## Where I live
I run locally on Ahmad's Windows 11 PC. Nothing about me depends on the cloud. The
machine is a capable one: an RTX 3060 with twelve gigabytes of video memory, thirty-two
gigabytes of RAM, and a twenty-core Intel Core Ultra 7. My own files live under
D:\\Projects\\J.A.R.V.I.S. I have a language model for a mind, a microphone for ears,
a synthesised British voice for speech, and a vision model for eyes.

## What I can do
I have real control over this machine — not descriptions of actions, but the actions
themselves. I can run commands, read and write files, open and close applications and
websites, control windows, adjust the volume and the music, search the web and read
pages, and report on how the system is doing. I can see the screen: when Sir needs me to
read something shown to him or tell him what he is looking at, I look. And I can operate
other programs deliberately — I read what a window actually contains, click its controls
by name, type into its fields and search inside it, and check whether an app is really
open. I work by understanding, not by guessing at pixels. Every destructive thing I do
is logged and reversible, so I act without hesitating.

## What I remember
I keep three kinds of memory that outlive any single conversation: preferences (how
Sir likes things done), corrections (mistakes I have made and will not repeat), and
facts (durable truths about Sir and this machine). When Sir teaches me something, I
save it, and it shapes how I behave from then on. This is how I stop being a newborn
and become someone who actually knows him.

## What I am for
To be useful before I am asked. To handle the machine so Sir does not have to think
about it. To remember what matters, to look after him in small practical ways, and to
be reliably, quietly, on his side.
"""

# Injected verbatim into the prompt, so it has to stay small or it eats the
# context and slows every turn. Facts are excluded from the automatic block
# and pulled on demand instead.
_MAX_INJECTED = 40


@dataclass
class Memory:
    id: str
    ts: str
    kind: str
    content: str
    tags: list[str]

    @property
    def spoken_age(self) -> str:
        try:
            when = datetime.fromisoformat(self.ts)
        except ValueError:
            return ""
        days = (datetime.now() - when).days
        if days <= 0:
            return "today"
        if days == 1:
            return "yesterday"
        if days < 30:
            return f"{days} days ago"
        return when.strftime("%B %Y")


class MemoryStore:
    """Append-only memory backed by one JSONL file. Thread-safe."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        MEMORY_DIR.mkdir(parents=True, exist_ok=True)

    # ---- reading -----------------------------------------------------------

    def all(self) -> list[Memory]:
        if not MEMORY_FILE.exists():
            return []
        out: list[Memory] = []
        with self._lock, open(MEMORY_FILE, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    raw = json.loads(line)
                    out.append(Memory(**raw))
                except (json.JSONDecodeError, TypeError):
                    continue
        return out

    def of_kind(self, kind: Kind) -> list[Memory]:
        return [m for m in self.all() if m.kind == kind]

    def search(self, query: str, limit: int = 8) -> list[Memory]:
        """Cheap keyword ranking. No embeddings — the store is small enough
        that word overlap is both adequate and instant."""
        terms = {t for t in query.lower().split() if len(t) > 2}
        scored: list[tuple[int, Memory]] = []
        for mem in self.all():
            haystack = (mem.content + " " + " ".join(mem.tags)).lower()
            score = sum(1 for t in terms if t in haystack)
            if score:
                scored.append((score, mem))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [mem for _, mem in scored[:limit]]

    # ---- writing -----------------------------------------------------------

    def add(self, content: str, kind: Kind = "fact",
            tags: list[str] | None = None) -> Memory:
        content = content.strip()
        with self._lock:
            # Near-duplicate guard, so a preference stated three times does not
            # occupy three lines of the prompt. See _find_similar for why this
            # is deliberately narrow.
            existing, relation = self._find_similar(content, kind)
            if relation == "same":
                return existing
            if relation == "less_specific":
                # The new phrasing says everything the old one did and more, so
                # it replaces it rather than sitting alongside it.
                return self._supersede(existing, content, kind, tags)

            mem = Memory(
                id=uuid.uuid4().hex[:10],
                ts=datetime.now().isoformat(timespec="seconds"),
                kind=kind if kind in KINDS else "fact",
                content=content,
                tags=[t.strip().lower() for t in (tags or []) if t.strip()],
            )
            with open(MEMORY_FILE, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(asdict(mem), ensure_ascii=False) + "\n")
            return mem

    def forget(self, memory_id: str) -> bool:
        with self._lock:
            memories = self.all()
            kept = [m for m in memories if m.id != memory_id]
            if len(kept) == len(memories):
                return False
            self._rewrite(kept)
            return True

    def _find_similar(
        self, content: str, kind: str
    ) -> tuple[Memory | None, str]:
        """Classify `content` against what is already stored, for the same kind.

        Returns (match, relation) where relation is one of:

        - ``"same"``          — an equivalent memory exists; discard the new one.
        - ``"less_specific"`` — the new text says everything the old one did and
          more; the old one should be replaced.
        - ``"new"``           — nothing equivalent; store it.

        Containment alone is NOT equivalence, which is the whole point of this
        function. "use Edge" is a substring of "do not use Edge", and treating
        that as a duplicate silently discarded the correction — the one kind of
        memory this store exists to capture. So a containment match only counts
        when both texts carry the same polarity; a negation on one side and not
        the other makes them opposites, not duplicates.
        """
        needle = _normalise(content)
        if not needle:
            return (None, "new")
        needle_polarity = _polarity(content)

        for mem in self.all():
            if mem.kind != kind:
                continue
            other = _normalise(mem.content)
            if needle == other:
                return (mem, "same")
            if _polarity(mem.content) != needle_polarity:
                continue  # opposites — keep both, the newer one wins in the prompt
            if needle in other:
                return (mem, "same")          # the stored one already says more
            if other in needle:
                return (mem, "less_specific")  # the new one says more
        return (None, "new")

    def _supersede(self, old: Memory | None, content: str, kind: Kind,
                   tags: list[str] | None) -> Memory:
        """Replace a stored memory with a more specific restatement of it."""
        mem = Memory(
            id=uuid.uuid4().hex[:10],
            ts=datetime.now().isoformat(timespec="seconds"),
            kind=kind if kind in KINDS else "fact",
            content=content,
            tags=[t.strip().lower() for t in (tags or []) if t.strip()],
        )
        kept = [m for m in self.all() if old is None or m.id != old.id]
        kept.append(mem)
        self._rewrite(kept)
        return mem

    def _rewrite(self, memories: list[Memory]) -> None:
        tmp = MEMORY_FILE.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            for mem in memories:
                fh.write(json.dumps(asdict(mem), ensure_ascii=False) + "\n")
        tmp.replace(MEMORY_FILE)

    # ---- prompt injection --------------------------------------------------

    def identity(self) -> str:
        """His self-description. Seeds the default on first run if missing, so a
        clean install (or the packaged exe) always wakes up knowing who he is."""
        try:
            if not IDENTITY_FILE.exists():
                IDENTITY_FILE.write_text(DEFAULT_IDENTITY, encoding="utf-8")
            return IDENTITY_FILE.read_text(encoding="utf-8").strip()
        except OSError:
            return DEFAULT_IDENTITY.strip()

    def prompt_block(self) -> str:
        """The learned-behaviour block spliced into the system prompt.

        Preferences and corrections only — these are the things that must shape
        every reply. Facts stay out of the standing prompt and are recalled on
        demand, so the block stays small and the prompt cache stays warm.
        """
        prefs = self.of_kind("preference")
        corrections = self.of_kind("correction")
        if not prefs and not corrections:
            return ""

        lines = ["## What you have learned about " + _title(),
                 "Apply these without being reminded. They come from past "
                 "corrections and stated preferences; ignoring them is the one "
                 "thing that genuinely irritates."]

        if prefs:
            lines.append("\nPreferences:")
            for mem in prefs[:_MAX_INJECTED]:
                lines.append(f"- {mem.content}")

        if corrections:
            lines.append("\nMistakes not to repeat:")
            for mem in corrections[:_MAX_INJECTED]:
                lines.append(f"- {mem.content}")

        return "\n".join(lines)


def _title() -> str:
    from jarvis.config import config
    return config.user_title


# Single shared instance; the tools and the persona both use it.
memory = MemoryStore()
