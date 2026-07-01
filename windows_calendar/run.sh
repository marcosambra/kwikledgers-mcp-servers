#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MCP_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
ENV_FILE="$SCRIPT_DIR/../../.env"
VENV_DIR="$MCP_ROOT/.venv"
PYTHON_BIN="$VENV_DIR/bin/python"
SERVER_NAME="$(basename "$SCRIPT_DIR")"
REQUIREMENTS_FILE="$SCRIPT_DIR/requirements.txt"
STAMP_DIR="$VENV_DIR/.requirements"
STAMP_FILE="$STAMP_DIR/$SERVER_NAME.sha256"

if [ ! -f "$ENV_FILE" ]; then
  cp "$SCRIPT_DIR/../../.env.example" "$ENV_FILE"
fi

if [ ! -x "$PYTHON_BIN" ]; then
  echo "Shared MCP runtime not initialized for $SERVER_NAME." >&2
  echo "Run agent/setup.sh (Linux/WSL) or agent/setup.ps1 (Windows) before starting the MCP servers." >&2
  exit 1
fi

mkdir -p "$STAMP_DIR"
CURRENT_HASH="$(sha256sum "$REQUIREMENTS_FILE" | awk '{print $1}')"
RECORDED_HASH="$(cat "$STAMP_FILE" 2>/dev/null || true)"

if [ "$CURRENT_HASH" != "$RECORDED_HASH" ]; then
  echo "Shared MCP runtime is out of date for $SERVER_NAME." >&2
  echo "Run agent/setup.sh (Linux/WSL) or agent/setup.ps1 (Windows) to install the current requirements." >&2
  exit 1
fi

exec "$PYTHON_BIN" "$SCRIPT_DIR/server.py"
