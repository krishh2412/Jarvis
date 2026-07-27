# J.A.R.V.I.S. — orientation for AI assistants

A local, offline voice+text assistant that really controls this Windows 11 PC, styled after
JARVIS from Iron Man. Everything runs on the machine: the language model is Ollama, speech
and vision are local models, and there is no server, no port and no cloud dependency. The
one optional exception is "Claude mode", which talks to the Anthropic API if a key exists.

Read this file before exploring. It is the map; it will save you a lot of searching.

---

## Run, test, build

```bash
.\run.ps1              # desktop app (native window)
.\run.ps1 -Console     # text console — no audio hardware needed, best for testing
.venv\Scripts\python.exe -m pytest            # 68 tests, ~0.5s
.venv\Scripts\python.exe scripts\practice.py  # 22 behaviour drills against the real model
.\build.ps1 -DistRoot release                 # package to release\JARVIS\JARVIS.exe
```

Use `.venv\Scripts\python.exe` explicitly — the project is not activated for you, and the
system Python lacks every dependency. `.venv-train` is a **separate** environment holding
torch/CUDA for fine-tuning only; never install app dependencies into it.

**Build to `release\`, not `dist\`** — Windows Defender holds locks on `dist\` that make
PyInstaller fail with "Access is denied". Do not wrap `build.ps1` in `*>&1 | Tee-Object`:
PyInstaller logs to stderr, and PowerShell 5.1 turns each line into an error record that
kills the pipeline mid-build.

---

## Architecture

```
jarvis/
  config.py        every tunable; loads config.json if present. NOTHING hardcodes a path.
  main.py          desktop entry point      cli.py  text console
  core/
    brain.py         Ollama chat + the multi-round tool-calling loop (the local brain)
    claude_brain.py  the same loop against the Anthropic API (Claude mode)
    manager.py       switches between the two brains at runtime
    persona.py       system prompts — BASE_PROMPT, TOOL_MAP, EXAMPLES, CLAUDE_PROMPT
    memory.py        preferences / facts / corrections  (data/memory/memories.jsonl)
    machine_map.py   the indexed model of this PC       (data/memory/machine_map.json)
    learning.py      episodic log, reflection pass, the learned-notes markdown files
    skills.py        the skill library — reusable Python the model writes for itself
    study.py         self-improvement sessions ("go study")
  tools/
    registry.py    @tool decorator -> JSON schema -> audited, gated execution
    system.py apps.py files.py screen.py media.py web.py ui.py
    machine.py     machine-map lookups          hearing.py  hears what the PC plays
    skills_tool.py learning_tool.py safety.py
  audio/           wake word, STT (faster-whisper), TTS (Kokoro), the voice loop
  ui/              pywebview native window (window.py) + the HUD (hud.py)
  safety/
    gate.py        allow / confirm / block, and the study-mode sandbox
    audit.py       append-only log of every tool call, written BEFORE it runs
    journal.py     undo journal + trash store
skills/            model-written skills + _index.json
scripts/           fetch_models, practice, gen_dataset, dataset_from_logs, train_lora, ...
data/
  ollama/          THE BRAINS — Ollama's model store lives INSIDE the project (~24 GB)
  models/          Kokoro + Whisper weights
  memory/          machine_map.json, identity.md, memories.jsonl, user_facts.md,
                   machine_notes.md, failures.md, open_questions.md, study_log.md
  learning/        episodic-YYYY-MM-DD.jsonl — every turn, raw, for reflection
```

Nothing under `data/` is committed. `release/`, `build/`, `.venv*` are ignored too.

---

## How a turn works

1. `Brain.respond()` sends the system prompt + history + **all 73 tool schemas** to the model.
2. The model may return tool calls; each goes through `registry.execute()`.
3. `execute()` audits the attempt *before* running, asks `gate.check()`, then runs the function.
4. Results go back as `role: "tool"` messages and the loop repeats (max 12 rounds).
5. The finished turn is appended to the episodic log for later reflection.

Adding a tool is just a decorated function — the schema is generated from type hints and the
docstring:

```python
@tool(category="system", destructive=True)
def do_thing(name: str, count: int = 3) -> dict:
    """One-line summary the model reads when choosing this tool.

    Args:
        name: What this is.
    """
