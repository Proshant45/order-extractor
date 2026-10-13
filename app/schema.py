from __future__ import annotations
import re
from datetime import date, datetime
from typing import Optional
from pydantic import BaseModel, Field, field_validator, model_validator

DATE_FORMATS = ("%d.%m.%Y", "%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d", "%d/%m/%y", "%d.%m.%y", "%m/%d/%Y", "%d-%b-%Y", "%d %b %Y")

def parse_date(v):
    if v in (None, ""):
        return None
    if isinstance(v, (date, datetime)):
        return v if isinstance(v, date) and not isinstance(v, datetime) else v.date()
    s = str(v).strip()
    for f in DATE_FORMATS:
        try:
            return datetime.strptime(s, f).date()
        except ValueError:
            pass
    raise ValueError(f"unparseable date: {s!r}")

def to_num(v):
    if v in (None, ""):
        return None
    if isinstance(v, (int, float)):
        return v
    s = str(v).replace(",", "").strip()
    return float(s)

SIZE_ALIASES = {"SMALL": "S", "MEDIUM": "M", "MED": "M", "LARGE": "L", "XLARGE": "XL", "EXTRALARGE": "XL", "XSMALL": "XS",
                "EXTRASMALL": "XS", "XXLARGE": "XXL", "2XL": "XXL", "XXXL": "3XL", "SM": "S", "LG": "L", "MD": "M"}

def norm_size(k) -> str:
    s = str(k).strip()
    u = re.sub(r"[\s\-_.]", "", s.upper())
    if u in SIZE_ALIASES:
        return SIZE_ALIASES[u]
    if re.fullmatch(r"\d+(\.\d+)?", s):
        return s.rstrip("0").rstrip(".") if "." in s else s
    return s.upper() if len(s) <= 4 else s

class POLine(BaseModel):
    style: Optional[str] = None
    color: str
    color_code: Optional[str] = None
    sizes: dict[str, int] = Field(default_factory=dict)
    order_qty: Optional[int] = None
    rate: Optional[float] = None
    net_value: Optional[float] = None

    @field_validator("sizes", mode="before")
    @classmethod
    def _sizes(cls, v):
        out = {}
        for k, q in (v or {}).items():
            n = to_num(q)
            if n is None:
                continue
            if n < 0 or n != int(n):
                raise ValueError(f"size {k}: quantity must be a non-negative whole number, got {q}")
            out[norm_size(k)] = out.get(norm_size(k), 0) + int(n)
        return out

    @field_validator("order_qty", mode="before")
    @classmethod
    def _qty(cls, v):
        n = to_num(v)
        return None if n is None else int(n)

    @field_validator("rate", "net_value", mode="before")
    @classmethod
    def _f(cls, v):
        return to_num(v)

class PO(BaseModel):
    po_number: str
    po_date: Optional[date] = None
    destination: Optional[str] = None
    ship_date: Optional[date] = None
    sales_contract_number: Optional[str] = None
    lines: list[POLine] = Field(default_factory=list)
    stated_total_qty: Optional[int] = None
    stated_total_value: Optional[float] = None
    currency: Optional[str] = None

    @field_validator("po_date", "ship_date", mode="before")
    @classmethod
    def _d(cls, v):
        return parse_date(v)

    @field_validator("stated_total_qty", mode="before")
    @classmethod
    def _q(cls, v):
        n = to_num(v)
        return None if n is None else int(n)

    @field_validator("stated_total_value", mode="before")
    @classmethod
    def _v(cls, v):
        return to_num(v)

    @property
    def total_qty(self):
        return sum(sum(l.sizes.values()) for l in self.lines)

class Order(BaseModel):
    buyer_name: Optional[str] = None
    brand: Optional[str] = None
    style_number: Optional[str] = None
    factory: Optional[str] = None
    planned_line: Optional[int] = 1
    fob: Optional[float] = None
    smv: Optional[float] = None
    cm: Optional[float] = None
    department: Optional[str] = None
    sizes: list[str] = Field(default_factory=list)
    colors: list[str] = Field(default_factory=list)
    currency: Optional[str] = None
    pos: list[PO] = Field(default_factory=list)

    @field_validator("fob", "smv", "cm", mode="before")
    @classmethod
    def _n(cls, v):
        return to_num(v)

    @model_validator(mode="after")
    def _derive(self):
        if not self.sizes:
            seen = []
            for p in self.pos:
                for l in p.lines:
                    for s in l.sizes:
                        if s not in seen:
                            seen.append(s)
            self.sizes = seen
        if not self.colors:
            self.colors = list(dict.fromkeys(l.color for p in self.pos for l in p.lines))
        return self

REQUIRED = ["buyer_name", "style_number", "factory", "department"]

def warnings_for(order: Order) -> list[str]:
    """Business-rule checks that should not block saving but must be shown."""
    w = []
    for f in REQUIRED:
        if not getattr(order, f):
            w.append(f"Missing required field: {f}")
    if not order.brand:
        w.append("Brand not found in document (fill manually)")
    if order.fob is None:
        w.append("FOB not set at header level (see per-line rates)")
    if not order.pos:
        w.append("No PO found")
    for p in order.pos:
        tag = f"PO {p.po_number}"
        if not p.ship_date:
            w.append(f"{tag}: missing ship date")
        if not p.destination:
            w.append(f"{tag}: missing destination")
        if not p.lines:
            w.append(f"{tag}: no color/size lines")
        for l in p.lines:
            s = sum(l.sizes.values())
            if l.order_qty is not None and l.order_qty != s:
                w.append(f"{tag} / {l.color}: order qty {l.order_qty} != sum of sizes {s}")
            if l.rate is not None and l.net_value is not None and abs(l.rate * s - l.net_value) > 0.01:
                w.append(f"{tag} / {l.color}: rate x qty {l.rate*s:.2f} != net value {l.net_value:.2f}")
        if p.stated_total_qty is not None and p.stated_total_qty != p.total_qty:
            w.append(f"{tag}: stated total qty {p.stated_total_qty} != computed {p.total_qty}")
        if p.stated_total_value is not None:
            vals = [l.net_value for l in p.lines if l.net_value is not None]
            if len(vals) == len(p.lines) and abs(sum(vals) - p.stated_total_value) > 0.01:
                w.append(f"{tag}: stated total value {p.stated_total_value} != sum of lines {sum(vals):.2f}")
    return w
