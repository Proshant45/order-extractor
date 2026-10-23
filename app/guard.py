"""AI-extraction guard: arithmetic cross-checks + comparison against deterministic parse / PDF text, with repair loop."""
from __future__ import annotations
import json, re
from . import llm, parsers
from .schema import Order, to_qty, to_num

def _norm(s):
    return re.sub(r"[^A-Z0-9]", "", str(s or "").upper())

def arithmetic_errors(d: dict, *, check_totals=True) -> list[str]:
    e = []
    for p in d.get("pos") or []:
        tq, tv = 0, 0.0
        for l in p.get("lines") or []:
            try:
                s = sum(to_qty(v) or 0 for v in (l.get("sizes") or {}).values()) if l.get("sizes") else (to_qty(l.get("order_qty")) or 0)
            except ValueError:
                e.append(f"{l.get('color')}: non-numeric size qty"); continue
            tq += s
            oq = l.get("order_qty")
            if oq is not None and int(float(oq)) != s:
                e.append(f"PO {p.get('po_number')} {l.get('style')}/{l.get('color')}: sizes sum {s} != order_qty {oq}")
            if l.get("rate") is not None and l.get("net_value") is not None:
                if abs(float(l["rate"]) * s - float(l["net_value"])) > 0.011:
                    e.append(f"PO {p.get('po_number')} {l.get('color')}: rate {l['rate']} x {s} != net_value {l['net_value']}")
            if l.get('net_value') is not None:
                tv += float(l['net_value'])
        if check_totals and p.get("stated_total_qty") is not None and int(float(p["stated_total_qty"])) != tq:
            st = int(float(p["stated_total_qty"]))
            hint = " (looks like per-pack breakup: multiply each size by the number of packs)" if tq and tq < st and st % tq == 0 or (tq and tq * 10 < st) else ""
            e.append(f"PO {p.get('po_number')}: lines sum {tq} != stated_total_qty {p['stated_total_qty']}{hint}")
        if check_totals and p.get("stated_total_value") is not None and any(l.get('net_value') is not None for l in p.get('lines') or []) and abs(tv - float(p["stated_total_value"])) > 0.011:
            e.append(f"PO {p.get('po_number')}: lines value {tv:.2f} != stated_total_value {p['stated_total_value']}")
    return e

def text_errors(d: dict, text: str | None) -> list[str]:
    """Every number / code the AI reports must literally exist in the PDF text (catches hallucination)."""
    if not text:
        return []
    T = text.replace(",", "")
    nt = _norm(text)
    e = []
    def number_present(value):
        try:
            target = float(str(value).replace(",", ""))
        except (ValueError, TypeError):
            return False
        return any(float(m.group()) == target for m in re.finditer(r"(?<![\d.])\d+(?:\.\d+)?(?![\d.])", T))
    for p in d.get("pos") or []:
        for key in ("stated_total_qty", "stated_total_value"):
            if p.get(key) is not None and not number_present(p[key]):
                e.append(f"PO {p.get('po_number')} {key} {p[key]} not in document text")
        if p.get("po_number") and not p.get("_po_inherited") and _norm(p["po_number"]) not in nt:
            e.append(f"po_number {p['po_number']} not in document text")
        for l in p.get("lines") or []:
            if not l.get("sizes") and l.get("order_qty") is not None and not number_present(l["order_qty"]):
                e.append(f"{l.get('color')} order_qty {l['order_qty']} not in document text")
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

PO_NUMBER = re.compile(r"\b(?:Purchase\s*Order|P\.?\s*O\.?)\s*(?:No\.?|Number|#)?\s*[:#]?\s*([A-Z]*\d[\w\-/]{3,})",re.I)

def coverage_errors(d, text):
    expected={_norm(m.group(1)) for m in PO_NUMBER.finditer(text or '')}
    found={_norm(p.get('po_number')) for p in d.get('pos') or []}
    return [f'Printed PO {no} was not extracted' for no in sorted(expected-found)]

