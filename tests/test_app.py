import io, json, os, subprocess, zipfile
from pathlib import Path
import pytest
from fastapi.testclient import TestClient
from app import config, main

FX = Path(__file__).parent / "fixtures"
PDF = Path(os.getenv("SAMPLE_PDF", FX / "sample_po.pdf"))
pytestmark = pytest.mark.skipif(not PDF.exists(), reason="sample PDF fixture missing")

# Ground truth typed by hand from the PDF (Aditya Birla PO 3110121365), cross-checked against `pdftotext -layout`.
EXPECTED_HEADER = {"buyer_name": "ADITYA BIRLA LIFESTYLE BRANDS LIMITED", "factory": "Good Earth Apparels Ltd.",
                   "style_number": "WEC015315, WEC015317, WEC015326", "department": "MEN'S SHIRTS", "currency": "USD"}
EXPECTED_PO = {"po_number": "3110121365", "po_date": "2026-03-26", "ship_date": "2026-07-20",
               "destination": "Bilaspur, 122413, IN", "stated_total_qty": 8350, "stated_total_value": 70013.5}
SIZES = ["35", "38", "39", "40", "42", "44"]
EXPECTED_LINES = [  # style, color_code, color, rate, net_value, order_qty, sizes
    ("WEC015315", "5331", "EYE SHADOW",    7.85, 6358.5,  810,  [32, 130, 243, 218, 130, 57]),
    ("WEC015317", "7459", "AE FLORAL",     8.50, 6885.0,  810,  [32, 130, 243, 218, 130, 57]),
    ("WEC015317", "7200", "Berry red",     9.10, 21203.0, 2330, [93, 373, 699, 629, 373, 163]),
    ("WEC015317", "7008", "CHARCOAL01",    9.10, 20657.0, 2270, [91, 363, 681, 613, 363, 159]),
    ("WEC015326", "5401", "AE Light Blue", 7.00, 14910.0, 2130, [85, 341, 639, 575, 341, 149]),
]

@pytest.fixture(scope="module")
def client():
    with TestClient(main.app, base_url="https://testserver") as c:
        r = c.post("/login", data={"username": "tester", "password": "pw-123456"}, follow_redirects=False)
        assert r.status_code == 303
        yield c

def up(client, name, data):
    r = client.post("/api/upload", files={"file": (name, data)})
    assert r.status_code == 200, r.text
    return r.json()

def diff_table(o):
    """Return rows (field, expected, extracted, ok) for the report."""
    rows = [(k, v, o[k]) for k, v in EXPECTED_HEADER.items()]
    p = o["pos"][0]
    rows += [(f"po.{k}", v, p[k]) for k, v in EXPECTED_PO.items()]
    for (st, cc, col, rate, nv, oq, qs), l in zip(EXPECTED_LINES, p["lines"]):
        t = f"{st}/{col}"
        rows += [(f"{t}.style", st, l["style"]), (f"{t}.color_code", cc, l["color_code"]), (f"{t}.color", col, l["color"]),
                 (f"{t}.rate", rate, l["rate"]), (f"{t}.net_value", nv, l["net_value"]), (f"{t}.order_qty", oq, l["order_qty"])]
        rows += [(f"{t}.size_{s}", q, l["sizes"].get(s)) for s, q in zip(SIZES, qs)]
    return [(f, e, x, e == x) for f, e, x in rows]

def test_pdf_text_extraction_matches_pdf(client):
    res = up(client, "sample_po.pdf", PDF.read_bytes())
    it = res["items"][0]
    assert it["status"] == "done" and it["method"] == "pdf_text:sap_po"
    o = client.get(f"/api/orders/{it['order_id']}").json()
    assert len(o["pos"]) == 1 and len(o["pos"][0]["lines"]) == 5
    rows = diff_table(o)
    Path(os.getenv("DIFF_OUT", "/tmp/oe_diff.json")).write_text(json.dumps(rows, indent=0))
    bad = [r for r in rows if not r[3]]
    assert not bad, bad
    assert o["sizes"] == SIZES
    # totals: row sums == order qty, PO sum == stated total
    p = o["pos"][0]
    assert sum(sum(l["sizes"].values()) for l in p["lines"]) == 8350
    assert round(sum(l["net_value"] for l in p["lines"]), 2) == 70013.50
    assert not any("!=" in w for w in o["warnings"]), o["warnings"]

