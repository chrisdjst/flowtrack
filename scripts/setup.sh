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
    echo "  uv installed."
    echo "  NOTE: Add 'export PATH=\"\$HOME/.local/bin:\$PATH\"' to your shell profile."
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
    echo "  Edit .env with your credentials before running."
else
    echo "  .env already exists, skipping."
fi

# 5. Database setup
echo "[5/5] Database setup..."
if command -v docker &> /dev/null; then
    running=$(docker ps --filter "name=flowtrack-db" --format "{{.Names}}" 2>/dev/null || true)
    if [ "$running" = "flowtrack-db" ]; then
        echo "  PostgreSQL container already running."
    else
        echo "  Starting PostgreSQL with Docker..."
        docker compose up -d db
        echo "  Waiting for database to be ready..."
        sleep 5
        echo "  PostgreSQL running on localhost:5432"
    fi

    echo "  Running migrations..."
    uv run alembic upgrade head
    echo "  Migrations applied."
else
    echo "  Docker not found. Options:"
    echo "    a) Install Docker and re-run this script"
    echo "    b) Install PostgreSQL manually, create 'flowtrack' database, then run:"
    echo "       uv run alembic upgrade head"
fi

echo ""
echo "=== Setup complete ==="
echo ""
echo "Quick start:"
echo "  uv run flowtrack --help        # Show all commands"
echo "  uv run flowtrack dev start     # Start a dev session"
echo "  uv run flowtrack status        # Show current status"
echo ""
