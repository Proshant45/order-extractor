"""Dual independent AI extraction + field-by-field agreement. Never silently accept a disagreement.

Vote A and vote B are independent reads (different free models where possible, different prompt framing / page order).
The deterministic parser, when it recognises a layout, is a third vote used only as a cross-check."""
from __future__ import annotations
import os, re
from concurrent.futures import ThreadPoolExecutor
from . import guard, llm

VARIANT_B = ("\nSECOND INDEPENDENT READ: work bottom-up. First locate every quantity table / list in the document "
             "(they may be in odd places, inside sentences, or split across pages), then the PO numbers and dates, then the header parties.")

def _s(v):
    return re.sub(r"\s+", " ", str(v or "")).strip().upper().replace("’", "'")

def _n(v):
    if v in (None, ""):
        return None
    try:
        return round(float(str(v).replace(",", "")), 2)
    except ValueError:
        return _s(v)

def _date(v):
    from .schema import parse_date
    try:
        d = parse_date(v); return d.isoformat() if d else None
    except ValueError:
        return _s(v)

HEADER = ("buyer_name", "brand", "style_number", "factory", "department", "currency")
PO_STR = ("destination", "sales_contract_number")

def flatten(d: dict) -> dict[str, object]:
    """Normalised field map: keys are stable paths, values comparable."""
    f = {k: _s(d.get(k)) or None for k in HEADER}
    for p in d.get("pos") or []:
        pn = _s(p.get("po_number"))
        b = f"PO[{pn}]"
        f[f"{b}.exists"] = True
        f[f"{b}.po_date"] = _date(p.get("po_date")); f[f"{b}.ship_date"] = _date(p.get("ship_date"))
        for k in PO_STR:
            f[f"{b}.{k}"] = _s(p.get(k)) or None
        f[f"{b}.stated_total_qty"] = _n(p.get("stated_total_qty")); f[f"{b}.stated_total_value"] = _n(p.get("stated_total_value"))
        for l in p.get("lines") or []:
            c = _s(l.get("color")); lb = f"{b}.line[{c}|{_n(l.get('rate'))}]"
            f[f"{lb}.style"] = _s(l.get("style")) or None
            for k in ("order_qty", "rate", "net_value"):
                f[f"{lb}.{k}"] = _n(l.get(k))
            from .schema import norm_size
            for sz, q in (l.get("sizes") or {}).items():
                f[f"{lb}.size[{norm_size(sz)}]"] = _n(q)
    return f

def compare(a: dict, b: dict, det: dict | None = None) -> list[dict]:
    fa, fb = flatten(a), flatten(b)
    fd = flatten(det) if det else {}
    out = []
    for k in sorted(set(fa) | set(fb)):
        va, vb = fa.get(k), fb.get(k)
        if va != vb:
            out.append({"field": k, "a": va, "b": vb, "deterministic": fd.get(k) if det else None})
    if det:   # third vote: AI-agreed values contradicting a recognised layout parse are also disagreements
        for k, vd in fd.items():
            if k.endswith(".exists") or vd in (None, ""):
                continue
            if fa.get(k) == fb.get(k) and fa.get(k) != vd and not any(x["field"] == k for x in out):
                out.append({"field": k, "a": fa.get(k), "b": fb.get(k), "deterministic": vd})
    return out

def models(image: bool) -> tuple[str | None, str | None]:
    if os.getenv("EXTRACTOR", "").lower() != "opencode" and os.getenv("AI_QUEUE") != "1":
        return None, None   # gemini/openai: same model, different prompt framing
    if image:   # only mimo reads images among the free Zen models (tested); B = same model, different framing
        v = os.getenv("OPENCODE_VISION_MODEL", "opencode/mimo-v2.6-flash-free")
        return v, os.getenv("OPENCODE_VISION_MODEL_B", v)
    return os.getenv("OPENCODE_MODEL", "opencode/big-pickle"), os.getenv("OPENCODE_MODEL_B", "opencode/nemotron-3-ultra-free")

def dual_read(*, text: str | None = None, image_png: bytes | None = None, det: dict | None, log) -> dict:
    ma, mb = models(image_png is not None)
    def vote(model, variant, tag):
        cands = [model] + [m for m in os.getenv("OPENCODE_FALLBACK_MODELS", "opencode/step-5-preview-free,opencode/big-pickle").split(",")
                           if m and m != model] if (os.getenv("EXTRACTOR", "").lower() == "opencode" or os.getenv("AI_QUEUE") == "1") and image_png is None else [model]
        last = None
        for m in cands:
            read = lambda extra, m=m: llm.read_page(text=text, image_png=image_png, extra=variant + extra, model=m)
            try:
                d, errs, n = guard.ai_extract_with_guard(read, text, lambda msg, lvl="info": log(f"[{tag}:{m}] {msg}", lvl), max_repairs=1, det=det)
                return {"data": d, "guard_errors": errs, "attempts": n, "model": m or "default"}
            except llm.AIUnavailable:
                raise                    # worker down: queue the whole file, don't burn fallbacks
            except RuntimeError as e:   # model unavailable / timed out every attempt -> next free model
                last = e
        return {"data": None, "guard_errors": [f"vote {tag} failed on all models: {last}"], "attempts": 0, "model": None}
    par = int(os.getenv("AI_PARALLEL", "2"))
    with ThreadPoolExecutor(max_workers=par) as ex:
        fa = ex.submit(vote, ma, "", "A"); fb = ex.submit(vote, mb, VARIANT_B, "B")
        A, B = fa.result(), fb.result()
    if A["data"] is None and B["data"] is None:
        raise RuntimeError("both independent AI reads failed: " + "; ".join(A["guard_errors"] + B["guard_errors"]))
    if A["data"] is None:   # keep the surviving read but never auto-accept a single vote
        A, B = B, A
    dis = compare(A["data"], B["data"], det) if B["data"] is not None else [{"field": "*", "a": "read ok", "b": "read failed", "deterministic": None}]
    arith = guard.arithmetic_errors(A["data"])
    agreed = not dis and not arith and not A["guard_errors"] and not B["guard_errors"]
    log(f"agreement: {'ACCEPTED' if agreed else 'NEEDS_REVIEW'} disagreements={len(dis)} arithmetic={len(arith)}", "info" if agreed else "warn")
    return {"status": "accepted" if agreed else "needs_review", "disagreements": dis, "arithmetic_errors": arith,
            "votes": {"a": A, "b": B, "deterministic": det}, "chosen": "a"}
