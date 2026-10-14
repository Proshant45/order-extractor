from __future__ import annotations
import asyncio, hashlib, json, logging, os, re, tempfile, zipfile
from pathlib import Path
from fastapi import FastAPI, HTTPException, Request, UploadFile, File
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse, Response, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool
from pydantic import ValidationError
from fastapi import Form
from fastapi.responses import RedirectResponse
from . import auth, config, db, extract, llm, zipio, documents
import shutil, threading, secrets as _sec
from .schema import Order, warnings_for

logging.basicConfig(level=logging.INFO)
BASE = Path(__file__).parent
app = FastAPI(title="Order Extractor")
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
tpl = Jinja2Templates(directory=BASE / "templates")
SEM = asyncio.Semaphore(1)   # one extraction at a time keeps RAM flat on a small instance

PUBLIC = {"/healthz", "/login", "/static/app.css"}
WORKER_PREFIX = "/api/worker/"

@app.middleware("http")
async def require_login(request: Request, call_next):
    path = request.url.path
    if path in PUBLIC:
        return await call_next(request)
    if path.startswith(WORKER_PREFIX):   # box worker: own bearer token, no session
        tok = os.getenv("WORKER_TOKEN", "")
        got = request.headers.get("authorization", "")[7:]
        if len(tok) < 32 or not auth.hmac.compare_digest(tok, got):
            return JSONResponse({"detail": "bad worker token"}, 401)
        return await call_next(request)
    user = auth.read_session(request.cookies.get(auth.SESSION_COOKIE))
    bearer = request.headers.get("authorization", "")
    if not user and bearer.lower().startswith("bearer ") and auth.check_token(bearer[7:].strip()):
        user = os.getenv("APP_USERNAME")
    if not user:
        if path.startswith("/api/") or path.startswith("/static/"):
            return JSONResponse({"detail": "login required"}, 401)
        return RedirectResponse("/login", 303)
    request.state.user = user
    return await call_next(request)

@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, error: str = ""):
    return tpl.TemplateResponse(request, "login.html", {"error": error, "configured": auth.configured()})

@app.post("/login")
def login(request: Request, username: str = Form(...), password: str = Form(...)):
    ip = (request.headers.get("x-forwarded-for") or request.client.host).split(",")[0].strip()
    wait = auth.locked(ip)
    if wait:
        return tpl.TemplateResponse(request, "login.html", {"error": f"Too many failed attempts. Try again in {wait//60+1} min.", "configured": True}, status_code=429)
    if not auth.check(username, password, ip):
        return tpl.TemplateResponse(request, "login.html", {"error": "Invalid username or password", "configured": auth.configured()}, status_code=401)
    r = RedirectResponse("/", 303)
    secure = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
    r.set_cookie(auth.SESSION_COOKIE, auth.make_session(username), max_age=auth.MAX_AGE, httponly=True, secure=secure, samesite="strict")
    return r

@app.post("/logout")
def logout():
    r = RedirectResponse("/login", 303); r.delete_cookie(auth.SESSION_COOKIE); return r

@app.on_event("startup")
def _startup():
    db.init()
    for p in config.TMP_DIR.glob("*"):   # stale temp files from a crash
        if p.is_file(): p.unlink(missing_ok=True)
    from .maintenance import clear_existing_queue_once
    report=clear_existing_queue_once()
    if not report["already_ran"]: logging.info("One-time queue cleanup: %s",report)
    db.recover_extractions()
    _recover_batches()
    threading.Thread(target=_retry_loop, daemon=True).start()

@app.get("/healthz")
def healthz():
    return {"ok": True, "llm": config.llm_provider() or "none"}

def _safe_name(n: str) -> str:
    return re.sub(r"[^\w.\- ]", "_", os.path.basename(n or "upload"))[:120]

async def _stream_to_disk(up: UploadFile) -> tuple[Path, int, str]:
    fd, tmp = tempfile.mkstemp(suffix=Path(_safe_name(up.filename)).suffix, dir=config.TMP_DIR)
    tmp, n, h = Path(tmp), 0, hashlib.sha256()
    try:
        with open(fd, "wb") as f:
            while chunk := await up.read(config.CHUNK):
                n += len(chunk)
                if n > max(config.MAX_UPLOAD_BYTES, config.MAX_ZIP_MB * 1024 * 1024):
                    raise HTTPException(413, f"Upload exceeds {config.MAX_ZIP_MB} MB (zip) limit")
                h.update(chunk); f.write(chunk)
    except BaseException:
        tmp.unlink(missing_ok=True); raise
    with open(tmp, "rb") as f:
        is_zip = f.read(4) == b"PK\x03\x04" and Path(up.filename or "").suffix.lower() not in (".xlsx", ".docx")
    if not is_zip and n > config.MAX_UPLOAD_BYTES:
        tmp.unlink(missing_ok=True)
        raise HTTPException(413, f"File exceeds {config.MAX_UPLOAD_MB} MB limit (zips up to {config.MAX_ZIP_MB} MB)")
    return tmp, n, h.hexdigest()

def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(config.CHUNK):
            h.update(chunk)
    return h.hexdigest()

