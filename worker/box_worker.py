#!/usr/bin/env python3
"""Box-side AI worker (pull model). Polls the Render app over HTTPS, runs opencode locally, posts results.

Env (from worker/.env, chmod 600): APP_URL, WORKER_TOKEN, OPENCODE_API_KEY, optional WORKER_NAME, WORKER_THREADS, OPENCODE_*.
Idempotent: results carry the lease_id; a late result after lease expiry is ignored server-side and the job re-runs."""
import base64, json, os, sys, tempfile, threading, time, traceback
from pathlib import Path
import httpx
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import opencode_provider

URL = os.environ["APP_URL"].rstrip("/"); TOK = os.environ["WORKER_TOKEN"]
NAME = os.getenv("WORKER_NAME", "box"); THREADS = int(os.getenv("WORKER_THREADS", "2"))
OWNER = os.getenv("APP_ACCOUNT", "fiazul")   # only jobs uploaded by this app account
DRAIN = "--drain" in sys.argv               # exit once the queue stays empty (launched by the cron tick)
H = {"Authorization": f"Bearer {TOK}"}
stop = threading.Event()

def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)

def handle(c, j):
    t = time.time()
    with tempfile.TemporaryDirectory(prefix="oew_") as d:
        f = Path(d) / ("page.png" if j.get("image_b64") else "page.txt")
        f.write_bytes(base64.b64decode(j["image_b64"])) if j.get("image_b64") else f.write_text(j.get("text") or "")
        try:
            out = opencode_provider.run(j["prompt"] + ("\nThe page image is attached. Output JSON only." if j.get("image_b64") else "\nOutput JSON only."), [f],
                                        j.get("model"), min(int(os.getenv("OPENCODE_TIMEOUT_S", "300")), j.get("lease_s", 360) - 30))
            body = {"id": j["id"], "lease_id": j["lease_id"], "text": out}
        except Exception as e:
            body = {"id": j["id"], "lease_id": j["lease_id"], "error": f"{e.__class__.__name__}: {e}"[:900]}
    for attempt in range(5):   # deliver result; safe to repeat (idempotent)
        try:
            r = c.post(URL + "/api/worker/result", json=body, headers=H, timeout=60)
            log(f"job {j['id']} model={j.get('model')} {round(time.time()-t)}s -> {r.json().get('ack')}" + (" ERR " + body["error"][:120] if "error" in body else ""))
            return
        except Exception as e:
            log("post result failed, retrying", e); time.sleep(5 * (attempt + 1))

def beat():
    """Heartbeat every 20s while running, so long opencode jobs don't show the worker as offline."""
    with httpx.Client(timeout=20) as c:
        while not stop.is_set():
            try: c.post(URL + "/api/worker/heartbeat", json={"worker": NAME, "info": "busy"}, headers=H)
            except Exception: pass
            stop.wait(20)

def loop(i):
    with httpx.Client(timeout=httpx.Timeout(60, connect=20), follow_redirects=False) as c:
        idle, empty = 2, 0
        while not stop.is_set():
            try:
                r = c.get(URL + "/api/worker/next", params={"worker": f"{NAME}-{i}", "owner": OWNER}, headers=H)
                if r.status_code == 204:
                    empty += 1
                    if DRAIN and empty >= int(os.getenv("DRAIN_IDLE_POLLS", "6")):
                        return
                    time.sleep(idle); idle = min(idle + 1, 5); continue
                empty = 0
                if r.status_code == 401:
                    log("worker token rejected; sleeping 60s"); time.sleep(60); continue
                r.raise_for_status(); idle = 2
                handle(c, r.json())
            except httpx.HTTPError as e:   # Render cold start / redeploy / network: back off and keep polling
                log(f"[{i}] poll error {e.__class__.__name__}; retry in 15s"); time.sleep(15)
            except Exception:
                traceback.print_exc(); time.sleep(10)

if __name__ == "__main__":
    if not opencode_provider.available():
        sys.exit("opencode binary not found")
    log(f"worker {NAME} -> {URL} threads={THREADS}")
    ts = [threading.Thread(target=loop, args=(i,), daemon=True) for i in range(THREADS)]
    [t.start() for t in ts]
    threading.Thread(target=beat, daemon=True).start()
    try:
        while any(t.is_alive() for t in ts):
            time.sleep(1)
    except KeyboardInterrupt:
        stop.set()
    stop.set()
    log("worker exiting (queue drained)" if DRAIN else "worker exiting")
