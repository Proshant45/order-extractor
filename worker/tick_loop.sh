#!/usr/bin/env bash
# No crontab on this box: nohup loop, one tick per minute; flock prevents overlapping ticks.
# Start:  nohup worker/tick_loop.sh >/dev/null 2>&1 &     (crontab equivalent: * * * * * flock -n /tmp/oe_tick.lock worker/tick.py)
cd "$(dirname "$0")/.."
while true; do
  flock -n /tmp/oe_tick.lock .venv/bin/python worker/tick.py
  sleep 60
done
