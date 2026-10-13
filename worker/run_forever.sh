#!/usr/bin/env bash
# Supervisor loop (no systemd): restarts the worker if it exits. Start with:  nohup worker/run_forever.sh >> worker/worker.log 2>&1 &
cd "$(dirname "$0")/.."
set -a; . worker/.env; set +a
export PATH="$HOME/.opencode/bin:$PATH"
while true; do
  .venv/bin/python worker/box_worker.py
  echo "$(date) worker exited ($?), restarting in 5s"; sleep 5
done
