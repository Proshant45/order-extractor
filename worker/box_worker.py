#!/usr/bin/env python3
"""Box-side AI worker (pull model). Polls the Render app over HTTPS, runs opencode locally, posts results.

Env (from worker/.env, chmod 600): APP_URL, WORKER_TOKEN, OPENCODE_API_KEY, optional WORKER_NAME, WORKER_THREADS, OPENCODE_*.
Idempotent: results carry the lease_id; a late result after lease expiry is ignored server-side and the job re-runs."""
import base64, json, os, sys, tempfile, threading, time, traceback, queue, signal
from dotenv import load_dotenv
from pathlib import Path
import httpx
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import opencode_provider

load_dotenv(Path(__file__).resolve().parent / ".env")
OUTBOX = Path(os.getenv("WORKER_OUTBOX", str(Path(__file__).resolve().parent / "outbox")))
outbox_lock = threading.Lock()
URL = os.environ["APP_URL"].rstrip("/"); TOK = os.environ["WORKER_TOKEN"]
NAME = os.getenv("WORKER_NAME", "box"); THREADS = int(os.getenv("WORKER_THREADS", "2"))
OWNER = os.getenv("APP_ACCOUNT", "fiazul")   # only jobs uploaded by this app account
DRAIN = "--drain" in sys.argv               # exit once the queue stays empty (launched by the cron tick)
H = {"Authorization": f"Bearer {TOK}"}
stop = threading.Event()
busy = [0]; busy_lock = threading.Lock()   # drain mode: idle threads stay while any sibling is still working

def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)

def _renewer(j, done):
    with httpx.Client(timeout=20) as c:
        while not done.wait(int(os.getenv("RENEW_EVERY_S", "120"))):
            try:
                r = c.post(URL + "/api/worker/renew", json={"id": j["id"], "lease_id": j["lease_id"]}, headers=H)
                r.raise_for_status()
                if r.json().get("ok") is not True:
                    log(f"job {j['id']} lease no longer valid"); return
            except Exception as e:
                log(f"job {j['id']} lease renewal unverified: {type(e).__name__}")

def handle(c, j):
    done = threading.Event(); threading.Thread(target=_renewer, args=(j, done), daemon=True).start()
    try:
        _handle(c, j)
    finally:
        done.set()

def _handle(c, j):
    t = time.time()
    sent_stages = set()
    telemetry = queue.Queue(maxsize=5)
    def report_progress():
        with httpx.Client(timeout=3) as reporter:
            while True:
                stage = telemetry.get()
                if stage is None: return
                try:
                    r = reporter.post(URL + "/api/worker/progress",json={"id":j["id"],"lease_id":j["lease_id"],"stage":stage},headers=H)
                    r.raise_for_status()
                    if r.json().get("ok") is not True: log(f"job {j['id']} progress rejected; lease not current")
                except Exception as e: log(f"job {j['id']} progress unverified: {type(e).__name__}")
    reporter_thread = threading.Thread(target=report_progress,daemon=True); reporter_thread.start()
    def progress(stage):
        if stage in sent_stages: return
        sent_stages.add(stage)
        try: telemetry.put_nowait(stage)
        except queue.Full: log(f"job {j['id']} progress queue full; stage unverified")
    def event(ev):
        if ev.get("type") == "step_start": progress("model_started")
        if ev.get("type") == "text": progress("output_received")
    progress("starting")
    with tempfile.TemporaryDirectory(prefix="oew_") as d:
        f = Path(d) / ("page.png" if j.get("image_b64") else "page.txt")
        f.write_bytes(base64.b64decode(j["image_b64"])) if j.get("image_b64") else f.write_text(j.get("text") or "")
        try:
            if len(j.get("text") or "") > int(os.getenv("WORKER_MAX_TEXT", "60000")):
                raise RuntimeError(f"document text too large for one AI read ({len(j['text'])} chars); needs manual review")
            out = opencode_provider.run(j["prompt"] + ("\nThe page image is attached. Output JSON only." if j.get("image_b64") else "\nOutput JSON only."), [f],
                                        j.get("model"), min(int(os.getenv("OPENCODE_TIMEOUT_S", "300")), j.get("lease_s", 360) - 30), on_event=event, stop_event=stop)
            body = {"id": j["id"], "lease_id": j["lease_id"], "text": out}
        except Exception as e:
            body = {"id": j["id"], "lease_id": j["lease_id"], "error": f"{e.__class__.__name__}: {e}"[:900]}
    progress("returning")
    ack = deliver(c, body, attempts=1 if stop.is_set() else 5)
    telemetry.put_nowait(None)
    reporter_thread.join(timeout=.1)
    log(f"job {j['id']} model={j.get('model')} {round(time.time()-t)}s outcome={ack or 'delivery_pending'}" + (" ERR " + body["error"][:120] if "error" in body else ""))

def _outbox_path(body):
    import hashlib
    lease = hashlib.sha256(body["lease_id"].encode()).hexdigest()[:24]
    return OUTBOX / f"{body['id']}-{lease}.json"

def deliver(c, body, attempts=5):
    OUTBOX.mkdir(parents=True, exist_ok=True)
    path = _outbox_path(body)
    with outbox_lock:
        if not path.exists():
            tmp = path.with_suffix(".tmp")
            with tmp.open("w") as f:
                json.dump(body, f); f.flush(); os.fsync(f.fileno())
            tmp.replace(path)
    for attempt in range(attempts):
        try:
            r = c.post(URL + "/api/worker/result", json=body, headers=H, timeout=60)
            r.raise_for_status()
            ack = r.json().get("ack")
            if ack not in {"ok", "already_done", "requeued", "error", "already_error", "cancelled", "already_cancelled", "stale_lease", "unknown"}:
                raise RuntimeError(f"unrecognised result acknowledgement: {ack!r}")
            path.unlink(missing_ok=True)
            return ack
        except Exception as e:
            log(f"job {body['id']} result delivery unverified ({type(e).__name__}); retained locally")
            if attempt + 1 < attempts: time.sleep(min(5 * (attempt + 1), 20))
    return None

def flush_outbox(c):
    if not OUTBOX.exists(): return
    for path in list(OUTBOX.glob("*.json")):
        try:
            body = json.loads(path.read_text())
            ack = deliver(c, body, attempts=1)
            if ack: log(f"job {body['id']} retained result outcome={ack}")
        except (OSError, ValueError, KeyError) as e:
            log(f"retained result unreadable: {path.name}: {type(e).__name__}")

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
                flush_outbox(c)
                r = c.get(URL + "/api/worker/next", params={"worker": f"{NAME}-{i}", "owner": OWNER}, headers=H)
                if r.status_code == 204:
                    empty += 1
                    if DRAIN and empty >= int(os.getenv("DRAIN_IDLE_POLLS", "6")) and busy[0] == 0:
                        return
                    time.sleep(idle); idle = min(idle + 1, 5); continue
                empty = 0
                if r.status_code == 401:
                    log("worker token rejected; sleeping 60s"); time.sleep(60); continue
                r.raise_for_status(); idle = 2
                with busy_lock: busy[0] += 1
                try:
                    handle(c, r.json())
                finally:
                    with busy_lock: busy[0] -= 1
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
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    try:
        while any(t.is_alive() for t in ts) and not stop.is_set():
            time.sleep(1)
    except KeyboardInterrupt:
        stop.set()
    stop.set()
    for thread in ts: thread.join(timeout=8)
    log("worker exiting (queue drained)" if DRAIN else "worker exiting")
