"""Central configuration for J.A.R.V.I.S.

Every tunable lives here. Values load from ``config.json`` next to the project
root when present, otherwise the defaults below apply. Nothing else in the
codebase should hardcode a path, model name or port.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path


def _project_root() -> Path:
    """Resolve the project root whether running from source or a PyInstaller exe."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


ROOT = _project_root()
DATA_DIR = ROOT / "data"
LOG_DIR = DATA_DIR / "logs"
TRASH_DIR = DATA_DIR / "trash"
MODEL_DIR = DATA_DIR / "models"
CONFIG_FILE = ROOT / "config.json"

for _d in (DATA_DIR, LOG_DIR, TRASH_DIR, MODEL_DIR):
    _d.mkdir(parents=True, exist_ok=True)


def _load_env() -> None:
    """Load a .env next to the project so ANTHROPIC_API_KEY is available.

    Kept optional: the app runs fully local without a key, and the file is
    gitignored so the secret never travels with the code.
    """
    env_path = ROOT / ".env"
    if not env_path.exists():
        return
    try:
        from dotenv import load_dotenv
        load_dotenv(env_path)
    except ImportError:
        # Minimal fallback parser so a missing python-dotenv is not fatal.
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_env()


@dataclass
class LLMConfig:
    # Qwen3 8B at Q4_K_M: ~6 GB VRAM, strong tool calling, ~35 tok/s on a 3060.
    model: str = "qwen3:8b"
    host: str = "http://127.0.0.1:11434"
    temperature: float = 0.6
    # Context is the main VRAM lever, but it is also a correctness floor: the
    # system prompt plus 70+ tool schemas is ~11-12k tokens before a single
    # message, so 8192 silently truncated the tool results the model needs and
    # sent it into repeat-call loops. 16384 fits the static prefix (which Ollama
    # prompt-caches, so it is processed once, not every turn) with real room for
    # the conversation and tool outputs on top. KV cache for this still sits
    # comfortably on the 12 GB card alongside the voice stack.
    num_ctx: int = 16384
    # Cap the reply length. JARVIS is terse by design, and an unbounded
    # num_predict is the difference between a two second answer and a
    # twenty second one when the model decides to be thorough.
    num_predict: int = 400
    # How long Ollama keeps the model resident after a turn. Measured cold
    # load on a 3060 is ~60 s versus ~10 s warm, so evicting between
    # questions is the single worst thing for perceived responsiveness.
    # "-1" pins it in VRAM until Ollama stops.
    keep_alive: str = "30m"
    # Qwen3 reasons before answering and returns it in a separate `thinking`
    # field, which is stripped before speech -- so it is paid for and thrown
    # away. Measured on a tool-calling turn: 164 generated tokens with it on
    # versus 16 with it off, 3.30 s versus 0.36 s. Set False if you want it
    # reasoning harder on complex requests and can accept the latency.
    disable_thinking: bool = True
    # Hard ceiling on tool-call rounds per user turn, so a confused model
    # cannot spin forever with full PC access.
    max_tool_rounds: int = 12
    # Turns of dialogue retained verbatim before the oldest are dropped.
    history_turns: int = 20


@dataclass
class ClaudeConfig:
    """The optional cloud brain — Claude, via the Anthropic API.

    Off by default and dormant until the user switches to Claude mode AND an
    API key is present. The key is read from ANTHROPIC_API_KEY (environment or
    a .env file next to the project); it is never stored in config.json.
    """

    model: str = "claude-sonnet-5"
    max_tokens: int = 4096
    # Thinking is disabled by default for a snappy assistant; flip on for
    # harder reasoning at the cost of latency.
    thinking: bool = False
    max_tool_rounds: int = 12


@dataclass
class AssistantConfig:
    # Which brain is active at launch: "jarvis" (local) or "claude" (cloud).
    active: str = "jarvis"


