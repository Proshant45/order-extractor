"""Deterministic parsers for text-extractable documents."""
from __future__ import annotations
import csv, re
from collections import OrderedDict

NUM = r"[\d,]+\.\d+|[\d,]+"

def _n(s):
    return float(s.replace(",", ""))

# ---- SAP style PO (e.g. Aditya Birla YVP_PO_DETAILS) -------------------
ROW = re.compile(
    r"^(?P<mat>[A-Z]{2,}\d{4,})\s+(?P<hsn>\d{6,8})\s+(?P<unit>PC|PCS|NOS|EA)\s+(?P<rate>[\d,]+\.\d+)"
    r"(?:\s+(?P<net>[\d,]+\.\d{2}))?.*?(?P<dd>\d{2}\.\d{2}\.\d{4})\s+(?P<size>\w{1,5})\s+(?P<mrp>[\d,]+\.\d)\b")
MATDESC = re.compile(r"^(?P<code>[A-Z]{2,}\d{8,})\s+(?P<desc>.+?),\s*(?P<size>\w+)\s{2,}(?P<color>\S.*?)\s*$")

def looks_like_sap_po(text: str) -> bool:
    return "Sizewise" in text and any(ROW.match(l.strip()) for l in text.splitlines())

def parse_sap_po(text: str) -> dict:
    lines = text.replace("\f", "\n").splitlines()
    g = lambda pat, flags=0: (m.group(1).strip() if (m := re.search(pat, text, flags)) else None)
    # material description table: code -> (desc, size, color)
    desc = {}
    for l in lines:
        m = MATDESC.match(l.strip())
        if m:
            desc[m["code"]] = (m["desc"].strip(), m["size"], m["color"].strip())
    groups: "OrderedDict[tuple, dict]" = OrderedDict()
    ship_dates = set()
    for i, raw in enumerate(lines):
        m = ROW.match(raw.strip())
        if not m:
            continue
        prev = re.findall(NUM, lines[i - 1]) if i else []
        if len(prev) < 2:
            raise ValueError(f"line {i+1}: expected qty + sizewise qty above material row")
        line_qty, size_qty = _n(prev[0]), _n(prev[-1])
        # color code is the first 3-6 digit token starting one of the next 3 lines
        ccode = None
        for nxt in lines[i + 1:i + 8]:
            if ROW.match(nxt.strip()):
                break
            mm = re.match(r"^\s*(\d{3,6})\b", nxt)
            if mm:
                ccode = mm.group(1); break
        style = m["mat"]
        if ccode is None and line_qty == 0:
            # continuation row whose colour code was lost (e.g. page break): attach to the open group of same style+rate
            for (st_, cc_, rt_), gg in reversed(groups.items()):
                if st_ == style and rt_ == _n(m["rate"]) and m["size"] not in gg["sizes"]:
                    ccode = cc_; break
        key = (style, ccode, _n(m["rate"]))
        g_ = groups.setdefault(key, {"style": style, "color_code": ccode, "rate": _n(m["rate"]),
                                     "sizes": OrderedDict(), "order_qty": None, "net_value": None, "hsn": m["hsn"]})
        if m["net"]:
            g_["net_value"] = _n(m["net"])
        if line_qty > 0:
            g_["order_qty"] = int(line_qty)
        g_["sizes"][m["size"]] = g_["sizes"].get(m["size"], 0) + int(size_qty)
        ship_dates.add(m["dd"])
    out_lines, depts = [], []
    for (style, cc, _), d in groups.items():
        color = None
        for sz in d["sizes"]:
            info = desc.get(f"{style}{cc}{sz}")
            if info:
                color = info[2]; depts.append(info[0]); break
        d["color"] = color or (cc or "UNKNOWN")
        d.pop("hsn")
        out_lines.append(d)
    styles = list(dict.fromkeys(l["style"] for l in out_lines))
    rates = list(dict.fromkeys(l["rate"] for l in out_lines))
    buyer = next((l.strip().split("  ")[0] for l in lines if l.strip()), None)
    supplier = g(r"SUPPLIER:\s*(.+?)(?:\s{2,}|$)", re.M)
    # ship-to block: city + postcode + country after "Ship To:"
    dest = None
    st = re.search(r"Ship To:(.*?)Material\s", text, re.S)
    if st:
        toks = [t.strip() for t in st.group(1).splitlines() if t.strip()]
        city = next((t for t in toks if re.fullmatch(r"[A-Za-z ]+", t) and t.upper() == t.upper() and t not in ("IN",) and "LIMITED" not in t.upper() and "ADITYA" not in t.upper() and "Village" not in t), None)
        pin = next((t for t in toks if re.fullmatch(r"\d{5,6}", t)), None)
        ctry = "IN" if "IN" in toks else None
        dest = ", ".join(x for x in (city, pin, ctry) if x) or None
    cur = g(r"Total Value\s+[\d,\.]+\s+([A-Z]{3})")
    dept = depts[0].replace("MEN S", "MEN'S") if depts else None
    return {
        "buyer_name": buyer,
        "brand": None,
        "style_number": ", ".join(styles) if styles else None,
        "factory": supplier,
        "fob": rates[0] if len(rates) == 1 else None,
        "department": dept,
        "currency": cur,
        "pos": [{
            "po_number": g(r"P\.O\s*No\.?\s*(\d+)"),
            "po_date": g(r"\bDate\s+(\d{2}\.\d{2}\.\d{4})"),
            "ship_date": sorted(ship_dates)[0] if len(ship_dates) == 1 else (min(ship_dates) if ship_dates else None),
            "destination": dest,
            "sales_contract_number": None,
            "stated_total_qty": g(r"Total Quantity\s+([\d,\.]+)"),
            "stated_total_value": g(r"Total Value\s+([\d,\.]+)"),
            "currency": cur,
            "lines": out_lines,
        }],
        "_meta": {"parser": "sap_po", "vendor_number": g(r"Vendor Number With Us\s*:\s*(\d+)"),
                  "payment_terms": g(r"Payment terms:\s*(.+)"), "multiple_rates": rates if len(rates) > 1 else None},
    }

