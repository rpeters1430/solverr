# Solverr High-Performance Native Windows Launcher
# Run Solverr directly on Windows host for 100% native CPU/GPU performance

$ErrorActionPreference = "Stop"

Write-Host "==========================================" -ForegroundColor Cyan
Write-Host "  ⚡ Solverr - High-Performance Native Runner" -ForegroundColor Yellow
Write-Host "==========================================" -ForegroundColor Cyan

# Check Python installation
if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    Write-Error "Python 3 is not installed or not in PATH. Please install Python 3.10+."
    exit 1
}

# Install / update requirements
Write-Host "[1/3] Checking Python dependencies..." -ForegroundColor Green
python -m pip install --quiet --upgrade pip
python -m pip install --quiet -r requirements.txt

# Ensure Camoufox and Playwright browsers are installed
Write-Host "[2/3] Verifying Camoufox Stealth Firefox & Playwright engines..." -ForegroundColor Green
python -m camoufox fetch
python -m playwright install chromium

# Check if Port 8191 is in use
$portConn = Get-NetTCPConnection -LocalPort 8191 -State Listen -ErrorAction SilentlyContinue
if ($portConn) {
    $existingPid = $portConn.OwningProcess
    $procName = (Get-Process -Id $existingPid -ErrorAction SilentlyContinue).ProcessName
    
    Write-Host "[!] Port 8191 is already in use by process: $procName (PID: $existingPid)" -ForegroundColor Yellow
    Write-Host "    Terminating existing process on port 8191 to launch fresh Solverr engine..." -ForegroundColor Cyan
    
    try {
        Stop-Process -Id $existingPid -Force -ErrorAction SilentlyContinue
        Start-Sleep -Seconds 1
    } catch {
        # Fallback if docker container
        if (Get-Command docker -ErrorAction SilentlyContinue) {
            & docker stop solverr 2>&1 | Out-Null
        }
    }
}

# Launch Solverr Engine with Native Host Optimization
Write-Host "[3/3] Launching Solverr Engine on http://localhost:8191..." -ForegroundColor Yellow
Write-Host "      Dashboard active at: http://localhost:8191" -ForegroundColor Cyan
Write-Host "      Press Ctrl+C to stop." -ForegroundColor Gray

$env:HOST = "0.0.0.0"
$env:PORT = "8191"
$env:MAX_BROWSER_WORKERS = "auto"
$env:ENABLE_GPU = "true"
$env:BROWSER_MAX_OLD_SPACE_SIZE = "2048"
$env:HEADLESS = "true"  # No browser windows popping up on screen during solves.
                         # Set to "false" here (or `$env:HEADLESS="false"` before
                         # running this script) if you want to watch a solve live.

python -m app.main