def test_pdf_independent_text_crosscheck():
    """Every size qty we expect must literally appear in poppler's own text of the PDF."""
    txt = subprocess.run(["pdftotext", "-layout", str(PDF), "-"], capture_output=True, text=True).stdout
    for *_, oq, qs in EXPECTED_LINES:
        assert f"{oq:,}.0" in txt
        for q in qs:
            assert f"{q}.0" in txt
    assert "8,350.000" in txt and "70,013.50" in txt

def _png_of_page1():
    import pymupdf
    d = pymupdf.open(PDF); png = d[0].get_pixmap(dpi=120).tobytes("png"); d.close(); return png

def test_image_path(client):
    res = up(client, "page1.png", _png_of_page1())["items"][0]
    if config.llm_provider() is None:
        assert res["status"] == "skipped" and "needs the AI reader" in res["reason"]
    else:
        assert res["status"] == "done", res
        o = client.get(f"/api/orders/{res['order_id']}").json()
        assert o["pos"][0]["po_number"] == "3110121365"

def test_scanned_pdf_without_key_is_skipped(client):
    import pymupdf
    img = pymupdf.open(); pg = img.new_page(); pg.insert_image(pg.rect, stream=_png_of_page1())
    data = img.tobytes(); img.close()
    res = up(client, "scan.pdf", data)["items"][0]
    if config.llm_provider() is None:
        assert res["status"] == "skipped"

def test_csv_and_xlsx_and_docx(client):
    csv = "Buyer:,Acme\nStyle:,ST-1\nFactory:,F1\nDepartment:,Mens\nPO Number:,PO77\nShip Date:,01/08/2026\nDestination:,Hamburg\n\nColor,XS,S,M,L,XL\nNavy,10,20,30,20,10\nRed,5,5,5,5,5\nTotal,15,25,35,25,15\n"
    r = up(client, "o.csv", csv.encode())["items"][0]; assert r["status"] == "done", r
    o = client.get(f"/api/orders/{r['order_id']}").json()
    assert o["pos"][0]["po_number"] == "PO77" and o["pos"][0]["ship_date"] == "2026-08-01"
    assert [sum(l["sizes"].values()) for l in o["pos"][0]["lines"]] == [90, 25]
    import openpyxl, docx
    wb = openpyxl.Workbook(); ws = wb.active
    for row in csv.strip().split("\n"): ws.append([int(c) if c.isdigit() else c for c in row.split(",")])
    b = io.BytesIO(); wb.save(b)
    r = up(client, "o.xlsx", b.getvalue())["items"][0]; assert r["status"] == "done", r
    d = docx.Document(); d.add_paragraph("Buyer: Acme"); d.add_paragraph("PO Number: PO88")
    t = d.add_table(rows=0, cols=6)
    for row in ["Color,XS,S,M,L,XL", "Navy,1,2,3,4,5"]:
        cells = t.add_row().cells
        for c, v in zip(cells, row.split(",")): c.text = v
    b = io.BytesIO(); d.save(b)
    r = up(client, "o.docx", b.getvalue())["items"][0]; assert r["status"] == "done", r
    assert client.get(f"/api/orders/{r['order_id']}").json()["pos"][0]["po_number"] == "PO88"

def test_bad_quantity_and_mismatch_surface(client):
    csv = "PO Number:,P1\nColor,S,M\nBlue,10,abc\n"
    r = up(client, "bad.csv", csv.encode())["items"][0]
    assert r["status"] in ("skipped", "failed")   # non-numeric qty never silently stored

def _zip(entries, compression=zipfile.ZIP_DEFLATED):
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w", compression) as z:
        for n, d in entries: z.writestr(n, d)
    return b.getvalue()