def process_one(path: Path, name: str, parent_id=None, size=None, sha=None, force_ai=False, on_file=None) -> dict:
    """Extract + validate + store one file. Never raises: failures become 'skipped'/'failed' records."""
    sha = sha or _sha256(path)
    hit = None if force_ai or os.getenv("DISABLE_CACHE") == "1" else db.cached_result(sha)
    if hit and hit['method']=='classify' and config.llm_provider() is not None:
        hit=None
    if hit:   # identical bytes already extracted: serve stored result, no AI job
        fid = db.add_file(name, kind=hit["kind"], size=size or path.stat().st_size, sha=sha, parent_id=parent_id, status="cached",
                          reason=f"identical to file {hit['file_id']}")
        documents.attach_source(fid,path,sha)
        db.update_file(fid, method="cache:" + (hit["method"] or ""), doc_type=hit["doc_type"], meta=hit["meta"])
        db.log(fid, f"cache hit (sha256 {sha[:12]}): served result of file {hit['file_id']}")
        cat = {"pdf": "pdf_text", "unknown": "other", "xls": "other", "eml": "other", "text": "other"}.get(hit["kind"], hit["kind"] or "other")
        out = {"file_id": fid, "name": name, "status": "done" if hit["order_id"] else "classified", "cached": True,
               "cached_from": hit["file_id"], "order_id": hit["order_id"], "method": "cache:" + (hit["method"] or ""),
               "doc_type": hit["doc_type"], "category": cat, "review_status": hit["review_status"], "warnings": []}
        if hit["meta"]:
            out["meta"] = json.loads(hit["meta"])
        return out
    fid = db.add_file(name, size=size or path.stat().st_size, sha=sha, parent_id=parent_id)
    documents.attach_source(fid,path,sha)
    db.recover_file_budget(fid,sha,name)
    if on_file: on_file(fid)
    llm.CURRENT.file_id = fid
    _prog_set(name, file_id=fid)
    lg = lambda m, lvl="info": db.log(fid, m, lvl)
    ctx = {"category": _cat_from_name(name), "force_ai": force_ai, "name": name,
           "buyer_hint": name.split("/")[1] if name.count("/") >= 2 else (name.split("/")[0] if "/" in name else None)}
    try:
        kind = extract.sniff(path)
        ctx["category"] = {"pdf": "pdf_text", "unknown": "other", "xls": "other", "eml": "other", "text": "other"}.get(kind, kind)
        db.update_file(fid, kind=kind)
        data, method = extract.extract_file(path, kind, lg, ctx)
        order, warns, errs = extract.validate(data)
        if errs:
            for e in errs: lg(e, "error")
            db.update_file(fid, status="failed", reason="validation: " + "; ".join(errs)[:500], method=method)
            return {"file_id": fid, "name": name, "status": "failed", "reason": "; ".join(errs), "category": ctx["category"]}
        for e in ctx.get("guard_errors") or []:
            warns.append("AI guard: " + e)
        for w in warns: lg(w, "warn")
        oid = db.save_order(order, warns, file_id=fid, raw=data)
        cons = ctx.get("consensus")
        if cons is None:   # deterministic parser alone is never trusted as the sole source
            cons = {"status": "needs_review", "disagreements": [], "arithmetic_errors": [],
                    "reason": "single source: deterministic parser only (no AI reader configured)", "models": None}
        db.set_review(oid, cons["status"], cons)
        lg(f"verification status: {cons['status']}", "info" if cons["status"] in ("accepted","checked") else "warn")
        db.update_file(fid, status="done", method=method, doc_type=ctx.get("doc_type"),
                       meta=json.dumps({"doc_type": ctx.get("doc_type"), "buyer_hint": ctx.get("buyer_hint"), **(ctx.get("refs") or {}),
                                        "po_numbers": [p.po_number for p in order.pos], "styles": [order.style_number]}))
        lg(f"stored order {oid} ({method})")
        return {"file_id": fid, "name": name, "status": "done", "order_id": oid, "method": method, "warnings": warns,
                "category": ctx["category"], "used_ai": not method.startswith(("pdf_text", "csv:", "xlsx:", "docx:")),
                "guard_errors": ctx.get("guard_errors") or [], "attempts": ctx.get("attempts"),
                "review_status": cons["status"], "doc_type": ctx.get("doc_type")}
    except extract.NotOrder as e:
        meta = {"doc_type": str(e), "buyer_hint": ctx.get("buyer_hint"), **(ctx.get("refs") or {})}
        if ctx.get('document'):
            meta.update(document=ctx['document'],original_document=ctx['document'],document_review='needs_review')
        method=f'{kind}_document_ai' if ctx.get('document') else 'classify'
        db.update_file(fid, status="classified", method=method, doc_type=str(e), meta=json.dumps(meta))
        lg(f"stored {e} document data" if ctx.get('document') else f"identified {e}; structured data unavailable")
        return {"file_id": fid, "name": name, "status": "classified", "doc_type": str(e), "category": ctx["category"], "meta": meta,"method":method}
    except llm.AIUnavailable as e:
        qdir = config.DATA_DIR / "queue"; qdir.mkdir(exist_ok=True)
        shutil.copyfile(path, qdir / f"{fid}{Path(name).suffix[:10]}")
        lg(f"queued for AI retry: {e}", "warn"); db.update_file(fid, status="queued_ai", reason=str(e))
        return {"file_id": fid, "name": name, "status": "queued_ai", "reason": str(e), "category": ctx["category"]}
    except db.ProcessingLimit as e:
        reason = str(e); lg(reason,"warn"); db.update_file(fid,status="skipped",reason=reason)
        return {"file_id":fid,"name":name,"status":"skipped","reason":reason,"category":ctx["category"]}
    except extract.Skip as s:
        lg(str(s), "warn"); db.update_file(fid, status="skipped", reason=str(s))
        return {"file_id": fid, "name": name, "status": "skipped", "reason": str(s), "need_ai": s.need_ai, "category": ctx["category"]}
    except Exception as e:
        logging.exception("extract failed")
        msg = f"{e.__class__.__name__}: {e}"[:500]
        lg(msg, "error"); db.update_file(fid, status="failed", reason=msg)
        return {"file_id": fid, "name": name, "status": "failed", "reason": msg, "category": ctx["category"]}