# ---- tabular (xlsx / csv) ---------------------------------------------
SIZE_TOKENS = {"XXS","XS","S","M","L","XL","XXL","2XL","3XL","4XL","XXXL"}

def _is_size(h):
    h = str(h).strip().upper()
    return h in SIZE_TOKENS or re.fullmatch(r"\d{2,3}", h) is not None

def parse_table_rows(rows_iter) -> dict | None:
    """rows_iter yields lists of cell values, streamed. Looks for a header row with Color + size columns."""
    header, idx, out_lines, meta = None, {}, [], {}
    po_number = None
    for row in rows_iter:
        cells = ["" if c is None else str(c).strip() for c in row]
        if header is None:
            low = [c.lower() for c in cells]
            # key: value lines before table
            if len(cells) >= 2 and cells[0] and cells[0].endswith(":"):
                meta[cells[0][:-1].strip().lower()] = cells[1]
            if any(c in ("color", "colour") for c in low) and sum(_is_size(c) for c in cells) >= 2:
                header = cells
                idx = {"color": next(i for i, c in enumerate(low) if c in ("color", "colour"))}
                idx["sizes"] = [(i, c) for i, c in enumerate(cells) if _is_size(c)]
                idx["po"] = next((i for i, c in enumerate(low) if c.replace(" ", "") in ("po", "pono", "ponumber", "po#")), None)
            continue
        if not any(cells):
            continue
        color = cells[idx["color"]] if idx["color"] < len(cells) else ""
        if not color or color.lower() in ("total", "grand total"):
            continue
        sizes = {}
        for i, s in idx["sizes"]:
            v = cells[i] if i < len(cells) else ""
            if v:
                sizes[s] = int(float(v.replace(",", "")))
        po = cells[idx["po"]] if idx["po"] is not None and idx["po"] < len(cells) else None
        out_lines.append({"color": color, "sizes": sizes, "_po": po})
    if header is None:
        return None
    pos = OrderedDict()
    default_po = meta.get("po number") or meta.get("po no") or meta.get("po") or "UNKNOWN"
    for l in out_lines:
        pn = l.pop("_po") or default_po
        pos.setdefault(pn, {"po_number": pn, "lines": [], "ship_date": meta.get("ship date"),
                            "destination": meta.get("destination")})["lines"].append(l)
    return {"buyer_name": meta.get("buyer") or meta.get("buyer name"), "brand": meta.get("brand"),
            "style_number": meta.get("style") or meta.get("style number"), "factory": meta.get("factory"),
            "department": meta.get("department"), "fob": meta.get("fob"),
            "pos": list(pos.values()), "_meta": {"parser": "table"}}

def iter_csv(path):
    with open(path, newline="", encoding="utf-8", errors="replace") as f:
        yield from csv.reader(f)

def iter_xlsx(path, max_rows=20000):
    import openpyxl
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)  # read_only streams rows
    try:
        ws = wb.worksheets[0]
        for i, row in enumerate(ws.iter_rows(values_only=True)):
            if i >= max_rows:
                break
            yield list(row)
    finally:
        wb.close()
