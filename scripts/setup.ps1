# FlowTrack - Setup script for Windows (PowerShell)
# Usage: .\scripts\setup.ps1

$ErrorActionPreference = "Stop"

Write-Host ""
Write-Host "=== FlowTrack Setup ===" -ForegroundColor Cyan
Write-Host ""

# 1. Check/install uv
Write-Host "[1/5] Checking uv..." -ForegroundColor Yellow
if (Get-Command uv -ErrorAction SilentlyContinue) {
    $uvVersion = uv --version
    Write-Host "  uv found: $uvVersion" -ForegroundColor Green
} else {
    Write-Host "  Installing uv..." -ForegroundColor Yellow
    powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
    $env:PATH = "$env:USERPROFILE\.local\bin;$env:PATH"
    if (-Not (Get-Command uv -ErrorAction SilentlyContinue)) {
        Write-Host "  uv installed but not in PATH. Restart your terminal and re-run this script." -ForegroundColor Red
        exit 1
    }
    Write-Host "  uv installed." -ForegroundColor Green
}

# 2. Install Python
Write-Host "[2/5] Checking Python..." -ForegroundColor Yellow
uv python install 3.13
Write-Host "  Python 3.13 ready." -ForegroundColor Green

# 3. Install dependencies
Write-Host "[3/5] Installing dependencies..." -ForegroundColor Yellow
uv sync --group dev
Write-Host "  Dependencies installed." -ForegroundColor Green

# 4. Create .env if it doesn't exist
Write-Host "[4/5] Checking .env..." -ForegroundColor Yellow
if (-Not (Test-Path ".env")) {
    Copy-Item ".env.example" ".env"
    Write-Host "  .env created from .env.example" -ForegroundColor Green
} else {
    Write-Host "  .env already exists, skipping." -ForegroundColor Green
}

# Load DB port from .env
$dbPort = 5433
$envContent = Get-Content ".env" -ErrorAction SilentlyContinue
foreach ($line in $envContent) {
    if ($line -match "^FLOWTRACK_DB_PORT=(\d+)") {
        $dbPort = $Matches[1]
    }
}

# 5. Database setup
Write-Host "[5/5] Database setup..." -ForegroundColor Yellow

if (-Not (Get-Command docker -ErrorAction SilentlyContinue)) {
    Write-Host "  Docker not found." -ForegroundColor Red
    Write-Host "  Install Docker Desktop: https://docs.docker.com/desktop/install/windows-install/" -ForegroundColor Yellow
    Write-Host "  Then re-run this script." -ForegroundColor Yellow
    Write-Host ""
    Write-Host "  Or set up PostgreSQL manually:" -ForegroundColor Yellow
    Write-Host "    1. Create database: CREATE DATABASE flowtrack;" -ForegroundColor Gray
    Write-Host "    2. Update FLOWTRACK_DATABASE_URL in .env" -ForegroundColor Gray
    Write-Host "    3. Run: uv run alembic upgrade head" -ForegroundColor Gray
    Write-Host ""
    Write-Host "=== Setup incomplete (database pending) ===" -ForegroundColor Yellow
    exit 0
}

$running = docker ps --filter "name=flowtrack-db" --format "{{.Names}}" 2>$null
if ($running -eq "flowtrack-db") {
    Write-Host "  PostgreSQL container already running on port $dbPort." -ForegroundColor Green
} else {
    # Check if port is available
    $portInUse = $false
    try {
        $conn = New-Object System.Net.Sockets.TcpClient
        $conn.Connect("localhost", $dbPort)
        $conn.Close()
        $portInUse = $true
    } catch {
        $portInUse = $false
    }

    if ($portInUse) {
        Write-Host "  Port $dbPort is already in use." -ForegroundColor Red
        Write-Host "  Options:" -ForegroundColor Yellow
        Write-Host "    a) Change FLOWTRACK_DB_PORT in .env to a free port and re-run" -ForegroundColor Gray
        Write-Host "    b) Stop the service using port $dbPort" -ForegroundColor Gray
        Write-Host ""
        Write-Host "=== Setup incomplete (port conflict) ===" -ForegroundColor Yellow
        exit 1
    }

    Write-Host "  Starting PostgreSQL on port $dbPort..." -ForegroundColor Yellow
    docker compose up -d db
    if ($LASTEXITCODE -ne 0) {
        Write-Host "  Failed to start PostgreSQL container." -ForegroundColor Red
        exit 1
    }

    Write-Host "  Waiting for database to be ready..." -ForegroundColor Yellow
    $retries = 0
    $maxRetries = 15
    while ($retries -lt $maxRetries) {
        $health = docker inspect --format "{{.State.Health.Status}}" flowtrack-db 2>$null
        if ($health -eq "healthy") {
            break
        }
        Start-Sleep -Seconds 2
        $retries++
    }

    if ($retries -eq $maxRetries) {
        Write-Host "  Database did not become ready in time." -ForegroundColor Red
        Write-Host "  Check: docker logs flowtrack-db" -ForegroundColor Yellow
        exit 1
    }

    Write-Host "  PostgreSQL running on localhost:$dbPort" -ForegroundColor Green
}

Write-Host "  Running migrations..." -ForegroundColor Yellow
uv run alembic upgrade head
if ($LASTEXITCODE -ne 0) {
    Write-Host "  Migration failed. Check your FLOWTRACK_DATABASE_URL in .env" -ForegroundColor Red
    exit 1
}
Write-Host "  Migrations applied." -ForegroundColor Green

Write-Host ""
Write-Host "=== Setup complete ===" -ForegroundColor Cyan
Write-Host ""
Write-Host "Quick start:" -ForegroundColor White
Write-Host "  uv run flowtrack --help        # Show all commands" -ForegroundColor Gray
Write-Host "  uv run flowtrack dev start     # Start a dev session" -ForegroundColor Gray
Write-Host "  uv run flowtrack status        # Show current status" -ForegroundColor Gray
Write-Host ""