def test_zip_batch_with_junk_and_corrupt(client):
    inner = _zip([("nested_po.pdf", PDF.read_bytes())])
    deep = _zip([("deeper.zip", _zip([("x.pdf", PDF.read_bytes())]))])
    data = _zip([
        ("orders/sample_po.pdf", PDF.read_bytes()),
        ("__MACOSX/orders/._sample_po.pdf", b"\x00\x05\x16\x07junk"),
        ("orders/.DS_Store", b"\x00\x00\x00\x01Bud1"),
        ("Thumbs.db", b"junk"),
        ("orders/.hidden.pdf", b"%PDF-1.4"),
        ("orders/corrupt.pdf", b"%PDF-1.4\nthis is not really a pdf\n%%EOF"),
        ("orders/broken.xlsx", b"PK\x03\x04garbage"),
        ("orders/notes.bin", os.urandom(512)),
        ("../evil.pdf", b"%PDF-1.4"),
        ("bomb.txt", b"0" * (5 * 1024 * 1024)),       # ~5000x ratio
        ("inner.zip", inner),                          # depth 1: allowed
        ("deep.zip", deep),                            # depth 2: refused
    ])
    res = up(client, "batch.zip", data)
    st = {i["name"]: (i["status"], i.get("reason", "")) for i in res["items"]}
    assert st["orders/sample_po.pdf"][0] == "done"
    assert st["inner.zip/nested_po.pdf"][0] == "done"
    assert "__MACOSX" in st["__MACOSX/orders/._sample_po.pdf"][1]
    assert st["orders/.DS_Store"][0] == "skipped" and st["Thumbs.db"][0] == "skipped" and st["orders/.hidden.pdf"][0] == "skipped"
    assert st["orders/corrupt.pdf"][0] in ("skipped", "failed") and "corrupt" in st["orders/corrupt.pdf"][1].lower()
    assert st["orders/broken.xlsx"][0] in ("skipped", "failed")
    assert st["orders/notes.bin"][0] == "skipped"
    assert "traversal" in st["../evil.pdf"][1]
    assert "ratio" in st["bomb.txt"][1]
    assert "depth" in st["deep.zip/deeper.zip"][1]
    files = client.get("/api/files").json()
    assert any(f["kind"] == "zip" and "2/" in (f["reason"] or "") for f in files)
    assert not list(config.TMP_DIR.glob("*")), "temp files leaked"

def test_corrupt_zip_and_size_limit(client, monkeypatch):
    r = up(client, "bad.zip", b"PK\x03\x04notazip")["items"]
    assert r[0]["status"] in ("skipped", "failed")
    monkeypatch.setattr(config, "MAX_UPLOAD_BYTES", 1000)
    r = client.post("/api/upload", files={"file": ("big.pdf", b"%PDF" + b"0" * 5000)})
    assert r.status_code == 413
    assert not list(config.TMP_DIR.glob("*"))

def test_edit_and_save_validation(client):
    oid = client.get("/api/orders").json()[-1]["id"]
    o = client.get(f"/api/orders/{oid}").json()
    o["brand"] = "Louis Philippe"; o["pos"][0]["lines"][0]["sizes"]["35"] = 40
    r = client.put(f"/api/orders/{oid}", json=o); assert r.status_code == 200
    assert any("order qty 810 != sum of sizes 818" in w for w in r.json()["warnings"])
    assert client.get(f"/api/orders/{oid}").json()["brand"] == "Louis Philippe"
    o["pos"][0]["lines"][0]["sizes"]["35"] = "x"
    assert client.put(f"/api/orders/{oid}", json=o).status_code == 422
    o["pos"][0]["ship_date"] = "31/31/2026"
    assert client.put(f"/api/orders/{oid}", json=o).status_code == 422

def test_pages_render(client):
    assert client.get("/healthz").json()["ok"]
    assert "Order Book" in client.get("/").text
    oid = client.get("/api/orders").json()[0]["id"]
    assert "Add PO" in client.get(f"/orders/{oid}").text

