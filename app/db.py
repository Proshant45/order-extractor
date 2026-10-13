import json, sqlite3, threading
from contextlib import contextmanager
from datetime import datetime, timezone
from . import config

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS files(id INTEGER PRIMARY KEY, parent_id INTEGER REFERENCES files(id), name TEXT NOT NULL,
  kind TEXT, size INTEGER, sha256 TEXT, status TEXT NOT NULL, reason TEXT, method TEXT, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS orders(id INTEGER PRIMARY KEY, file_id INTEGER REFERENCES files(id), buyer_name TEXT, brand TEXT,
  style_number TEXT, factory TEXT, planned_line INTEGER, fob REAL, smv REAL, cm REAL, department TEXT, sizes TEXT, colors TEXT,
  currency TEXT, status TEXT DEFAULT 'draft', warnings TEXT, raw_json TEXT, created_at TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS pos(id INTEGER PRIMARY KEY, order_id INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
  po_number TEXT NOT NULL, po_date TEXT, destination TEXT, ship_date TEXT, sales_contract_number TEXT,
  stated_total_qty INTEGER, stated_total_value REAL, currency TEXT, seq INTEGER);
CREATE TABLE IF NOT EXISTS po_lines(id INTEGER PRIMARY KEY, po_id INTEGER NOT NULL REFERENCES pos(id) ON DELETE CASCADE,
  seq INTEGER, style TEXT, color TEXT NOT NULL, color_code TEXT, sizes TEXT NOT NULL, order_qty INTEGER, rate REAL, net_value REAL);
CREATE TABLE IF NOT EXISTS ai_jobs(id INTEGER PRIMARY KEY, status TEXT NOT NULL, prompt TEXT, model TEXT, text TEXT, image BLOB,
  result TEXT, error TEXT, attempts INTEGER DEFAULT 0, lease_id TEXT, lease_until REAL, worker TEXT, created_at TEXT, updated_at TEXT);
CREATE INDEX IF NOT EXISTS ai_jobs_status ON ai_jobs(status, id);
CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS batches(id TEXT PRIMARY KEY, status TEXT, name TEXT, result TEXT, created_at TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS extraction_logs(id INTEGER PRIMARY KEY, file_id INTEGER REFERENCES files(id), level TEXT, message TEXT, created_at TEXT);
"""
_lock = threading.Lock()
now = lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")

@contextmanager
def conn():
    c = sqlite3.connect(config.DB_PATH, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    try:
        with _lock:
            yield c
            c.commit()
    finally:
        c.close()

def init():
    config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with conn() as c:
        c.executescript(SCHEMA)
        cols = {r[1] for r in c.execute("PRAGMA table_info(orders)")}
        jcols = {r[1] for r in c.execute("PRAGMA table_info(ai_jobs)")}
        if "owner" not in jcols:
            c.execute("ALTER TABLE ai_jobs ADD COLUMN owner TEXT")
        fcols = {r[1] for r in c.execute("PRAGMA table_info(files)")}
        if "doc_type" not in fcols:
            c.execute("ALTER TABLE files ADD COLUMN doc_type TEXT")
            c.execute("ALTER TABLE files ADD COLUMN meta TEXT")
        if "review_status" not in cols:
            c.execute("ALTER TABLE orders ADD COLUMN review_status TEXT")
            c.execute("ALTER TABLE orders ADD COLUMN review_json TEXT")

def set_review(oid, status, review):
    with conn() as c:
        c.execute("UPDATE orders SET review_status=?, review_json=? WHERE id=?", (status, json.dumps(review, default=str), oid))

def add_file(name, kind=None, size=None, sha=None, status="processing", parent_id=None, reason=None):
    with conn() as c:
        return c.execute("INSERT INTO files(parent_id,name,kind,size,sha256,status,reason,created_at) VALUES(?,?,?,?,?,?,?,?)",
                         (parent_id, name, kind, size, sha, status, reason, now())).lastrowid

def update_file(fid, **kw):
    with conn() as c:
        c.execute(f"UPDATE files SET {','.join(k+'=?' for k in kw)} WHERE id=?", (*kw.values(), fid))

def log(fid, msg, level="info"):
    with conn() as c:
        c.execute("INSERT INTO extraction_logs(file_id,level,message,created_at) VALUES(?,?,?,?)", (fid, level, msg, now()))

def save_order(order, warnings, file_id=None, order_id=None, raw=None):
    d = order.model_dump(mode="json")
    with conn() as c:
        vals = (d["buyer_name"], d["brand"], d["style_number"], d["factory"], d["planned_line"], d["fob"], d["smv"], d["cm"],
                d["department"], json.dumps(d["sizes"]), json.dumps(d["colors"]), d["currency"], json.dumps(warnings), now())
        if order_id is None:
            order_id = c.execute("""INSERT INTO orders(buyer_name,brand,style_number,factory,planned_line,fob,smv,cm,department,sizes,colors,
              currency,warnings,updated_at,file_id,raw_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
              (*vals, file_id, json.dumps(raw) if raw else None, now())).lastrowid
        else:
            c.execute("""UPDATE orders SET buyer_name=?,brand=?,style_number=?,factory=?,planned_line=?,fob=?,smv=?,cm=?,department=?,
              sizes=?,colors=?,currency=?,warnings=?,updated_at=?,status='saved' WHERE id=?""", (*vals, order_id))
            c.execute("DELETE FROM pos WHERE order_id=?", (order_id,))
        for i, p in enumerate(d["pos"]):
            pid = c.execute("""INSERT INTO pos(order_id,po_number,po_date,destination,ship_date,sales_contract_number,stated_total_qty,
              stated_total_value,currency,seq) VALUES(?,?,?,?,?,?,?,?,?,?)""",
              (order_id, p["po_number"], p["po_date"], p["destination"], p["ship_date"], p["sales_contract_number"],
               p["stated_total_qty"], p["stated_total_value"], p["currency"], i)).lastrowid
            for j, l in enumerate(p["lines"]):
                c.execute("INSERT INTO po_lines(po_id,seq,style,color,color_code,sizes,order_qty,rate,net_value) VALUES(?,?,?,?,?,?,?,?,?)",
                          (pid, j, l["style"], l["color"], l["color_code"], json.dumps(l["sizes"]), l["order_qty"], l["rate"], l["net_value"]))
    return order_id

def get_order(oid):
    with conn() as c:
        o = c.execute("SELECT o.*, f.name file_name, f.method FROM orders o LEFT JOIN files f ON f.id=o.file_id WHERE o.id=?", (oid,)).fetchone()
        if not o:
            return None
        o = dict(o)
        for k in ("sizes", "colors", "warnings"):
            o[k] = json.loads(o[k] or "[]")
        o["review"] = json.loads(o.get("review_json") or "null"); o.pop("review_json", None); o.pop("raw_json", None)
        o["pos"] = []
        for p in c.execute("SELECT * FROM pos WHERE order_id=? ORDER BY seq", (oid,)):
            p = dict(p)
            p["lines"] = [dict(l, sizes=json.loads(l["sizes"])) for l in c.execute("SELECT * FROM po_lines WHERE po_id=? ORDER BY seq", (p["id"],))]
            o["pos"].append(p)
        return o

def list_orders():
    with conn() as c:
        return [dict(r) for r in c.execute("""SELECT o.id,o.review_status,o.buyer_name,o.style_number,o.factory,o.status,o.updated_at,f.name file_name,
          (SELECT GROUP_CONCAT(po_number) FROM pos WHERE order_id=o.id) po_numbers,
          (SELECT COUNT(*) FROM pos WHERE order_id=o.id) n_pos FROM orders o LEFT JOIN files f ON f.id=o.file_id ORDER BY o.id DESC""")]

def list_files(limit=50):
    with conn() as c:
        return [dict(r) for r in c.execute("""SELECT f.*, (SELECT id FROM orders WHERE file_id=f.id) order_id FROM files f ORDER BY f.id DESC LIMIT ?""", (limit,))]

def file_logs(fid):
    with conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM extraction_logs WHERE file_id=? ORDER BY id", (fid,))]

# ---- AI job queue (pull model) ----
import secrets as _secrets, time as _time
LEASE_S = 360
MAX_JOB_ATTEMPTS = 3

def enqueue_job(prompt, model, text, image, owner=None):
    with conn() as c:
        return c.execute("INSERT INTO ai_jobs(status,prompt,model,text,image,owner,created_at,updated_at) VALUES('queued',?,?,?,?,?,?,?)",
                         (prompt, model, text, image, owner, now(), now())).lastrowid

def pending_jobs(owner):
    """Cheap count for the box cron tick: queued jobs (or expired leases) belonging to the given app account."""
    with conn() as c:
        return c.execute("SELECT COUNT(*) FROM ai_jobs WHERE owner=? AND (status='queued' OR (status='leased' AND lease_until<?))",
                         (owner, _time.time())).fetchone()[0]

def cached_result(sha):
    """Most recent successful extraction of identical bytes."""
    with conn() as c:
        r = c.execute("SELECT f.id file_id, f.method, f.doc_type, f.meta, f.kind, f.status, o.id order_id, o.review_status FROM files f "
                      "LEFT JOIN orders o ON o.file_id=f.id WHERE f.sha256=? AND f.status IN ('done','classified') ORDER BY f.id DESC LIMIT 1", (sha,)).fetchone()
        return dict(r) if r else None

def get_job(jid):
    with conn() as c:
        r = c.execute("SELECT id,status,result,error,attempts FROM ai_jobs WHERE id=?", (jid,)).fetchone()
        return dict(r) if r else None

def cancel_job(jid):
    with conn() as c:
        c.execute("UPDATE ai_jobs SET status='cancelled', updated_at=? WHERE id=? AND status IN ('queued','leased')", (now(), jid))

def claim_job(worker, owner=None):
    """Atomically lease the oldest queued job (or one whose lease expired). Returns job dict incl. lease_id."""
    t = _time.time()
    with conn() as c:
        c.execute("BEGIN IMMEDIATE")
        c.execute("UPDATE ai_jobs SET status=CASE WHEN attempts>=? THEN 'error' ELSE 'queued' END, error=CASE WHEN attempts>=? THEN 'lease expired too often' ELSE error END "
                  "WHERE status='leased' AND lease_until<?", (MAX_JOB_ATTEMPTS, MAX_JOB_ATTEMPTS, t))
        r = c.execute("SELECT id,prompt,model,text,image FROM ai_jobs WHERE status='queued' AND (? IS NULL OR owner=?) ORDER BY id LIMIT 1",
                      (owner, owner)).fetchone()
        if not r:
            return None
        lease = _secrets.token_hex(12)
        c.execute("UPDATE ai_jobs SET status='leased', lease_id=?, lease_until=?, attempts=attempts+1, worker=?, updated_at=? WHERE id=?",
                  (lease, t + LEASE_S, worker, now(), r["id"]))
        return dict(r, lease_id=lease, lease_s=LEASE_S)

def finish_job(jid, lease_id, result=None, error=None):
    """Idempotent: a result for an already-finished job or a stale lease is acknowledged and ignored."""
    with conn() as c:
        r = c.execute("SELECT status,lease_id,attempts FROM ai_jobs WHERE id=?", (jid,)).fetchone()
        if not r:
            return "unknown"
        if r["status"] in ("done", "error", "cancelled"):
            return "already_" + r["status"]
        if r["lease_id"] != lease_id:
            return "stale_lease"
        if error is not None:
            st = "error" if r["attempts"] >= MAX_JOB_ATTEMPTS else "queued"   # retry on another poll
            c.execute("UPDATE ai_jobs SET status=?, error=?, lease_id=NULL, updated_at=? WHERE id=?", (st, error[:1000], now(), jid))
            return "requeued" if st == "queued" else "error"
        c.execute("UPDATE ai_jobs SET status='done', result=?, image=NULL, text=NULL, updated_at=? WHERE id=?", (result, now(), jid))
        return "ok"

def heartbeat(worker, info=""):
    with conn() as c:
        c.execute("INSERT INTO kv(k,v) VALUES('worker_hb',?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (json.dumps({"t": _time.time(), "worker": worker, "info": info}),))

def worker_status():
    with conn() as c:
        r = c.execute("SELECT v FROM kv WHERE k='worker_hb'").fetchone()
        q = dict(c.execute("SELECT COUNT(*) FILTER (WHERE status='queued') q, COUNT(*) FILTER (WHERE status='leased') l FROM ai_jobs").fetchone())
    hb = json.loads(r["v"]) if r else None
    age = _time.time() - hb["t"] if hb else None
    return {"online": age is not None and age < 60, "last_seen_s": round(age) if age is not None else None,
            "worker": hb and hb["worker"], "queued": q["q"], "leased": q["l"]}

def worker_online():
    return worker_status()["online"]

def save_batch(bid, status, name, result=None):
    with conn() as c:
        c.execute("INSERT INTO batches(id,status,name,result,created_at,updated_at) VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET status=excluded.status, result=excluded.result, updated_at=excluded.updated_at",
                  (bid, status, name, json.dumps(result) if result is not None else None, now(), now()))

def get_batch(bid):
    with conn() as c:
        r = c.execute("SELECT * FROM batches WHERE id=?", (bid,)).fetchone()
    if not r:
        return None
    d = dict(r); d["result"] = json.loads(d["result"]) if d["result"] else None
    return d

def queued_files():
    with conn() as c:
        return [dict(r) for r in c.execute("SELECT id,name FROM files WHERE status='queued_ai' ORDER BY id")]

def cached_count():
    with conn() as c:
        return c.execute("SELECT COUNT(*) FROM files WHERE status='cached'").fetchone()[0]
