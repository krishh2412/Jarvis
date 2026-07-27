# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for J.A.R.V.I.S.

Build with:  .\\build.ps1      (or)  .venv\\Scripts\\python.exe -m PyInstaller jarvis.spec

Design decisions worth knowing before you edit this file
--------------------------------------------------------

*One-folder, not one-file.* A one-file build packs everything into the exe and
re-extracts several hundred megabytes to %TEMP% on *every* launch. For an
assistant you start by double-clicking that is a five-to-twenty second stall
each time, plus a doubled disk footprint. One-folder starts instantly.

*No console.* This is a GUI app: a native Win32 window driven by pywebview on
the WebView2 runtime. There is no server, no port, no browser tab. A console
window would just be an artefact flapping behind the HUD.

*Model weights stay on disk, beside the exe.* ``data/models`` holds the Kokoro
voice pack and the ONNX weights — several hundred MB that never change between
builds. Bundling them would make every rebuild copy them again and make the
exe unpatchable. ``jarvis/config.py``'s ``_project_root()`` already returns
``Path(sys.executable).parent`` when ``sys.frozen`` is set, so a frozen JARVIS
looks for ``data/`` next to ``JARVIS.exe`` — exactly where ``build.ps1`` puts
it. The shipped layout is::

    JARVIS/
      JARVIS.exe
      config.json          (optional, overrides defaults)
      _internal/           (PyInstaller payload — do not touch)
      data/
        models/            Kokoro + ONNX weights, copied by build.ps1
        logs/  trash/      created at first run

