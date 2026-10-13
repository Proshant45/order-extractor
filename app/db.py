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
        o["pos"] = []
        for p in c.execute("SELECT * FROM pos WHERE order_id=? ORDER BY seq", (oid,)):
            p = dict(p)
            p["lines"] = [dict(l, sizes=json.loads(l["sizes"])) for l in c.execute("SELECT * FROM po_lines WHERE po_id=? ORDER BY seq", (p["id"],))]
            o["pos"].append(p)
        return o

def list_orders():
    with conn() as c:
        return [dict(r) for r in c.execute("""SELECT o.id,o.buyer_name,o.style_number,o.factory,o.status,o.updated_at,f.name file_name,
          (SELECT GROUP_CONCAT(po_number) FROM pos WHERE order_id=o.id) po_numbers,
          (SELECT COUNT(*) FROM pos WHERE order_id=o.id) n_pos FROM orders o LEFT JOIN files f ON f.id=o.file_id ORDER BY o.id DESC""")]

def list_files(limit=50):
    with conn() as c:
        return [dict(r) for r in c.execute("""SELECT f.*, (SELECT id FROM orders WHERE file_id=f.id) order_id FROM files f ORDER BY f.id DESC LIMIT ?""", (limit,))]

def file_logs(fid):
    with conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM extraction_logs WHERE file_id=? ORDER BY id", (fid,))]
