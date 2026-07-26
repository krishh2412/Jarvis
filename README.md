# J.A.R.V.I.S.

A local voice assistant with full control of a Windows PC.

Say "hey JARVIS", ask for something, and it happens — files moved, windows
closed, volume dropped, the web searched, PowerShell run. It answers in a
British male voice, in two sentences, and does not ask permission first.

Everything runs on your machine. No API keys, no accounts, no cloud, no
telemetry. The only network traffic is the web searches you ask for.

```
     _     _    ____  __     __ ___  ____
    | |   / \  |  _ \ \ \   / /|_ _|/ ___|
 _  | |  / _ \ | |_) | \ \ / /  | | \___ \
| |_| | / ___ \|  _ <   \ V /   | |  ___) |
 \___/ /_/   \_\_| \_\   \_/   |___||____/
```

---

## What it actually is

A native Windows desktop application. Double-click `JARVIS.exe` and a window
opens — a real Win32 window rendered by the WebView2 runtime that ships with
Windows 11. There is no local web server, no port, no `localhost` URL and no
browser tab. The HUD is handed to the window as an HTML string and talks to
Python over pywebview's `js_api` bridge.

There is also a text console (`run.ps1 -Console`) that exercises the same brain
and the same tools with no audio hardware in the loop. It is the fastest way to
check whether JARVIS actually does what you asked.

---

## Hardware

Built and tuned for an **RTX 3060 12 GB / 32 GB RAM** desktop. The VRAM budget
is the design constraint that everything else falls out of:

| Component | VRAM | Notes |
| --- | --- | --- |
| Qwen3 8B (Q4_K_M) via Ollama | ~6 GB | ~35 tok/s on a 3060 |
| faster-whisper `small.en` (int8_float16) | ~1 GB | |
| Kokoro TTS (ONNX, CUDA provider) | ~0.5 GB | faster than realtime |
| openWakeWord | — | CPU, always on, negligible |

That leaves headroom on a 12 GB card with all four resident at once. The LLM
context defaults to 16384 — not a VRAM choice so much as a correctness floor,
since the system prompt plus 73 tool schemas is ~11–12k tokens before a single
message, and a smaller window silently truncated the tool results the model
needed.

**Minimum:** any NVIDIA GPU with 8 GB will work if you drop to a smaller LLM.
**Without a GPU:** everything still runs — set `stt.device` to `cpu` and
`tts.provider` to `CPUExecutionProvider` — but speech goes from snappy to
several seconds per turn.

**Also required:** Windows 10/11 (the tooling is Win32 and COM throughout),
Python 3.11 or 3.12, roughly 20 GB free disk for models and the build, a
microphone, and speakers.

---

## Architecture

Four models, four processes' worth of work, one Python process plus Ollama.

```
  microphone
      |
  openWakeWord ("hey_jarvis", ONNX, CPU, always listening)
      |
  webrtcvad  ---> endpointing: 0.8s of silence ends the utterance
      |
  faster-whisper small.en  (CTranslate2, CUDA, int8_float16)
      |
  Brain  <---->  Ollama / qwen3:8b       reasoning + tool calling
      |            (HTTP, 127.0.0.1:11434, its own process)
      |
      +--------> tool registry: 73 tools, up to 12 rounds per turn
      |
  Kokoro-82M (ONNX, CUDA)  --->  speakers
      |
  pywebview HUD (WebView2, js_api bridge)
```

**Deliberately torch-free.** Speech-to-text runs on CTranslate2, TTS and the
wake word on ONNX Runtime, and the LLM lives in Ollama's own process. That
keeps the packaged application around 700 MB instead of the ~3 GB a PyTorch
stack would cost, and removes any dependency on a system-wide CUDA install —
cuBLAS and cuDNN arrive as pip wheels.

**The reasoning loop** (`jarvis/core/brain.py`) is a straightforward multi-round
tool-calling loop: send the conversation plus every tool schema to Ollama, run
whatever tools come back, feed the results in, repeat until the model replies
with prose instead of a tool call. It stops after `max_tool_rounds` (12) and
says so rather than spinning. Qwen3's `<think>` blocks are stripped before
anything is displayed or spoken, and markdown is flattened before it reaches
the TTS so you do not hear asterisks read aloud.