*CUDA comes from pip wheels, not a system install.* nvidia-cublas-cu12 and
nvidia-cudnn-cu12 drop their DLLs in ``nvidia/cublas/bin`` and
``nvidia/cudnn/bin`` inside site-packages. CTranslate2 (faster-whisper) and
onnxruntime-gpu both dlopen them by name, so they must be collected *and* they
must land at the same package-relative path, because ``config.prepare_cuda()``
locates them via ``nvidia.cublas.__file__``. ``collect_dynamic_libs`` preserves
that layout, which is why it is used instead of a hand-rolled glob.
"""

from pathlib import Path

from PyInstaller.utils.hooks import (
    collect_data_files,
    collect_dynamic_libs,
    collect_submodules,
)

# --------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------
# SPECPATH is injected by PyInstaller and points at this file's directory.
ROOT = Path(SPECPATH).resolve()  # noqa: F821
ENTRY = ROOT / "jarvis" / "main.py"

if not ENTRY.exists():
    raise SystemExit(
        f"\n  Entry point not found: {ENTRY}\n"
        "  jarvis/main.py must exist and expose main(). Nothing to build.\n"
    )


# --------------------------------------------------------------------------
# Collection helpers
# --------------------------------------------------------------------------
# Dependencies install in the background and some are optional at build time.
# A missing package should produce one loud line, not a traceback that kills
# the whole spec before it has told you what else is wrong.
_missing: list[str] = []


def _try(fn, package, **kwargs):
    try:
        found = fn(package, **kwargs)
    except Exception as exc:  # noqa: BLE001 - any import failure is "not there"
        _missing.append(f"{package} ({fn.__name__}: {type(exc).__name__})")
        return []
    if not found:
        _missing.append(f"{package} ({fn.__name__}: nothing collected)")
    return found


def datas_of(package, **kwargs):
    return _try(collect_data_files, package, **kwargs)


def libs_of(package, **kwargs):
    return _try(collect_dynamic_libs, package, **kwargs)


def submodules_of(package):
    return _try(collect_submodules, package)


# --------------------------------------------------------------------------
# Hidden imports
# --------------------------------------------------------------------------
# Everything PyInstaller's static analysis cannot see: dynamic imports, COM
# plumbing, native backends selected at runtime.

hiddenimports: list[str] = []

# -- JARVIS's own dynamically-loaded modules --------------------------------
# registry.load_all() imports these to run their @tool decorators. The import
# is inside a function body, which PyInstaller usually follows, but the whole
# tool surface disappearing silently is too expensive a failure to gamble on.
hiddenimports += [
    "jarvis.tools.apps",
    "jarvis.tools.files",
    "jarvis.tools.hearing",
    "jarvis.tools.machine",
    "jarvis.tools.media",
    "jarvis.tools.memory",
    "jarvis.tools.safety",
    "jarvis.tools.screen",
    "jarvis.tools.skills_tool",
    "jarvis.tools.learning_tool",
    "jarvis.tools.system",
    "jarvis.tools.ui",
    "jarvis.tools.web",
    "jarvis.safety.audit",
    "jarvis.safety.gate",
    "jarvis.safety.journal",
    "jarvis.core.brain",
    "jarvis.core.claude_brain",
    "jarvis.core.learning",
    "jarvis.core.machine_map",
    "jarvis.core.manager",
    "jarvis.core.memory",
    "jarvis.core.persona",
    "jarvis.core.skills",
    "jarvis.core.study",
    "jarvis.cli",
    # Optional cloud brain — imported lazily when the user switches to Claude.
    "anthropic",
    "dotenv",
    # UI and audio layers. Listed by name so the build fails visibly (a
    # "hidden import not found" warning) rather than shipping a window that
    # cannot import its own HUD.
    "jarvis.ui.window",
    "jarvis.ui.hud",
    "jarvis.audio.wake",
    "jarvis.audio.stt",
    "jarvis.audio.tts",
    "jarvis.audio.voice",
]

# -- pywebview / WebView2 ---------------------------------------------------
# The backend is chosen at runtime by probing, so no import of
# webview.platforms.edgechromium is ever visible statically. That module in
# turn reaches .NET through pythonnet's `clr`, which is a compiled extension
# with its own runtime assemblies.
hiddenimports += [
    "webview",
    "webview.platforms.edgechromium",
    "webview.platforms.winforms",
    "clr",
    "clr_loader",
    "clr_loader.netfx",
    "clr_loader.util",
    "pythonnet",
]
# Everything except the platform backends we will never reach on Windows.
# Pulling in webview.platforms.{gtk,qt,cocoa,cef,android} only produces a wall
# of "missing module PyQt5 / gi / cefpython3" warnings.
_dead_backends = (
    "webview.platforms.gtk",
    "webview.platforms.qt",
    "webview.platforms.cocoa",
    "webview.platforms.cef",
    "webview.platforms.android",
)
hiddenimports += [
    m for m in submodules_of("webview") if not m.startswith(_dead_backends)
]

# -- Speech to text: faster-whisper on CTranslate2 --------------------------
hiddenimports += [
    "faster_whisper",
    "faster_whisper.transcribe",
    "faster_whisper.feature_extractor",
    "faster_whisper.tokenizer",
    "faster_whisper.vad",
    "ctranslate2",
    "ctranslate2.converters",
    "ctranslate2.specs",
    "tokenizers",
    "huggingface_hub",
    "av",  # faster-whisper decodes audio through PyAV
]

# -- ONNX Runtime (wake word on CPU, Kokoro TTS on CUDA) --------------------
# The provider list is resolved at session-creation time from strings; nothing
# imports the provider shims directly.
hiddenimports += [
    "onnxruntime",
    "onnxruntime.capi",
    "onnxruntime.capi._pybind_state",
    "onnxruntime.capi.onnxruntime_pybind11_state",
    "onnxruntime.capi.onnxruntime_inference_collection",
]

# -- Wake word + TTS --------------------------------------------------------
hiddenimports += [
    "openwakeword",
    "openwakeword.model",
    "openwakeword.utils",
    "kokoro_onnx",
    "kokoro_onnx.tokenizer",
    "espeakng_loader",
    "phonemizer",
    "phonemizer.backend",
    "phonemizer.backend.espeak",
    "phonemizer.backend.espeak.wrapper",
]

# -- Audio I/O --------------------------------------------------------------
hiddenimports += [
    "sounddevice",
    "_sounddevice_data",
    "webrtcvad",
    "scipy.signal",
    "scipy.io.wavfile",
    "numpy",
    # soundcard drives WASAPI loopback through cffi in ABI mode (no compile),
    # calling system DLLs by name. Its platform backend is imported dynamically.
    "soundcard",
    "soundcard.mediafoundation",
    "cffi",
    "_cffi_backend",
]

# -- Windows control: COM, Core Audio, UI automation ------------------------
# pycaw drives Core Audio through comtypes, which builds interface wrappers at
# runtime under comtypes.gen. comtypes.stream in particular is a perennial
# PyInstaller miss.
hiddenimports += [
    "comtypes",
    "comtypes.client",
    "comtypes.stream",
    "comtypes.persist",
    "comtypes.automation",
    # comtypes.gen is deliberately absent: it does not exist until comtypes
    # generates it, and in a frozen app it is created in a temp directory at
    # runtime. Naming it here only produces a spurious "not found" warning.
    "pycaw",
    "pycaw.pycaw",
    "pycaw.utils",
    "pycaw.constants",
    "win32api",
    "win32con",
    "win32gui",
    "win32process",
    "win32com",
    "win32com.client",
    "win32timezone",  # pywin32's classic silent omission
    "pythoncom",
    "pywintypes",
    "uiautomation",
    "pyautogui",
    "pyscreeze",
    "pymsgbox",
    "pytweening",
    "mouseinfo",
    "mss",
    "mss.windows",
    "pyperclip",
    "psutil",
]
hiddenimports += submodules_of("pycaw")

# -- LLM + web --------------------------------------------------------------
hiddenimports += [
    "ollama",
    "httpx",
    "httpcore",
    "h11",
    "anyio",
    "certifi",
    "trafilatura",
    "lxml",
    "lxml._elementpath",
    "lxml.etree",
    "courlan",
    "charset_normalizer",
]

# -- CUDA wheels ------------------------------------------------------------
# These are namespace packages with empty __init__.py files. They must be in
# the PYZ for config.prepare_cuda()'s __import__("nvidia.cublas") to succeed
# and resolve __file__ to the bundled bin/ directory.
hiddenimports += [
    "nvidia",
    "nvidia.cublas",
    "nvidia.cudnn",
]

hiddenimports = sorted(set(hiddenimports))


# --------------------------------------------------------------------------
# Binaries
# --------------------------------------------------------------------------
binaries: list[tuple[str, str]] = []

# CUDA runtime, shipped as pip wheels. Without these CTranslate2 raises
# "Library cublas64_12.dll is not found" the moment a WhisperModel is
# constructed, and onnxruntime-gpu silently falls back to CPU.
#
# Collected by globbing rather than collect_dynamic_libs: cuda_runtime, cufft
# and nvjitlink ship no __init__.py, so they are not importable packages and
# collect_dynamic_libs cannot see them at all -- which is precisely how the
# CUDA provider ends up missing cudart and failing with "error 126".
def _collect_nvidia_dlls() -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    try:
        import nvidia
        roots = [Path(p) for p in getattr(nvidia, "__path__", [])]
    except ImportError:
        return found

    for root in roots:
        if not root.is_dir():
            continue
        for child in sorted(root.iterdir()):
            bin_dir = child / "bin"
            if not bin_dir.is_dir():
                continue
            for dll in bin_dir.glob("*.dll"):
                found.append((str(dll), f"nvidia/{child.name}/bin"))
    return found


_nvidia = _collect_nvidia_dlls()
if not _nvidia:
    print("  [warn] no NVIDIA CUDA DLLs found; the build will be CPU-only")
else:
    print(f"  [ok]   collected {len(_nvidia)} CUDA DLLs from "
          f"{len({d for _, d in _nvidia})} packages")
binaries += _nvidia

# CTranslate2's own native core, and the ONNX Runtime provider DLLs
# (onnxruntime_providers_cuda.dll / _shared.dll) which live in capi/.
binaries += libs_of("ctranslate2")
binaries += libs_of("onnxruntime")

# PortAudio (sounddevice), uiautomation's helper DLL, and pythonnet's managed
# assemblies (Python.Runtime.dll and friends).
binaries += libs_of("_sounddevice_data")
binaries += libs_of("uiautomation")
binaries += libs_of("clr_loader")
binaries += libs_of("pythonnet")

# Not listed here on purpose:
#   av        - its 25 FFmpeg DLLs live in a sibling av.libs/ directory, which
#               collect_dynamic_libs does not see. pyinstaller-hooks-contrib
#               ships a hook for it that does the right thing.
#   webrtcvad - a bare extension module, not a package. The hidden import is
#               enough; collect_dynamic_libs would only warn.


# --------------------------------------------------------------------------
# Data files
# --------------------------------------------------------------------------
datas: list[tuple[str, str]] = []

# onnxruntime keeps version metadata and provider manifests beside its capi
# extension; a bare .pyd load fails without them.
datas += datas_of("onnxruntime")

# openWakeWord's pretrained models (hey_jarvis among them) are NOT in the
# wheel — openwakeword.utils.download_models() fetches them into the package's
# resources/models/ directory on first use, which setup.ps1 step 8 triggers.
# A fresh venv therefore yields nothing here, and the frozen app would start
# with a wake word that can never fire.
_owv = datas_of("openwakeword")
if not _owv:
    print(
        "\n[jarvis.spec] openwakeword has no bundled models to collect.\n"
        "  The exe will build but 'hey JARVIS' will never trigger.\n"
        "  Fix before shipping:\n"
        "    .venv\\Scripts\\python.exe scripts\\fetch_models.py\n"
    )
datas += _owv

# espeak-ng phonemiser data + libespeak-ng.dll, used by Kokoro to turn text
# into phonemes. Without the data directory it fails with a bare "no voice".
datas += datas_of("espeakng_loader")

# faster-whisper bundles the Silero VAD model and tokenizer assets.
datas += datas_of("faster_whisper")

# PortAudio binary is shipped as package data, not as a linked library.
datas += datas_of("_sounddevice_data")

# pywebview: the injected JS bridge plus the WebView2 interop assemblies under
# webview/lib/. pythonnet: Python.Runtime.dll.
datas += datas_of("webview")
datas += datas_of("pythonnet")
datas += datas_of("clr_loader")

# trafilatura reads settings.cfg at import time.
datas += datas_of("trafilatura")

# Kokoro's phonemiser chain (kokoro_onnx -> phonemizer-fork -> segments ->
# csvw -> language_tags) carries data files that are loaded lazily at
# synthesis time, so a build missing them looks fine until the first spoken
# word and then dies with a bare FileNotFoundError on index.json.
datas += datas_of("language_tags")
datas += datas_of("phonemizer")

# justext ships per-language stopword lists that trafilatura loads when
# extracting article text; without them read_webpage fails at runtime.
datas += datas_of("justext")
datas += datas_of("uritemplate")

datas += datas_of("kokoro_onnx")
datas += datas_of("ctranslate2")
datas += datas_of("uiautomation")

# soundcard ships its cffi definition files (the ABI headers it parses at
# import time); without them the loopback capture backend fails to initialise.
datas += datas_of("soundcard")

# NOT bundled, deliberately: data/models/*.onnx and *.bin. Several hundred MB
# of weights that would bloat every rebuild and every future patch. build.ps1
# copies them next to the exe instead; config._project_root() finds them there.


# --------------------------------------------------------------------------
# Runtime hook: register the CUDA DLL directories before anything imports
# ctranslate2 or onnxruntime.
# --------------------------------------------------------------------------
# os.add_dll_directory() has to run *before* the first native load, which is
# earlier than jarvis.main gets a chance to call config.prepare_cuda(). This
# writes a tiny hook into build/ (a PyInstaller scratch directory, not source)
# so the ordering is guaranteed no matter how main.py is structured.
_hook_dir = ROOT / "build"
_hook_dir.mkdir(parents=True, exist_ok=True)
_hook_file = _hook_dir / "pyi_rth_jarvis_cuda.py"
_hook_file.write_text(
    '''"""Generated by jarvis.spec -- do not edit, it is rewritten every build.