```

The **docstring is the model's decision-making input**, so write it for the model, not for a
human reader. If the model keeps picking the wrong tool, fix the docstring before touching
the system prompt (see the law about defensive tools below).

---

## Safety model — read before changing anything here

`config.safety.level` is `full` / `standard` (default) / `strict`. Three layers, in order:

1. **Hard denylist** — catastrophic shell commands are refused at *every* level.
2. **Classification** — safe / reversible / irreversible. Reversible destructive file ops are
   journaled and undoable, so they pass silently.
3. **Level policy** — `standard` confirms only irreversible actions.

Confirmation goes to a **human**, synchronously, through an approver callback that the UI
(`ui/window.py`) and console (`cli.py`) register at startup. **There is deliberately no
`confirm_action` tool and no token.** An earlier design let the model pass a token back to
approve its own actions, which put an 8B model in charge of authorising shutdowns. Do not
reintroduce it. If no approver is registered — a bare script, a headless embed — confirmable
actions are **refused**, and that is correct, not a bug. (This is why calling `run_command`
from a test script returns `denied`; test through the app or register an approver.)

Study mode (`study.py`) runs inside `gate.enter_study()`: it may read anything but may only
write inside the project, via a small allowlist.

---

## Laws learned the hard way (these cost real debugging time)

**`num_ctx` is a correctness floor, not just a VRAM knob.** The system prompt plus 73 tool
schemas is ~12.5k tokens *before* any message. At the old `num_ctx=8192` the model silently
lost tool *results* and looped calling the same tool 12 times. It is 16384 now. If you add
many tools or grow the prompt, re-check this budget first — it presents as bizarre model
behaviour, not as an error.

**A small model reproduces the *shape* of whatever you show it.** This bit three times:
- Realistic numbers in few-shot examples → recited as if they were live readings
  ("Sitting at eleven percent, Sir" with no tool call — pure confabulation).
- `[call system_stats]` annotations added to fix that → typed out *as speech*.
- `User:` / `You:` labels in examples → the literal `You:` prefix leaked into replies.

The design that works: `EXAMPLES` holds spoken replies only, for requests with no fabricable
numbers; tool selection lives in `TOOL_MAP`, a plain question→tool reference table. Reference
shapes get used as reference; dialogue shapes get echoed. **Never put a token in an example
you would not want spoken aloud.**

**Specification vs measurement.** The prompt draws this line and it matters: specifications
(CPU model, installed RAM, default browser) may be recited from the machine map; measurements
(load, free space, volume, brightness, time) must be tool-called *this turn*. Settings count
as measurements — the user may have changed them a second ago.

**When prompting cannot fix a tool confusion, fix the tool.** The model kept answering "do I
have Photoshop installed?" with `is_app_running`, even with an explicit warning in the
docstring. Rather than fight it, `is_app_running` now consults the machine map and reports
`installed: true` when the app exists but is not running — so the "wrong" tool returns a right
answer. Prefer designing tools where a plausible mis-selection still yields truth.

**Windows ctypes: always declare `.argtypes`/`.restype`.** A 64-bit HANDLE marshalled as a
C `int` is silently truncated. `SetMonitorBrightness` (dxva2) and `AssocQueryStringW`
(Shlwapi) both failed this way; reads sometimes survive by luck, writes do not.

**Qwen3 needs `think=False` as an API field.** The in-prompt `/no_think` directive does
nothing. Measured 9x fewer generated tokens on a tool-calling turn.

**Hearing needs `soundcard`, not `sounddevice`.** This PortAudio build has no loopback flag,
and Realtek "Stereo Mix" only mirrors the onboard output — it is silent when audio goes to a
USB headset. `soundcard` does WASAPI loopback on whatever the current default output is.

**`OLLAMA_MODELS` must point at `<project>\data\ollama`.** The model store lives inside the
project so the whole app is one portable folder. `run.ps1` sets it per-launch, but the
packaged exe relies on the persistent user variable — **if the project folder is ever moved,
repoint that variable or Ollama loses every model.**

Other traps: `onnxruntime` and `onnxruntime-gpu` collide (pin the GPU one only); several CUDA
wheels ship no `__init__.py`, so glob for their DLLs rather than importing; cuDNN must be
≥9.3; PowerShell parses an entire script before executing, so one bad line prevents *all* of
it running.

---

## How it improves itself

Not by retraining weights — by accumulating files it reads back at startup:

- **Episodic log** (`data/learning/`) — every turn, written by both brains.
- **Reflection** (`learning.reflect()`) — the model reads new turns and writes dated notes to
  `user_facts.md` / `machine_notes.md` / `failures.md`. It has caught real bugs the test
  drills could not, because drills score *tool choice* while reflection reads *outcomes*.
- **Skill library** (`skills/`) — solved multi-step problems saved as `def run(ctx, ...)`
  Python, where `ctx.call("tool", ...)` reaches any tool through the normal gate.
- **Study mode** — explores, reflects, tests its own skills, researches `open_questions.md`.
- **Startup context** — `persona._startup_context()` splices the machine-map summary, the
  learned notes and the skill index into the prompt, so it boots already knowing this PC.

`scripts/practice.py` is the measuring stick: 22 drills naming the tool a good answer would
use. Run it after touching prompts or tool descriptions — behaviour regressions are invisible
otherwise. It went 71% → 100% over one session of fixes.

Fine-tuning exists (`scripts/gen_dataset.py`, `dataset_from_logs.py`, `train_lora.py`,
`merge_to_ollama.py`, in `.venv-train`) but is **not** the way to add capability: it is good
for voice, bad for tool-calling on a small model. `qwen3:8b` remains the tool brain.

---

## Conventions

- **Both brains must stay at parity.** `brain.py` and `claude_brain.py` each need episodic
  logging, the loop-breaker and the shared startup context. It is easy to add a feature to one
  and forget the other — that has happened.
- Tools return plain dicts. Errors come back as `{"error": ...}` strings the model can read
  and adapt to, never raised exceptions, which would end the turn.
- Comments explain *why*, not what. The codebase is written to be read; match that density.
- Replies are spoken aloud: no markdown, no lists, one or two sentences. `speakable()` strips
  what slips through.
- `config.py` owns every path and tunable. Do not hardcode.
