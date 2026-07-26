"""Study mode: JARVIS improving himself while no one is asking anything.

Run on a schedule (once a day) or on demand ("go study"). For the duration he is
not an assistant answering questions — he is a system maintaining itself. Five
things happen, each best-effort and independently wrapped so one failure never
sinks the whole session:

1. **Explore** — rescan the machine map and note what changed (new apps, drives),
   so his picture of the PC stays current.
2. **Reflect** — distil any un-reflected recent sessions into dated notes.
3. **Test skills** — load every skill, confirm it still compiles and exposes
   run(); flag the broken ones for repair.
4. **Research** — take unanswered questions from open_questions.md, search the
   web, and write back what he learned.
5. **Learn from failure** — read the failure log and, when a recurring failure
   clearly warrants it, write a new skill to prevent the repeat.

Everything runs inside the study-mode sandbox (gate.enter_study): he may read and
explore anything, but may only write inside his own project folder. A short report
of what changed is written to study_log.md.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from jarvis.core import learning, machine_map, skills
from jarvis.safety import gate
from jarvis.tools.registry import execute


def run_study(max_questions: int = 3, allow_new_skills: bool = True) -> dict[str, Any]:
    """Run one self-improvement session. Returns a structured report."""
    started = datetime.now()
    report: dict[str, Any] = {"started": started.isoformat(timespec="seconds")}

    gate.enter_study()
    try:
        report["explore"] = _explore_system()
        report["reflect"] = _safe(lambda: learning.reflect(), "reflect")
        report["skills"] = _validate_skills()
        report["research"] = _research(max_questions)
        if allow_new_skills:
            report["new_skill"] = _learn_from_failures()
    finally:
        gate.exit_study()

    report["finished"] = datetime.now().isoformat(timespec="seconds")
    report["seconds"] = round((datetime.now() - started).total_seconds(), 1)
    _write_report(report)
    return report


def _safe(fn, label: str) -> Any:
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{label} failed: {type(exc).__name__}: {exc}"}


# ---- 1. explore ------------------------------------------------------------


def _explore_system() -> dict[str, Any]:
    """Rescan the machine and note anything that changed since last time."""
    before = machine_map.load() or {}
    before_apps = {a["name"] for a in before.get("apps", [])}
    before_drives = {d["drive"] for d in before.get("hardware", {}).get("disks", [])}

    updated = machine_map.build()
    after_apps = {a["name"] for a in updated.get("apps", [])}
    after_drives = {d["drive"] for d in updated.get("hardware", {}).get("disks", [])}

    new_apps = sorted(after_apps - before_apps)
    gone_apps = sorted(before_apps - after_apps)
    new_drives = sorted(after_drives - before_drives)

    notes: list[str] = []
    if new_apps:
        notes.append(f"New apps installed: {', '.join(new_apps[:20])}")
    if gone_apps:
        notes.append(f"Apps no longer present: {', '.join(gone_apps[:20])}")
    if new_drives:
        notes.append(f"New drive(s) detected: {', '.join(new_drives)}")
    if notes:
        learning.append_notes(learning.MACHINE_NOTES, notes)

    return {"apps_now": len(after_apps), "new_apps": new_apps,
            "removed_apps": gone_apps, "new_drives": new_drives}


# ---- 3. test skills --------------------------------------------------------


def _validate_skills() -> dict[str, Any]:
    """Confirm each skill still loads and exposes run(); flag the broken ones."""
    checked, broken = [], []
    for entry in skills.list_skills():
        name = entry["name"]
        source = skills.get_source(name)
        ok = True
        reason = ""
        if source is None:
            ok, reason = False, "file missing"
        else:
            try:
                ns: dict[str, Any] = {}
                exec(compile(source, f"<skill:{name}>", "exec"), ns)  # noqa: S102
                if not callable(ns.get("run")):
                    ok, reason = False, "no run(ctx, ...)"
            except Exception as exc:  # noqa: BLE001
                ok, reason = False, f"{type(exc).__name__}: {exc}"
        checked.append(name)
        if not ok:
            broken.append({"name": name, "reason": reason})

    if broken:
        learning.append_notes(learning.FAILURES, [
            f"Skill '{b['name']}' is broken ({b['reason']}) — repair with save_skill"
            for b in broken
        ])
    return {"checked": checked, "broken": broken}


# ---- 4. research -----------------------------------------------------------


def _open_questions() -> list[tuple[int, str]]:
    """Unanswered question lines from open_questions.md, as (line_index, text)."""
    path = learning.OPEN_QUESTIONS
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    out: list[tuple[int, str]] = []
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped.startswith("- "):
            continue
        if "[answered" in stripped.lower() or "[x]" in stripped.lower():
            continue
        text = stripped[2:].strip().lstrip("[ ]").strip()
        if text:
            out.append((i, text))
    return out


def _research(max_questions: int) -> dict[str, Any]:
    """Web-research a few open questions and write the answers back."""
    questions = _open_questions()
    if not questions:
        return {"answered": 0, "note": "no open questions"}

    answered: list[dict[str, str]] = []
    path = learning.OPEN_QUESTIONS
    lines = path.read_text(encoding="utf-8").splitlines()

    for line_idx, question in questions[:max_questions]:
        found = execute("search_and_read", {"query": question, "results_to_read": 2})
        answer = _synthesise_answer(question, found)
        if not answer:
            continue
        # Mark the question answered in place, and record the Q&A.
        lines[line_idx] = lines[line_idx].rstrip() + \
            f"  [answered {datetime.now():%Y-%m-%d}]"
        answered.append({"question": question, "answer": answer})

    if answered:
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        learning.append_notes(learning.USER_FACTS, [
            f"Q: {a['question']} — A: {a['answer']}" for a in answered
        ])
    return {"answered": len(answered), "items": answered}


def _synthesise_answer(question: str, search_result: Any) -> str:
    """Ask the model to answer the question from what the web search returned."""
    material = str(search_result)[:6000]
    prompt = (
        f"Question: {question}\n\nWeb search results:\n{material}\n\n"
        "Answer the question in ONE short factual sentence based only on the "
        "results above. If the results do not actually answer it, reply exactly "
        "'INSUFFICIENT'."
    )
    try:
        import ollama
        from jarvis.config import config
        response = ollama.Client(host=config.llm.host).chat(
            model=config.llm.model,
            messages=[{"role": "user", "content": prompt}],
            options={"temperature": 0.2, "num_ctx": config.llm.num_ctx},
            think=False,
        )
        answer = (response.get("message", {}).get("content", "") or "").strip()
    except Exception:  # noqa: BLE001
        return ""
    return "" if not answer or "INSUFFICIENT" in answer.upper() else answer


# ---- 5. learn from failures ------------------------------------------------


def _learn_from_failures() -> dict[str, Any]:
    """When the failure log shows a recurring, automatable problem, write a skill.

    Conservative on purpose: the model proposes at most one skill per session,
    and it must pass the same safety scan and compile check as any hand-written
    skill before it is saved.
    """
    fails = learning.read_notes(learning.FAILURES, 2000)
    if not fails or fails.count("- ") < 2:
        return {"created": None, "note": "not enough failure history to act on"}

    existing = ", ".join(s["name"] for s in skills.list_skills()) or "none"
    prompt = (
        "Below is JARVIS's failure log. If — and only if — one recurring failure "
        "could be prevented by a small reusable skill, write ONE skill for it. A "
        "skill is Python defining `def run(ctx, ...)`, where ctx.call('tool', "
        "arg=val) runs a JARVIS tool and ctx.folders holds known folders.\n\n"
        f"Existing skills (do not duplicate): {existing}\n\n"
        f"Failure log:\n{fails}\n\n"
        "Reply with a JSON object {\"name\": snake_case, \"description\": one line, "
        "\"code\": python} — or exactly {\"name\": null} if no skill is warranted. "
        "No prose, no code fences."
    )
    try:
        import ollama
        from jarvis.config import config
        response = ollama.Client(host=config.llm.host).chat(
            model=config.llm.model,
            messages=[{"role": "user", "content": prompt}],
            options={"temperature": 0.3, "num_ctx": config.llm.num_ctx},
            think=False,
        )
        raw = response.get("message", {}).get("content", "") or ""
    except Exception as exc:  # noqa: BLE001
        return {"created": None, "error": f"model call failed: {exc}"}

    proposal = learning._extract_json(raw)
    if not proposal or not proposal.get("name") or not proposal.get("code"):
        return {"created": None, "note": "model proposed no skill"}

    try:
        skills.save(proposal["name"], proposal.get("description", ""),
                    proposal["code"])
        return {"created": proposal["name"],
                "description": proposal.get("description", "")}
    except skills.SkillError as exc:
        return {"created": None, "rejected": str(exc)}


# ---- report ----------------------------------------------------------------


def _write_report(report: dict[str, Any]) -> None:
    """Append a short, human-readable summary of the session to study_log.md."""
    learning._ensure(learning.STUDY_LOG)
    ts = report.get("finished", datetime.now().isoformat(timespec="seconds"))
    lines = [f"\n## Study session {ts} ({report.get('seconds', '?')}s)"]

    ex = report.get("explore", {})
    if ex.get("new_apps"):
        lines.append(f"- Explored: {len(ex['new_apps'])} new app(s) — "
                     f"{', '.join(ex['new_apps'][:8])}")
    else:
        lines.append(f"- Explored: {ex.get('apps_now', '?')} apps mapped, nothing new")

    ref = report.get("reflect", {})
    if isinstance(ref, dict) and ref.get("notes_written"):
        nw = ref["notes_written"]
        lines.append(f"- Reflected on {ref.get('reflected', 0)} interaction(s): "
                     f"{nw.get('user_facts', 0)} facts, {nw.get('machine_notes', 0)} "
                     f"machine notes, {nw.get('failures', 0)} failures")
    else:
        lines.append(f"- Reflected: {ref.get('note', ref) if isinstance(ref, dict) else ref}")

    sk = report.get("skills", {})
    lines.append(f"- Skills: {len(sk.get('checked', []))} checked, "
                 f"{len(sk.get('broken', []))} broken")

    res = report.get("research", {})
    if res.get("answered"):
        for item in res.get("items", []):
            lines.append(f"- Researched: {item['question']} → {item['answer']}")
    else:
        lines.append(f"- Research: {res.get('note', 'nothing to research')}")

    ns = report.get("new_skill", {})
    if ns and ns.get("created"):
        lines.append(f"- Wrote new skill '{ns['created']}': {ns.get('description', '')}")

    try:
        with open(learning.STUDY_LOG, "a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
    except OSError:
        pass
