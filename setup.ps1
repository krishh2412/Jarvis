<#
    J.A.R.V.I.S. — one-shot setup for Windows.

    Creates the virtual environment, installs Python dependencies, installs
    Ollama if missing, and pulls the language model. Safe to re-run: every
    step checks before acting.

    Usage:  powershell -ExecutionPolicy Bypass -File setup.ps1
#>

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

function Write-Step($text) { Write-Host "`n=== $text ===" -ForegroundColor Cyan }
function Write-Ok($text)   { Write-Host "  [ok]   $text" -ForegroundColor Green }
function Write-Warn($text) { Write-Host "  [warn] $text" -ForegroundColor Yellow }
function Write-Info($text) { Write-Host "  $text" -ForegroundColor Gray }

Write-Host @"

     _     _    ____  __     __ ___  ____
    | |   / \  |  _ \ \ \   / /|_ _|/ ___|
 _  | |  / _ \ | |_) | \ \ / /  | | \___ \
| |_| | / ___ \|  _ <   \ V /   | |  ___) |
 \___/ /_/   \_\_| \_\   \_/   |___||____/

  Local assistant setup
"@ -ForegroundColor Cyan

# --- 1. Python -------------------------------------------------------------
Write-Step "Checking Python"
$python = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $python) { throw "Python not found on PATH. Install Python 3.11 or 3.12 first." }

$versionText = (& python --version) -replace 'Python ', ''
$version = [version]($versionText -split '\+')[0]
if ($version.Major -ne 3 -or $version.Minor -lt 10 -or $version.Minor -gt 12) {
    Write-Warn "Python $versionText detected. 3.11 or 3.12 is recommended; some wheels lack 3.13 builds."
} else {
    Write-Ok "Python $versionText"
}

# --- 2. Virtual environment ------------------------------------------------
Write-Step "Virtual environment"
if (-not (Test-Path "$root\.venv")) {
    & python -m venv "$root\.venv"
    Write-Ok "created .venv"
} else {
    Write-Ok ".venv already exists"
}
$venvPy = "$root\.venv\Scripts\python.exe"

# --- 3. Python dependencies ------------------------------------------------
Write-Step "Installing Python dependencies (this takes a few minutes)"
& $venvPy -m pip install --upgrade pip --quiet
& $venvPy -m pip install -r "$root\requirements.txt"
if ($LASTEXITCODE -ne 0) { throw "pip install failed. Scroll up for the failing package." }
Write-Ok "dependencies installed"

# openwakeword depends on the CPU-only `onnxruntime` package, which unpacks
# over the top of `onnxruntime-gpu` -- same module directory, different
# distribution name, so pip sees no conflict and the GPU providers just
# disappear. Reinstalling GPU last is the reliable way to win that race.
Write-Info "ensuring the GPU build of onnxruntime is the one on disk..."
& $venvPy -m pip uninstall -y onnxruntime --quiet 2>$null
& $venvPy -m pip install --force-reinstall --no-deps onnxruntime-gpu==1.20.1 --quiet
$providers = & $venvPy -c "import onnxruntime; print(','.join(onnxruntime.get_available_providers()))" 2>$null
if ($providers -match "CUDAExecutionProvider") {
    Write-Ok "onnxruntime providers: $providers"
} else {
    Write-Warn "CUDAExecutionProvider missing (got: $providers). TTS will run on CPU."
}

# Server packages contradict the native-window design; strip any that a
# previous install left behind so they cannot creep into the frozen exe.
& $venvPy -m pip uninstall -y fastapi uvicorn starlette --quiet 2>$null | Out-Null

# --- 4. espeak-ng (phonemiser for Kokoro TTS) ------------------------------
Write-Step "Checking espeak-ng"
$espeak = (Get-Command espeak-ng -ErrorAction SilentlyContinue)
if (-not $espeak -and -not (Test-Path "C:\Program Files\eSpeak NG\espeak-ng.exe")) {
    Write-Info "installing espeak-ng via winget..."
    try {
        winget install --id eSpeak-NG.eSpeak-NG --silent --accept-package-agreements --accept-source-agreements
        Write-Ok "espeak-ng installed"
    } catch {
        Write-Warn "winget could not install espeak-ng. Grab it from https://github.com/espeak-ng/espeak-ng/releases"
    }
} else {
    Write-Ok "espeak-ng present"
}