def test_auth_required_and_rate_limit():
    with TestClient(main.app, base_url="https://testserver") as c:
        assert c.get("/healthz").status_code == 200
        assert c.get("/api/orders").status_code == 401
        assert c.post("/api/upload", files={"file": ("a.pdf", b"%PDF")}).status_code == 401
        assert c.post("/api/extract", files={"file": ("a.pdf", b"%PDF")}).status_code == 401
        r = c.get("/", follow_redirects=False); assert r.status_code == 303 and r.headers["location"] == "/login"
        codes = [c.post("/login", data={"username": "tester", "password": "nope"}, follow_redirects=False).status_code for _ in range(6)]
        assert codes[:5] == [401] * 5 and codes[5] == 429
        r = c.post("/login", data={"username": "tester", "password": "pw-123456"}, follow_redirects=False)
        assert r.status_code == 429   # locked even with the right password
    main.auth._fails.clear()
    with TestClient(main.app, base_url="https://testserver") as c:
        r = c.post("/login", data={"username": "tester", "password": "pw-123456"}, follow_redirects=False)
        ck = r.headers["set-cookie"].lower()
        assert "httponly" in ck and "secure" in ck and "samesite=strict" in ck
        assert c.get("/api/orders").status_code == 200
        c.post("/logout"); c.cookies.clear()
        assert c.get("/api/orders").status_code == 401

def test_upload_summary_counts(client):
    import openpyxl
    wb = openpyxl.Workbook(); ws = wb.active; ws.append(["PO Number:", "Z1"]); ws.append(["Color", "S", "M"]); ws.append(["Red", 1, 2]); b = io.BytesIO(); wb.save(b)
    data = _zip([("a/sample.pdf", PDF.read_bytes()), ("a/o.csv", b"PO Number:,Z2\nColor,S,M\nBlue,3,4\n"), ("a/o.xlsx", b.getvalue()),
                 ("a/pic.png", _png_of_page1()), ("__MACOSX/a/._x", b"j"), ("a/.DS_Store", b"j"), ("a/bad.pdf", b"%PDF-1.4 broken"),
                 ("in.zip", _zip([("n.csv", b"PO Number:,Z3\nColor,S,M\nGreen,5,6\n")]))])
    j = client.post("/api/upload", files={"file": ("mix.zip", data)}).json()
    s = j["summary"]
    assert s["files_received"] == 8
    assert s["by_format"] == {"pdf_text": 2, "pdf_scanned": 0, "image": 1, "xlsx": 1, "csv": 2, "docx": 0, "zip": 2, "other": 2}
    assert s["extracted_ok"] == 4 and s["skipped"] == 4 and s["need_ai"] == 1
    logs = client.get(f"/api/files/{j['zip_id']}/logs").json()
    assert any(l["level"] == "summary" and "UPLOAD SUMMARY" in l["message"] for l in logs)

def test_api_extract_shape(client, monkeypatch):
    monkeypatch.setenv("DISABLE_CACHE", "1")   # fresh extraction (an earlier test edited the cached order)
    j = client.post("/api/extract", files={"file": ("sample_po.pdf", PDF.read_bytes())}).json()
    r = j["results"][0]
    assert r["status"] == "done" and r["format"] == "pdf_text"
    assert r["totals"] == {"qty": 8350, "value": 70013.5, "stated_qty": 8350, "stated_value": 70013.5, "currency": "USD"}
    assert len(r["lines"]) == 5 and r["validation"]["errors"] == []

