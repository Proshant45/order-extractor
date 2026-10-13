from __future__ import annotations
import asyncio, hashlib, json, logging, os, re, tempfile
from pathlib import Path
from fastapi import FastAPI, HTTPException, Request, UploadFile, File
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool
from pydantic import ValidationError
from fastapi import Form
from fastapi.responses import RedirectResponse
from . import auth, config, db, extract, zipio
from .schema import Order, warnings_for

logging.basicConfig(level=logging.INFO)
BASE = Path(__file__).parent
app = FastAPI(title="Order Extractor")
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
tpl = Jinja2Templates(directory=BASE / "templates")
SEM = asyncio.Semaphore(1)   # one extraction at a time keeps RAM flat on a small instance

PUBLIC = {"/healthz", "/login", "/static/app.css"}

@app.middleware("http")
async def require_login(request: Request, call_next):
    path = request.url.path
    if path in PUBLIC:
        return await call_next(request)
    user = auth.read_session(request.cookies.get(auth.SESSION_COOKIE))
    bearer = request.headers.get("authorization", "")
    if not user and bearer.lower().startswith("bearer ") and auth.check_token(bearer[7:].strip()):
        user = "api"
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
        p.unlink(missing_ok=True)

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
                if n > config.MAX_UPLOAD_BYTES:
                    raise HTTPException(413, f"File exceeds {config.MAX_UPLOAD_MB} MB limit")
                h.update(chunk); f.write(chunk)
    except BaseException:
        tmp.unlink(missing_ok=True); raise
    return tmp, n, h.hexdigest()

def process_one(path: Path, name: str, parent_id=None, size=None, sha=None, force_ai=False) -> dict:
    """Extract + validate + store one file. Never raises: failures become 'skipped'/'failed' records."""
    fid = db.add_file(name, size=size or path.stat().st_size, sha=sha, parent_id=parent_id)
    lg = lambda m, lvl="info": db.log(fid, m, lvl)
    ctx = {"category": _cat_from_name(name), "force_ai": force_ai}
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
        lg(f"agreement status: {cons['status']}", "info" if cons["status"] == "accepted" else "warn")
        db.update_file(fid, status="done", method=method)
        lg(f"stored order {oid} ({method})")
        return {"file_id": fid, "name": name, "status": "done", "order_id": oid, "method": method, "warnings": warns,
                "category": ctx["category"], "used_ai": not method.startswith(("pdf_text", "csv:", "xlsx:", "docx:")),
                "guard_errors": ctx.get("guard_errors") or [], "attempts": ctx.get("attempts"),
                "review_status": cons["status"]}
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
    return {"upload": upload_name, "files_received": len(items), "by_format": by,
            "extracted_ok": sum(i["status"] == "done" for i in items),
            "skipped": sum(i["status"] == "skipped" for i in items),
            "accepted": sum(i.get("review_status") == "accepted" for i in items),
            "needs_review": sum(i.get("review_status") == "needs_review" for i in items),
            "failed": sum(i["status"] == "failed" for i in items),
            "need_ai": sum(bool(i.get("need_ai")) for i in items),
            "used_ai": sum(bool(i.get("used_ai")) for i in items),
            "skipped_reasons": [{"name": i["name"], "reason": i.get("reason")} for i in items if i["status"] != "done"]}

def process_upload(path: Path, name: str, size: int, sha: str, force_ai=False) -> dict:
    res = _process_upload(path, name, size, sha, force_ai)
    res["summary"] = summarize(res["items"], name, "zip_id" in res)
    log_id = res.get("zip_id") or res["items"][0]["file_id"]
    sm = res["summary"]
    db.log(log_id, "UPLOAD SUMMARY " + json.dumps(sm), "summary")
    return res

def _process_upload(path: Path, name: str, size: int, sha: str, force_ai=False) -> dict:
    try:
        top = extract.sniff(path)
    except extract.Skip:
        top = "zip"   # corrupt zip container: iter_zip records it as skipped
    if top != "zip":
        return {"items": [process_one(path, name, size=size, sha=sha, force_ai=force_ai)]}
    zid = db.add_file(name, kind="zip", size=size, sha=sha)
    items = []
    for ename, tmp, reason in zipio.iter_zip(path):
        if ename == path.name:
            ename = name
        if reason:
            fid = db.add_file(ename, status="skipped", parent_id=zid, reason=reason)
            db.log(fid, reason, "warn")
            items.append({"file_id": fid, "name": ename, "status": "skipped", "reason": reason,
                          "category": "zip" if reason.startswith(("nested zip", "corrupt zip")) else _cat_from_name(ename)})
            continue
        try:
            items.append(process_one(tmp, ename, parent_id=zid, force_ai=force_ai))
        finally:
            tmp.unlink(missing_ok=True)
    done = sum(i["status"] == "done" for i in items)
    db.update_file(zid, status="done", method="zip", reason=f"{done}/{len(items)} entries extracted")
    return {"zip_id": zid, "items": items}

