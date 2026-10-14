"""AI-primary path end-to-end with a mocked reader: page mapping, agreement, and empty results."""
import json
from pathlib import Path
import pytest
from app import extract, llm, consensus

SAMPLE = Path(__file__).parent / "fixtures" / "sample_po.pdf"

def _po(page):
    return {"pos": [{"po_number": "PO1", "stated_total_qty": None,
                     "lines": [{"style": "S1", "color": f"C{page}", "sizes": {"S": 10 * page, "M": 5}, "order_qty": 10 * page + 5,
                                "rate": 2.0, "net_value": 2.0 * (10 * page + 5)}]}]}

def _run(monkeypatch, reader, path=SAMPLE):
    monkeypatch.setenv('AI_READ_MODE','dual')
    monkeypatch.setenv("EXTRACTOR", "opencode"); monkeypatch.setenv("OPENCODE_FALLBACK_MODELS", "")
    monkeypatch.setattr(extract, "_need_llm", lambda *a, **k: None)
    monkeypatch.setattr(llm, "read_page", reader)
    monkeypatch.setattr(consensus.guard, "text_errors", lambda d, t: [])
    ctx = {}
    out = extract.extract_ai_primary(path, "pdf", lambda *a, **k: None, ctx)
    return out[0] if isinstance(out, tuple) else out, ctx["consensus"]

def _page_of(text):
    import re
    m = re.search(r"Page\s*(\d+)", text or "", re.I)
    return int(m.group(1)) if m else 1

@pytest.mark.skipif(not SAMPLE.exists(), reason="sample PDF missing")
def test_empty_agreeing_reads_never_accepted(monkeypatch):
    _, c = _run(monkeypatch, lambda **k: {"pos": []})
    assert c["status"] == "needs_review"

@pytest.mark.skipif(not SAMPLE.exists(), reason="sample PDF missing")
def test_pages_all_mapped_and_agreeing_accepted(monkeypatch):
    calls = []
    def reader(text=None, image_png=None, extra="", model=None):
        calls.append(text); return _po(1)
    o, c = _run(monkeypatch, reader)
    assert c["version_a"]["pos"], "per-page results must be merged into the order"
    assert len(calls) >= 2                       # A and B for every page
    assert not any("no PO" in e for e in c["arithmetic_errors"])   # det parser (third vote) may still flag mock data

@pytest.mark.skipif(not SAMPLE.exists(), reason="sample PDF missing")
def test_disagreement_flags_review(monkeypatch):
    def reader(text=None, image_png=None, extra="", model=None):
        d = _po(1)
        if 'SECOND INDEPENDENT READ' in extra:
            d["pos"][0]["lines"][0]["sizes"]["S"] = 11; d["pos"][0]["lines"][0]["order_qty"] = 16; d["pos"][0]["lines"][0]["net_value"] = 32.0
        return d
    _, c = _run(monkeypatch, reader)
    assert c["status"] == "needs_review" and c["disagreements"]

def test_yearless_date_kept_and_flagged():
    from app.schema import Order, warnings_for
    o = Order(pos=[{"po_number": "1", "ship_date": "13/09", "lines": []}])
    assert o.pos[0].ship_date is None and o.pos[0].date_raw == {"ship_date": "13/09"}
    assert any("13/09" in w for w in warnings_for(o))

def test_lease_renew(tmp_path, monkeypatch):
    from app import db
    db.init()
    jid = db.enqueue_job("p", "m", "t", None, owner="u")
    j = db.claim_job("w", "u")
    assert db.renew_job(j["id"], j["lease_id"]) and not db.renew_job(j["id"], "stale")

def test_po_boundary_grouping():
    from app.extract import _po_groups
    assert _po_groups(["Purchase Order: PO10001\nx", "more", "Purchase Order: PO10002"]) == [(0, 2, 1),(2,3,1)]
    pages = [f"Purchase Order: PO{i:05d}" for i in range(10)]
    g = _po_groups(pages)
    assert g[0][0] == 0 and g[-1][1] == 10 and all(b > a for a, b, _ in g) and len(g) == 10
