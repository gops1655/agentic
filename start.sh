#!/usr/bin/env bash
# Start the Invoice Agent on macOS / Linux: ./start.sh
cd "$(dirname "$0")"
if [ ! -d .venv ]; then
  echo "First run: setting up..."
  python3 -m venv .venv
  .venv/bin/pip install -q -r requirements.txt
fi
exec .venv/bin/python main.py "$@"
