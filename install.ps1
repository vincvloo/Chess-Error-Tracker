# Installs Chess Error Tracker and starts the web app (Windows).
#
#   powershell -ExecutionPolicy Bypass -File install.ps1
#
# Safe to run again: it skips whatever is already done, so running it later
# simply starts the app. Options:
#   -NoLaunch        set everything up but don't start the app
#   -SkipStockfish   don't try to install Stockfish

param(
    [switch]$NoLaunch,
    [switch]$SkipStockfish
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

function Say($message) { Write-Host "==> $message" -ForegroundColor Cyan }
function Fail($message) { Write-Host "ERROR: $message" -ForegroundColor Red; exit 1 }

# 1. Python 3.10 or newer ---------------------------------------------------
function Find-Python {
    $candidates = @(@("py", "-3"), @("python"), @("python3"))
    foreach ($c in $candidates) {
        if (-not (Get-Command $c[0] -ErrorAction SilentlyContinue)) { continue }
        try {
            $extra = @()
            if ($c.Count -gt 1) { $extra = $c[1..($c.Count - 1)] }
            $ok = & $c[0] @extra -c "import sys; print(sys.version_info >= (3, 10))" 2>$null
            if ($ok -eq "True") { return $c }
        } catch { }
    }
    return $null
}

$python = Find-Python
if (-not $python) {
    Fail "Python 3.10 or newer was not found. Install it from https://www.python.org/downloads/ (tick 'Add python.exe to PATH'), then run this again."
}
$pyExe = $python[0]
$pyArgs = @()
if ($python.Count -gt 1) { $pyArgs = $python[1..($python.Count - 1)] }

# 2. Virtual environment + the app -------------------------------------------
$venv = Join-Path $PSScriptRoot ".venvs\chess"
$venvPython = Join-Path $venv "Scripts\python.exe"

if (-not (Test-Path $venvPython)) {
    Say "Creating a virtual environment in .venvs\chess"
    & $pyExe @pyArgs -m venv $venv
    if ($LASTEXITCODE -ne 0) { Fail "Could not create the virtual environment." }
}

Say "Installing Chess Error Tracker (this can take a minute the first time)"
& $venvPython -m pip install --disable-pip-version-check --quiet -e ".[web]"
if ($LASTEXITCODE -ne 0) { Fail "Installing the app failed. Scroll up for the pip error." }

# 3. Stockfish ----------------------------------------------------------------
function Find-Stockfish {
    $found = & $venvPython -c "from chess_tracker.engine import find_engine; print(find_engine() or '')"
    return "$found".Trim()
}

if (-not $SkipStockfish) {
    if (Find-Stockfish) {
        Say "Stockfish found"
    } elseif (Get-Command winget -ErrorAction SilentlyContinue) {
        Say "Installing Stockfish with winget"
        winget install Stockfish --accept-package-agreements --accept-source-agreements
        if (Find-Stockfish) {
            Say "Stockfish installed"
        } else {
            Write-Host "Stockfish was installed but isn't visible yet. Close this window and run install.ps1 again if the app can't find it." -ForegroundColor Yellow
        }
    } else {
        Write-Host "Stockfish wasn't found and winget isn't available. Download the Windows build from https://stockfishchess.org/download/ and unzip it to C:\Tools\stockfish\ - the app finds it there automatically." -ForegroundColor Yellow
    }
}

# 4. Start ---------------------------------------------------------------------
if ($NoLaunch) {
    Say "Done. Start the app any time with: .venvs\chess\Scripts\chess-tracker serve"
    exit 0
}

Say "Starting the app (press Ctrl+C in this window to stop it)"
& (Join-Path $venv "Scripts\chess-tracker.exe") serve