Puts the wheel-shipped CUDA DLLs on the search path before ctranslate2 or
onnxruntime load their native extensions. collect_dynamic_libs() preserves the
package-relative layout, so the DLLs sit under sys._MEIPASS in the same
nvidia/<lib>/bin shape they had in site-packages.
"""
import os
import sys

_base = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
_nvidia_root = os.path.join(_base, "nvidia")
_pkgs = sorted(os.listdir(_nvidia_root)) if os.path.isdir(_nvidia_root) else []
for _pkg in _pkgs:
    _p = os.path.join(_nvidia_root, _pkg, "bin")
    if not os.path.isdir(_p):
        continue
    if hasattr(os, "add_dll_directory"):
        try:
            os.add_dll_directory(_p)
        except OSError:
            pass
    if _p not in os.environ.get("PATH", ""):
        os.environ["PATH"] = _p + os.pathsep + os.environ.get("PATH", "")
''',
    encoding="utf-8",
)


# --------------------------------------------------------------------------
# Exclusions
# --------------------------------------------------------------------------
# Nothing here is imported by JARVIS; they arrive as optional transitive
# dependencies of scipy, scikit-learn and friends and cost tens of MB each.
excludes = [
    "tkinter",
    "matplotlib",
    "pytest",
    "PyQt5",
    "PyQt6",
    "PySide2",
    "PySide6",
    "IPython",
    "jupyter",
    "notebook",
    "nbconvert",
    "sphinx",
    "pandas",
    # JARVIS is not a web app. These sit in the venv as leftovers from other
    # installs; excluding them makes sure a stray import can never turn the
    # desktop build into something that listens on a socket.
    "fastapi",
    "uvicorn",
    "starlette",
    "websockets",
    # Deliberately torch-free: if one of these ever shows up in the build it
    # means a dependency changed and the exe is about to triple in size.
    "torch",
    "torchvision",
    "torchaudio",
    "tensorflow",
]


# --------------------------------------------------------------------------
# Build
# --------------------------------------------------------------------------
if _missing:
    print("\n[jarvis.spec] could not collect from these packages:")
    for item in _missing:
        print(f"    - {item}")
    print(
        "  If the venv is still installing, wait for setup.ps1 to finish.\n"
        "  A missing CUDA/onnxruntime/webview entry here means a broken exe.\n"
    )

_icon = None
for _candidate in (ROOT / "assets" / "jarvis.ico", ROOT / "data" / "jarvis.ico"):
    if _candidate.exists():
        _icon = str(_candidate)
        break


# --------------------------------------------------------------------------
# Hook overrides
# --------------------------------------------------------------------------
# hooks-contrib ships hook-webrtcvad.py, which calls
# importlib.metadata.version("webrtcvad") and aborts the whole build with
# PackageNotFoundError. We install `webrtcvad-wheels` -- the maintained fork
# that provides the same `webrtcvad` module under a different distribution
# name -- so that metadata genuinely does not exist. A same-named hook on
# hookspath takes precedence over the contrib one, so this empty override
# neutralises it. The module itself is a single self-contained extension with
# nothing to collect, so there is nothing lost.
_hooks_dir = ROOT / "build" / "hooks"
_hooks_dir.mkdir(parents=True, exist_ok=True)
(_hooks_dir / "hook-webrtcvad.py").write_text(
    '"""Override for hooks-contrib\'s webrtcvad hook.\n\n'
    "Generated by jarvis.spec. We ship webrtcvad-wheels, whose distribution\n"
    "name differs from the module name, which makes the contrib hook's\n"
    "metadata lookup raise and kill the build.\n"
    '"""\n\n'
    "hiddenimports = []\n"
    "datas = []\n"
    "binaries = []\n",
    encoding="utf-8",
)
print(f"  [ok]   hook overrides in {_hooks_dir}")


a = Analysis(  # noqa: F821
    [str(ENTRY)],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[str(_hooks_dir)],
    hooksconfig={},
    runtime_hooks=[str(_hook_file)],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)  # noqa: F821

exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,      # one-folder: binaries live in _internal/
    name="JARVIS",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,                  # UPX mangles the CUDA and WebView2 DLLs
    console=False,              # native window, no terminal
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=_icon,
)

coll = COLLECT(  # noqa: F821
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="JARVIS",
)