CATS = ["pdf_text", "pdf_scanned", "image", "xlsx", "csv", "docx", "zip", "other"]
EXT_CAT = {".pdf": "pdf_text", ".png": "image", ".jpg": "image", ".jpeg": "image", ".webp": "image", ".tif": "image", ".tiff": "image",
           ".bmp": "image", ".xlsx": "xlsx", ".csv": "csv", ".docx": "docx", ".zip": "zip"}

def _cat_from_name(n):
    return EXT_CAT.get(Path(n).suffix.lower(), "other")

def summarize(items, upload_name, zipped):
    by = {c: 0 for c in CATS}
    from .classify import TYPES
    dt = {t: 0 for t in TYPES}
    for i in items:
        if i.get("doc_type"): dt[i["doc_type"]] += 1
    zips = set()
    for i in items:
        parts = i["name"].split("/")
        for k, seg in enumerate(parts):
            if seg.lower().endswith(".zip"):
                zips.add("/".join(parts[:k + 1]))
        c = i.get("category") or _cat_from_name(i["name"])
        if c != "zip":
            by[c] += 1
    if zipped:
        by["zip"] = 1 + len(zips)
    return {"upload": upload_name, "files_received": len(items), "by_format": by, "by_doc_type": dt,
            "classified_only": sum(i["status"] == "classified" for i in items),
            "cached": sum(bool(i.get("cached")) for i in items),
            "extracted_ok": sum(i["status"] == "done" for i in items),
            "skipped": sum(i["status"] == "skipped" for i in items),
            "accepted": sum(i.get("review_status") == "accepted" for i in items),
            "checked": sum(i.get("review_status") == "checked" for i in items),
            "needs_review": sum(i.get("review_status") == "needs_review" for i in items),
            "failed": sum(i["status"] == "failed" for i in items),
            "queued_ai": sum(i["status"] == "queued_ai" for i in items),
            "processing": sum(i["status"] == "processing" for i in items),
            "need_ai": sum(bool(i.get("need_ai")) for i in items),
            "used_ai": sum(bool(i.get("used_ai")) for i in items),
            "skipped_reasons": [{"name": i["name"], "reason": i.get("reason")} for i in items if i["status"] != "done"]}

EXTRACT_LOCK = threading.Lock()   # one extraction at a time (request, background batch, retry loop)

def process_upload(path: Path, name: str, size: int, sha: str, force_ai=False, owner=None) -> dict:
    llm.CURRENT.owner = owner
    with EXTRACT_LOCK:
        res = _process_upload(path, name, size, sha, force_ai)
    res["summary"] = summarize(res["items"], name, "zip_id" in res)
    log_id = res.get("zip_id") or res["items"][0]["file_id"]
    sm = res["summary"]
    db.log(log_id, "UPLOAD SUMMARY " + json.dumps(sm), "summary")
    return res

# ---- live progress (in memory; one extraction at a time) ----
PROG: dict = {}          # bid -> {"name", "files": [{name, step, reason, order_id, t0, t1, order_doc}], "started"}
PROG_LAST = [None]
_tl = threading.local()

def _prog_files(bid, names):
    from . import classify
    P = PROG.setdefault(bid, {"files": [], "started": __import__("time").time()})
    for n in names:
        P["files"].append({"name": n, "step": "queued", "reason": None, "order_id": None, "t0": None, "t1": None,
                           "order_doc": classify.classify(n, "")[0] in classify.ORDER_TYPES or classify.classify(n, "")[0] == "other"})

def _prog_set(name, **kw):
    bid = getattr(_tl, "bid", None)
    if not bid or bid not in PROG: return
    import time as _t
    for f in PROG[bid]["files"]:
        if f["name"] == name and f["step"] not in ("saved", "failed", "skipped", "classified"):
            if kw.get("step") == "reading": kw.setdefault("t0", _t.time())
            if kw.get("step") in ("saved", "failed", "skipped", "classified"): kw.setdefault("t1", _t.time())
            f.update(kw); return

