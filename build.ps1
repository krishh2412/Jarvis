<#
    Build J.A.R.V.I.S. into a double-clickable JARVIS.exe.

    Produces a one-folder distribution at dist\JARVIS\ containing JARVIS.exe,
    an _internal\ payload, and a data\ folder holding the model weights. The
    exe opens a native window; there is no server and nothing to point a
    browser at.

    Usage:
        .\build.ps1              normal build
        .\build.ps1 -Clean       wipe build\ and dist\ first
        .\build.ps1 -NoModels    skip copying data\models (faster iteration)

    Expect five to fifteen minutes and roughly 2-4 GB of disk churn.
#>

param(
    [switch]$Clean,
    [switch]$NoModels,
    # Output root. Defaults to dist\. Use a fresh one (e.g. -DistRoot release)
    # when a previous dist\JARVIS is stuck under an antivirus file lock and
    # cannot be overwritten.
    [string]$DistRoot = "dist"
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

function Write-Step($text) { Write-Host "`n=== $text ===" -ForegroundColor Cyan }
function Write-Ok($text)   { Write-Host "  [ok]   $text" -ForegroundColor Green }
function Write-Warn($text) { Write-Host "  [warn] $text" -ForegroundColor Yellow }
function Write-Info($text) { Write-Host "  $text" -ForegroundColor Gray }
function Die($text) {
    Write-Host "`n  [fail] $text`n" -ForegroundColor Red
    exit 1
}

function Get-FolderSizeMB($path) {
    if (-not (Test-Path $path)) { return 0 }
    $bytes = (Get-ChildItem -Path $path -Recurse -File -Force |
              Measure-Object -Property Length -Sum).Sum
    if (-not $bytes) { return 0 }
    return [math]::Round($bytes / 1MB, 1)
}

Write-Host @"

  Building J.A.R.V.I.S.
  one-folder, windowed, model weights kept on disk
"@ -ForegroundColor Cyan

# --- 1. Preflight ----------------------------------------------------------
Write-Step "Preflight"

$venvPy = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPy)) {
    Die "No virtual environment at .venv\Scripts\python.exe. Run setup.ps1 first."
}
Write-Ok "venv found"

$specFile = Join-Path $root "jarvis.spec"
if (-not (Test-Path $specFile)) { Die "jarvis.spec is missing from $root." }

$entry = Join-Path $root "jarvis\main.py"
if (-not (Test-Path $entry)) {
    Die @"
jarvis\main.py does not exist, so there is no entry point to freeze.
       It must define main() and be importable as 'jarvis.main'.
"@
}
Write-Ok "entry point jarvis\main.py"

# PyInstaller must live in the venv, not on the system Python, or it will
# freeze the wrong interpreter's site-packages.
& $venvPy -c "import PyInstaller" 2>$null
if ($LASTEXITCODE -ne 0) {
    Die @"
PyInstaller is not installed in the virtual environment.
       Fix with:  .venv\Scripts\python.exe -m pip install pyinstaller==6.11.1
"@
}
$pyiVersion = (& $venvPy -c "import PyInstaller; print(PyInstaller.__version__)").Trim()
Write-Ok "PyInstaller $pyiVersion"

# A quick import census. Any of these missing produces a build that launches
# and then dies on first use, which is a far worse failure than not building.
$critical = @{
    "webview"        = "pywebview - the native window itself"
    "clr"            = "pythonnet - the WebView2 backend"
    "faster_whisper" = "speech to text"
    "ctranslate2"    = "the STT inference engine"
    "onnxruntime"    = "wake word and TTS inference"
    "openwakeword"   = "wake word detection"
    "kokoro_onnx"    = "text to speech"
    "sounddevice"    = "audio I/O"
    "ollama"         = "the LLM client"
    "pycaw"          = "system volume control"
    "win32gui"       = "pywin32 - window control"
}
$absent = @()
foreach ($mod in $critical.Keys) {
    & $venvPy -c "import $mod" 2>$null
    if ($LASTEXITCODE -ne 0) { $absent += "$mod  ($($critical[$mod]))" }
}
if ($absent.Count -gt 0) {
    Write-Warn "these imports failed in the venv:"
    foreach ($a in $absent) { Write-Info "- $a" }
    Write-Info "The build will still run, but those features will be dead in the exe."
    Write-Info "Run setup.ps1 to finish installing dependencies."
} else {
    Write-Ok "all critical imports resolve"
}

# openWakeWord's pretrained models are downloaded into the package at first
# use, not shipped in the wheel. If they are absent now they will be absent in
# the exe, and "hey JARVIS" will never fire.
$owvProbe = @'
import pathlib, sys
try:
    import openwakeword
