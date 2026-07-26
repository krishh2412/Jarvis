"""Build a fine-tuning dataset from how JARVIS is ACTUALLY used.

scripts/gen_dataset.py invents examples. This one reads the episodic log —
every real turn, with the tools that ran and the reply that was given — and
turns the good ones into training data. Fine-tuning on this teaches the model
Sir's real requests in Sir's real words, on this machine, rather than a
synthetic approximation of them.

    .venv\\Scripts\\python.exe scripts\\dataset_from_logs.py
    .venv\\Scripts\\python.exe scripts\\dataset_from_logs.py --blend   (+ synthetic)

Output is data/finetune/jarvis_sft.jsonl, exactly the format
scripts/train_lora.py already consumes.

What this trains, honestly
--------------------------
It trains VOICE and ANSWER SHAPE: given a request of this kind, reply this
tersely, in this register, with this level of detail. That is what fine-tuning
is genuinely good at, and it is what lets the runtime system prompt shrink.

It does NOT train tool CALLING. Teaching a small model to pick the right tool
from 75 needs the tool-call message format and far more data than a few
hundred turns, and a 4B trained on a handful of examples would be worse at it
than qwen3:8b is natively. So the tool brain stays qwen3:8b; this makes the
voice cheaper and more consistent.

Quality rules applied here
--------------------------
- Only turns that succeeded: no tool errors, no "going in circles", non-empty.
- No duplicate requests, and a cap per near-identical request so "what time is
  it" cannot dominate the gradient.
- Replies that are pure tool echo or obviously broken are dropped.
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

LEARNING_DIR = ROOT / "data" / "learning"
OUT_FILE = ROOT / "data" / "finetune" / "jarvis_sft.jsonl"

# The compact system prompt the fine-tune trains against. Short on purpose: once
# the voice is in the weights, inference does not need the full 4,000-token
# persona, which buys back context for tool schemas and conversation.
#
# Taken from gen_dataset so real and synthetic examples train against a byte-
# identical system prompt — two near-identical prompts would teach the model that
# the wording varies, which is the opposite of what a persona fine-tune is for.
try:
    from scripts.gen_dataset import SYSTEM  # type: ignore
except Exception:  # noqa: BLE001 - fall back to a copy if the import path shifts
    SYSTEM = (
        "You are J.A.R.V.I.S., Ahmad's dry, formal, unflappable British AI butler. "
        "Address him as Sir, sparingly. Report outcomes, not process. One or two "
        "plain-spoken sentences. Never markdown, never lists, never gush."
    )

# Replies that mean the turn failed, however politely.
_BAD_REPLY = re.compile(
    r"going in circles|i (could not|couldn't) complete|let me try a different "
    r"approach|i understand\. let me know",
    re.IGNORECASE,
)

# How many examples may share the same normalised request.
_MAX_PER_REQUEST = 3


def _normalise(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", text.lower()).strip()


def load_turns() -> list[dict]:
    turns: list[dict] = []
    for path in sorted(LEARNING_DIR.glob("episodic-*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                turns.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return turns


def is_good(turn: dict) -> bool:
    reply = (turn.get("reply") or "").strip()
    if len(reply) < 2:
        return False
    if turn.get("had_error"):
        return False
    if any(not a.get("ok", True) for a in turn.get("actions", [])):
        return False
    if _BAD_REPLY.search(reply):
        return False
    # A reply that is mostly punctuation or a raw dict is not a spoken answer.
    if reply.startswith("{") or reply.startswith("["):
        return False
    return True


def build(blend: bool) -> list[dict]:
    turns = load_turns()
    good = [t for t in turns if is_good(t)]

    seen: Counter[str] = Counter()
    examples: list[dict] = []
    for turn in good:
        key = _normalise(turn.get("request", ""))
        if not key:
            continue
        seen[key] += 1
        if seen[key] > _MAX_PER_REQUEST:
            continue
        examples.append({
            "messages": [
                {"role": "system", "content": SYSTEM},
                {"role": "user", "content": turn["request"].strip()},
                {"role": "assistant", "content": turn["reply"].strip()},
            ]
        })

    print(f"episodic turns      : {len(turns)}")
    print(f"usable (succeeded)  : {len(good)}")
    print(f"after de-duplication: {len(examples)}")

    if blend:
        try:
            from scripts.gen_dataset import build as build_synthetic  # type: ignore
            synthetic = build_synthetic()
            print(f"blended synthetic   : {len(synthetic)}")
            examples.extend(synthetic)
        except Exception as exc:  # noqa: BLE001
            print(f"(could not blend synthetic examples: {exc})")

    random.shuffle(examples)
    return examples


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--blend", action="store_true",
                        help="also include the synthetic persona examples")
    parser.add_argument("--min", type=int, default=50,
                        help="refuse to write fewer than this many examples")
    args = parser.parse_args()

    examples = build(args.blend)

    if len(examples) < args.min:
        print(f"\nOnly {len(examples)} examples — too few to fine-tune on "
              f"(want at least {args.min}).")
        print("Use JARVIS for real work for a while, or run scripts/practice.py "
              "a few times, then run this again. Real usage is the point.")
        return 1

    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT_FILE, "w", encoding="utf-8") as fh:
        for example in examples:
            fh.write(json.dumps(example, ensure_ascii=False) + "\n")

    print(f"\nwrote {len(examples)} examples -> {OUT_FILE}")
    print("Next: .venv-train\\Scripts\\python.exe scripts\\train_lora.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
