#!/usr/bin/env python3
"""AI-less tick (run every ~15s). Cheap GET /api/worker/pending for the app account; exits at once if 0.
If jobs are pending, runs the codex worker in --drain mode (it exits when the queue is empty). One log line per run."""
import os, subprocess, sys, time, urllib.request, urllib.parse, json
from dotenv import load_dotenv
from pathlib import Path
HERE = Path(__file__).resolve().parent
LOG = HERE / "tick.log"; MAX_LOG = 512 * 1024

def env():
    load_dotenv(HERE / ".env")

def logline(msg):
    if LOG.exists() and LOG.stat().st_size > MAX_LOG:
        LOG.replace(LOG.with_suffix(".log.1"))   # simple rotation: keep one previous file
    with open(LOG, "a") as f:
        f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n")

def main():
    env()
    owner = os.getenv("APP_ACCOUNT", "fiazul")
    req = urllib.request.Request(os.environ["APP_URL"].rstrip("/") + "/api/worker/pending?" + urllib.parse.urlencode({"owner":owner,"worker":os.getenv("WORKER_NAME","tick")}),
                                 headers={"Authorization": "Bearer " + os.environ["WORKER_TOKEN"]})
    try:
        n = json.load(urllib.request.urlopen(req, timeout=90))["pending"]
    except Exception as e:
        logline(f"pending check failed: {e.__class__.__name__}: {str(e)[:120]}"); return
    if not n and not any((HERE / "outbox").glob("*.json")):
        return
    t = time.time()
    env2 = dict(os.environ, PATH=f"{Path.home()}/.local/bin:" + os.environ.get("PATH", ""))
    rc = subprocess.call([str(HERE.parent / ".venv/bin/python"), str(HERE / "box_worker.py"), "--drain"], env=env2,
                         stdout=open(HERE / "worker.log", "a"), stderr=subprocess.STDOUT)
    logline(f"pending={n} -> worker ran {round(time.time()-t)}s rc={rc}")

if __name__ == "__main__":
    main()