def progress(bid):
    import time as _t
    P = PROG.get(bid)
    persisted = db.get_batch(bid)
    if not P:
        batch = persisted
        if not batch: return None
        if not batch.get('result') or 'items' not in batch['result']:
            if batch['status'] not in ('failed', 'cancelled'): return None
            return {'batch_id':bid,'name':batch['name'],'batch_status':batch['status'],
                    'active':False,'phase':batch['status'],'files':[],'total':0,'finished':0,
                    'error':(batch.get('result') or {}).get('error')}
        files = []
        for item in batch["result"]["items"]:
            stage = {"done":"saved","classified":"classified","skipped":"skipped","queued_ai":"queued"}.get(item["status"],"failed")
            files.append({"name":item["name"],"file_id":item.get("file_id"),"step":stage,"reason":item.get("reason"),"order_id":item.get("order_id"),"t0":None,"t1":None if stage == "queued" else 1,"order_doc":item["status"] != "classified"})
        P = {"name":batch["name"],"started":_t.time(),"files":files}
    fs = P["files"]; fin = [f for f in fs if f["t1"]]
    timed = [f["t1"] - f["t0"] for f in fin if f["t0"] is not None and f["step"] in ("saved", "failed") and f["t1"] - f["t0"] > 5]
    avg = sum(timed[-8:]) / len(timed[-8:]) if timed else 150.0      # rolling avg seconds per order doc
    remaining_docs = sum(1 for f in fs if not f["t1"] and f["order_doc"])
    cur = next((f for f in fs if f["step"] == "reading"), None)
    eta = remaining_docs * avg - (min(_t.time() - cur["t0"], avg * 0.9) if cur and cur["t0"] else 0)
    docs = [f for f in fs if f["order_doc"]]
    jobs = db.file_job_progress([f["file_id"] for f in fs if f.get("file_id")])
    output_files = []
    for f in fs:
        reads = jobs.get(f.get("file_id"), {"total":0,"done":0,"failed":0,"jobs":[]})
        active = [j for j in reads["jobs"] if j["status"] == "leased"]
        if f["step"] == "saved": stage = "saved"
        elif f["step"] in ("failed","skipped","classified"): stage = f["step"]
        elif active: stage = active[-1].get("stage") or "received"
        elif any(j['status']=='queued' for j in reads['jobs']): stage = 'queued'
        elif reads["failed"]: stage = 'retry_pending' if f['step']=='queued' else 'read_failed'
        elif reads["total"] and reads["done"] == reads["total"]: stage = "validating"
        else: stage = "preparing" if f["step"] == "reading" else "pending"
        output_files.append({**{k:f[k] for k in ("name","step","reason","order_id")}, "worker_stage":stage,"reads":reads})
    return {"batch_id": bid, "name": P.get("name"), "total": len(fs), "finished": len(fin),
            "order_docs": len(docs), "order_docs_done": sum(1 for f in docs if f["t1"]),
            "avg_s_per_doc": round(avg), "eta_s": max(0, round(eta)) if remaining_docs else 0,
            "active": not (persisted and persisted['status'] in ('cancelled','failed')) and (len(fin) < len(fs) or bool(persisted and persisted["status"] == "processing")), "phase":"waiting" if not fs and persisted and persisted["status"] == "processing" else "processing", "batch_status":persisted["status"] if persisted else None, "error":(persisted.get('result') or {}).get('error') if persisted else None,
            "files": output_files, "reads_done":sum(x["reads"]["done"] for x in output_files), "reads_total":sum(x["reads"]["total"] for x in output_files),
            "reads_failed":sum(x['reads']['failed'] for x in output_files),
            "reads_queued":sum(j['status']=='queued' for x in output_files for j in x['reads']['jobs']),
            "files_pending":sum(x['worker_stage']=='pending' for x in output_files),
            "files_retrying":sum(x['worker_stage']=='retry_pending' for x in output_files)}

def _process_upload(path: Path, name: str, size: int, sha: str, force_ai=False) -> dict:
    bid = getattr(_tl, "bid", None)
    if bid:
        try:
            if zipfile.is_zipfile(path):
                with zipfile.ZipFile(path) as z:
                    _prog_files(bid, [i.filename for i in z.infolist() if not i.is_dir() and not zipio.junk_reason(i.filename)])
            else:
                _prog_files(bid, [name])
        except Exception:
            _prog_files(bid, [name])
    try:
        top = extract.sniff(path)
    except extract.Skip:
        top = "zip"   # corrupt zip container: iter_zip records it as skipped
    if top != "zip":
        _prog_set(name, step="reading")
        it = process_one(path, name, size=size, sha=sha, force_ai=force_ai); _prog_done(it)
        return {"items": [it]}
    zid = db.add_file(name, kind="zip", size=size, sha=sha)
    documents.attach_source(zid,path,sha)
    documents.index_archive(path,zid)
    items = []
    for ename, tmp, reason in zipio.iter_zip(path):
        if ename == path.name:
            ename = name
        if reason:
            fid = db.add_file(ename, status="skipped", parent_id=zid, reason=reason)
            db.log(fid, reason, "warn")
            _prog_set(ename, step="skipped", reason=reason)
            items.append({"file_id": fid, "name": ename, "status": "skipped", "reason": reason,
                          "category": "zip" if reason.startswith(("nested zip", "corrupt zip")) else
                          ("other" if zipio.junk_reason(ename) else _cat_from_name(ename))})
            continue
        try:
            _prog_set(ename, step="reading")
            it = process_one(tmp, ename, parent_id=zid, force_ai=force_ai); _prog_done(it)
            items.append(it)
        finally:
            tmp.unlink(missing_ok=True)
    done = sum(i["status"] == "done" for i in items)
    db.update_file(zid, status="done", method="zip", reason=f"{done}/{len(items)} entries extracted")
    return {"zip_id": zid, "items": items}

def _prog_done(it):
    st = it.get("status")
    step = {"done": "saved", "cached": "saved", "classified": "classified", "skipped": "skipped", "queued_ai": "queued"}.get(st, "failed")
    _prog_set(it.get("name"), step=step, reason=it.get("reason"), order_id=it.get("order_id"))

