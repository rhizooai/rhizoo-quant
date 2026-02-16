#!/usr/bin/env bash
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="$SCRIPT_DIR/.venv"

echo "============================================"
echo "  Rhizoo Alpha Bot: Environment Setup"
echo "============================================"

# Create venv if it doesn't exist
if [ ! -d "$VENV_DIR" ]; then
    echo "[1/3] Creating virtual environment..."
    python3 -m venv "$VENV_DIR" 2>/dev/null || python3 -m venv --without-pip "$VENV_DIR"
    # Bootstrap pip if created without it
    if [ ! -f "$VENV_DIR/bin/pip" ]; then
        echo "       Bootstrapping pip..."
        curl -sS https://bootstrap.pypa.io/get-pip.py | "$VENV_DIR/bin/python3"
    fi
else
    echo "[1/3] Virtual environment already exists, skipping."
fi

# Activate
source "$VENV_DIR/bin/activate"

# Upgrade pip
echo "[2/3] Upgrading pip..."
pip install --upgrade pip --quiet

# Install dependencies
echo "[3/3] Installing dependencies from requirements.txt..."
pip install -r "$SCRIPT_DIR/requirements.txt" --quiet

echo ""
echo "============================================"
echo "  Setup complete."
echo "  Activate with: source $VENV_DIR/bin/activate"
echo "============================================"
