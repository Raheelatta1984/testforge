#!/usr/bin/env bash
cd ~/testforge
source venv/bin/activate
export TF_BROWSER_MODE=bundled
export TF_ARTIFACTS=$HOME/testforge/artifacts
exec uvicorn app.main:app --host 0.0.0.0 --port 8000
