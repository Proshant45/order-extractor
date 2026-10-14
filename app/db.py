import json, os, sqlite3, threading
from pathlib import Path
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
CREATE TABLE IF NOT EXISTS file_budget(file_id INTEGER PRIMARY KEY, created_at TEXT NOT NULL);
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
        for name, declaration in (("style_image", "TEXT"),):
            if name not in cols:
                c.execute(f"ALTER TABLE orders ADD COLUMN {name} {declaration}")
        pcols = {r[1] for r in c.execute("PRAGMA table_info(pos)")}
        if "date_raw" not in pcols:
            c.execute("ALTER TABLE pos ADD COLUMN date_raw TEXT")
        bcols = {r[1] for r in c.execute("PRAGMA table_info(batches)")}
        for name, declaration in (("input_path", "TEXT"), ("input_size", "INTEGER"), ("input_sha", "TEXT"), ("force_ai", "INTEGER DEFAULT 0"), ("owner", "TEXT")):
            if name not in bcols:
                c.execute(f"ALTER TABLE batches ADD COLUMN {name} {declaration}")
        jcols = {r[1] for r in c.execute("PRAGMA table_info(ai_jobs)")}
        if "owner" not in jcols:
            c.execute("ALTER TABLE ai_jobs ADD COLUMN owner TEXT")
        for name, declaration in (("file_id", "INTEGER"), ("page", "TEXT"), ("vote", "TEXT"), ("stage", "TEXT"), ("received_at", "TEXT"), ("started_at", "TEXT"), ("output_at", "TEXT"), ("returned_at", "TEXT"), ('read_key','TEXT'),('transport',"TEXT DEFAULT 'queue'")):
            if name not in jcols: c.execute(f"ALTER TABLE ai_jobs ADD COLUMN {name} {declaration}")
        c.execute("CREATE INDEX IF NOT EXISTS ai_jobs_file ON ai_jobs(file_id,id)")
        c.execute('CREATE INDEX IF NOT EXISTS ai_jobs_read ON ai_jobs(read_key,status)')
        fcols = {r[1] for r in c.execute("PRAGMA table_info(files)")}
        if "source_path" not in fcols: c.execute("ALTER TABLE files ADD COLUMN source_path TEXT")
        if "budget_file_id" not in fcols: c.execute("ALTER TABLE files ADD COLUMN budget_file_id INTEGER")
        if "doc_type" not in fcols:
            c.execute("ALTER TABLE files ADD COLUMN doc_type TEXT")
            c.execute("ALTER TABLE files ADD COLUMN meta TEXT")
        if not c.execute("SELECT 1 FROM kv WHERE k='file_budget_initialized'").fetchone():
            c.execute("INSERT OR IGNORE INTO file_budget(file_id,created_at) SELECT f.id,f.created_at FROM files f WHERE (f.method LIKE '%ai%' AND f.method NOT LIKE 'cache:%') OR EXISTS(SELECT 1 FROM ai_jobs j WHERE j.file_id=f.id AND j.attempts>0)")
            c.execute("UPDATE files SET budget_file_id=id WHERE id IN (SELECT file_id FROM file_budget)")
            c.execute("INSERT INTO kv(k,v) VALUES('file_budget_initialized','1')")
        if "review_status" not in cols:
            c.execute("ALTER TABLE orders ADD COLUMN review_status TEXT")
            c.execute("ALTER TABLE orders ADD COLUMN review_json TEXT")

def set_review(oid, status, review):
    with conn() as c:
        c.execute("UPDATE orders SET review_status=?, review_json=? WHERE id=?", (status, json.dumps(review, default=str), oid))

