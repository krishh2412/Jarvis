"""Tools that let JARVIS remember, recall and forget across sessions."""

from __future__ import annotations

from typing import Literal

from jarvis.core.memory import memory
from jarvis.tools.registry import tool


@tool(category="memory", destructive=True)
def remember(content: str,
             kind: Literal["preference", "fact", "correction"] = "fact",
             tags: str = "") -> dict:
    """Save something worth keeping between sessions.

    Use this whenever you learn something durable about the user or their
    machine, and ALWAYS when corrected — save the correction so you never make
    the same mistake twice.

    - preference: how they like things done ("open videos in Firefox").
    - fact: a durable truth about them or the system ("the media drive is E:").
    - correction: a mistake and its fix ("YouTube is a website, open it in the
      browser, not an application").

    Do not save trivia, one-off details, or anything that only mattered for the
    current request.

    Args:
        content: The thing to remember, written as a clear standalone statement.
        kind: Which sort of memory this is.
        tags: Optional comma-separated keywords to help recall it later.
    """
    tag_list = [t for t in (tags.split(",") if tags else []) if t.strip()]
    mem = memory.add(content, kind=kind, tags=tag_list)
    return {"saved": mem.content, "kind": mem.kind, "id": mem.id}


@tool(category="memory")
def recall(query: str, limit: int = 8) -> dict:
    """Search long-term memory for anything relevant to a topic.

    Preferences and corrections are already applied automatically, so reach for
    this mainly to look up facts — names, paths, past decisions.

    Args:
        query: What to look for.
        limit: Most results to return.
    """
    hits = memory.search(query, limit=limit)
    return {
        "query": query,
        "memories": [
            {"content": m.content, "kind": m.kind, "when": m.spoken_age, "id": m.id}
            for m in hits
        ],
    }


@tool(category="memory")
def list_memories(kind: Literal["all", "preference", "fact", "correction"] = "all") -> dict:
    """List everything currently remembered, optionally filtered by kind.

    Args:
        kind: Restrict to one kind, or "all".
    """
    items = memory.all() if kind == "all" else memory.of_kind(kind)  # type: ignore[arg-type]
    return {
        "count": len(items),
        "memories": [
            {"content": m.content, "kind": m.kind, "when": m.spoken_age, "id": m.id}
            for m in items
        ],
    }


@tool(category="memory", destructive=True)
def forget(memory_id: str) -> dict:
    """Delete a specific memory by its id.

    Args:
        memory_id: The id from recall or list_memories.
    """
    ok = memory.forget(memory_id)
    return {"forgotten": ok, "id": memory_id} if ok else {"error": f"no memory {memory_id}"}
