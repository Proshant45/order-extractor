# Order Extractor

Upload a buyer PO / order sheet (PDF, image, XLSX, CSV, DOCX, or a ZIP of them). It extracts a structured order
(header + POs + colour x size quantities), validates it, stores it in SQLite and shows it in a Texora360-style New Order page you can edit and save.

## Pipeline
1. Upload streamed to disk in 1 MB chunks, hard cap `MAX_UPLOAD_MB` (default 25). One extraction at a time (asyncio semaphore), `EXTRACT_TIMEOUT_S` timeout.
2. Type sniffed from magic bytes. ZIPs iterated entry by entry (each streamed to a temp file, deleted after): max entries, max total/entry
   uncompressed size (checked on real bytes, not just headers), compression-ratio guard, path traversal, encrypted entries, nested depth 1.
   Junk (`__MACOSX`, `.DS_Store`, `Thumbs.db`, hidden/temp files) and corrupt/unsupported files are logged as skipped with a reason. One bad file never fails the batch.
3. PDF: pdfplumber page by page (`flush_cache` per page, `MAX_PAGES` cap). Known layouts (SAP PO forms) parse deterministically.
   Unknown layout -> AI reader on page text; scanned pages -> render that one page with PyMuPDF at `RENDER_DPI` and send to the vision model.
   XLSX (openpyxl read-only streaming) / CSV / DOCX tables with a Color + size header parse deterministically; otherwise AI reader.
   Images are downscaled to `MAX_IMAGE_PX` with a decompression-bomb guard.
4. pydantic validation (numeric whole quantities, dates, required PO number) + warnings (missing required fields, row qty vs size sum,
   rate x qty vs net value, PO stated totals vs computed).
5. SQLite tables: `files`, `orders`, `pos`, `po_lines`, `extraction_logs`.

## AI reader keys
Env only: `GEMINI_API_KEY` (preferred, model `GEMINI_MODEL`, default gemini-2.5-flash) or `OPENAI_API_KEY` (`OPENAI_MODEL`, default gpt-4o-mini).

## Run
    pip install -r requirements-dev.txt
    uvicorn app.main:app --reload
    pytest -q            # put the sample PO at tests/fixtures/sample_po.pdf (gitignored) or set SAMPLE_PDF

## Render
`render.yaml` defines the web service (`/healthz` health check, 1 GB disk at `/var/data` for SQLite) and a 10-minute keep-alive cron.
Persistent disks need a paid plan; on the free plan SQLite is wiped on each deploy/restart and a cron job is not available,
so use an external pinger (e.g. GitHub Actions schedule or cron-job.org) hitting `/healthz` every 10 minutes instead.