def add_file(name, kind=None, size=None, sha=None, status="processing", parent_id=None, reason=None):
    with conn() as c:
        if parent_id is not None and status in ('processing','cached','skipped'):
            row=c.execute("SELECT id FROM files WHERE parent_id=? AND name=? AND status='pending' ORDER BY id DESC LIMIT 1",(parent_id,name)).fetchone()
            if row:
                c.execute('UPDATE files SET kind=COALESCE(?,kind),size=COALESCE(?,size),sha256=COALESCE(?,sha256),status=?,reason=? WHERE id=?',(kind,size,sha,status,reason,row['id']))
                return row['id']
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
              sizes=?,colors=?,currency=?,warnings=?,updated_at=?,status=? WHERE id=?""", (*vals, d["status"], order_id))
            c.execute("DELETE FROM pos WHERE order_id=?", (order_id,))
            c.execute("UPDATE files SET status='edited', reason='order edited; upload again for fresh extraction' WHERE id=(SELECT file_id FROM orders WHERE id=?)", (order_id,))
            c.execute("UPDATE orders SET review_status='unverified', review_json=NULL WHERE id=?", (order_id,))
        c.execute("UPDATE orders SET status=?, style_image=? WHERE id=?", (d["status"], d["style_image"], order_id))
        for i, p in enumerate(d["pos"]):
            pid = c.execute("""INSERT INTO pos(order_id,po_number,po_date,destination,ship_date,sales_contract_number,stated_total_qty,
              stated_total_value,currency,seq) VALUES(?,?,?,?,?,?,?,?,?,?)""",
              (order_id, p["po_number"], p["po_date"], p["destination"], p["ship_date"], p["sales_contract_number"],
               p["stated_total_qty"], p["stated_total_value"], p["currency"], i)).lastrowid
            c.execute("UPDATE pos SET date_raw=? WHERE id=?", (json.dumps(p.get("date_raw") or {}), pid))
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
            p = dict(p); p["date_raw"] = json.loads(p.get("date_raw") or "{}")
            p["lines"] = [dict(l, sizes=json.loads(l["sizes"])) for l in c.execute("SELECT * FROM po_lines WHERE po_id=? ORDER BY seq", (p["id"],))]
            o["pos"].append(p)
        return o

def list_orders(q: str | None = None):
    sql = """SELECT o.id,o.buyer_name,o.brand,o.style_number,o.factory,o.status,o.currency,o.created_at,o.updated_at,f.name file_name,
          (SELECT GROUP_CONCAT(po_number, ', ') FROM pos WHERE order_id=o.id) po_numbers,
          (SELECT COUNT(*) FROM pos WHERE order_id=o.id) n_pos,
          (SELECT COALESCE(SUM(COALESCE((SELECT SUM(value) FROM json_each(l.sizes)), l.order_qty, 0)),0) FROM po_lines l JOIN pos p ON p.id=l.po_id WHERE p.order_id=o.id) qty,
          (SELECT SUM(COALESCE(l.net_value, l.rate*COALESCE((SELECT SUM(value) FROM json_each(l.sizes)), l.order_qty))) FROM po_lines l JOIN pos p ON p.id=l.po_id WHERE p.order_id=o.id) value
          FROM orders o LEFT JOIN files f ON f.id=o.file_id"""
    args = []
    if q:
        like = f"%{q.strip()}%"
        sql += """ WHERE o.buyer_name LIKE ? OR o.style_number LIKE ? OR o.brand LIKE ? OR o.factory LIKE ? OR f.name LIKE ?
                   OR EXISTS(SELECT 1 FROM pos WHERE order_id=o.id AND po_number LIKE ?)"""
        args = [like] * 6
    with conn() as c:
        return [dict(r) for r in c.execute(sql + " ORDER BY o.id DESC", args)]

def delete_order(oid) -> bool:
    with conn() as c:
        c.execute("DELETE FROM po_lines WHERE po_id IN (SELECT id FROM pos WHERE order_id=?)", (oid,))
        c.execute("DELETE FROM pos WHERE order_id=?", (oid,))
        return c.execute("DELETE FROM orders WHERE id=?", (oid,)).rowcount == 1

FILE_ORDER_SQL = "COALESCE((SELECT id FROM orders WHERE file_id=f.id), (SELECT o.id FROM orders o JOIN files original ON original.id=o.file_id WHERE original.sha256=f.sha256 ORDER BY o.id DESC LIMIT 1))"


def list_files(limit=50):
    with conn() as c:
        rows=[dict(r) for r in c.execute(f"SELECT f.*, {FILE_ORDER_SQL} order_id FROM files f ORDER BY f.id DESC LIMIT ?", (limit,))]
    for row in rows:
        row["source_available"] = bool(row.get("source_path") and Path(row["source_path"]).is_file())
    return rows


def get_file(fid):
    with conn() as c:
        row=c.execute(f"SELECT f.*, {FILE_ORDER_SQL} order_id FROM files f WHERE f.id=?", (fid,)).fetchone()
        return dict(row) if row else None

def file_logs(fid):
    with conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM extraction_logs WHERE file_id=? ORDER BY id", (fid,))]

# ---- AI job queue (pull model) ----
import secrets as _secrets, time as _time
LEASE_S = int(os.getenv("AI_LEASE_S", "720"))  # long text docs can take >5 min on free models
MAX_JOB_ATTEMPTS = 3

def enqueue_job(prompt, model, text, image, owner=None, *, file_id=None, page=None, vote=None):
    with conn() as c:
        c.execute("BEGIN IMMEDIATE")
        _reserve_file_budget(c, file_id)
        return c.execute("INSERT INTO ai_jobs(status,prompt,model,text,image,owner,created_at,updated_at,file_id,page,vote,stage,read_key) VALUES('queued',?,?,?,?,?,?,?,?,?,?, 'queued',?)",
                         (prompt, model, text, image, owner, now(), now(), file_id, page, vote,read_key(prompt,model,text,image))).lastrowid


def read_key(prompt,model,text,image):
    import hashlib
    h=hashlib.sha256(json.dumps([prompt,model,text],ensure_ascii=False).encode())
    h.update(image or b'')
    return h.hexdigest()


def completed_read(file_id,prompt,model,text,image):
    if file_id is None:return None
    with conn() as c:
        root=c.execute('SELECT COALESCE(budget_file_id,id) root FROM files WHERE id=?',(file_id,)).fetchone()
        if not root:return None
        row=c.execute("SELECT j.result FROM ai_jobs j JOIN files f ON f.id=j.file_id WHERE COALESCE(f.budget_file_id,f.id)=? AND j.read_key=? AND j.status='done' AND j.result IS NOT NULL ORDER BY j.id DESC LIMIT 1",(root['root'],read_key(prompt,model,text,image))).fetchone()
        return row['result'] if row else None


def start_api_read(prompt,model,text,image,*,file_id=None,page=None,vote=None,owner=None):
    lease=_secrets.token_hex(12)
    stamp=now()
    with conn() as c:
        c.execute('BEGIN IMMEDIATE');_reserve_file_budget(c,file_id)
        jid=c.execute("INSERT INTO ai_jobs(status,prompt,model,text,image,owner,created_at,updated_at,file_id,page,vote,stage,read_key,transport,worker,attempts,lease_id,lease_until,received_at,started_at) VALUES('leased',?,?,?,?,?,?,?,?,?,?,'model_started',?,'openrouter','OpenRouter',?,?,?,?,?)",(prompt,model,text,image,owner,stamp,stamp,file_id,page,vote,read_key(prompt,model,text,image),MAX_JOB_ATTEMPTS,lease,_time.time()+config.LLM_TIMEOUT_S+45,stamp,stamp)).lastrowid
    return {'id':jid,'lease_id':lease}

def pending_jobs(owner):
    """Cheap count for the box cron tick: queued jobs (or expired leases) belonging to the given app account."""
    with conn() as c:
        return c.execute("SELECT COUNT(*) FROM ai_jobs WHERE owner=? AND transport='queue' AND (status='queued' OR (status='leased' AND lease_until<?))",
                         (owner, _time.time())).fetchone()[0]

def cached_result(sha):
    """Most recent successful extraction of identical bytes."""
    with conn() as c:
        r = c.execute("SELECT f.id file_id, f.method, f.doc_type, f.meta, f.kind, f.status, o.id order_id, o.review_status FROM files f "
                      "LEFT JOIN orders o ON o.file_id=f.id WHERE f.sha256=? AND f.status IN ('done','classified') AND (o.id IS NOT NULL OR f.status='classified') AND NOT EXISTS(SELECT 1 FROM files edited WHERE edited.sha256=f.sha256 AND edited.method='document_edited' AND edited.id>=f.id) ORDER BY f.id DESC LIMIT 1", (sha,)).fetchone()
        return dict(r) if r else None

def get_job(jid):
    with conn() as c:
        r = c.execute("SELECT id,status,result,error,attempts FROM ai_jobs WHERE id=?", (jid,)).fetchone()
        return dict(r) if r else None

def cancel_job(jid):
    with conn() as c:
        c.execute("UPDATE ai_jobs SET status='cancelled', stage='cancelled', updated_at=? WHERE id=? AND status IN ('queued','leased')", (now(), jid))

def claim_job(worker, owner=None, *, image_batches=True):
    """Atomically lease the oldest queued job (or one whose lease expired). Returns job dict incl. lease_id."""
    t = _time.time()
    with conn() as c:
        c.execute("BEGIN IMMEDIATE")
        c.execute("UPDATE ai_jobs SET status=CASE WHEN attempts>=? THEN 'error' ELSE 'queued' END, error=CASE WHEN attempts>=? THEN 'lease expired too often' ELSE error END "
                  "WHERE status='leased' AND lease_until<?", (MAX_JOB_ATTEMPTS, MAX_JOB_ATTEMPTS, t))
        while True:
            r = c.execute("SELECT id,prompt,model,text,image,file_id,page,vote FROM ai_jobs WHERE status='queued' AND transport='queue' AND (? IS NULL OR owner=?) AND (? OR image IS NULL OR substr(image,1,7)<>?) ORDER BY id LIMIT 1", (owner,owner,int(image_batches),b'OEIMG1:')).fetchone()
            if not r: return None
            try: _reserve_file_budget(c, r["file_id"])
            except ProcessingLimit as e:
                c.execute("UPDATE ai_jobs SET status='error',stage='failed',error=?,updated_at=? WHERE id=?", (str(e),now(),r["id"]))
                continue
            break
        lease = _secrets.token_hex(12)
        c.execute("UPDATE ai_jobs SET status='leased', stage='received', received_at=?, started_at=NULL, output_at=NULL, returned_at=NULL, lease_id=?, lease_until=?, attempts=attempts+1, worker=?, updated_at=? WHERE id=?",
                  (now(), lease, t + LEASE_S, worker, now(), r["id"]))
        return dict(r, lease_id=lease, lease_s=LEASE_S)

def renew_job(jid, lease_id):
    """Extend a live lease (worker still running a long read). False if the lease is gone/stale."""
    with conn() as c:
        n = c.execute("UPDATE ai_jobs SET lease_until=?, updated_at=? WHERE id=? AND lease_id=? AND status='leased' AND lease_until>?",
                      (_time.time() + LEASE_S, now(), jid, lease_id, _time.time())).rowcount
    return n == 1

def finish_job(jid, lease_id, result=None, error=None):
    """Idempotent: a result for an already-finished job or a stale lease is acknowledged and ignored."""
    with conn() as c:
        r = c.execute("SELECT status,lease_id,lease_until,attempts FROM ai_jobs WHERE id=?", (jid,)).fetchone()
        if not r:
            return "unknown"
        if r["status"] in ("done", "error", "cancelled"):
            return "already_" + r["status"]
        if r["status"] != "leased" or r["lease_id"] != lease_id or (r["lease_until"] or 0) <= _time.time():
            return "stale_lease"
        if error is not None:
            st = "error" if r["attempts"] >= MAX_JOB_ATTEMPTS else "queued"   # retry on another poll
            c.execute("UPDATE ai_jobs SET status=?, stage=?, error=?, lease_id=NULL, updated_at=? WHERE id=?", (st, "queued" if st == "queued" else "failed", error[:1000], now(), jid))
            return "requeued" if st == "queued" else "error"
        c.execute("UPDATE ai_jobs SET status='done', stage='returned', returned_at=?, result=?, error=NULL, image=NULL, text=NULL, updated_at=? WHERE id=?", (now(), result, now(), jid))
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

def save_batch(bid, status, name, result=None, *, input_path=None, input_size=None, input_sha=None, force_ai=False, owner=None):
    with conn() as c:
        c.execute("INSERT INTO batches(id,status,name,result,created_at,updated_at,input_path,input_size,input_sha,force_ai,owner) VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET status=excluded.status, result=excluded.result, updated_at=excluded.updated_at",
                  (bid, status, name, json.dumps(result) if result is not None else None, now(), now(), str(input_path) if input_path else None, input_size, input_sha, int(force_ai), owner))

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


def interrupted_batches():
    with conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM batches WHERE status='processing' ORDER BY created_at")]


def set_batch_input(bid, path, size, sha, force_ai, owner):
    with conn() as c:
        c.execute("UPDATE batches SET input_path=?, input_size=?, input_sha=?, force_ai=?, owner=? WHERE id=?",
                  (str(path), size, sha, int(force_ai), owner, bid))


def recover_extractions():
    with conn() as c:
        c.execute("UPDATE ai_jobs SET status='cancelled', error='server restarted; extraction replayed', updated_at=? WHERE status IN ('queued','leased')", (now(),))
        c.execute("UPDATE files SET status='done', reason='recovered saved order after restart' WHERE status='processing' AND EXISTS(SELECT 1 FROM orders WHERE file_id=files.id)")
        c.execute("UPDATE files SET status='failed', reason='server restarted during extraction; batch will retry retained input' WHERE status='processing'")


def batches_with_results():
    with conn() as c:
        return [dict(r) for r in c.execute("SELECT id,name,result FROM batches WHERE status='done' AND result IS NOT NULL")]


def update_job_progress(jid, lease_id, stage):
    if stage not in {"starting", "model_started", "output_received", "returning"}: return False
    with conn() as c:
        column = {"model_started":"started_at", "output_received":"output_at"}.get(stage)
        stamp = f", {column}=COALESCE({column},?)" if column else ""
        values = (stage, now(), *((now(),) if column else ()), jid, lease_id, _time.time())
        return c.execute(f"UPDATE ai_jobs SET stage=?, updated_at=?{stamp} WHERE id=? AND lease_id=? AND status='leased' AND lease_until>?", values).rowcount == 1


def file_job_progress(file_ids):
    if not file_ids: return {}
    with conn() as c:
        rows = [dict(r) for r in c.execute(f"SELECT id,file_id,page,vote,model,status,stage,worker,attempts,updated_at,error,received_at,started_at,output_at,returned_at FROM ai_jobs WHERE file_id IN ({','.join('?' for _ in file_ids)}) ORDER BY id", file_ids)]
    out = {}
    for r in rows:
        if r['status'] in ('cancelled', 'error'):
            r['stage'] = 'cancelled' if r['status'] == 'cancelled' else 'failed'
        x = out.setdefault(r["file_id"], {"total":0,"done":0,"failed":0,"jobs":[]})
        x["total"] += 1; x["done"] += r["status"] == "done"; x["failed"] += r["status"] in ("error", "cancelled")
        x["jobs"].append(r)
    return out


def order_options():
    fields = ('buyer_name', 'brand', 'factory', 'department', 'currency')
    options = {field: set() for field in (*fields, 'destination', 'colors', 'sizes')}
    with conn() as c:
        for row in c.execute('SELECT buyer_name,brand,factory,department,currency,sizes,colors FROM orders'):
            for field in fields:
                if row[field] and row[field].strip(): options[field].add(row[field].strip())
            sizes = json.loads(row['sizes'] or '[]')
            if sizes: options['sizes'].add(', '.join(sizes))
            options['colors'].update(x for x in json.loads(row['colors'] or '[]') if x)
        options['destination'].update(row[0] for row in c.execute("SELECT DISTINCT destination FROM pos WHERE destination IS NOT NULL AND destination<>''"))
        options['colors'].update(row[0] for row in c.execute("SELECT DISTINCT color FROM po_lines WHERE color<>''"))
    return {field: sorted(values, key=str.casefold) for field, values in options.items()}


class ProcessingLimit(RuntimeError):
    pass


def _reserve_file_budget(c, fid):
    if fid is None: return
    row = c.execute("SELECT budget_file_id FROM files WHERE id=?", (fid,)).fetchone()
    if not row: raise ValueError("AI processing requires a recorded source file")
    root = row["budget_file_id"] or fid
    if c.execute("SELECT 1 FROM file_budget WHERE file_id=?", (root,)).fetchone(): return
    used = c.execute("SELECT COUNT(*) FROM file_budget").fetchone()[0]
    if used >= config.MAX_PROCESSING_FILES:
        raise ProcessingLimit(f"System processing limit reached: {config.MAX_PROCESSING_FILES} files total. No further file sent to AI.")
    c.execute("INSERT INTO file_budget(file_id,created_at) VALUES(?,?)", (root,now()))
    c.execute("UPDATE files SET budget_file_id=? WHERE id=?", (root,fid))


def reserve_file_budget(fid):
    with conn() as c:
        c.execute("BEGIN IMMEDIATE")
        _reserve_file_budget(c,fid)


def copy_file_budget(old_fid,new_fid):
    with conn() as c:
        row = c.execute("SELECT budget_file_id FROM files WHERE id=?", (old_fid,)).fetchone()
        if row and row["budget_file_id"]:
            c.execute("UPDATE files SET budget_file_id=? WHERE id=?", (row["budget_file_id"],new_fid))


def recover_file_budget(fid,sha,name):
    with conn() as c:
        row = c.execute("SELECT budget_file_id FROM files WHERE sha256=? AND name=? AND id<>? AND budget_file_id IS NOT NULL AND (status='queued_ai' OR reason LIKE 'server restarted%') ORDER BY id DESC LIMIT 1", (sha,name,fid)).fetchone()
        if row: c.execute("UPDATE files SET budget_file_id=? WHERE id=?", (row["budget_file_id"],fid))


def processing_budget_status():
    with conn() as c: used = c.execute("SELECT COUNT(*) FROM file_budget").fetchone()[0]
    return {"limit":config.MAX_PROCESSING_FILES,"used":used,"remaining":max(0,config.MAX_PROCESSING_FILES-used)}