def _bg_batch(bid, tmp, name, size, sha, force_ai, owner=None):
    _tl.bid = bid; PROG_LAST[0] = bid; PROG.setdefault(bid, {"files": [], "started": __import__("time").time()})["name"] = name
    try:
        res = process_upload(tmp, name, size, sha, force_ai, owner)
        db.save_batch(bid, "done", name, res)
    except Exception as e:
        logging.exception("batch failed"); db.save_batch(bid, "failed", name, {"error": f"{e.__class__.__name__}: {e}"})
    finally:
        tmp.unlink(missing_ok=True)
        import time as _t
        for f in PROG.get(bid, {}).get("files", []):
            if not f["t1"] and f["step"] != "queued": f.update(step="failed", reason=f["reason"] or "processing interrupted", t1=_t.time())

def _recover_batches():
    for b in db.interrupted_batches():
        p = Path(b["input_path"]) if b.get("input_path") else None
        if p is None or not p.is_file():
            db.save_batch(b["id"], "failed", b["name"], {"error": "Retained upload missing; please upload again"})
            continue
        threading.Thread(target=_bg_batch, args=(b["id"], p, b["name"], b["input_size"], b["input_sha"], bool(b["force_ai"]), b["owner"]), daemon=True).start()

async def _run_upload(file: UploadFile, force_ai: bool, wait: bool = True, owner=None):
    name = _safe_name(file.filename)
    tmp, size, sha = await _stream_to_disk(file)
    bid = _sec.token_hex(8)
    retained = config.DATA_DIR / "uploads"; retained.mkdir(exist_ok=True)
    target = retained / (bid + tmp.suffix)
    tmp.replace(target); tmp = target
    db.save_batch(bid, "processing", name, input_path=tmp, input_size=size, input_sha=sha, force_ai=force_ai, owner=owner)
    threading.Thread(target=_bg_batch, args=(bid, tmp, name, size, sha, force_ai, owner), daemon=True).start()
    if not wait:
        return {"batch_id": bid, "status": "processing"}
    import time
    deadline = time.monotonic() + config.EXTRACT_TIMEOUT_S
    while time.monotonic() < deadline:
        batch = db.get_batch(bid)
        if batch["status"] == "done": return batch["result"]
        if batch["status"] == "failed": raise HTTPException(500, {"batch_id":bid, "error":batch["result"].get("error")})
        await asyncio.sleep(.1)
    raise HTTPException(504, {"batch_id":bid, "status":"processing", "detail":"Extraction still processing; upload retained. Poll the batch for its outcome."})

@app.post("/api/upload")
async def upload(request: Request, file: UploadFile = File(...), wait: int = 1):
    return await _run_upload(file, False, bool(wait), getattr(request.state, "user", None))

@app.get("/api/progress")
def api_progress(bid: str = ""):
    return progress(bid or PROG_LAST[0] or "") or {"active": False}

@app.get("/api/batches/{bid}")
def api_batch(bid: str, view: str = "upload"):
    b = db.get_batch(bid)
    if not b: raise HTTPException(404)
    if b["status"] == "done" and view == "api":
        b["result"] = {"summary": b["result"]["summary"], "results": [api_view(i) for i in b["result"]["items"]]}
    return b

@app.get("/api/batches/{bid}/events")
async def batch_events(request: Request, bid: str):
    if not db.get_batch(bid): raise HTTPException(404)
    async def stream():
        last = None
        while not await request.is_disconnected():
            batch = db.get_batch(bid)
            snapshot = progress(bid) or {"batch_id":bid,"active":batch["status"] == "processing","files":[]}
            snapshot["batch_status"] = batch["status"]
            serialized = json.dumps(snapshot, separators=(",", ":"))
            if serialized != last:
                yield "event: progress\ndata: " + serialized + "\n\n"
                last = serialized
            else: yield ": heartbeat\n\n"
            if batch["status"] != "processing" and not snapshot["active"]:
                yield "event: complete\ndata: " + serialized + "\n\n"; return
            await asyncio.sleep(1)
    return StreamingResponse(stream(),media_type="text/event-stream",headers={"Cache-Control":"no-cache", "X-Accel-Buffering":"no"})

# ---- AI worker pull API (box polls Render over HTTPS) ----
import base64 as _b64

@app.get("/api/worker/pending")
def worker_pending(owner: str = "", worker: str = "tick"):
    """Cheap check for the box cron tick. Only counts jobs uploaded by the configured app account."""
    acct = os.getenv("APP_USERNAME", "")
    if owner != acct:
        return {"pending": 0, "owner": owner, "note": "only jobs of the app account are served"}
    db.heartbeat(worker, "polling")
    return {"pending": db.pending_jobs(acct)}

@app.get("/api/worker/next")
def worker_next(worker: str = "box", owner: str = "", image_batches:int=0):
    db.heartbeat(worker)
    acct = os.getenv("APP_USERNAME", "")
    if owner != acct:
        return Response(status_code=204)
    j = db.claim_job(worker, acct,image_batches=bool(image_batches))
    if not j:
        return Response(status_code=204)
    if j.get("image") is not None:
        images=llm.unpack_images(j['image'])
        if images is not None:
            j['images_b64']=[_b64.b64encode(p).decode() for p in images];j.pop('image')
        else:j["image_b64"] = _b64.b64encode(j.pop("image")).decode()
    else:
        j.pop("image", None)
    return j

