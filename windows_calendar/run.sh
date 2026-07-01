#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MCP_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
ENV_FILE="$SCRIPT_DIR/../../.env"
VENV_DIR="$MCP_ROOT/.venv"
PYTHON_BIN="$VENV_DIR/bin/python"

if [ ! -f "$ENV_FILE" ]; then
  cp "$SCRIPT_DIR/../../.env.example" "$ENV_FILE"
fi

if [ ! -x "$PYTHON_BIN" ]; then
  python3 -m venv "$VENV_DIR"
fi

"$PYTHON_BIN" -m pip install -q -r "$SCRIPT_DIR/requirements.txt"

exec "$PYTHON_BIN" "$SCRIPT_DIR/server.py"