**The persona** (`jarvis/core/persona.py`) is deliberately terse: replies are
spoken, so the system prompt pushes hard for one or two sentences, outcomes
rather than method, and no markdown. It also instructs the model to act rather
than ask, and to search the web for anything current instead of guessing.

### Layout

Everything JARVIS is — code, weights, brains, memory and skills — lives under this
one folder. Nothing is installed into your user profile: `run.ps1` points
`OLLAMA_MODELS` at `data/ollama`, so the language models sit beside the app
rather than under `C:\Users\<you>\.ollama`. Copy this directory to another drive
or machine and the whole assistant goes with it.

```
jarvis/
  config.py        every tunable, loaded from config.json
  cli.py           text console
  main.py          desktop entry point
  core/
    brain.py       Ollama chat + tool-calling loop
    persona.py     system prompt, greetings, startup context
    memory.py      preferences / facts / corrections
    machine_map.py the indexed model of this PC (apps, folders, hardware)
    learning.py    episodic log, reflection, learned notes
    skills.py      the skill library: reusable procedures it writes itself
    study.py       self-improvement sessions ("go study")
    text.py        polarity-aware text comparison, shared by memory + learning
  tools/
    registry.py    @tool decorator, schema generation, execution, auditing
    system.py apps.py files.py screen.py media.py web.py ui.py
    machine.py     machine-map lookups
    hearing.py     hears what the PC plays (loopback -> Whisper)
    skills_tool.py learning_tool.py safety.py
  audio/           wake word, STT, TTS, the voice loop
  ui/              native window and HUD
  safety/
    gate.py        block / confirm / allow, and the study-mode sandbox
    audit.py       append-only log of every tool call
    journal.py     undo journal and trash store
skills/            model-written Python skills + _index.json
tests/             the safety layer, the undo journal, the memory store
scripts/
  fetch_models.py  one-time download of Kokoro / Whisper / wake word weights
  gen_dataset.py train_lora.py merge_to_ollama.py   fine-tuning pipeline
data/
  ollama/          THE BRAINS — Ollama's model store (~24 GB, not in git)
  models/          Kokoro + Whisper weights (not in git, not in the exe)
  memory/          machine_map.json, identity.md, memories.jsonl,
                   user_facts.md, machine_notes.md, failures.md,
                   open_questions.md, study_log.md
  learning/        episodic-YYYY-MM-DD.jsonl — every turn, for reflection
  finetune/        dataset + LoRA adapter from training runs
  logs/            audit-YYYY-MM-DD.jsonl
  trash/           stashed originals for undo
  screenshots/     last 40 captures
  journal.jsonl    undo entries
release/JARVIS/    the built JARVIS.exe and its payload
```

---

## Install

```powershell
powershell -ExecutionPolicy Bypass -File setup.ps1
```

`setup.ps1` is idempotent — every step checks before acting, so re-running it
is safe. It:

1. Checks Python (warns on anything outside 3.10–3.12).
2. Creates `.venv` and installs `requirements.txt`.
3. Installs **espeak-ng** via winget — Kokoro needs it as a phonemiser.
4. Installs **Ollama** via winget if it is missing, starts the daemon, and
   waits for `127.0.0.1:11434` to answer.
5. Pulls the language model (`qwen3:8b`, about 5 GB — or whatever `config.json`
   names instead).
6. Runs `scripts/fetch_models.py` to download Kokoro (~340 MB), Whisper
   (~500 MB) and the openWakeWord models.
7. Prints your GPU.

Budget 15–30 minutes and about 10 GB on the first run, almost all of it
downloads.

---

## Running

```powershell
.\run.ps1              # desktop app — native window, voice, wake word
.\run.ps1 -Console     # text console, no audio hardware needed
```

Both start Ollama first if it is not already up.

Console commands:

| Command | Effect |
| --- | --- |
| `/help` | command list |
| `/tools` | every registered tool, grouped by category |
| `/health` | is Ollama up, is the model pulled |
| `/reset` | clear the conversation |
| `/audit` | the last 15 tool calls |
| `/undo` | reverse the last reversible file change |
| `/quit` | exit |

### The built executable

```powershell
.\build.ps1            # normal build
.\build.ps1 -Clean     # wipe build\ and dist\ first
.\build.ps1 -NoModels  # skip the weight copy, for faster iteration
```