async def _run_upload(file: UploadFile, force_ai: bool):
    name = _safe_name(file.filename)
    tmp, size, sha = await _stream_to_disk(file)
    try:
        async with SEM:
            try:
                res = await asyncio.wait_for(run_in_threadpool(process_upload, tmp, name, size, sha, force_ai), config.EXTRACT_TIMEOUT_S)
            except asyncio.TimeoutError:
                raise HTTPException(504, f"Extraction exceeded {config.EXTRACT_TIMEOUT_S}s")
    finally:
        tmp.unlink(missing_ok=True)
    return res

@app.post("/api/upload")
async def upload(file: UploadFile = File(...)):
    return await _run_upload(file, False)

def api_view(item: dict) -> dict:
    """API-style structured response for one processed file."""
    out = {"status": item["status"], "file": item["name"], "format": item.get("category"), "method": item.get("method"),
           "reason": item.get("reason"), "attempts": item.get("attempts")}
    if item.get("order_id"):
        o = db.get_order(item["order_id"])
        out["order"] = {k: o[k] for k in ("id", "buyer_name", "brand", "style_number", "factory", "planned_line", "fob", "smv", "cm", "department", "sizes", "colors", "currency")}
        out["pos"] = [{k: p[k] for k in ("po_number", "po_date", "ship_date", "destination", "sales_contract_number", "stated_total_qty", "stated_total_value", "currency")} for p in o["pos"]]
        out["lines"] = [{"po_number": p["po_number"], **{k: l[k] for k in ("style", "color", "color_code", "sizes", "order_qty", "rate", "net_value")}} for p in o["pos"] for l in p["lines"]]
        tq = sum(sum(l["sizes"].values()) for l in out["lines"]); tv = round(sum(l["net_value"] or 0 for l in out["lines"]), 2)
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
async def api_extract(file: UploadFile = File(...), reader: str = "auto"):
    """reader=auto (deterministic first, AI fallback) | ai (force AI reader, deterministic parse kept as guard)."""
    res = await _run_upload(file, reader == "ai")
    return {"summary": res["summary"], "results": [api_view(i) for i in res["items"]]}

@app.get("/api/orders")
def api_orders():
    return db.list_orders()

@app.get("/api/orders/{oid}")
def api_order(oid: int):
    o = db.get_order(oid)
    if not o: raise HTTPException(404)
    return o

@app.put("/api/orders/{oid}")
async def api_save(oid: int, req: Request):
    if not db.get_order(oid): raise HTTPException(404)
    try:
        order = Order.model_validate(await req.json())
    except ValidationError as e:
        return JSONResponse({"errors": [f"{'.'.join(map(str, x['loc']))}: {x['msg']}" for x in e.errors()]}, 422)
    w = warnings_for(order)
    db.save_order(order, w, order_id=oid)
    o = db.get_order(oid); rv = o.get("review") or {}
    rv["human_reviewed_by"] = getattr(req.state, "user", None); rv["status_before_review"] = rv.get("status")
    db.set_review(oid, "reviewed", rv)
    return {"ok": True, "warnings": w}

@app.get("/api/files")
def api_files():
    return db.list_files()

@app.get("/api/files/{fid}/logs")
def api_logs(fid: int):
    return db.file_logs(fid)

@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return tpl.TemplateResponse(request, "index.html", {"orders": db.list_orders(), "files": db.list_files(),
                                "llm": config.llm_provider(), "max_mb": config.MAX_UPLOAD_MB, "active": "book"})

@app.get("/orders/{oid}", response_class=HTMLResponse)
def order_page(request: Request, oid: int):
    o = db.get_order(oid)
    if not o: raise HTTPException(404)
    return tpl.TemplateResponse(request, "order.html", {"o": o, "llm": config.llm_provider(), "active": "book"})