@app.post("/api/worker/result")
async def worker_result(req: Request):
    b = await req.json()
    if not isinstance(b, dict) or type(b.get("id")) is not int or not isinstance(b.get("lease_id"), str):
        raise HTTPException(400, "id and lease_id required")
    if b.get("error") is None and not isinstance(b.get("text"), str):
        raise HTTPException(400, "text or error required")
    if b.get("error") is not None and not isinstance(b["error"], str):
        raise HTTPException(400, "error must be text")
    st = db.finish_job(b["id"], b["lease_id"], result=b.get("text"), error=b.get("error"))
    return {"ack": st}

@app.post("/api/worker/progress")
async def worker_progress(req: Request):
    body = await req.json()
    if not isinstance(body,dict) or type(body.get("id")) is not int or not isinstance(body.get("lease_id"),str) or not isinstance(body.get("stage"),str):
        raise HTTPException(400,"id, lease_id and stage required")
    return {"ok":db.update_job_progress(body["id"],body["lease_id"],body["stage"])}

@app.post("/api/worker/renew")
async def worker_renew(req: Request):
    b = await req.json()
    return {"ok": db.renew_job(int(b.get("id", 0)), str(b.get("lease_id", "")))}

@app.post("/api/worker/heartbeat")
async def worker_hb(req: Request):
    b = await req.json(); db.heartbeat(str(b.get("worker", "box"))[:60], str(b.get("info", ""))[:200]); return {"ok": True}

@app.get("/api/worker/status")
def worker_status_api():
    return db.worker_status()

def ai_info():
    return {"model_a": os.getenv("OPENCODE_MODEL", "opencode/big-pickle"), "model_b": os.getenv("OPENCODE_MODEL_B", "opencode/mimo-v2.6-flash-free"),
            "model_image": os.getenv("OPENCODE_VISION_MODEL", "opencode/mimo-v2.6-flash-free"),
            "queued_files": len(db.queued_files()), "cached": db.cached_count()}

@app.get("/api/processing-limit")
def processing_limit():
    return db.processing_budget_status()

@app.get("/api/ai-status")
def ai_status():
    return {"processing_limit":db.processing_budget_status(),"provider": config.llm_provider(), **db.worker_status(), **ai_info()}

def _retry_loop():
    """Re-run files that were queued because the AI worker was offline, once it comes back."""
    import time
    while True:
        time.sleep(int(os.getenv("AI_RETRY_S", "60")))
        try:
            if config.llm_provider() != "queue" or not db.worker_online():
                continue
            for f in db.queued_files():
                qp = next((config.DATA_DIR / "queue").glob(f"{f['id']}.*"), None) or (config.DATA_DIR / "queue" / str(f["id"]))
                if not qp.exists():
                    reason = "queued copy lost (instance restarted)"
                    db.update_file(f["id"], status="failed", reason=reason)
                    _record_retry(f["id"], {"file_id":f["id"],"name":f["name"],"status":"failed","reason":reason,"category":_cat_from_name(f["name"])})
                    continue
                with EXTRACT_LOCK:
                    r = process_one(qp, f["name"], on_file=lambda new: _bind_retry(f["id"], new))
                _record_retry(f["id"], r)
                db.update_file(f["id"], status="retried", reason=f"re-processed as file {r['file_id']}: {r['status']}")
                qp.unlink(missing_ok=True)
        except Exception:
            logging.exception("retry loop")

def _bind_retry(fid, new_fid):
    db.copy_file_budget(fid,new_fid)
    for b in db.batches_with_results():
        payload = json.loads(b["result"])
        matching = [i for i in payload.get("items",[]) if i.get("file_id") == fid or i.get("retry_from") == fid]
        if not matching: continue
        for item in matching: item.update(file_id=new_fid, retry_from=fid, status="processing", reason=None)
        payload["summary"] = summarize(payload["items"], b["name"], "zip_id" in payload)
        db.save_batch(b["id"], "done", b["name"], payload)
        for f in PROG.get(b["id"],{}).get("files",[]):
            if f.get("file_id") == fid: f.update(file_id=new_fid,step="reading",t0=__import__("time").time(),t1=None,reason=None)

def _record_retry(fid, result):
    for b in db.batches_with_results():
        payload = json.loads(b["result"])
        if not any((i.get("file_id") == fid or i.get("retry_from") == fid) for i in payload.get("items", [])): continue
        payload["items"] = [result if i.get("file_id") == fid or i.get("retry_from") == fid else i for i in payload["items"]]
        payload["summary"] = summarize(payload["items"], b["name"], "zip_id" in payload)
        db.save_batch(b["id"], "done", b["name"], payload)
        if b["id"] in PROG:
            for f in PROG[b["id"]]["files"]:
                if f["name"] == result["name"] and f["step"] in ("queued","reading"):
                    f.update(file_id=result["file_id"],step={"done":"saved", "classified":"classified", "skipped":"skipped", "queued_ai":"queued"}.get(result["status"], "failed"), reason=result.get("reason"), order_id=result.get("order_id"), t1=None if result["status"] == "queued_ai" else __import__("time").time())

