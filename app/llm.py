"""Vision/text LLM reader. Keys come only from env (GEMINI_API_KEY preferred, OPENAI_API_KEY fallback)."""
from __future__ import annotations
import base64, json, os, re
import httpx
from . import config

PROMPT = """You read garment buyer order / purchase order documents. Extract ONLY what is visible on this page.
Return strict JSON (no prose) with this shape; use null for anything not present, never guess:
{"buyer_name":str|null,"brand":str|null,"style_number":str|null,"factory":str|null,"fob":number|null,
 "smv":number|null,"cm":number|null,"department":str|null,"currency":str|null,
 "pos":[{"po_number":str,"po_date":"DD.MM.YYYY"|null,"destination":str|null,"ship_date":"DD.MM.YYYY"|null,
   "sales_contract_number":str|null,"stated_total_qty":number|null,"stated_total_value":number|null,
   "lines":[{"style":str|null,"color":str,"color_code":str|null,"sizes":{"<size label>":int},
             "order_qty":int|null,"rate":number|null,"net_value":number|null}]}]}
One line per (style,color). "style" is the base style/article code as printed in the style or material column (e.g. "WEC015315"), without appended colour or size codes; put a separate numeric colour code in "color_code". "color" is the colour NAME as printed (e.g. from a material description table), not a code. Dates as printed. Copy every quantity exactly. Sizes keyed exactly as printed (e.g. "S","M","38","40"). Quantities are integers."""

def _json(text: str) -> dict:
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError("LLM returned no JSON")
    return json.loads(m.group(0))

def read_page(*, image_png: bytes | None = None, text: str | None = None, extra: str = "", model: str | None = None) -> dict:
    prov = config.llm_provider()
    if prov is None:
        raise RuntimeError("No LLM key configured (set GEMINI_API_KEY or OPENAI_API_KEY)")
    if text is not None:
        text = text[:60000]
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

def merge_pages(pages: list[dict]) -> dict:
    """Merge per-page partial results: first non-null header wins, POs merged by number, lines appended."""
    out = {"pos": []}
    byno = {}
    for p in pages:
        for k, v in p.items():
            if k != "pos" and v not in (None, "") and not out.get(k):
                out[k] = v
        for po in p.get("pos") or []:
            no = str(po.get("po_number") or "UNKNOWN")
            if no not in byno:
                byno[no] = {**po, "po_number": no, "lines": []}
                out["pos"].append(byno[no])
            tgt = byno[no]
            for k, v in po.items():
                if k != "lines" and v not in (None, "") and not tgt.get(k):
                    tgt[k] = v
            tgt["lines"].extend(po.get("lines") or [])
    return out
