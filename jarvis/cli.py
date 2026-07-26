"""Text-mode console for JARVIS.

The full desktop app is the intended interface, but this exercises the whole
brain and tool stack with no audio hardware in the loop — which makes it the
right place to test whether he actually does what you asked.

    python -m jarvis.cli
"""

from __future__ import annotations

import asyncio
import sys
from typing import Any

from jarvis.config import config
from jarvis.core import persona
from jarvis.core.brain import Brain
from jarvis.tools import registry

# ANSI colours; Windows Terminal and PowerShell 7 handle these natively.
DIM = "\033[2m"
CYAN = "\033[36m"
GOLD = "\033[33m"
RED = "\033[31m"
GREEN = "\033[32m"
RESET = "\033[0m"

BANNER = f"""{CYAN}
     _     _    ____  __     __ ___  ____
    | |   / \\  |  _ \\ \\ \\   / /|_ _|/ ___|
 _  | |  / _ \\ | |_) | \\ \\ / /  | | \\___ \\
| |_| | / ___ \\|  _ <   \\ V /   | |  ___) |
 \\___/ /_/   \\_\\_| \\_\\   \\_/   |___||____/
{RESET}{DIM}  text console — /help for commands{RESET}
"""


async def on_event(event: dict[str, Any]) -> None:
    kind = event["type"]
    if kind == "tool_call":
        args = event.get("args") or {}
        preview = ", ".join(f"{k}={v!r}" for k, v in list(args.items())[:3])
        if len(preview) > 90:
            preview = preview[:90] + "..."
        print(f"  {DIM}> {event['tool']}({preview}){RESET}")
    elif kind == "tool_result":
        result = event.get("result")
        if isinstance(result, dict) and "error" in result:
            print(f"  {RED}! {result['error']}{RESET}")
    elif kind == "error":
        print(f"{RED}{event['message']}{RESET}")


def approve_on_stdin(tool: str, description: str, args: dict[str, Any]) -> bool:
    """Ask on the console whether a confirmable action should run.

    Registered with the safety gate at startup. It is called on a tool worker
    thread while the main coroutine is parked awaiting the reply, so stdin is
    free and blocking here is safe. Anything but an explicit yes is a no,
    including EOF and a stray Ctrl-C.
    """
    preview = ", ".join(f"{k}={v!r}" for k, v in list(args.items())[:3])
    if len(preview) > 120:
        preview = preview[:120] + "..."
    print(f"\n  {RED}CONFIRM{RESET}  About to {description}")
    if preview:
        print(f"           {DIM}{preview}{RESET}")
    try:
        answer = input(f"           approve? [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    granted = answer in ("y", "yes")
    print(f"           {GREEN if granted else RED}"
          f"{'approved' if granted else 'denied'}{RESET}\n")
    return granted


HELP = f"""
{GOLD}Commands{RESET}
  /help            this message
  /tools           list every registered tool
  /health          check Ollama and the model
  /reset           clear the conversation
  /audit           recent tool calls
  /undo            reverse the last file change
  /quit            exit
"""


async def handle_command(line: str, brain: Brain) -> bool:
    """Returns False when the console should exit."""
    cmd = line[1:].strip().lower()

    if cmd in ("quit", "exit", "q"):
        return False

    if cmd == "help":
        print(HELP)

    elif cmd == "tools":
        by_category: dict[str, list[str]] = {}
        for tool_entry in registry.REGISTRY.values():
            by_category.setdefault(tool_entry.category, []).append(tool_entry.name)
        print()
        for category in sorted(by_category):
            names = ", ".join(sorted(by_category[category]))
            print(f"  {GOLD}{category:<8}{RESET} {names}")
        print(f"\n  {len(registry.REGISTRY)} tools registered\n")

    elif cmd == "health":
        status = await brain.health()
        mark = f"{GREEN}ok{RESET}" if status["ok"] else f"{RED}problem{RESET}"
        print(f"\n  Ollama : {mark}")
        print(f"  Model  : {status['model']}")
        if status.get("installed"):
            print(f"  Present: {', '.join(status['installed'])}")
        if status.get("error"):
            print(f"  {RED}{status['error']}{RESET}")
        print()

    elif cmd == "reset":
        brain.reset()
        print(f"  {DIM}conversation cleared{RESET}")

    elif cmd == "audit":
        from jarvis.safety import audit

        entries = audit.tail(15)
        if not entries:
            print(f"  {DIM}nothing logged yet today{RESET}")
        for entry in entries:
            mark = " " if entry.get("ok") else "!"
            flag = "*" if entry.get("destructive") else " "
            print(f"  {mark}{flag} {entry['ts'][11:19]}  {entry['tool']}")
        print()

    elif cmd == "undo":
        from jarvis.safety import journal

        print(f"  {journal.undo()}")

    else:
        print(f"  {DIM}unknown command; /help for the list{RESET}")

    return True


async def main() -> int:
    print(BANNER)

    # Confirmable actions are put to the person at the keyboard. Without this
    # the gate has nobody to ask and refuses them outright.
    from jarvis.safety import gate

    gate.set_approver(approve_on_stdin)

    brain = Brain()
    print(f"  {DIM}{len(registry.REGISTRY)} tools loaded{RESET}")

    status = await brain.health()
    if not status["ok"]:
        print(f"  {RED}{status.get('error')}{RESET}")
        print(f"  {DIM}Run setup.ps1, or start Ollama with 'ollama serve'.{RESET}\n")
    else:
        print(f"  {DIM}model {config.llm.model} ready{RESET}\n")
        print(f"{GOLD}JARVIS{RESET}  {persona.greeting()}\n")

    while True:
        try:
            line = input(f"{CYAN}you{RESET}     ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if not line:
            continue

        if line.startswith("/"):
            if not await handle_command(line, brain):
                break
            continue

        reply = await brain.respond(line, on_event)
        print(f"{GOLD}JARVIS{RESET}  {reply}\n")

    print(f"{DIM}Goodbye, {config.user_title}.{RESET}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        sys.exit(0)
