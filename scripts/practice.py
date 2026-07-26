"""Practice drills: exercise JARVIS on real daily tasks and score him.

This is how the assistant is *trained* without touching a single weight. It runs
a curated set of realistic requests through the actual brain, watches which tools
he reaches for, and reports where he got it wrong. Three things come out of it:

1. **A tool-selection score.** Each drill names the tool a competent answer would
   use. Picking `screen_info` when asked about brightness is the exact failure
   this catches — and it is invisible until you measure it.
2. **Real episodic data.** Every turn lands in data/learning/episodic-*.jsonl, so
   the reflection pass has genuine material, and any later fine-tune trains on
   how Sir's machine is actually used rather than on invented examples.
3. **A list of things to fix.** Failures here become sharper tool descriptions,
   new skills, or notes in failures.md.

Run it:

    .venv\\Scripts\\python.exe scripts\\practice.py            all drills
    .venv\\Scripts\\python.exe scripts\\practice.py --only system
    .venv\\Scripts\\python.exe scripts\\practice.py --safe     read-only drills

Drills are deliberately non-destructive: they read state, or make changes that
are restored afterwards. Nothing here deletes, installs or powers anything off.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# JARVIS writes proper typography — em dashes, curly quotes — and a Windows
# console defaults to cp1252, which cannot encode them. Printing a reply would
# then kill the run partway through. Force UTF-8 and never fail on a character.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from jarvis.core.brain import Brain  # noqa: E402


@dataclass
class Drill:
    """One practice request and what a good answer looks like."""

    category: str
    request: str
    # Any one of these tools counts as the right instinct. Empty means the drill
    # should be answered from knowledge/context with no tool at all.
    expect: tuple[str, ...] = ()
    # Changes machine state; skipped in --safe mode.
    mutates: bool = False
    # Run after the drill to put the machine back as it was.
    restore: str = ""
    # Answering straight from the prompt's machine-map summary is also fine here,
    # so no tool call counts as a pass as well as any tool in `expect`.
    allow_no_tool: bool = False


DRILLS: list[Drill] = [
    # ---- the things asked most often ------------------------------------
    Drill("basics", "what time is it?", ("get_datetime",)),
    Drill("basics", "what's today's date?", ("get_datetime",)),

    # ---- system health ---------------------------------------------------
    Drill("system", "how's my CPU doing?", ("system_stats",)),
    Drill("system", "how much RAM have I got free?", ("system_stats",)),
    Drill("system", "how much space is left on my D drive?",
          ("system_stats", "machine_overview", "run_skill")),
    Drill("system", "what's my GPU temperature?", ("system_stats",)),
    Drill("system", "what's using the most memory right now?", ("list_processes",)),

    # ---- knowing the machine --------------------------------------------
    # Reciting from the machine-map summary is ideal (instant), but pulling the
    # same data with machine_overview is also correct — both are accepted.
    Drill("machine", "what are my PC specs?", ("machine_overview",),
          allow_no_tool=True),
    # is_app_running is accepted here because it now consults the machine map and
    # reports installed-but-not-running, so it answers this correctly too.
    Drill("machine", "do I have Photoshop installed?", ("find_installed_app",
                                                        "installed_apps",
                                                        "is_app_running")),
    # No tool wanted: the default browser is a static specification and it is
    # already in the machine-map summary, so reaching for a tool is waste.
    Drill("machine", "what's my default browser?"),
    Drill("machine", "what CPU do I have?"),

    # ---- display and sound ----------------------------------------------
    Drill("display", "what brightness are my monitors at?", ("get_brightness",)),
    Drill("display", "set my monitors to 65 percent brightness",
          ("set_brightness",), mutates=True,
          restore="set both monitors back to 80 percent brightness"),
    Drill("sound", "what's my volume at?", ("get_volume",)),
    Drill("sound", "turn the volume down to 30", ("set_volume", "adjust_volume"),
          mutates=True, restore="set the volume to 50"),

    # ---- windows and apps ------------------------------------------------
    Drill("apps", "what windows do I have open?", ("list_windows",)),
    Drill("apps", "is Brave running?", ("is_app_running",)),

    # ---- the web ---------------------------------------------------------
    Drill("web", "what's the population of Japan?", ("search_and_read", "web_search")),

    # ---- eyes and ears ---------------------------------------------------
    Drill("senses", "what's on my screen right now?", ("see_screen",)),
    Drill("senses", "can you hear what's playing? tell me what they're saying",
          ("hear_audio", "run_skill")),

    # ---- memory and learning --------------------------------------------
    Drill("memory", "remember that I prefer my brightness at 80 percent",
          ("remember",), mutates=True),
    Drill("skills", "what skills do you have?", ("list_skills",)),
]


@dataclass
class Result:
    drill: Drill
    tools: list[str] = field(default_factory=list)
    reply: str = ""
    seconds: float = 0.0

    @property
    def looped(self) -> bool:
        return "going in circles" in self.reply.lower()

    @property
    def passed(self) -> bool:
        if self.looped or not self.reply.strip():
            return False
        if not self.tools:
            return self.drill.allow_no_tool or not self.drill.expect
        if not self.drill.expect:
            return False
        return any(t in self.drill.expect for t in self.tools)

    @property
    def verdict(self) -> str:
        if self.passed:
            return "PASS"
        if self.looped:
            return "LOOP"
        if not self.tools:
            return "NO-TOOL"
        return "WRONG-TOOL"


async def run_drill(brain: Brain, drill: Drill) -> Result:
    result = Result(drill=drill)

    async def on_event(event: dict) -> None:
        if event.get("type") == "tool_call":
            result.tools.append(event["tool"])

    # Each drill is a fresh conversation: practice measures the instinct on a
    # cold request, not whether context from the previous answer carried it.
    brain.reset()
    started = time.perf_counter()
    result.reply = await brain.respond(drill.request, on_event)
    result.seconds = time.perf_counter() - started
    return result


async def main() -> int:
    parser = argparse.ArgumentParser(description="Practice drills for JARVIS")
    parser.add_argument("--only", default="", help="run one category only")
    parser.add_argument("--safe", action="store_true",
                        help="skip drills that change machine state")
    args = parser.parse_args()

    drills = DRILLS
    if args.only:
        drills = [d for d in drills if d.category == args.only]
    if args.safe:
        drills = [d for d in drills if not d.mutates]
    if not drills:
        print("no drills match that filter")
        return 1

    brain = Brain()
    print(f"Running {len(drills)} drills...\n")

    results: list[Result] = []
    for index, drill in enumerate(drills, 1):
        result = await run_drill(brain, drill)
        results.append(result)
        mark = {"PASS": "+", "LOOP": "!", "NO-TOOL": "-", "WRONG-TOOL": "x"}[result.verdict]
        print(f"[{index:2}/{len(drills)}] {mark} {result.verdict:11} "
              f"{drill.request[:46]:46} {result.seconds:5.1f}s")
        print(f"          tools: {result.tools or '(none)'}")
        print(f"          says : {result.reply.strip()[:110]}")
        if not result.passed and drill.expect:
            print(f"          wanted: one of {list(drill.expect)}")
        print()

        if drill.restore:
            await run_drill(brain, Drill(drill.category, drill.restore))

    # ---- scorecard ---------------------------------------------------------
    passed = sum(1 for r in results if r.passed)
    total = len(results)
    print("=" * 72)
    print(f"SCORE: {passed}/{total} ({100 * passed // total}%)")

    by_category: dict[str, list[Result]] = {}
    for result in results:
        by_category.setdefault(result.drill.category, []).append(result)
    for category, items in sorted(by_category.items()):
        ok = sum(1 for r in items if r.passed)
        print(f"  {category:10} {ok}/{len(items)}")

    failures = [r for r in results if not r.passed]
    if failures:
        print("\nNeeds work:")
        for r in failures:
            print(f"  [{r.verdict}] {r.drill.request}")
            print(f"      chose {r.tools or '(nothing)'}, wanted "
                  f"{list(r.drill.expect) or '(no tool)'}")
    else:
        print("\nEvery drill passed.")

    slowest = sorted(results, key=lambda r: r.seconds, reverse=True)[:3]
    print("\nSlowest: " + ", ".join(f"{r.drill.request[:28]} {r.seconds:.1f}s"
                                    for r in slowest))
    return 0 if not failures else 2


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