@dataclass
class VisionConfig:
    # A separate vision-capable model, because qwen3:8b is text-only and cannot
    # see an image. Called on demand when JARVIS needs to look at the screen.
    #
    # Default is the 3B: at ~3 GB it stays resident alongside the 8B brain and
    # the voice stack (6+1+1+3 = 11 GB of 12), so a "look at my screen" answers
    # in ~3 s. The 7B reads fine detail and small text better but is also ~6 GB,
    # which forces Ollama to swap the brain out and back — pushing a single
    # look to ~25 s. Switch to "qwen2.5vl:7b" here if you want the accuracy and
    # can accept that wait.
    model: str = "qwen2.5vl:3b"
    # How long Ollama keeps the vision model in VRAM after a look. Short, so it
    # releases the memory back to the main brain when you are not using it.
    keep_alive: str = "5m"
    # Downscale big screenshots before sending; a 4K frame is slow and adds
    # nothing over ~1600px wide for reading a screen.
    max_width: int = 1600


@dataclass
class STTConfig:
    # "small" hits the accuracy/latency sweet spot on a 3060; "medium" is
    # noticeably better on accents but adds ~400 ms.
    model: str = "small.en"
    device: str = "cuda"
    compute_type: str = "int8_float16"  # ~1 GB VRAM
    language: str = "en"
    beam_size: int = 1  # greedy: latency matters more than the last 1% of WER


@dataclass
class TTSConfig:
    # British male presets. bm_george reads closest to the JARVIS register;
    # bm_fable is warmer, bm_lewis deeper.
    voice: str = "bm_george"
    speed: float = 1.05  # JARVIS speaks briskly, never languid
    sample_rate: int = 24000
    provider: str = "CUDAExecutionProvider"


@dataclass
class WakeConfig:
    enabled: bool = True
    model: str = "hey_jarvis"  # pretrained and shipped with openwakeword
    threshold: float = 0.5
    # Seconds of silence after speech before the utterance is considered done.
    endpoint_silence: float = 0.8
    # Hard cap on a single utterance so a stuck mic cannot record forever.
    max_utterance: float = 30.0


@dataclass
class AudioConfig:
    input_device: int | None = None   # None = system default
    output_device: int | None = None
    frame_ms: int = 30                # webrtcvad accepts 10/20/30
    vad_aggressiveness: int = 2       # 0 permissive .. 3 strict
    # Barge-in: how much speech must be detected during playback before we
    # cut JARVIS off, the way you can talk over ChatGPT's voice mode. Lower is
    # snappier but more prone to firing on a cough or an "mm-hm". 200 ms is a
    # responsive-but-safe middle; raise it if he cuts himself off on open
    # speakers hearing his own voice (there is no echo cancellation).
    bargein_speech_ms: int = 200


@dataclass
class SafetyConfig:
    # How much JARVIS confirms before acting. Adjustable live from the app.
    #   "full"     - unrestricted; no confirmation ever (catastrophic commands
    #                are still hard-blocked). Maximum flow.
    #   "standard" - hybrid: silent for anything reversible/journaled; confirm
    #                only the truly irreversible or dangerous. (Default.)
    #   "strict"   - confirm every destructive action.
    level: str = "standard"
    # How long an action waits on the Approve/Deny card before it gives up and
    # refuses itself. Long enough to walk back to the desk, short enough that a
    # forgotten prompt does not wedge the turn indefinitely.
    confirm_timeout: int = 120
    # Everything is logged (before AND after it runs), and destructive
    # filesystem operations route through a trash store so `undo` reverses them.
    audit_log: bool = True
    journal_deletes: bool = True
    trash_retention_days: int = 30
    # Paths JARVIS will refuse to touch at any level. A bad tool call here
    # bricks the machine rather than annoying you.
    protected_paths: list[str] = field(
        default_factory=lambda: [
            r"C:\Windows",
            r"C:\Program Files\WindowsApps",
            r"C:\$Recycle.Bin",
            r"C:\System Volume Information",
        ]
    )
    # Shell commands that are HARD-BLOCKED at every level (even "full") unless
    # the user disables this list by hand. These wipe disks, brick the OS, or
    # are irreversible at a scale no undo journal can catch. Case-insensitive
    # regex, matched against run_command / run_powershell input.
    command_denylist: list[str] = field(
        default_factory=lambda: [
            r"\bformat\s+[a-z]:",            # format C:
            r"\bmkfs\b",                      # make filesystem
            r"diskpart",                       # disk partitioning
            r"\bclean\b.*\ball\b",            # diskpart clean all
            r"rm\s+-rf\s+[/\\]\s*$",          # rm -rf /
            r"rmdir\s+/s\s+.*(windows|system32)",
            r"del\s+/[sfq].*\\(windows|system32)",
            r"reg\s+delete\s+HKLM",          # registry hive deletion
            r"cipher\s+/w",                    # secure-wipe free space
            r"bcdedit",                        # boot config
            r"vssadmin\s+delete",            # delete shadow copies
            r":\(\)\s*\{.*\|\s*:",            # fork bomb
        ]
    )


