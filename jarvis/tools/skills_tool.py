"""Tools for the skill library: write, list, inspect, run and repair skills.

This is how JARVIS turns a solved problem into a reusable capability. When a
multi-step task is worked out for the first time, save_skill records it; next
time, run_skill replays it. run_skill is classified irreversible by the safety
gate because a skill is generated code.
"""

from __future__ import annotations

from typing import Any

from jarvis.core import skills
from jarvis.tools.registry import tool


@tool(category="skills")
def list_skills() -> dict:
    """List the reusable skills you have written, with when to use each.

    Check this before solving a multi-step task from scratch — you may have
    already written the procedure.
    """
    found = skills.list_skills()
    return {"skills": found} if found else {"skills": [], "note": "no skills yet"}


@tool(category="skills")
def save_skill(name: str, description: str, code: str) -> dict:
    """Save a reusable skill as Python, or repair an existing one by overwriting it.

    Write a skill the first time you solve a multi-step task you might face again,
    and repair a skill (same name, corrected code) when run_skill reports it
    failed. The code MUST define a top-level function:

        def run(ctx, ...):
            ...
            return <result>

    Inside run, use ctx.call("tool_name", arg=value) to invoke any JARVIS tool
    (it goes through the normal audit and safety gate), ctx.folders for the
    user's known folders, and ctx.machine for the full machine map. Keep skills
    composed of existing tools rather than raw file/system operations.

    Args:
        name: Short lowercase identifier, e.g. "clear_old_downloads".
        description: One line — when to use this skill.
        code: The full Python source defining run(ctx, ...).
    """
    try:
        return skills.save(name, description, code)
    except skills.SkillError as exc:
        return {"error": str(exc)}


@tool(category="skills")
def view_skill(name: str) -> dict:
    """Show a skill's source code, e.g. to inspect it before repairing it.

    Args:
        name: The skill's name.
    """
    source = skills.get_source(name)
    if source is None:
        return {"error": f"no skill named '{name}'"}
    return {"name": name, "source": source}


@tool(category="skills", destructive=True)
def run_skill(name: str, args: dict[str, Any] | None = None) -> dict:
    """Run one of your saved skills by name.

    If it returns an error with a traceback, read it and fix the skill with
    save_skill rather than abandoning the task.

    Args:
        name: The skill to run.
        args: Optional keyword arguments passed to the skill's run(ctx, ...).
    """
    try:
        return skills.run(name, args or {})
    except skills.SkillError as exc:
        return {"error": str(exc)}


@tool(category="skills", destructive=True)
def delete_skill(name: str) -> dict:
    """Delete a skill that is wrong beyond repair or no longer needed.

    Args:
        name: The skill to delete.
    """
    return {"deleted": skills.delete(name)}
