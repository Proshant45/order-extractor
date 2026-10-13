from __future__ import annotations
import asyncio, hashlib, logging, os, re, tempfile
from pathlib import Path
from fastapi import FastAPI, HTTPException, Request, UploadFile, File
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool
from pydantic import ValidationError
from . import config, db, extract, zipio
from .schema import Order, warnings_for

logging.basicConfig(level=logging.INFO)
BASE = Path(__file__).parent
app = FastAPI(title="Order Extractor")
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
tpl = Jinja2Templates(directory=BASE / "templates")
SEM = asyncio.Semaphore(1)   # one extraction at a time keeps RAM flat on a small instance

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

def process_one(path: Path, name: str, parent_id=None, size=None, sha=None) -> dict:
    """Extract + validate + store one file. Never raises: failures become 'skipped'/'failed' records."""
    fid = db.add_file(name, size=size or path.stat().st_size, sha=sha, parent_id=parent_id)
    lg = lambda m, lvl="info": db.log(fid, m, lvl)
    try:
        kind = extract.sniff(path)
        db.update_file(fid, kind=kind)
        data, method = extract.extract_file(path, kind, lg)
        order, warns, errs = extract.validate(data)
        if errs:
            for e in errs: lg(e, "error")
            db.update_file(fid, status="failed", reason="validation: " + "; ".join(errs)[:500], method=method)
            return {"file_id": fid, "name": name, "status": "failed", "reason": "; ".join(errs)}
        for w in warns: lg(w, "warn")
        oid = db.save_order(order, warns, file_id=fid, raw=data)
        db.update_file(fid, status="done", method=method)
        lg(f"stored order {oid} ({method})")
        return {"file_id": fid, "name": name, "status": "done", "order_id": oid, "method": method, "warnings": warns}
    except extract.Skip as s:
        lg(str(s), "warn"); db.update_file(fid, status="skipped", reason=str(s))
        return {"file_id": fid, "name": name, "status": "skipped", "reason": str(s)}
    except Exception as e:
        logging.exception("extract failed")
        msg = f"{e.__class__.__name__}: {e}"[:500]
        lg(msg, "error"); db.update_file(fid, status="failed", reason=msg)
        return {"file_id": fid, "name": name, "status": "failed", "reason": msg}

def process_upload(path: Path, name: str, size: int, sha: str) -> dict:
    try:
        top = extract.sniff(path)
    except extract.Skip:
        top = "zip"   # corrupt zip container: iter_zip records it as skipped
    if top != "zip":
        return {"items": [process_one(path, name, size=size, sha=sha)]}
    zid = db.add_file(name, kind="zip", size=size, sha=sha)
    items = []
    for ename, tmp, reason in zipio.iter_zip(path):
        if ename == path.name:
            ename = name
        if reason:
            fid = db.add_file(ename, status="skipped", parent_id=zid, reason=reason)
            db.log(fid, reason, "warn")
            items.append({"file_id": fid, "name": ename, "status": "skipped", "reason": reason})
            continue
        try:
            items.append(process_one(tmp, ename, parent_id=zid))
        finally:
            tmp.unlink(missing_ok=True)
    done = sum(i["status"] == "done" for i in items)
    db.update_file(zid, status="done", method="zip", reason=f"{done}/{len(items)} entries extracted")
    return {"zip_id": zid, "items": items}

@app.post("/api/upload")
async def upload(file: UploadFile = File(...)):
    name = _safe_name(file.filename)
    tmp, size, sha = await _stream_to_disk(file)
    try:
        async with SEM:
            try:
                res = await asyncio.wait_for(run_in_threadpool(process_upload, tmp, name, size, sha), config.EXTRACT_TIMEOUT_S)
            except asyncio.TimeoutError:
                raise HTTPException(504, f"Extraction exceeded {config.EXTRACT_TIMEOUT_S}s")
    finally:
        tmp.unlink(missing_ok=True)
    return res

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
