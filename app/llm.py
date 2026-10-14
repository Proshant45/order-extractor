"""AI reader: page text or image -> order JSON. On Render it goes through the job queue to the box worker (opencode)."""
from __future__ import annotations
import base64, json, os, re
import httpx
from . import config

PROMPT = """You read garment buyer order / purchase order documents. Extract ONLY what is visible in the supplied document or section.
Return strict JSON (no prose) with this shape; use null for anything not present, never guess:
{"buyer_name":str|null,"brand":str|null,"style_number":str|null,"factory":str|null,"fob":number|null,
 "smv":number|null,"cm":number|null,"department":str|null,"currency":str|null,
 "pos":[{"po_number":str,"po_date":"DD.MM.YYYY"|null,"destination":str|null,"ship_date":"DD.MM.YYYY"|null,
   "sales_contract_number":str|null,"stated_total_qty":number|null,"stated_total_value":number|null,
   "lines":[{"style":str|null,"color":str,"color_code":str|null,"sizes":{"<size label>":int},
             "order_qty":int|null,"rate":number|null,"net_value":number|null}]}]}
For style_number, join all distinct base styles found in the line items with comma-space if there is no single header style. For department, copy the garment category from the product description if present (for example MEN S SHIRTS). One line per (style,color). "style" is the base style/article code as printed in the style or material column (e.g. "WEC015315"), without appended colour or size codes; put a separate numeric colour code in "color_code". "color" is the colour NAME as printed (e.g. from a material description table), not a code. Dates as printed. Copy every quantity exactly. Sizes keyed by their size code (e.g. "S","M","L","XL","38","15.5","2T"); write words like "small"/"medium"/"large" as S/M/L. Quantities are integers.
PACKS: if quantities are given as ratio/assortment/pre-packs (e.g. "Qty of Ratio Packs 130" with a size "Break up Qty" or "Pack Breakup" of 2,2,1...), the size quantity is pieces = number of packs x pieces of that size per pack (130 x 2 = 260). Never report the per-pack breakup as the size quantity. Sizes must add up to the stated total pieces. If several packs share a colour, add their pieces per size.
PACKS ACROSS DELIVERIES (e.g. Primark): when pack definitions (Pack Id with a size ratio such as XS 2 / S 4 / M 9, or a solid-size pack of 15) are listed once and each delivery/destination lists "Pack Id ... Packs Units", the pieces per size = sum over every delivery of (packs of that Pack Id x that size's ratio units). Add all deliveries together. The result must equal "Total Units".
ITEMISED LINES WIN: if the document lists one row per SKU (part number / item with its colour, size and quantity) AND also a colour x size summary grid, take the quantities from the itemised rows. Summary grids lose column alignment in extracted text and shift values into the wrong size. Use the grid only to check the totals.
SHIP DATE: if no separate ship/handover date is printed, use the line "Due Date" / delivery date as ship_date."""

def _json(text: str) -> dict:
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError("LLM returned no JSON")
    return json.loads(m.group(0))

