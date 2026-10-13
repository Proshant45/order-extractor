"""File -> structured order. Memory-conscious: page by page, bounded image sizes."""
from __future__ import annotations
import io, zipfile
from pathlib import Path
from pydantic import ValidationError
from . import config, llm, parsers
from .schema import Order, warnings_for

class Skip(Exception):
    """File is not processable; recorded as skipped with a reason, never fatal."""

def sniff(path: Path) -> str:
    with open(path, "rb") as f:
        head = f.read(8)
    ext = path.suffix.lower()
    if head.startswith(b"%PDF"):
        return "pdf"
    if head.startswith(b"\x89PNG") or head[:3] == b"\xff\xd8\xff" or head[:4] in (b"GIF8", b"II*\x00", b"MM\x00*") or head[:4] == b"RIFF":
        return "image"
    if head.startswith(b"PK"):
        if ext == ".xlsx": return "xlsx"
        if ext == ".docx": return "docx"
        try:
            with zipfile.ZipFile(path) as z:
                names = z.namelist()[:50]
        except zipfile.BadZipFile:
            raise Skip("corrupt zip container")
        if any(n.startswith("xl/") for n in names): return "xlsx"
        if any(n.startswith("word/") for n in names): return "docx"
        return "zip"
    if ext in (".csv", ".txt", ".tsv"):
        return "csv"
    if ext in (".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff", ".bmp", ".heic"):
        return "image"  # extension says image even if magic is odd: let AI reader try
    return "unknown"

def _png_bytes_from_image(path: Path) -> bytes:
    from PIL import Image
    Image.MAX_IMAGE_PIXELS = 60_000_000  # decompression-bomb guard
    try:
        with Image.open(path) as im:
            im.draft("RGB", (config.MAX_IMAGE_PX, config.MAX_IMAGE_PX))  # cheap JPEG downscale on decode
            im = im.convert("RGB")
            im.thumbnail((config.MAX_IMAGE_PX, config.MAX_IMAGE_PX))
            buf = io.BytesIO(); im.save(buf, "PNG", optimize=True)
            return buf.getvalue()
    except Exception as e:
        raise Skip(f"unreadable image: {e.__class__.__name__}")

def _need_llm(log, what):
    if config.llm_provider() is None:
        raise Skip(f"{what} needs the AI reader but no GEMINI_API_KEY / OPENAI_API_KEY is set")
    log(f"AI reader ({config.llm_provider()}) for {what}")

def extract_pdf(path: Path, log) -> tuple[dict, str]:
    import pdfplumber, pymupdf
    texts, scanned = [], []
    try:
        pdf = pdfplumber.open(path)
    except Exception as e:
        raise Skip(f"corrupt PDF: {e.__class__.__name__}")
    with pdf:
        n = len(pdf.pages)
        if n > config.MAX_PAGES:
            log(f"PDF has {n} pages, only first {config.MAX_PAGES} processed", "warn")
        for i, page in enumerate(pdf.pages[:config.MAX_PAGES]):
            t = page.extract_text(layout=True) or ""
            page.flush_cache()           # drop parsed objects for this page
            texts.append(t)
            if len(t.strip()) < 30:
                scanned.append(i)
    full = "\n\f".join(texts)
    log(f"PDF pages={len(texts)} text_pages={len(texts)-len(scanned)} scanned_pages={len(scanned)}")
    if parsers.looks_like_sap_po(full):
        log("deterministic parser: sap_po")
        return parsers.parse_sap_po(full), "pdf_text:sap_po"
    _need_llm(log, "PDF with unknown layout or scanned pages")
    pages = []
    doc = pymupdf.open(path)
    try:
        for i in range(min(len(doc), config.MAX_PAGES)):
            if i in scanned:
                pix = doc[i].get_pixmap(dpi=config.RENDER_DPI)   # one page image in memory at a time
                png = pix.tobytes("png"); del pix
                pages.append(llm.read_page(image_png=png)); del png
            else:
                pages.append(llm.read_page(text=texts[i]))
            log(f"AI read page {i+1}")
    finally:
        doc.close()
    return llm.merge_pages(pages), "pdf_llm"

def extract_file(path: Path, kind: str, log) -> tuple[dict, str]:
    if kind == "pdf":
        return extract_pdf(path, log)
    if kind in ("csv", "xlsx"):
        try:
            rows = parsers.iter_csv(path) if kind == "csv" else parsers.iter_xlsx(path)
            d = parsers.parse_table_rows(rows)
        except Exception as e:
            raise Skip(f"corrupt {kind}: {e.__class__.__name__}")
        if d:
            return d, f"{kind}:table"
        _need_llm(log, f"{kind} without a Color/size header")
        rows = parsers.iter_csv(path) if kind == "csv" else parsers.iter_xlsx(path)
        txt = "\n".join("\t".join("" if c is None else str(c) for c in r) for _, r in zip(range(2000), rows))
        return llm.merge_pages([llm.read_page(text=txt)]), f"{kind}_llm"
    if kind == "docx":
        import docx
        try:
            d = docx.Document(str(path))
        except Exception as e:
            raise Skip(f"corrupt docx: {e.__class__.__name__}")
        paras = [p.text for p in d.paragraphs]
        meta_rows = [[p.split(":", 1)[0] + ":", p.split(":", 1)[1].strip()] for p in paras if ":" in p]
        for t in d.tables:
            r = parsers.parse_table_rows(meta_rows + [[c.text for c in row.cells] for row in t.rows])
            if r:
                return r, "docx:table"
        _need_llm(log, "docx")
        txt = "\n".join(paras + ["\t".join(c.text for c in row.cells) for t in d.tables for row in t.rows])
        return llm.merge_pages([llm.read_page(text=txt)]), "docx_llm"
    if kind in ("image", "unknown"):
        png = _png_bytes_from_image(path)  # unknown files: try as image; Skip if not decodable
        _need_llm(log, "image")
        return llm.merge_pages([llm.read_page(image_png=png)]), "image_llm"
    raise Skip(f"unsupported type {kind}")

def validate(data: dict) -> tuple[Order | None, list[str], list[str]]:
    data = {k: v for k, v in data.items() if not k.startswith("_")}
    try:
        o = Order.model_validate(data)
    except ValidationError as e:
        return None, [], [f"{'.'.join(map(str, er['loc']))}: {er['msg']}" for er in e.errors()]
    return o, warnings_for(o), []