except Exception:
    sys.exit(2)
root = pathlib.Path(openwakeword.__file__).parent
n = sum(1 for p in root.rglob("*") if p.suffix in (".onnx", ".tflite"))
print(n)
sys.exit(0 if n else 1)
'@
$owvFile = Join-Path $env:TEMP "jarvis_owv_probe.py"
$owvProbe | Out-File -FilePath $owvFile -Encoding utf8
$owvCount = (& $venvPy $owvFile) 2>$null
if ($LASTEXITCODE -eq 0) {
    Write-Ok "openWakeWord models present ($($owvCount) files)"
} elseif ($LASTEXITCODE -eq 1) {
    Write-Warn "openWakeWord has no downloaded models."
    Write-Info "The exe will build, but the wake word will never trigger."
    Write-Info "Fix with: .venv\Scripts\python.exe scripts\fetch_models.py"
} else {
    Write-Warn "could not probe openWakeWord (import failed)"
}
Remove-Item $owvFile -Force -ErrorAction SilentlyContinue

# A JARVIS.exe still running from a previous build keeps dist\ locked, which
# makes PyInstaller fail with "Access is denied". Stop it before touching dist.
$runningExe = Get-Process JARVIS -ErrorAction SilentlyContinue
if ($runningExe) {
    Write-Info "stopping a running JARVIS.exe so dist\ can be rebuilt..."
    $runningExe | Stop-Process -Force
    Start-Sleep -Seconds 2
    Write-Ok "stopped running instance"
}

# Even after the process exits, Windows Defender can hold a transient lock on
# the freshly-scanned DLLs (VCRUNTIME140.dll and friends), so a plain delete
# fails with "Access is denied" and PyInstaller cannot overwrite dist. A
# directory *move* does not open those files, so it succeeds where delete does
# not: park the old build aside and let PyInstaller create a clean dist\JARVIS.
$distJarvis = Join-Path $root "$DistRoot\JARVIS"
if (Test-Path $distJarvis) {
    try {
        Remove-Item -LiteralPath $distJarvis -Recurse -Force -ErrorAction Stop
    } catch {
        $aside = Join-Path $root ("dist\_old_JARVIS-" + (Get-Date -Format "HHmmss"))
        Move-Item -LiteralPath $distJarvis -Destination $aside -ErrorAction SilentlyContinue
        Write-Info "old build was locked; parked it at $(Split-Path $aside -Leaf)"
    }
}

# Sweep any parked builds from earlier runs once Defender has let go of them.
Get-ChildItem (Join-Path $root "dist") -Directory -Filter "_old_JARVIS-*" -ErrorAction SilentlyContinue |
    ForEach-Object { Remove-Item -LiteralPath $_.FullName -Recurse -Force -ErrorAction SilentlyContinue }

# --- 2. Clean --------------------------------------------------------------
if ($Clean) {
    Write-Step "Cleaning"
    foreach ($dir in @("build", "dist")) {
        $target = Join-Path $root $dir
        if (Test-Path $target) {
            Remove-Item -Recurse -Force $target -Confirm:$false
            Write-Ok "removed $dir\"
        }
    }
}

# --- 3. Freeze -------------------------------------------------------------
Write-Step "Running PyInstaller (this takes several minutes)"
Write-Info "spec: $specFile"

# Windows Defender scans each freshly-written DLL and briefly locks it, which
# can make PyInstaller fail mid-build with "Access is denied" on something like
# VCRUNTIME140.dll. The lock is transient: on a retry the file is already in
# Defender's scan cache and is not re-locked. So try a few times before giving
# up, clearing the partial dist between attempts.
$maxAttempts = 3
$built = $false
for ($attempt = 1; $attempt -le $maxAttempts; $attempt++) {
    if ($attempt -gt 1) {
        Write-Warn "build attempt $($attempt - 1) failed (likely an antivirus file lock); retrying..."
        Start-Sleep -Seconds 8
        $partial = Join-Path $root "$DistRoot\JARVIS"
        if (Test-Path $partial) {
            try { Remove-Item -LiteralPath $partial -Recurse -Force -ErrorAction Stop }
            catch {
                Move-Item -LiteralPath $partial -Destination (Join-Path $root ("dist\_old_JARVIS-" + (Get-Date -Format "HHmmssff"))) -ErrorAction SilentlyContinue
            }
        }
    }
    & $venvPy -m PyInstaller --noconfirm --log-level=WARN --distpath (Join-Path $root $DistRoot) $specFile
    if ($LASTEXITCODE -eq 0) { $built = $true; break }
}
if (-not $built) {
    Die @"
PyInstaller exited with code $LASTEXITCODE after $maxAttempts attempts.
       Scroll up for the first ERROR line - that is the real cause; everything
       after it is fallout. Common ones:
         'Access is denied'          -> antivirus lock; add this folder to your
                                        Windows Security exclusions, or close any
                                        running JARVIS.exe, and rebuild
         'Unable to find <pkg>'      -> package not installed in .venv
         'hidden import not found'   -> a module named in jarvis.spec is absent
         'RecursionError'            -> add the offending package to excludes
"@
}

