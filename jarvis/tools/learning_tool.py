"""Tools for the self-learning loop: study mode, reflection, open questions.

These let JARVIS drive his own improvement — kick off a study session when Sir
says "go study", note a knowledge gap to research later, or reflect on recent
sessions on demand.
"""

from __future__ import annotations

from jarvis.core import learning, study
from jarvis.tools.registry import tool


@tool(category="learning")
def study_now(research_questions: int = 3) -> dict:
    """Run a self-improvement session now: explore, reflect, test skills, research.

    Use when Sir says "go study" or "go learn". JARVIS steps out of assistant
    mode and improves himself — rescans the machine, distils lessons from recent
    sessions, checks his skills still work, researches open questions on the web,
    and may write a new skill. Read-only outside the project folder throughout.
    Returns a summary; the full report is written to study_log.md.

    Args:
        research_questions: How many open questions to research this session.
    """
    report = study.run_study(max_questions=research_questions)
    return {
        "seconds": report.get("seconds"),
        "explored": report.get("explore", {}).get("new_apps", []),
        "reflected": report.get("reflect", {}),
        "skills_broken": report.get("skills", {}).get("broken", []),
        "researched": report.get("research", {}).get("answered", 0),
        "new_skill": report.get("new_skill", {}).get("created"),
    }


@tool(category="learning")
def note_open_question(question: str) -> dict:
    """Record something you don't know, to research later in study mode.

    When you hit a genuine knowledge gap — a fact you couldn't confirm, a tool
    that behaved unexpectedly, something about Sir's setup you'd benefit from
    knowing — note it here. Study mode works through these.

    Args:
        question: The open question, phrased so it can be researched.
    """
    added = learning.append_notes(learning.OPEN_QUESTIONS, [question])
    return {"noted": bool(added), "question": question}


@tool(category="learning")
def reflect_now() -> dict:
    """Distil lessons from recent sessions into memory right now.

    Reads the episodic log since the last reflection and writes dated notes about
    Sir, the machine, and any failures. Normally runs in study mode; this forces
    it on demand.
    """
    return learning.reflect()