Output is `dist\JARVIS\JARVIS.exe`. Double-click it; the native window opens.

It is a **one-folder** build, not a single file. A one-file exe would
re-extract several hundred megabytes to `%TEMP%` on every single launch, which
turns a double-click into a ten-second stall. One-folder starts immediately.

**Ship the whole `dist\JARVIS` folder, not the exe alone.** The layout matters:

```
JARVIS\
  JARVIS.exe
  config.json          optional
  _internal\           PyInstaller payload
  data\
    models\            Kokoro + ONNX weights, copied by build.ps1
    logs\ trash\ screenshots\
```

Model weights are read from disk beside the exe rather than bundled inside it.
`config.py`'s `_project_root()` returns `Path(sys.executable).parent` when
`sys.frozen` is set, so the frozen app looks for `data\` next to `JARVIS.exe`.
This keeps rebuilds fast and lets you swap a voice pack without a rebuild.

Ollama still has to be installed and running on the target machine — it is a
separate process, not something that can be frozen into the exe.

---

## Configuration

Drop a `config.json` next to the project root (or next to `JARVIS.exe`). Every
key is optional; sections merge key by key, so a partial file overrides only
what it names. Full defaults are in `jarvis/config.py`.

```jsonc
{
  "user_title": "Sir",              // how JARVIS addresses you

  "llm": {
    "model": "qwen3:8b",
    "host": "http://127.0.0.1:11434",
    "temperature": 0.6,
    "num_ctx": 16384,               // main VRAM lever; see Hardware above
    "max_tool_rounds": 12,          // ceiling on tool calls per turn
    "history_turns": 20
  },

  "stt": {
    "model": "small.en",            // "medium.en" is better on accents, +400ms
    "device": "cuda",               // "cpu" if you have no NVIDIA GPU
    "compute_type": "int8_float16",
    "language": "en",
    "beam_size": 1
  },

  "tts": {
    "voice": "bm_george",           // bm_fable warmer, bm_lewis deeper
    "speed": 1.05,
    "sample_rate": 24000,
    "provider": "CUDAExecutionProvider"
  },

  "wake": {
    "enabled": true,
    "model": "hey_jarvis",
    "threshold": 0.5,               // lower = more sensitive, more false fires
    "endpoint_silence": 0.8,        // seconds of quiet that end an utterance
    "max_utterance": 30.0
  },

  "audio": {
    "input_device": null,           // null = system default
    "output_device": null,
    "frame_ms": 30,                 // webrtcvad accepts 10, 20 or 30
    "vad_aggressiveness": 2,        // 0 permissive .. 3 strict
    "bargein_speech_ms": 240        // speech needed to interrupt playback
  },

  "safety": {
    "level": "standard",            // "full" | "standard" | "strict"
    "confirm_timeout": 120,         // seconds a confirmation waits before refusing
    "audit_log": true,
    "journal_deletes": true,
    "trash_retention_days": 30,
    "protected_paths": [
      "C:\\Windows",
      "C:\\Program Files\\WindowsApps",
      "C:\\$Recycle.Bin",
      "C:\\System Volume Information"
    ]
  },

  "ui": {
    "window_width": 1100,
    "window_height": 760,
    "frameless": true,
    "resizable": true,
    "on_top": false,
    "start_minimised": false,
    "theme_accent": "#4dd0e1"
  }
}
```

There is no `host` or `port` under `ui` by design. The HUD is not served over
HTTP and nothing listens on a socket.

---

## Tools

73 tools in twelve categories. Each is a plain Python function decorated with
`@tool`; the JSON schema the model sees is generated from its type hints and
docstring, so adding a tool means adding a function. Tools marked **`!`** are
flagged destructive in the audit log; the file ones additionally route through
the undo journal, and the irreversible ones through the confirmation gate.

These tables are generated from the registry — `/tools` in the console prints
the same list live, and is the authority if they ever drift apart.

### `files` — 12

| Tool | |
| --- | --- |
| `copy_path` **!** | Copy a file or folder. |
| `create_directory` **!** | Create a directory, including any missing parents. |
| `delete_path` **!** | Delete a file or folder, recoverably. |
| `find_files` | Search for files by name pattern anywhere under a directory. |
| `list_directory` | List the contents of a directory. |
| `move_path` **!** | Move or rename a file or folder. |
| `read_file` | Read a text file's contents. |
| `recent_changes` | List recent reversible changes with their undo ids. |
| `search_in_files` | Search file contents for a string, returning matching lines. |
| `undo_last_change` **!** | Reverse a delete or overwrite, restoring the original from the trash store. |
| `user_folders` | Get the paths of the standard user folders (Desktop, Documents, etc.). |
| `write_file` **!** | Write text to a file, creating parent directories as needed. |

### `system` — 13

| Tool | |
| --- | --- |
| `environment_info` | Report OS version, hostname, username and Python version. |
| `find_installed_app` | Look up an installed application in the machine map by spoken name. |
| `get_datetime` | Get the current local date, time, timezone and day of week. |
| `kill_process` **!** | Terminate a process by name or PID. |
| `list_processes` | List running processes, heaviest first. |
| `machine_overview` | Report the machine map's summary: hardware, OS, folders, installed apps. |
| `power_action` **!** | Lock, sleep, restart, shut down or sign out of Windows. |
| `read_clipboard` | Read the current contents of the Windows clipboard. |
| `refresh_machine_map` | Rescan this PC and rebuild the machine map (apps, folders, hardware). |
| `run_command` **!** | Run a shell command via cmd.exe and return its output. |
| `run_powershell` **!** | Run a PowerShell command and return its output. |
| `system_stats` | Report CPU, memory, disk and GPU utilisation. |
| `write_clipboard` **!** | Replace the Windows clipboard contents. |

### `apps` — 5

| Tool | |
| --- | --- |
| `active_window` | Get the title and process of the currently focused window. |
| `control_window` **!** | Focus, minimise, maximise, restore or close a window by title. |
| `installed_apps` | List installed applications found in the Start Menu. |
| `launch_app` **!** | Open an application by name. |
| `list_windows` | List open windows with their titles and owning processes. |

### `screen` — 10

| Tool | |
| --- | --- |
| `click_at` **!** | Click the mouse at absolute screen coordinates. |
| `drag_mouse` **!** | Drag the mouse from one point to another with the left button held. |
| `mouse_position` | Get the current mouse cursor position. |
| `press_keys` **!** | Press a key or a keyboard shortcut. |
| `read_window_text` | Read the text content of a window via the accessibility tree. |
| `screen_info` | Report monitor count and the resolution of each. |
| `scroll_screen` **!** | Scroll the mouse wheel. |
| `see_screen` | Actually look at the screen and answer a question about what is there. |
| `take_screenshot` | Capture the screen to a PNG file and return its path. |
| `type_text` **!** | Type text into whatever currently has keyboard focus. |

### `ui` — 4

| Tool | |
| --- | --- |
| `click_element` **!** | Click a control by its name rather than by coordinates. |
| `is_app_running` | Check whether an application is currently OPEN and running right now. |
| `list_ui_elements` | List the on-screen controls of a window and where to click them. |
| `type_in_field` **!** | Type text into a specific text box or search field by name. |

### `media` — 9

| Tool | |
| --- | --- |
| `adjust_volume` **!** | Change the volume RELATIVE to whatever it is now, by a number of points. |
| `audio_sessions` | List applications currently playing audio, with their volumes. |
| `get_brightness` | Get the current brightness of each connected monitor, as a percentage. |
| `get_volume` | Get the current system output volume and mute state. |
| `media_control` **!** | Send a media transport key, controlling whatever app has media focus. |
| `set_app_volume` **!** | Set the volume of one application without touching the system volume. |
| `set_brightness` **!** | Set monitor brightness by percentage, via DDC/CI. |
| `set_mute` **!** | Mute or unmute the system audio output. |
| `set_volume` **!** | Set the system output volume to an ABSOLUTE level. |

### `web` — 5

| Tool | |
| --- | --- |
| `open_url` **!** | Open a URL in the default browser. |
| `open_website` **!** | Open a website in the browser by common name or address. |
| `read_webpage` | Fetch a web page and extract its readable text. |
| `search_and_read` | Search the web then read the top results in one step. |
| `web_search` | Search the web and return titles, URLs and snippets. |

### `hearing` — 1

| Tool | |
| --- | --- |
| `hear_audio` | Listen to what the computer is currently playing and transcribe it. |

### `memory` — 4

| Tool | |
| --- | --- |
| `forget` **!** | Delete a specific memory by its id. |
| `list_memories` | List everything currently remembered, optionally filtered by kind. |
| `recall` | Search long-term memory for anything relevant to a topic. |
| `remember` **!** | Save something worth keeping between sessions. |

### `learning` — 3

| Tool | |
| --- | --- |
| `note_open_question` | Record something you don't know, to research later in study mode. |
| `reflect_now` | Distil lessons from recent sessions into memory right now. |
| `study_now` | Run a self-improvement session now: explore, reflect, test skills, research. |

### `skills` — 5

| Tool | |
| --- | --- |
| `delete_skill` **!** | Delete a skill that is wrong beyond repair or no longer needed. |
| `list_skills` | List the reusable skills you have written, with when to use each. |
| `run_skill` **!** | Run one of your saved skills by name. |
| `save_skill` | Save a reusable skill as Python, or repair an existing one by overwriting it. |
| `view_skill` | Show a skill's source code, e.g. |

### `safety` — 2

| Tool | |
| --- | --- |
| `safety_status` | Report the current safety level and whether confirmations can be asked. |
| `set_safety_level` **!** | Change how much JARVIS confirms before doing risky things. |

If a tool raises, the exception is caught and returned to the model as
`{"error": "..."}` rather than ending the turn — the model reads "no such
file" and adapts. Arguments the model invents that the function does not accept
are dropped rather than allowed to `TypeError`.

---

## Tests

```powershell
.\.venv\Scripts\python.exe -m pytest
```

Deliberately narrow. Most of this project fails by being unhelpful, which you
notice immediately; the parts covered here fail by letting a wrong tool call
shut down the machine or lose a file, which you might not notice at all. So the
suite covers the safety gate's decision table, the fact that only a human can
approve an action, the undo journal round-tripping files and directories, and
the memory store's de-duplication — and nothing else.

Nothing here needs a GPU, a microphone, or Ollama running.

---

## Safety

Read this section. It is not boilerplate.

### JARVIS acts rather than asks

The system prompt instructs the model to act, not to seek permission. If you say
"delete that folder", it deletes it. If you say "close everything", it closes
everything. Most actions have no "are you sure?" step, and that is deliberate —
an assistant that confirms every action is not worth talking to.

It means JARVIS is roughly as dangerous as a person with your keyboard who
occasionally misunderstands you. **Do not run it on a machine where a wrong
guess would be expensive.**

### The gate: three levels, and one thing no level can authorise

`jarvis/safety/gate.py` classifies every call before it runs as *safe*,
*reversible* (journaled, so `undo_last_change` puts it back) or *irreversible*
(a shutdown, a killed process, an arbitrary shell command, a generated skill).
What happens next depends on `safety.level`:

| Level | Behaviour |
| --- | --- |
| `full` | Nothing is confirmed. Maximum flow. |
| `standard` *(default)* | Silent for reversible work; confirms the irreversible. |
| `strict` | Confirms every destructive action. |

Above all three sits a hard denylist that no level can override: `format`,
`mkfs`, `diskpart`, registry hive deletion, shadow-copy deletion, `bcdedit`,
fork bombs. These are refused outright and you have to edit
`safety.command_denylist` by hand to change that.

### Confirmation is asked of *you*, never of the model

When a call needs a yes, the tool thread blocks and an Approve/Deny card appears
in the transcript (or a prompt on stdin, in the console). Only that click runs
the action. It times out after `safety.confirm_timeout` seconds, and a timeout
is a refusal.

The model is deliberately not in this loop, and there is no tool it can call to
approve anything. An earlier design parked the call and handed the model a token
to pass back to a `confirm_action` tool once you agreed — which meant the only
thing between a shutdown and a misread instruction was an 8B model's compliance.
It could approve itself. It no longer holds a token or a tool to use one with.

For the same reason the gate **fails closed**: if the gate itself raises — a bad
regex in a hand-edited `config.json`, say — destructive tools are refused and
only reads go through, so a broken gate degrades JARVIS to read-only rather than
to unrestricted.

### Three things stand between a mistake and a disaster

**1. Every tool call is logged.** `jarvis/safety/audit.py` appends one JSON
line per call to `data/logs/audit-YYYY-MM-DD.jsonl` — timestamp, tool name,
arguments, result or error, duration, and whether it was flagged destructive.
It is written synchronously and flushed on every call, so a crash mid-action
still leaves a record of what was attempted. Large payloads are truncated at
2000 characters so the log stays greppable. Read the recent tail with `/audit`
in the console.

**2. Destructive file operations are reversible.** Deletes and overwrites do
not touch the recycle bin and do not unlink in place. The original is copied
into a timestamped bucket under `data/trash/` and a journal entry is appended
to `data/journal.jsonl` before the source is removed. `undo_last_change` (or
`/undo`) puts it back. Entries expire after `safety.trash_retention_days`
(30 by default), at which point the stashed copies are purged.

Two honest limits: `undo` refuses to restore over a path that exists again
rather than clobbering it, and the trash store grows until it expires — check
it if disk space gets tight.

**3. A handful of paths are refused outright.** `safety.protected_paths` lists
directories JARVIS will not touch even under full autonomy: `C:\Windows`,
`C:\Program Files\WindowsApps`, `C:\$Recycle.Bin` and
`C:\System Volume Information`. These are the ones where a bad tool call bricks
the machine rather than merely annoying you. Edit the list freely if you
disagree — it is a guard rail, not a security boundary.

### What is *not* protected

- `run_powershell` and `run_command` execute arbitrary shell commands with your
  full user privileges. The denylist reads the command string, but it is a
  pattern match, not a sandbox — it stops a confused model, not a determined
  one, and the protected-path list does not apply inside a shell command.
- Nothing outside the filesystem tools is reversible. A killed process, a shut
  down machine, a sent keystroke or a clicked button cannot be undone. The gate
  can only ask you first.
- There is no sandbox and no OS-level permission model. Every tool is available
  on every turn; the gate decides what runs, and it runs in the same process.
- The undo journal covers *JARVIS's* file operations. Anything a PowerShell
  command it ran did to your disk is outside it.
- A skill that overruns its timeout is reported as abandoned, but the thread
  keeps running — Python cannot kill one. Treat the timeout as a warning, not a
  stop button.

### Privacy

Audio never leaves the machine — wake word, transcription and speech synthesis
all run locally, as does the LLM. The only outbound traffic is `web_search`,
`read_webpage` and `search_and_read`, which reach DuckDuckGo and whatever pages
you asked about. No telemetry, no accounts, no keys.

The audit log is plain text on your disk and records tool arguments verbatim —
including file contents passed to `write_file` and text passed to
`write_clipboard`. Treat `data/logs/` as sensitive.

---

## Troubleshooting

**"I cannot reach the language model."** Ollama is not running. `ollama serve`,
or re-run `setup.ps1`.

**"The model qwen3:8b is not installed."** `ollama pull qwen3:8b`.

**Voice does not start / `cublas64_12.dll` not found.** The CUDA wheels are
missing or not on the DLL search path. Confirm `nvidia-cublas-cu12` and
`nvidia-cudnn-cu12` are installed in `.venv`; `config.prepare_cuda()` puts
their `bin` directories on `PATH` at startup and the frozen build does the same
through a runtime hook.

**TTS produces silence or complains about voices.** espeak-ng is missing.
Install it with winget (`eSpeak-NG.eSpeak-NG`) or from the espeak-ng releases
page.

**Wake word never fires.** Lower `wake.threshold` (0.3 is quite sensitive),
check `audio.input_device` against `python -m sounddevice`, and confirm
`openwakeword.utils.download_models()` ran — `setup.ps1` step 8 does it.

**The built exe starts and immediately dies.** Build with `console=True`
temporarily in `jarvis.spec` to see the traceback, or check
`data/logs/audit-*.jsonl` for the last thing it managed to do.

**Note on `onnxruntime` and `onnxruntime-gpu`.** `requirements.txt` lists both.
They install into the same `onnxruntime` package directory, so whichever pip
installs last wins. If TTS silently falls back to CPU, check what
`onnxruntime.get_available_providers()` reports.

---

## Licence and credits

The models are other people's work: Qwen3 (Alibaba, Apache 2.0), Whisper
(OpenAI, MIT) via faster-whisper, Kokoro-82M (Apache 2.0), and openWakeWord
(Apache 2.0). Their licences apply to them, not to this repository.