$distDir = Join-Path $root "$DistRoot\JARVIS"
$exePath = Join-Path $distDir "JARVIS.exe"
if (-not (Test-Path $exePath)) {
    Die "PyInstaller reported success but $exePath does not exist. Check dist\ for a differently-named output."
}
Write-Ok "frozen to dist\JARVIS\"

$frozenMB = Get-FolderSizeMB $distDir
Write-Info "payload without models: $frozenMB MB"

# --- 4. Model weights ------------------------------------------------------
# Weights are read from disk at runtime, never bundled. config._project_root()
# resolves to the exe's own directory when sys.frozen is set, so data\ has to
# sit beside JARVIS.exe.
Write-Step "Model weights"

$srcModels = Join-Path $root "data\models"
$dstData   = Join-Path $distDir "data"
$dstModels = Join-Path $dstData "models"

if ($NoModels) {
    Write-Warn "-NoModels given; skipping. The exe will start but voice will not work."
} elseif (-not (Test-Path $srcModels)) {
    Write-Warn "data\models does not exist. Run setup.ps1 (which runs scripts\fetch_models.py)."
} else {
    $weights = @(Get-ChildItem -Path $srcModels -File -Recurse -Force)
    if ($weights.Count -eq 0) {
        Write-Warn "data\models is empty. Run: .venv\Scripts\python.exe scripts\fetch_models.py"
    } else {
        New-Item -ItemType Directory -Force -Path $dstModels | Out-Null
        Copy-Item -Path (Join-Path $srcModels "*") -Destination $dstModels -Recurse -Force
        $modelMB = Get-FolderSizeMB $dstModels
        Write-Ok "copied $($weights.Count) file(s), $modelMB MB -> dist\JARVIS\data\models"
    }
}

# Empty working directories, so the first run does not have to create them
# under a path the user may have dropped somewhere read-only.
foreach ($sub in @("logs", "trash", "screenshots")) {
    New-Item -ItemType Directory -Force -Path (Join-Path $dstData $sub) | Out-Null
}
Write-Ok "created data\logs, data\trash, data\screenshots"

# The app icon is resolved at runtime from assets\ next to the exe (that is
# where _project_root() points when frozen), so copy it beside JARVIS.exe.
$srcAssets = Join-Path $root "assets"
if (Test-Path $srcAssets) {
    $dstAssets = Join-Path $distDir "assets"
    New-Item -ItemType Directory -Force -Path $dstAssets | Out-Null
    Copy-Item -Path (Join-Path $srcAssets "*") -Destination $dstAssets -Recurse -Force
    Write-Ok "copied assets (app icon)"
} else {
    Write-Info "no assets folder; run scripts\make_icon.py to generate the icon"
}

# An existing config.json travels with the build so the packaged app behaves
# like the one you tested from source.
$srcConfig = Join-Path $root "config.json"
if (Test-Path $srcConfig) {
    Copy-Item $srcConfig -Destination $distDir -Force
    Write-Ok "copied config.json"
} else {
    Write-Info "no config.json to copy; the exe will use built-in defaults"
}

# --- 5. Report -------------------------------------------------------------
Write-Step "Result"

$totalMB = Get-FolderSizeMB $distDir
$exeKB   = [math]::Round((Get-Item $exePath).Length / 1KB, 0)

Write-Host ""
Write-Host "  JARVIS.exe        $exeKB KB (launcher; the payload is in _internal\)" -ForegroundColor White
Write-Host "  dist\JARVIS\      $totalMB MB total" -ForegroundColor White
Write-Host ""
Write-Host "  $exePath" -ForegroundColor Cyan
Write-Host ""
Write-Info "Ship the whole dist\JARVIS folder, not the exe on its own -"
Write-Info "JARVIS.exe reads data\ and _internal\ from beside itself."
Write-Info "Ollama must be installed and running on the target machine."
Write-Host ""
