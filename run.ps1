<#
    Launch J.A.R.V.I.S.

    Usage:
        .\run.ps1            desktop app
        .\run.ps1 -Console   text console (no audio hardware needed)
#>

param(
    [switch]$Console
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

$venvPy = "$root\.venv\Scripts\python.exe"
if (-not (Test-Path $venvPy)) {
    Write-Host "No virtual environment found. Run setup.ps1 first." -ForegroundColor Red
    exit 1
}

# Keep everything in one place: the language models live inside the project at
# data\ollama rather than under the user profile on C:. Pointing OLLAMA_MODELS
# at them here means the whole assistant -- code, weights, memory, skills and
# brains -- is one self-contained folder you can move or back up as a unit.
# Ollama reads this at startup, so it must be set before the server launches.
$modelStore = Join-Path $root "data\ollama"
if (Test-Path $modelStore) {
    $env:OLLAMA_MODELS = $modelStore
}

# Ollama has to be up before the brain can answer anything.
$running = $false
try {
    $null = Invoke-WebRequest -Uri "http://127.0.0.1:11434/api/tags" -TimeoutSec 2 -UseBasicParsing
    $running = $true
} catch { }

if (-not $running) {
    Write-Host "Starting Ollama..." -ForegroundColor Gray
    Start-Process -FilePath "ollama" -ArgumentList "serve" -WindowStyle Hidden
    for ($i = 0; $i -lt 15; $i++) {
        Start-Sleep -Milliseconds 700
        try {
            $null = Invoke-WebRequest -Uri "http://127.0.0.1:11434/api/tags" -TimeoutSec 2 -UseBasicParsing
            break
        } catch { }
    }
}

if ($Console) {
    & $venvPy -m jarvis.cli
} else {
    & $venvPy -m jarvis.main
}
