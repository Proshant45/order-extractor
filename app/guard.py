"""AI-extraction guard: arithmetic cross-checks + comparison against deterministic parse / PDF text, with repair loop."""
from __future__ import annotations
import json, re
from . import llm, parsers

def _norm(s):
    return re.sub(r"[^A-Z0-9]", "", str(s or "").upper())

def arithmetic_errors(d: dict) -> list[str]:
    e = []
    for p in d.get("pos") or []:
        tq, tv = 0, 0.0
        for l in p.get("lines") or []:
            try:
                s = sum(int(float(str(v).replace(",", ""))) for v in (l.get("sizes") or {}).values())
            except ValueError:
                e.append(f"{l.get('color')}: non-numeric size qty"); continue
            tq += s
            oq = l.get("order_qty")
            if oq is not None and int(float(oq)) != s:
                e.append(f"PO {p.get('po_number')} {l.get('style')}/{l.get('color')}: sizes sum {s} != order_qty {oq}")
            if l.get("rate") is not None and l.get("net_value") is not None:
                if abs(float(l["rate"]) * s - float(l["net_value"])) > 0.011:
                    e.append(f"PO {p.get('po_number')} {l.get('color')}: rate {l['rate']} x {s} != net_value {l['net_value']}")
                tv += float(l["net_value"])
        if p.get("stated_total_qty") is not None and int(float(p["stated_total_qty"])) != tq:
            st = int(float(p["stated_total_qty"]))
            hint = " (looks like per-pack breakup: multiply each size by the number of packs)" if tq and tq < st and st % tq == 0 or (tq and tq * 10 < st) else ""
            e.append(f"PO {p.get('po_number')}: lines sum {tq} != stated_total_qty {p['stated_total_qty']}{hint}")
        if p.get("stated_total_value") is not None and tv and abs(tv - float(p["stated_total_value"])) > 0.011:
            e.append(f"PO {p.get('po_number')}: lines value {tv:.2f} != stated_total_value {p['stated_total_value']}")
    return e

def text_errors(d: dict, text: str | None) -> list[str]:
    """Every number / code the AI reports must literally exist in the PDF text (catches hallucination)."""
    if not text:
        return []
    T = text.replace(",", "")
    nt = _norm(text)
    e = []
    for p in d.get("pos") or []:
        if p.get("po_number") and _norm(p["po_number"]) not in nt:
            e.append(f"po_number {p['po_number']} not in document text")
        for l in p.get("lines") or []:
            if l.get("color") and _norm(l["color"]) not in nt:
                e.append(f"color {l['color']!r} not in document text")
            for sz, q in (l.get("sizes") or {}).items():
                if not re.search(rf"(?<![\d.]){int(float(q))}(\.0+)?(?![\d])", T):
                    e.append(f"{l.get('color')} size {sz} qty {q} not in document text")
    return e

def compare_to_deterministic(ai: dict, det: dict) -> list[str]:
    e = []
    al = [l for p in ai.get("pos") or [] for l in p.get("lines") or []]
    dl = [l for p in det.get("pos") or [] for l in p.get("lines") or []]
    if len(al) != len(dl):
        e.append(f"AI found {len(al)} lines, deterministic parser found {len(dl)}")
    key = lambda l: (_norm(l.get("color")), round(float(l.get("rate") or 0), 2))
    dm = {key(l): l for l in dl}
    for l in al:
        g = dm.get(key(l)) or next((x for x in dl if _norm(x.get("color")) == _norm(l.get("color"))), None)
        if not g:
            e.append(f"line color {l.get('color')!r} not found by deterministic parser"); continue
        if _norm(l.get("style")) != _norm(g.get("style")):
            if g.get("style"):
                e.append(f"{l.get('color')}: style should be {g['style']!r} (base style code without colour/size suffix), got {l.get('style')!r}")
        from .schema import norm_size
        a = {norm_size(k): int(float(v)) for k, v in (l.get("sizes") or {}).items()}
        if a != {norm_size(k): int(v) for k, v in g["sizes"].items()}:
            e.append(f"{l.get('style')}/{l.get('color')} sizes {a} != {dict(g['sizes'])}")
        for f in ("rate", "net_value", "order_qty"):
            if l.get(f) is not None and g.get(f) is not None and abs(float(l[f]) - float(g[f])) > 0.001:
                e.append(f"{l.get('color')} {f} {l[f]} != {g[f]}")
    return e

def ai_extract_with_guard(read, text: str | None, log, max_repairs: int = 2, det=None) -> tuple[dict, list[str], int]:
    """read(extra_instructions) -> dict. Returns (result, residual_errors, attempts)."""
    if det is None and text and parsers.looks_like_sap_po(text):
        det = parsers.parse_sap_po(text)
    extra, best, best_err = "", None, None
    for attempt in range(1, max_repairs + 2):
        try:
            d = read(extra)
        except llm.AIUnavailable:
            raise
        except Exception as ex:
            log(f"AI attempt {attempt} failed: {ex}", "warn"); errs = [f"AI call failed: {ex}"]; d = None
        else:
            errs = arithmetic_errors(d) + text_errors(d, text) + (compare_to_deterministic(d, det) if det else [])
        log(f"AI attempt {attempt}: {len(errs)} guard error(s)" + (": " + "; ".join(errs[:8]) if errs else ""), "warn" if errs else "info")
        if d is not None and (best_err is None or len(errs) < len(best_err)):
            best, best_err = d, errs
        if d is not None and not errs:
            return d, [], attempt
        extra = ("\nYour previous answer failed these checks. Re-read the document carefully and fix them. "
                 "Do not invent numbers; every quantity must appear in the document:\n- " + "\n- ".join(errs[:25]))
    if best is None:
        raise RuntimeError("AI reader failed on every attempt")
    return best, best_err, max_repairs + 1