def test_dual_read_consensus(client, monkeypatch):
    """AI-primary: two independent reads must agree; any disagreement -> needs_review with both versions stored."""
    from app import llm, config as cfg
    good = {"buyer_name": "Acme", "style_number": "S1", "factory": "F", "department": "D",
            "pos": [{"po_number": "P9", "ship_date": "01.08.2026", "destination": "X", "stated_total_qty": 30,
                     "lines": [{"style": "S1", "color": "Navy", "sizes": {"S": 10, "M": 20}, "order_qty": 30}]}]}
    import copy
    monkeypatch.setattr(cfg, "llm_provider", lambda: "fake")
    monkeypatch.setattr(llm, "read_page", lambda **kw: copy.deepcopy(good))
    j = client.post("/api/extract", files={"file": ("a.txt", b"Acme PO P9 Navy S 10 M 20 total 30 ship 01.08.2026 S1 F D X")}).json()
    r = j["results"][0]
    assert r["agreement"]["status"] == "accepted" and r["agreement"]["disagreements"] == []
    def flaky(**kw):
        d = copy.deepcopy(good)
        if "SECOND INDEPENDENT READ" in kw.get("extra", ""):
            d["pos"][0]["lines"][0]["sizes"]["M"] = 21; d["pos"][0]["lines"][0]["order_qty"] = 31; d["pos"][0]["stated_total_qty"] = 31
        return d
    monkeypatch.setattr(llm, "read_page", flaky)
    j = client.post("/api/extract", files={"file": ("b.txt", b"Acme PO P9 Navy S 10 M 20 21 total 30 31 ship 01.08.2026 S1 F D X")}).json()
    r = j["results"][0]
    assert r["agreement"]["status"] == "needs_review"
    fields = {d["field"] for d in r["agreement"]["disagreements"]}
    assert any("size[M]" in f for f in fields) and "versions" in r
    assert j["summary"]["needs_review"] == 1
    oid = r["order"]["id"]
    page = client.get(f"/orders/{oid}").text; assert "const HL=" in page and "q|" in page   # second-read differences become field highlights

def test_worker_queue_pull_model(client, monkeypatch):
    import threading, time, json as _j, copy
    from app import db, config as cfg, llm
    monkeypatch.setenv("WORKER_TOKEN", "w" * 40); monkeypatch.setenv("AI_QUEUE", "1")
    monkeypatch.setenv("AI_PICKUP_S", "3"); monkeypatch.setenv("AI_JOB_WAIT_S", "60")
    W = {"Authorization": "Bearer " + "w" * 40}
    assert client.get("/api/worker/next", headers={"Authorization": "Bearer nope"}).status_code == 401
    with TestClient(main.app, base_url="https://testserver") as anon:
        assert anon.get("/api/worker/next", params={"owner": "tester"}, headers=W).status_code == 204   # token auth, no session needed
        assert anon.get("/api/worker/pending", params={"owner": "tester"}, headers=W).json()["pending"] == 0
        db.enqueue_job("x", "m", "t", None, owner="someone-else")
        assert anon.get("/api/worker/pending", params={"owner": "someone-else"}, headers=W).json()["pending"] == 0   # other owners ignored
        assert anon.get("/api/worker/next", params={"owner": "someone-else"}, headers=W).status_code == 204
    # idempotency + stale lease + lease expiry
    jid = db.enqueue_job("p", "m", "t", None, owner="tester")
    j = client.get("/api/worker/next", params={"owner": "tester"}, headers=W).json(); assert j["id"] == jid
    assert client.post("/api/worker/result", headers=W, json={"id": jid, "lease_id": "bad", "text": "x"}).json()["ack"] == "stale_lease"
    monkeypatch.setattr(db, "LEASE_S", -1)
    jid2 = db.enqueue_job("p2", "m", "t", None, owner="tester")
    a = client.get("/api/worker/next", params={"owner": "tester"}, headers=W).json()    # leases jid2 with an already-expired lease
    b = client.get("/api/worker/next", params={"owner": "tester"}, headers=W).json()    # expiry re-queues -> claimed again with a new lease
    assert {a["id"], b["id"]} <= {jid, jid2} and b["lease_id"] != a["lease_id"]
    monkeypatch.setattr(db, "LEASE_S", 360)
    for x in (a, b):
        client.post("/api/worker/result", headers=W, json={"id": x["id"], "lease_id": x["lease_id"], "text": "{}"})
    r = client.post("/api/worker/result", headers=W, json={"id": b["id"], "lease_id": b["lease_id"], "text": "{}"}).json()
    assert r["ack"].startswith("already_") or r["ack"] == "stale_lease"
    # full extraction through a fake worker thread
    answer = {"buyer_name": "Acme", "style_number": "S1", "factory": "F", "department": "D",
              "pos": [{"po_number": "P9", "ship_date": "01.08.2026", "destination": "X", "stated_total_qty": 30,
                       "lines": [{"style": "S1", "color": "Navy", "sizes": {"S": 10, "M": 20}, "order_qty": 30}]}]}
    stop = threading.Event()
    def fake_worker():
        w = TestClient(main.app, base_url="https://testserver")   # no lifespan: startup would wipe TMP_DIR
        if True:
            while not stop.is_set():
                r = w.get("/api/worker/next", params={"owner": "tester"}, headers=W)
                if r.status_code == 204: time.sleep(0.2); continue
                jj = r.json(); w.post("/api/worker/result", headers=W, json={"id": jj["id"], "lease_id": jj["lease_id"], "text": _j.dumps(answer)})
    t = threading.Thread(target=fake_worker, daemon=True); t.start()
    try:
        j = client.post("/api/extract", files={"file": ("q.txt", b"Acme PO P9 Navy S 10 M 20 total 30 ship 01.08.2026 S1 F D X")}).json()
        res = j["results"][0]
        assert res["status"] == "done" and res["agreement"]["status"] == "accepted", res
        assert client.get("/api/ai-status").json()["online"] is True
    finally:
        stop.set(); t.join(5)
    # worker offline -> queued_ai, not failed
    db.heartbeat("box"); 
    with db.conn() as c: c.execute("UPDATE kv SET v=? WHERE k='worker_hb'", (_j.dumps({"t": 0, "worker": "box"}),))
    j = client.post("/api/extract", files={"file": ("q2.txt", b"Acme PO P9 Navy S 10 M 20")}).json()
    assert j["results"][0]["status"] == "queued_ai" and j["summary"]["queued_ai"] == 1
    assert list((cfg.DATA_DIR / "queue").glob("*"))


