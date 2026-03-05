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
    Write-Host "  uv installed." -ForegroundColor Green
    Write-Host "  NOTE: Restart your terminal or add ~/.local/bin to PATH if 'uv' is not found." -ForegroundColor Yellow
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
    Write-Host "  Edit .env with your credentials before running." -ForegroundColor Yellow
} else {
    Write-Host "  .env already exists, skipping." -ForegroundColor Green
}

# 5. Database setup
Write-Host "[5/5] Database setup..." -ForegroundColor Yellow
if (Get-Command docker -ErrorAction SilentlyContinue) {
    $running = docker ps --filter "name=flowtrack-db" --format "{{.Names}}" 2>$null
    if ($running -eq "flowtrack-db") {
        Write-Host "  PostgreSQL container already running." -ForegroundColor Green
    } else {
        Write-Host "  Starting PostgreSQL with Docker..." -ForegroundColor Yellow
        docker compose up -d db
        Write-Host "  Waiting for database to be ready..." -ForegroundColor Yellow
        Start-Sleep -Seconds 5
        Write-Host "  PostgreSQL running on localhost:5432" -ForegroundColor Green
    }

    Write-Host "  Running migrations..." -ForegroundColor Yellow
    uv run alembic upgrade head
    Write-Host "  Migrations applied." -ForegroundColor Green
} else {
    Write-Host "  Docker not found. Options:" -ForegroundColor Yellow
    Write-Host "    a) Install Docker Desktop and re-run this script" -ForegroundColor Yellow
    Write-Host "    b) Install PostgreSQL manually, create 'flowtrack' database, then run:" -ForegroundColor Yellow
    Write-Host "       uv run alembic upgrade head" -ForegroundColor Yellow
}

Write-Host ""
Write-Host "=== Setup complete ===" -ForegroundColor Cyan
Write-Host ""
Write-Host "Quick start:" -ForegroundColor White
Write-Host "  uv run flowtrack --help        # Show all commands" -ForegroundColor Gray
Write-Host "  uv run flowtrack dev start     # Start a dev session" -ForegroundColor Gray
Write-Host "  uv run flowtrack status        # Show current status" -ForegroundColor Gray
Write-Host ""