@dataclass
class UIConfig:
    """Native window settings.

    There is no host or port here by design: the HUD is handed to a WebView2
    window as an HTML string and talks to Python over pywebview's js_api
    bridge. Nothing is served over HTTP and nothing listens on a socket.
    """

    window_width: int = 1100
    window_height: int = 760
    frameless: bool = True          # custom titlebar drawn in the HUD
    resizable: bool = True
    on_top: bool = False
    start_minimised: bool = False
    theme_accent: str = "#4dd0e1"   # arc-reactor cyan


@dataclass
class Config:
    llm: LLMConfig = field(default_factory=LLMConfig)
    claude: ClaudeConfig = field(default_factory=ClaudeConfig)
    assistant: AssistantConfig = field(default_factory=AssistantConfig)
    vision: VisionConfig = field(default_factory=VisionConfig)
    stt: STTConfig = field(default_factory=STTConfig)
    tts: TTSConfig = field(default_factory=TTSConfig)
    wake: WakeConfig = field(default_factory=WakeConfig)
    audio: AudioConfig = field(default_factory=AudioConfig)
    safety: SafetyConfig = field(default_factory=SafetyConfig)
    ui: UIConfig = field(default_factory=UIConfig)

    # Spoken name used in greetings and the system prompt.
    user_title: str = "Sir"

    @classmethod
    def load(cls) -> "Config":
        cfg = cls()
        if not CONFIG_FILE.exists():
            return cfg
        try:
            raw = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            print(f"[config] ignoring unreadable config.json: {exc}")
            return cfg

        for f in fields(cls):
            if f.name not in raw:
                continue
            current = getattr(cfg, f.name)
            incoming = raw[f.name]
            # Nested dataclass sections merge key by key so a partial
            # config.json only overrides what it actually names.
            if hasattr(current, "__dataclass_fields__") and isinstance(incoming, dict):
                for key, value in incoming.items():
                    if hasattr(current, key):
                        setattr(current, key, value)
            else:
                setattr(cfg, f.name, incoming)
        return cfg

    def save(self) -> None:
        CONFIG_FILE.write_text(
            json.dumps(asdict(self), indent=2), encoding="utf-8"
        )


config = Config.load()


def cuda_dll_paths() -> list[str]:
    """Directories holding the pip-installed CUDA runtime DLLs.

    CTranslate2 and onnxruntime-gpu both need CUDA libraries on the DLL search
    path. Shipping them as wheels means there is no system CUDA install to keep
    in sync, but we do have to point Windows at them explicitly before either
    library loads.

    CTranslate2 needs only cuBLAS and cuDNN. onnxruntime's CUDA provider needs
    considerably more -- cudart, cuFFT, cuRAND and nvJitLink -- and when any
    one is missing it fails with a bare "LoadLibrary failed with error 126"
    and falls back to CPU *without raising*, so the symptom is silent slowness
    rather than an error.

    Discovery globs the ``nvidia`` package directory rather than importing each
    subpackage: several of these wheels (cuda_runtime, cufft, nvjitlink) ship
    no ``__init__.py`` at all, so an import-based probe silently skips exactly
    the ones onnxruntime needs most.
    """
    try:
        import nvidia
    except ImportError:
        return []

    roots = [Path(p) for p in getattr(nvidia, "__path__", [])]
    paths: list[str] = []
    for root in roots:
        if not root.is_dir():
            continue
        for child in sorted(root.iterdir()):
            candidate = child / "bin"
            if candidate.is_dir() and any(candidate.glob("*.dll")):
                paths.append(str(candidate))
    return paths


def prepare_cuda() -> None:
    """Make the wheel-shipped CUDA DLLs loadable. Safe to call more than once."""
    for path in cuda_dll_paths():
        if hasattr(os, "add_dll_directory"):
            try:
                os.add_dll_directory(path)
            except OSError:
                pass
        if path not in os.environ.get("PATH", ""):
            os.environ["PATH"] = path + os.pathsep + os.environ.get("PATH", "")