def api_view(item: dict) -> dict:
    """API-style structured response for one processed file."""
    out = {"status": item["status"], "file": item["name"], "format": item.get("category"), "doc_type": item.get("doc_type"),
           "meta": item.get("meta"), "method": item.get("method"), "cached": bool(item.get("cached")), "cached_from": item.get("cached_from"),
           "reason": item.get("reason"), "attempts": item.get("attempts")}
    if item.get("order_id"):
        o = db.get_order(item["order_id"])
        if not o:
            out.update(status="order_deleted", reason="The previously extracted order was deleted")
            return out
        out["order"] = {k: o[k] for k in ("id", "buyer_name", "brand", "style_number", "factory", "planned_line", "fob", "smv", "cm", "department", "sizes", "colors", "currency")}
        out["pos"] = [{k: p[k] for k in ("po_number", "po_date", "ship_date", "destination", "sales_contract_number", "stated_total_qty", "stated_total_value", "currency")} for p in o["pos"]]
        out["lines"] = [{"po_number": p["po_number"], **{k: l[k] for k in ("style", "color", "color_code", "sizes", "order_qty", "rate", "net_value")}} for p in o["pos"] for l in p["lines"]]
        tq = sum(sum(l["sizes"].values()) if l["sizes"] else (l["order_qty"] or 0) for l in out["lines"]); tv = round(sum(l["net_value"] or 0 for l in out["lines"]), 2)
        out["totals"] = {"qty": tq, "value": tv, "stated_qty": sum(p["stated_total_qty"] or 0 for p in o["pos"]),
                         "stated_value": round(sum(p["stated_total_value"] or 0 for p in o["pos"]), 2), "currency": o["currency"]}
        rv = o.get("review") or {}
        out["validation"] = {"errors": item.get("guard_errors") or [], "warnings": [w for w in o["warnings"] if not w.startswith("AI guard")]}
        out["agreement"] = {"status": rv.get("status"), "models": rv.get("models"), "deterministic_vote": rv.get("deterministic_vote", False),
                            "disagreements": rv.get("disagreements", []), "arithmetic_errors": rv.get("arithmetic_errors", []),
                            "reason": rv.get("reason")}
        if rv.get("status") != "accepted":
            out["versions"] = {"a": rv.get("version_a"), "b": rv.get("version_b")}
    else:
        out["validation"] = {"errors": [item.get("reason")] if item["status"] == "failed" else [], "warnings": [item.get("reason")] if item["status"] == "skipped" else []}
    return out

@app.post("/api/extract")
async def api_extract(request: Request, file: UploadFile = File(...), reader: str = "auto", wait: int = 1):
    """reader=auto|ai. wait=0 -> {batch_id}; poll GET /api/batches/{id}?view=api."""
    res = await _run_upload(file, reader == "ai", bool(wait), getattr(request.state, "user", None))
    if not wait:
        return res
    return {"summary": res["summary"], "results": [api_view(i) for i in res["items"]]}

@app.get("/api/orders")
def api_orders(q: str = ""):
    return db.list_orders(q or None)

def _parse_order(body):
    try:
        return Order.model_validate(body), None
    except ValidationError as e:
        return None, JSONResponse({"errors": [f"{'.'.join(map(str, x['loc']))}: {x['msg']}" for x in e.errors()]}, 422)

@app.post("/api/orders", status_code=201)
async def api_create(req: Request):
    order, err = _parse_order(await req.json())
    if err: return err
    w = warnings_for(order)
    oid = db.save_order(order, w)
    return {"ok": True, "id": oid, "warnings": w}

@app.delete("/api/orders/{oid}")
def api_delete(oid: int):
    if not db.delete_order(oid): raise HTTPException(404)
    return {"ok": True}

@app.get("/api/orders/{oid}")
def api_order(oid: int):
    o = db.get_order(oid)
    if not o: raise HTTPException(404)
    o["extraction_status"] = o.get("review_status") or ("unverified" if o.get("method") else None)
    return o

@app.put("/api/orders/{oid}")
async def api_save(oid: int, req: Request):
    if not db.get_order(oid): raise HTTPException(404)
    order, err = _parse_order(await req.json())
    if err: return err
    w = warnings_for(order)
    db.save_order(order, w, order_id=oid)
    return {"ok": True, "id": oid, "warnings": w}

@app.get("/api/links")
def api_links():
    """Group stored documents by buyer, and link docs that share a PO number or style reference."""
    files = [f for f in db.list_files(2000) if f.get("meta")]
    groups = {}
    for f in files:
        m = json.loads(f["meta"]); b = m.get("buyer_hint") or "unknown"
        g = groups.setdefault(b, {"buyer": b, "documents": [], "po_numbers": set(), "styles": set()})
        g["documents"].append({"file_id": f["id"], "name": f["name"], "doc_type": f.get("doc_type"), "order_id": f.get("order_id"),
                               "po_refs": m.get("po_numbers") or m.get("po_refs"), "style_refs": m.get("styles") or m.get("style_refs")})
        g["po_numbers"].update(m.get("po_numbers") or m.get("po_refs") or []); g["styles"].update([s for s in (m.get("styles") or m.get("style_refs") or []) if s])
    return [dict(g, po_numbers=sorted(g["po_numbers"]), styles=sorted(g["styles"])) for g in groups.values()]

@app.get("/api/files")
def api_files():
    return [{k:v for k,v in f.items() if k != "source_path"} for f in documents.library_files()]

