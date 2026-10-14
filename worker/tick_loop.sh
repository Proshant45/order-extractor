#!/usr/bin/env bash
set -u
cd "$(dirname "$0")/.." || exit 1
exec 9>"worker/.tick.lock"
flock -n 9 || exit 0
trap 'exit 0' INT TERM
while true; do
  .venv/bin/python worker/tick.py
  sleep "${WORKER_INTERVAL_S:-15}" &
  wait $! || exit 0
done
