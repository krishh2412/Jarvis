"""The safety gate: decides whether a tool call runs, blocks, or needs a yes.

Three layers, checked in order for every tool call:

1. **Hard denylist** — catastrophic shell commands (format, mkfs, registry
   hive deletes, fork bombs) are refused at EVERY safety level. No prompt can
   authorise them; the user has to edit the denylist by hand.
2. **Danger classification** — each call is rated safe / reversible /
   irreversible. Reversible destructive actions are journaled and undoable, so
   they pass silently. Irreversible ones (shutdown, installs, arbitrary code,
   sending data out) are the ones a confirmation protects.
3. **Level policy** — "full" waves everything through (except layer 1),
   "standard" confirms only irreversible actions, "strict" confirms anything
   destructive.

When a confirmation is required the question goes to a *human*, synchronously,
through an approver callback that the UI (or the console) registers at startup.
The tool thread blocks on the answer and then either runs the call or refuses
it, so the model sees an ordinary tool result either way.

The model is deliberately not part of this loop. An earlier design parked the
call and handed the model a token to pass back to a ``confirm_action`` tool once
the user agreed — which meant the only thing standing between a shutdown and a
misread instruction was an 8B model's compliance. It could approve itself, and
under the retry nudges in the reasoning loop, eventually would. There is now no
token for it to hold and no tool for it to call: approval is something only a
person can give.

If no approver is registered (a headless embedding, say) a confirmable action is
refused rather than allowed. There is no way to ask, so the answer is no.
"""

from __future__ import annotations

import re
import threading
from typing import Any, Callable, Literal

from jarvis.config import config

Danger = Literal["safe", "reversible", "irreversible"]
Decision = Literal["allow", "confirm", "block"]

# Tools that change or remove state but are journaled/undoable — reversible.
# (delete/write/move route through the trash store + undo journal.)
_REVERSIBLE_TOOLS = {
    "write_file", "delete_path", "move_path", "copy_path", "create_directory",
    "remember", "forget", "set_volume", "adjust_volume", "set_mute",
    "set_app_volume", "media_control", "write_clipboard", "control_window",
    "click_at", "type_text", "press_keys", "scroll_screen", "drag_mouse",
    "click_element", "type_in_field", "launch_app", "open_url", "open_website",
    "undo_last_change",
}

# Tools whose effects cannot be undone by us — irreversible. A confirmation is
# meaningful here.
_IRREVERSIBLE_TOOLS = {
    "kill_process",     # unsaved work is gone
    "power_action",     # shutdown / restart / logout
    "run_command",      # arbitrary shell
    "run_powershell",   # arbitrary shell
    "run_skill",        # executes generated Python
}


# Asked (tool, description, args); returns True only if a person said yes.
Approver = Callable[[str, str, dict[str, Any]], bool]

_approver: Approver | None = None
_approver_lock = threading.Lock()


def set_approver(fn: Approver | None) -> None:
    """Register who gets asked when an action needs a human yes.

    The HUD registers a callback that draws an Approve/Deny card and blocks
    until it is answered; the console registers one that prompts on stdin.
    """
    global _approver
    with _approver_lock:
        _approver = fn


def has_approver() -> bool:
    return _approver is not None


def ask(tool: str, args: dict[str, Any], description: str) -> tuple[bool, str]:
    """Put a confirmable action to the human. Returns (approved, reason).

    Never raises and never defaults to yes: a missing approver, a timeout or a
    broken callback all come back as a refusal with something to say about it.
    """
    approver = _approver
    if approver is None:
        return (False, f"{description} needs {config.user_title}'s approval, but "
                       "there is no interface attached to ask through")
    try:
        granted = bool(approver(tool, description, dict(args)))
    except Exception as exc:  # noqa: BLE001 - a broken approver is a refusal
        return (False, f"could not ask for approval ({type(exc).__name__}), so "
                       f"{description} was not run")
    if granted:
        return (True, "")
    return (False, f"{config.user_title} declined: {description}")


# Study mode is JARVIS improving himself unattended. The rule (chosen by Sir) is
# that he may explore and read anything, but only *write* inside his own project
# folder — never modify the wider system while self-studying, even at "full"
# safety. Enforced here as an allowlist: safe/read-only tools always pass, plus
# these few destructive tools whose writes only ever touch project files or
# memory. Everything else destructive is blocked for the duration.
_study_mode = False
_STUDY_WRITE_ALLOW = {
    "save_skill", "delete_skill",          # skills/ — inside the project
    "remember", "forget",                   # data/memory — inside the project
    "refresh_machine_map",                  # rescans; writes only the map file
}


def enter_study() -> None:
    global _study_mode
    _study_mode = True


def exit_study() -> None:
    global _study_mode
    _study_mode = False


def in_study() -> bool:
    return _study_mode


def _denylisted(text: str) -> str | None:
    """Return the matching denylist pattern if `text` is catastrophic."""
    for pattern in config.safety.command_denylist:
        try:
            if re.search(pattern, text, re.IGNORECASE):
                return pattern
        except re.error:
            continue
    return None


def _classify(tool: str, args: dict[str, Any]) -> Danger:
    if tool in _IRREVERSIBLE_TOOLS:
        # A restart/shutdown is irreversible; a plain 'lock' is not.
        if tool == "power_action" and str(args.get("action", "")) in ("lock", "sleep"):
            return "reversible"
        return "irreversible"
    if tool in _REVERSIBLE_TOOLS:
        return "reversible"
    return "safe"


def _describe(tool: str, args: dict[str, Any]) -> str:
    """A short, plain-language line for the confirmation prompt."""
    if tool == "power_action":
        return f"{args.get('action', 'a power action')} the PC"
    if tool in ("run_command", "run_powershell"):
        cmd = str(args.get("command", ""))[:120]
        return f"run this command: {cmd}"
    if tool == "kill_process":
        return f"force-quit {args.get('name_or_pid', 'a process')}"
    if tool == "run_skill":
        return f"execute the skill '{args.get('name', '?')}'"
    return f"{tool}({', '.join(f'{k}={v!r}' for k, v in list(args.items())[:2])})"


def check(tool: str, args: dict[str, Any]) -> tuple[Decision, str]:
    """Decide what to do with a tool call, without running or asking anything.

    Returns (decision, reason). "block" means refuse outright; "confirm" means
    the caller must get a human yes via :func:`ask` before running it. Pure and
    side-effect free, which is what makes it testable.
    """
    # Layer 1: catastrophic shell commands, blocked at every level.
    if tool in ("run_command", "run_powershell"):
        hit = _denylisted(str(args.get("command", "")))
        if hit:
            return ("block", f"That command is on the hard safety denylist "
                             f"(pattern {hit!r}) and will not run at any level.")

    level = config.safety.level
    danger = _classify(tool, args)

    # Study-mode sandbox: read freely, but no writes to the wider system.
    if _study_mode and danger != "safe" and tool not in _STUDY_WRITE_ALLOW:
        return ("block", "study mode is read-only outside the project folder; "
                         f"{tool} would modify the system and was not run")

    if danger == "safe":
        return ("allow", "")
    if level == "full":
        return ("allow", "")  # unrestricted, layer-1 already passed
    if level == "strict" and danger in ("reversible", "irreversible"):
        return ("confirm", _describe(tool, args))
    if level == "standard" and danger == "irreversible":
        return ("confirm", _describe(tool, args))
    return ("allow", "")


def describe(tool: str, args: dict[str, Any]) -> str:
    """Public wrapper over the plain-language description of a call."""
    return _describe(tool, args)