@app.get("/api/files/{fid}")
def api_file(fid:int):
    file=db.get_file(fid)
    if not file:raise HTTPException(404,"File not found")
    return {k:v for k,v in documents.describe_file(file).items() if k != "source_path"}


@app.get('/api/files/{fid}/data')
def file_data(fid:int):
    file=db.get_file(fid)
    if not file:raise HTTPException(404,'File not found')
    return documents.stored_data(file)


@app.put('/api/files/{fid}/data')
async def save_file_data(fid:int,req:Request):
    from .document_data import DocumentData
    file=db.get_file(fid)
    if not file:raise HTTPException(404,'File not found')
    if file.get('order_id'):raise HTTPException(409,'Edit the linked order using its order form')
    if file['status'] in ('processing','queued_ai'):raise HTTPException(409,'Wait for extraction to finish before editing')
    try:document=DocumentData.model_validate(await req.json()).model_dump(mode='json')
    except (ValidationError,ValueError) as e:raise HTTPException(422,str(e))
    meta=json.loads(file.get('meta') or '{}')
    if meta.get('document') and 'original_document' not in meta:meta['original_document']=meta['document']
    meta.update(document=document,document_review='edited')
    db.update_file(fid,meta=json.dumps(meta),doc_type=document['document_type'],status='edited',method='document_edited',reason=None)
    db.log(fid,'Document data edited and saved; AI verification cleared')
    return {'ok':True,'file_id':fid}


@app.get("/api/files/{fid}/source")
def file_source(fid:int,download:int=0):
    file=db.get_file(fid)
    if not file:raise HTTPException(404,"File not found")
    path=documents.source_for_file(file)
    if path is None:raise HTTPException(404,"Original document unavailable; upload it again")
    import mimetypes
    mime=mimetypes.guess_type(file["name"])[0] or "application/octet-stream"
    safe_inline={"application/pdf","image/png","image/jpeg","image/webp","image/gif"}
    if not download and mime not in safe_inline:mime="text/plain"
    return FileResponse(path,filename=Path(file["name"]).name,media_type=mime,content_disposition_type="attachment" if download else "inline",headers={"X-Content-Type-Options":"nosniff"})


@app.get("/files/{fid}",response_class=HTMLResponse)
def file_page(request:Request,fid:int):
    file=db.get_file(fid)
    if not file:raise HTTPException(404,"File not found")
    info=documents.describe_file(file);path=documents.source_for_file(file);preview={"kind":"missing"}
    if path:
        try:preview=documents.preview(file,path)
        except Exception as e:preview={"kind":"error","message":f"Preview unavailable: {type(e).__name__}. Download the original to inspect it."}
    data=documents.stored_data(file);order=data['order']
    if order:
        for po in order['pos']:
            po['display_sizes']=list(dict.fromkeys(size for line in po['lines'] for size in line['sizes']))
    return tpl.TemplateResponse(request,"file.html",{"file":info,"preview":preview,"order":order,"data":data,"document":data['metadata'].get('document'),"logs":db.file_logs(fid),"active":"book"})

@app.get("/api/files/{fid}/logs")
def api_logs(fid: int):
    return db.file_logs(fid)

@app.get("/", response_class=HTMLResponse)
def index(request: Request, q: str = ""):
    return tpl.TemplateResponse(request, "index.html", {"orders": db.list_orders(q or None), "q": q, "files": documents.library_files(), "ws": db.worker_status(), "ai": ai_info(),
                                "llm": config.llm_provider(), "max_mb": config.MAX_UPLOAD_MB, "active": "book", "file_limit":db.processing_budget_status()})

EMPTY = {"id": None, "buyer_name": None, "brand": None, "style_number": None, "factory": None, "planned_line": 1, "fob": None, "smv": None,
         "cm": None, "department": None, "sizes": [], "colors": [], "currency": None, "warnings": [], "pos": [], "file_name": None, "method": None, "review": None}

@app.get("/orders/new", response_class=HTMLResponse)
def order_new(request: Request):
    return tpl.TemplateResponse(request, "order.html", {"o": EMPTY, "hl": {}, "active": "book", "options": db.order_options()})

def _highlights(o):
    """Fields where the second AI read differed: {field_key: other_value}, shown as a subtle hint in the form."""
    out = {}
    for d in ((o.get("review") or {}).get("disagreements") or []):
        f = d.get("field") or ""
        m = re.match(r"PO\[(.+?)\]\.line\[(.+?)\|.*?\]\.size\[(.+?)\]$", f)
        if m: out[f"q|{m[1]}|{m[2]}|{m[3]}"] = d.get("b"); continue
        m = re.match(r"PO\[(.+?)\]\.(\w+)$", f)
        if m: out[f"po|{m[1]}|{m[2]}"] = d.get("b"); continue
        if re.match(r"^\w+$", f): out[f"h|{f}"] = d.get("b")
    return out

@app.get("/orders/{oid}", response_class=HTMLResponse)
def order_page(request: Request, oid: int):
    o = db.get_order(oid)
    if not o: raise HTTPException(404)
    hl = _highlights(o)
    o["extraction_status"] = o.get("review_status") or ("unverified" if o.get("method") else None)
    o = {k: v for k, v in o.items() if k not in ("review", "review_status")}   # page only needs the form data + highlights
    return tpl.TemplateResponse(request, "order.html", {"o": o, "hl": hl, "active": "book", "options": db.order_options()})
