"""Document-type classification (filename + first pages of text) and reference extraction for linking."""
from __future__ import annotations
import re
from pathlib import Path

TYPES = ["purchase_order", "sales_contract", "proforma_invoice", "tech_pack", "bom", "control_sheet", "other"]
RULES = {
    "tech_pack": [r"\btech\s*-?\s*pack", r"\btack\s*pack", r"\bTP\b", r"\dTP\d", r"\bTP\s*-", r"measurement\s+spec", r"\bPOM\b", r"spec\s+sheet",
                  r"construction\s+details", r"graded\s+spec", r"tolerance", r"artwork"],
    "bom": [r"\bBOM\b", r"bill\s+of\s+materials?", r"trims?\s+list", r"consumption"],
    "proforma_invoice": [r"pro\s*-?\s*forma", r"\bPI\s*\d", r"\bPI\b"],
    "sales_contract": [r"sales\s*contract", r"\bseals\s*conta?c?t", r"\bsalse\s*contract", r"\bS/?C\s*(no|#)", r"\bSC\d", r"contract\s+no", r"\bL/?C\b.*advis", r"export\s+contract"],
    "control_sheet": [r"control\s*sheet", r"standard\s+pack", r"packing\s+list", r"assortment", r"ratio\s+pack"],
    "purchase_order": [r"purchase\s*order", r"\bP\.?\s*O\.?\s*(no|number|#)", r"order\s*sheet", r"order\s+details", r"\bPO\d", r"\bPO\b"],
}
NAME_ONLY = {"sales_contract": [r"(^|[\s\-])S\.?C(\s*\d+)?\.(pdf|jpe?g|png|xlsx?|docx?)$", r"\bSC\s*\d"],
             "purchase_order": [r"(^|[\s\-])P\.?O(\s*\d+)?\.(pdf|jpe?g|png|xlsx?|docx?)$"],
             "proforma_invoice": [r"(^|[\s\-])P\.?I(\s*\d+)?\.(pdf|jpe?g|png|xlsx?|docx?)$"]}
W_NAME, W_TEXT = 3, 1

def classify(name: str, text: str | None) -> tuple[str, dict]:
    nm = Path(name).name.replace("_", " ")
    head = (text or "")[:15000]
    scores = {}
    for t, pats in RULES.items():
        s = 0
        for p in pats:
            if re.search(p, nm, re.I):
                s += W_NAME
            s += min(len(re.findall(p, head, re.I)), 5) * W_TEXT
        s += sum(W_NAME for p in NAME_ONLY.get(t, []) if re.search(p, nm, re.I))
        scores[t] = s
    best = max(scores, key=scores.get)
    if scores[best] == 0:
        return "other", scores
    # quantities table + PO wording beats incidental 'TP' mentions
    if best == "tech_pack" and scores["purchase_order"] >= scores["tech_pack"] - 1 and re.search(r"purchase\s*order", head, re.I):
        best = "purchase_order"
    return best, scores

ORDER_TYPES = {"purchase_order", "sales_contract", "proforma_invoice", "control_sheet"}

def refs(text: str | None) -> dict:
    t = text or ""
    po = set(re.findall(r"(?:P\.?\s*O\.?\s*(?:No\.?|Number|#)\s*[:.]?\s*)([A-Z0-9][A-Z0-9\-/]{4,20})", t, re.I))
    style = set(re.findall(r"(?:Style\s*(?:No\.?|Number|#|Code)?\s*[:.]?\s*)([A-Z0-9][A-Z0-9\-/.]{3,24})", t, re.I))
    return {"po_refs": sorted(po)[:20], "style_refs": sorted(style)[:20]}