def test_sha_cache(client):
    j1 = client.post("/api/extract", files={"file": ("again.pdf", PDF.read_bytes())}).json()["results"][0]
    assert j1["cached"] is True and j1["cached_from"] and j1["order"]["id"]
    data = _zip([("a/x1.pdf", PDF.read_bytes()), ("b/x2.pdf", PDF.read_bytes())])
    j = client.post("/api/extract", files={"file": ("dup.zip", data)}).json()
    assert j["summary"]["cached"] == 2 and all(r["cached"] for r in j["results"])

def test_crud(client):
    body = {"buyer_name": "Test Buyer", "style_number": "ST-1", "brand": "B", "factory": "F", "department": "D", "planned_line": 1,
            "sizes": ["S", "M"], "colors": ["Navy"],
            "pos": [{"po_number": "CRUD-1", "ship_date": "2026-12-01", "lines": [{"color": "Navy", "sizes": {"S": 10, "M": 20}, "rate": 2.5}]}]}
    r = client.post("/api/orders", json=body); assert r.status_code == 201; oid = r.json()["id"]
    o = client.get(f"/api/orders/{oid}").json(); assert o["buyer_name"] == "Test Buyer" and o["pos"][0]["lines"][0]["sizes"] == {"S": 10, "M": 20}
    lst = client.get("/api/orders", params={"q": "CRUD-1"}).json(); assert [x["id"] for x in lst] == [oid] and lst[0]["qty"] == 30
    body["buyer_name"] = "Edited"; body["pos"][0]["lines"][0]["sizes"]["S"] = 11
    assert client.put(f"/api/orders/{oid}", json=body).status_code == 200
    o = client.get(f"/api/orders/{oid}").json(); assert o["buyer_name"] == "Edited" and o["pos"][0]["lines"][0]["sizes"]["S"] == 11
    assert client.put(f"/api/orders/{oid}", json={"pos": [{"po_number": "x", "lines": [{"color": "a", "sizes": {"S": "abc"}}]}]}).status_code == 422
    assert client.get("/orders/new").status_code == 200 and client.get(f"/orders/{oid}").status_code == 200
    assert "Edited" in client.get("/", params={"q": "Edited"}).text
    assert client.delete(f"/api/orders/{oid}").status_code == 200
    assert client.get(f"/api/orders/{oid}").status_code == 404 and client.delete(f"/api/orders/{oid}").status_code == 404
    assert "needs review" not in client.get("/").text.lower()
