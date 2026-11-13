# NetPulse Pro - Windows build script.
# Installs dependencies and packages the app into a single executable.

$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$AppName = "NetPulse Pro"
$Python = "python"

if (-not (Get-Command $Python -ErrorAction SilentlyContinue)) {
    throw "Python was not found on PATH. Install Python 3.10+ and retry."
}

function Invoke-Step {
    param([string]$Message, [scriptblock]$Action)

    Write-Host "==> $Message" -ForegroundColor Cyan
    & $Action
    if ($LASTEXITCODE -ne 0) {
        throw "Step failed: $Message (exit code $LASTEXITCODE)"
    }
}

Invoke-Step "Upgrading pip" {
    & $Python -m pip install --upgrade pip
}

Invoke-Step "Installing dependencies" {
    & $Python -m pip install psutil pystray Pillow customtkinter pyinstaller
}

Invoke-Step "Building $AppName" {
    & $Python -m PyInstaller `
        --noconfirm `
        --clean `
        --onefile `
        --windowed `
        --name "$AppName" `
        --hidden-import pystray._win32 `
        "$ProjectRoot\main.py"
}

$ExePath = Join-Path $ProjectRoot "dist\$AppName.exe"
if (Test-Path $ExePath) {
    Write-Host "Build succeeded: $ExePath" -ForegroundColor Green
} else {
    throw "Build did not produce $ExePath"
}