def first_po_number(text):
    m = PO_NUMBER.search(text or "")
    return m.group(1) if m else None

def _not_a_number(v):
    if v in (None, "") or isinstance(v, (int, float)) and not isinstance(v, bool):
        return False
    try:
        to_num(v); return False
    except (ValueError, TypeError):
        return True

def soften(d, po_hint=None) -> list[str]:
    """Recoverable model slips become warnings instead of rejecting the whole read:
    text in a numeric price field -> null + warning; a PO without a number (continuation chunk) inherits po_hint."""
    notes = []
    if not isinstance(d, dict):
        return notes
    for k in ("fob", "smv", "cm"):
        if _not_a_number(d.get(k)):
            notes.append(f"warning: {k} was text ({str(d[k])[:60]!r}); set to empty, fill manually"); d[k] = None
    for p in d.get("pos") or []:
        if not isinstance(p, dict):
            continue
        if po_hint and _norm(p.get("po_number")) == _norm(po_hint):
            p["_po_inherited"] = True   # continuation chunk repeating the PO number from its context: not a hallucination
        elif not str(p.get("po_number") or "").strip():
            if po_hint:
                p["po_number"] = str(po_hint); p["_po_inherited"] = True
                notes.append(f"warning: PO number not printed in this chunk; continued from PO {po_hint}")
        if _not_a_number(p.get("stated_total_value")):
            notes.append(f"warning: PO {p.get('po_number')} stated_total_value was text ({str(p['stated_total_value'])[:60]!r}); set to empty")
            p["stated_total_value"] = None
        for l in p.get("lines") or []:
            if not isinstance(l, dict):
                continue
            for k in ("rate", "net_value"):
                if _not_a_number(l.get(k)):
                    notes.append(f"warning: PO {p.get('po_number')} {l.get('style') or ''}/{l.get('color') or ''} {k} was text ({str(l[k])[:60]!r}); set to empty")
                    l[k] = None
    return notes

def ai_extract_with_guard(read, text: str | None, log, max_repairs: int = 2, det=None, partial=False, check_coverage=False, po_hint=None) -> tuple[dict, list[str], int]:
    """read(extra_instructions) -> dict. Returns (result, residual_errors + soft warnings, attempts)."""
    if det is None and text and parsers.looks_like_sap_po(text):
        det = parsers.parse_sap_po(text)
    extra, best, best_err, best_notes = "", None, None, []
    for attempt in range(1, max_repairs + 2):
        try:
            d = read(extra)
        except llm.AIUnavailable:
            raise
        except Exception as ex:
            log(f"AI attempt {attempt} failed: {ex}", "warn"); errs = [f"AI call failed: {ex}"]; d = None; notes = []
        else:
            try:
                notes = soften(d, po_hint)
                for n in notes: log(n, "warn")
                Order.model_validate(d)
                errs = arithmetic_errors(d,check_totals=not partial) + text_errors(d, text) + (compare_to_deterministic(d, det) if det else [])
                if check_coverage:
                    errs += coverage_errors(d,text)
                    if not d.get('pos') or any(not p.get('lines') for p in d.get('pos') or []):
                        errs.append('No usable PO lines extracted')
            except (ValueError, TypeError, AttributeError) as ex:
                errs = [f"invalid structured output: {ex}"]; d = None; notes = []
        log(f"AI attempt {attempt}: {len(errs)} guard error(s)" + (": " + "; ".join(errs[:8]) if errs else ""), "warn" if errs else "info")
        if d is not None and (best_err is None or len(errs) < len(best_err)):
            best, best_err, best_notes = d, errs, notes
        if d is not None and not errs:
            return d, notes, attempt
        extra = ("\nYour previous answer failed these checks. Re-read the document carefully and fix them. "
                 "Do not invent numbers; every quantity must appear in the document:\n- " + "\n- ".join(errs[:25]))
    if best is None:
        raise RuntimeError("AI reader failed on every attempt: " + "; ".join(errs[:3]))
    return best, best_err + best_notes, max_repairs + 1
