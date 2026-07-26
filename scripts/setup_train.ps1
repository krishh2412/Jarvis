<#
    Set up an ISOLATED training environment for fine-tuning.

    Creates .venv-train and installs the heavy torch/CUDA/bitsandbytes stack
    there, so the lean, torch-free app venv is never touched. Downloads ~3 GB;
    one-time cost.

    Usage:  powershell -ExecutionPolicy Bypass -File scripts\setup_train.ps1
#>

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $root

Write-Host "=== Training environment setup ===" -ForegroundColor Cyan

if (-not (Test-Path "$root\.venv-train")) {
    python -m venv "$root\.venv-train"
    Write-Host "  created .venv-train" -ForegroundColor Green
}
$tpy = "$root\.venv-train\Scripts\python.exe"

& $tpy -m pip install --upgrade pip --quiet

Write-Host "  installing torch CUDA 12.1 - large download..." -ForegroundColor Gray
& $tpy -m pip install torch --index-url https://download.pytorch.org/whl/cu121
if ($LASTEXITCODE -ne 0) { throw "torch install failed" }

Write-Host "  installing training stack..." -ForegroundColor Gray
& $tpy -m pip install "transformers>=4.46" "peft>=0.13" "trl>=0.12" "bitsandbytes>=0.44" "datasets>=3.0" "accelerate>=1.0" sentencepiece protobuf
if ($LASTEXITCODE -ne 0) { throw "training-stack install failed" }

Write-Host "  verifying..." -ForegroundColor Gray
& $tpy "$root\scripts\_check_cuda.py"

Write-Host "=== Training environment ready ===" -ForegroundColor Green