def read_page(*, image_png: bytes | None = None, text: str | None = None, extra: str = "", model: str | None = None) -> dict:
    prov = config.llm_provider()
    if prov is None:
        raise RuntimeError("AI reader not enabled on this server (AI_QUEUE/WORKER_TOKEN not set)")
    if text is not None:
        if len(text) > 60000:
            raise ValueError(f"document text too large for one read ({len(text)} chars); split required, no text discarded")
    if prov == "queue":
        return _json(queue_read(PROMPT + extra, text=text, image_png=image_png,
                                 model=model or (os.getenv("OPENCODE_VISION_MODEL", "opencode/mimo-v2.6-flash-free") if image_png
                                                 else os.getenv("OPENCODE_MODEL", "opencode/big-pickle"))))
    if prov == "opencode":
        import tempfile
        from pathlib import Path
        from . import opencode_provider
        with tempfile.TemporaryDirectory(dir=config.TMP_DIR) as d:
            f = Path(d) / ("page.png" if image_png else "page.txt")
            f.write_bytes(image_png) if image_png else f.write_text(text)
            model = model or (os.getenv("OPENCODE_VISION_MODEL", "opencode/mimo-v2.6-flash-free") if image_png else os.getenv("OPENCODE_MODEL", "opencode/big-pickle"))
            return _json(opencode_provider.run(PROMPT + extra + f"\nThe document is in the attached file {f.name}. Output JSON only.", [f], model=model))
    with httpx.Client(timeout=config.LLM_TIMEOUT_S) as c:
        if prov == "gemini":
            model = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")  # dual read uses prompt variants
            parts = [{"text": PROMPT + extra}]
            if image_png:
                parts.append({"inline_data": {"mime_type": "image/png", "data": base64.b64encode(image_png).decode()}})
            if text:
                parts.append({"text": "PAGE TEXT:\n" + text})
            r = c.post(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                       headers={"x-goog-api-key": os.environ["GEMINI_API_KEY"]},
                       json={"contents": [{"parts": parts}],
                             "generationConfig": {"temperature": 0, "responseMimeType": "application/json"}})
            r.raise_for_status()
            return _json(r.json()["candidates"][0]["content"]["parts"][0]["text"])
        model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
        content = [{"type": "text", "text": PROMPT + extra}]
        if image_png:
            content.append({"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(image_png).decode()}})
        if text:
            content.append({"type": "text", "text": "PAGE TEXT:\n" + text})
        r = c.post("https://api.openai.com/v1/chat/completions",
                   headers={"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}"},
                   json={"model": model, "temperature": 0, "response_format": {"type": "json_object"},
                         "messages": [{"role": "user", "content": content}]})
        r.raise_for_status()
        return _json(r.json()["choices"][0]["message"]["content"])

import threading as _th
CURRENT = _th.local()   # .owner = logged-in account that uploaded the file (jobs are tagged with it)

class AIUnavailable(RuntimeError):
    """AI worker offline / job not picked up in time: file is marked queued_ai and retried later, never failed silently."""

def queue_read(prompt, *, text=None, image_png=None, model=None) -> str:
    """Pull model: enqueue a job in SQLite; the box worker claims it over HTTPS, runs opencode, posts the result."""
    import time
    from . import db
    jid = db.enqueue_job(prompt, model, text, image_png, owner=getattr(CURRENT, "owner", None) or os.getenv("APP_USERNAME"),
                         file_id=getattr(CURRENT,"file_id",None), page=getattr(CURRENT,"page",None), vote=getattr(CURRENT,"vote",None))
    wait = int(os.getenv("AI_JOB_WAIT_S", "900"))
    t0 = time.time()
    while time.time() - t0 < wait:
        j = db.get_job(jid)
        if j["status"] == "done":
            return j["result"]
        if j["status"] == "error":
            raise RuntimeError(f"AI worker error: {j['error']}")
        if j["status"] == "queued" and time.time() - t0 > int(os.getenv("AI_PICKUP_S", "90")) and not db.worker_online():
            db.cancel_job(jid)
            raise AIUnavailable("AI worker offline (no heartbeat); job not picked up")
        time.sleep(1)
    db.cancel_job(jid)
    raise AIUnavailable(f"AI job {jid} not finished within {wait}s")

def merge_pages(pages: list[dict]) -> dict:
    """Merge per-page partial results: first non-null header wins, POs merged by number, lines appended."""
    out = {"pos": [], "_merge_conflicts": []}
    byno = {}
    for p in pages:
        for k, v in p.items():
            if k == "pos" or k.startswith("_"): continue
            if v not in (None, ""):
                if out.get(k) not in (None, "", v): out["_merge_conflicts"].append(f"header {k}: {out[k]!r} vs {v!r}")
                if not out.get(k): out[k] = v
        for po in p.get("pos") or []:
            no = str(po.get("po_number") or "UNKNOWN")
            if no not in byno:
                byno[no] = {**po, "po_number": no, "lines": []}
                out["pos"].append(byno[no])
            tgt = byno[no]
            for k, v in po.items():
                if k != "lines" and v not in (None, ""):
                    if tgt.get(k) not in (None, "", v): out["_merge_conflicts"].append(f"PO {no} {k}: {tgt[k]!r} vs {v!r}")
                    if not tgt.get(k): tgt[k] = v
            tgt["lines"].extend(po.get("lines") or [])
    return out
