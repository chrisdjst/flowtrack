#!/usr/bin/env bash
# FlowTrack - Setup script for Linux/macOS
# Usage: bash scripts/setup.sh

set -e

echo ""
echo "=== FlowTrack Setup ==="
echo ""

# 1. Check/install uv
echo "[1/5] Checking uv..."
if command -v uv &> /dev/null; then
    echo "  uv found: $(uv --version)"
else
    echo "  Installing uv..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
    if ! command -v uv &> /dev/null; then
        echo "  uv installed but not in PATH. Restart your terminal and re-run this script."
        exit 1
    fi
    echo "  uv installed."
fi

# 2. Install Python
echo "[2/5] Checking Python..."
uv python install 3.13
echo "  Python 3.13 ready."

# 3. Install dependencies
echo "[3/5] Installing dependencies..."
uv sync --group dev
echo "  Dependencies installed."

# 4. Create .env if it doesn't exist
echo "[4/5] Checking .env..."
if [ ! -f ".env" ]; then
    cp .env.example .env
    echo "  .env created from .env.example"
else
    echo "  .env already exists, skipping."
fi

# Load DB port from .env
DB_PORT=$(grep -E "^FLOWTRACK_DB_PORT=" .env 2>/dev/null | cut -d= -f2 || echo "5433")
DB_PORT=${DB_PORT:-5433}

# 5. Database setup
echo "[5/5] Database setup..."

if ! command -v docker &> /dev/null; then
    echo "  Docker not found."
    echo "  Install Docker: https://docs.docker.com/engine/install/"
    echo "  Then re-run this script."
    echo ""
    echo "  Or set up PostgreSQL manually:"
    echo "    1. Create database: CREATE DATABASE flowtrack;"
    echo "    2. Update FLOWTRACK_DATABASE_URL in .env"
    echo "    3. Run: uv run alembic upgrade head"
    echo ""
    echo "=== Setup incomplete (database pending) ==="
    exit 0
fi

running=$(docker ps --filter "name=flowtrack-db" --format "{{.Names}}" 2>/dev/null || true)
if [ "$running" = "flowtrack-db" ]; then
    echo "  PostgreSQL container already running on port $DB_PORT."
else
    # Check if port is available
    if command -v ss &> /dev/null; then
        port_in_use=$(ss -tln "sport = :$DB_PORT" 2>/dev/null | grep -c "$DB_PORT" || true)
    elif command -v lsof &> /dev/null; then
        port_in_use=$(lsof -i ":$DB_PORT" -sTCP:LISTEN 2>/dev/null | wc -l || true)
    else
        port_in_use=0
    fi

    if [ "$port_in_use" -gt 0 ] 2>/dev/null; then
        echo "  Port $DB_PORT is already in use."
        echo "  Options:"
        echo "    a) Change FLOWTRACK_DB_PORT in .env to a free port and re-run"
        echo "    b) Stop the service using port $DB_PORT"
        echo ""
        echo "=== Setup incomplete (port conflict) ==="
        exit 1
    fi

    echo "  Starting PostgreSQL on port $DB_PORT..."
    docker compose up -d db
    if [ $? -ne 0 ]; then
        echo "  Failed to start PostgreSQL container."
        exit 1
    fi

    echo "  Waiting for database to be ready..."
    retries=0
    max_retries=15
    while [ $retries -lt $max_retries ]; do
        health=$(docker inspect --format "{{.State.Health.Status}}" flowtrack-db 2>/dev/null || echo "starting")
        if [ "$health" = "healthy" ]; then
            break
        fi
        sleep 2
        retries=$((retries + 1))
    done

    if [ $retries -eq $max_retries ]; then
        echo "  Database did not become ready in time."
        echo "  Check: docker logs flowtrack-db"
        exit 1
    fi

    echo "  PostgreSQL running on localhost:$DB_PORT"
fi

echo "  Running migrations..."
if ! uv run alembic upgrade head; then
    echo "  Migration failed. Check your FLOWTRACK_DATABASE_URL in .env"
    exit 1
fi
echo "  Migrations applied."

echo ""
echo "=== Setup complete ==="
echo ""
echo "Quick start:"
echo "  uv run flowtrack --help        # Show all commands"
echo "  uv run flowtrack dev start     # Start a dev session"
echo "  uv run flowtrack status        # Show current status"
echo ""