# --- 5. Ollama -------------------------------------------------------------
Write-Step "Checking Ollama"
$ollama = (Get-Command ollama -ErrorAction SilentlyContinue).Source
if (-not $ollama) {
    Write-Info "installing Ollama via winget..."
    try {
        winget install --id Ollama.Ollama --silent --accept-package-agreements --accept-source-agreements
        $env:PATH = "$env:PATH;$env:LOCALAPPDATA\Programs\Ollama"
        Write-Ok "Ollama installed"
    } catch {
        throw "Could not install Ollama automatically. Download it from https://ollama.com/download and re-run this script."
    }
} else {
    Write-Ok "Ollama at $ollama"
}

# --- 6. Start the Ollama daemon --------------------------------------------
Write-Step "Starting Ollama service"
$running = $false
try {
    $null = Invoke-WebRequest -Uri "http://127.0.0.1:11434/api/tags" -TimeoutSec 3 -UseBasicParsing
    $running = $true
} catch { }

if (-not $running) {
    Start-Process -FilePath "ollama" -ArgumentList "serve" -WindowStyle Hidden
    Write-Info "waiting for the daemon..."
    for ($i = 0; $i -lt 20; $i++) {
        Start-Sleep -Seconds 1
        try {
            $null = Invoke-WebRequest -Uri "http://127.0.0.1:11434/api/tags" -TimeoutSec 2 -UseBasicParsing
            $running = $true
            break
        } catch { }
    }
}
if ($running) { Write-Ok "Ollama is responding on port 11434" }
else { Write-Warn "Ollama did not come up. Start it manually with 'ollama serve'." }

# --- 7. Pull the model -----------------------------------------------------
Write-Step "Language model"
$model = "qwen3:8b"
if (Test-Path "$root\config.json") {
    try {
        $cfg = Get-Content "$root\config.json" -Raw | ConvertFrom-Json
        if ($cfg.llm.model) { $model = $cfg.llm.model }
    } catch { }
}

$visionModel = "qwen2.5vl:3b"
if (Test-Path "$root\config.json") {
    try {
        $cfg = Get-Content "$root\config.json" -Raw | ConvertFrom-Json
        if ($cfg.vision.model) { $visionModel = $cfg.vision.model }
    } catch { }
}

$installed = (& ollama list) 2>$null | Out-String
if ($installed -match [regex]::Escape($model)) {
    Write-Ok "$model already pulled"
} else {
    Write-Info "pulling $model (about 5 GB, one time)..."
    & ollama pull $model
    if ($LASTEXITCODE -ne 0) { Write-Warn "pull failed; run 'ollama pull $model' manually" }
    else { Write-Ok "$model ready" }
}

# Vision model — lets JARVIS actually see the screen. Separate from the text
# brain because qwen3 cannot read images.
if ($installed -match [regex]::Escape($visionModel)) {
    Write-Ok "$visionModel already pulled"
} else {
    Write-Info "pulling vision model $visionModel (about 3 GB, one time)..."
    & ollama pull $visionModel
    if ($LASTEXITCODE -ne 0) { Write-Warn "vision pull failed; run 'ollama pull $visionModel' manually" }
    else { Write-Ok "$visionModel ready" }
}

# --- 8. Speech models ------------------------------------------------------
Write-Step "Speech models"
Write-Info "downloading Whisper and Kokoro weights (about 500 MB, one time)..."
& $venvPy "$root\scripts\fetch_models.py"
if ($LASTEXITCODE -ne 0) { Write-Warn "model download incomplete; voice may not start" }
else { Write-Ok "speech models ready" }

# --- 9. GPU check ----------------------------------------------------------
Write-Step "GPU"
try {
    $gpu = (& nvidia-smi --query-gpu=name,memory.total --format=csv,noheader) 2>$null
    if ($gpu) {
        Write-Ok $gpu.Trim()
    } else {
        Write-Warn "No NVIDIA GPU detected. Everything still runs, just slower on CPU."
    }
} catch {
    Write-Warn "nvidia-smi not available; assuming CPU-only operation."
}

Write-Host "`n=== Setup complete ===" -ForegroundColor Green
Write-Host "  Launch with:  .\run.ps1" -ForegroundColor White
Write-Host ""
