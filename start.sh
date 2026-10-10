#!/usr/bin/env bash
cd "$(dirname "$0")"
if [ -f .venv/bin/activate ]; then
  source .venv/bin/activate
elif [ -f venv/bin/activate ]; then
  source venv/bin/activate
fi
export TF_BROWSER_MODE="${TF_BROWSER_MODE:-bundled}"
export TF_ARTIFACTS="${TF_ARTIFACTS:-$HOME/testforge/artifacts}"
export PORT="${PORT:-8000}"
exec uvicorn app.main:app --host 0.0.0.0 --port "$PORT"
