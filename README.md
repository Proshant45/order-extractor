# Order Extractor

Upload buyer documents (POs, sales contracts, order sheets, PIs, control sheets, tech packs, BOMs) one at a time or as a ZIP.
Order documents are extracted into a structured order (header + POs + colour x size quantities), validated, stored in SQLite and shown
in a Texora360-style New Order page you can review, edit and save. Login required (single account).

## How extraction works
1. **Upload**: streamed to disk in 1 MB chunks. Single files up to `MAX_UPLOAD_MB` (25), ZIPs up to `MAX_ZIP_MB` (100).
   ZIP entries are processed one at a time (each streamed to a temp file): entry count, total and per-entry size (checked on real bytes),
   compression-ratio (zip bomb), path traversal, encrypted entries, nesting depth 1. Junk (`__MACOSX`, `.DS_Store`, `Thumbs.db`, hidden) and
   corrupt files are skipped with a reason; one bad file never fails the batch.
2. **Cache**: every file / ZIP entry is hashed (SHA-256). Identical bytes already extracted are served from the stored result (`cached`).
3. **Classify**: purchase_order, sales_contract, proforma_invoice, control_sheet, tech_pack, bom, other. Order-type documents are extracted;
   tech packs / BOMs / other are stored with their type and style/PO references and linked per buyer (`GET /api/links`).
4. **AI read**: each order document is read twice by opencode free models and saved as an order draft after the reads and checks finish.
   A second independent read (read B) is compared field by field; fields where it differs are lightly highlighted in the
   order form (hover shows read B's value). There is no review queue. Arithmetic checks (size sums, qty x rate, totals) show as warnings.
5. **Store**: SQLite tables `files`, `orders`, `pos`, `po_lines`, `extraction_logs`, `ai_jobs`, `batches`, `kv`.

## AI worker (pull model)
The web app never runs a model. AI reads are queued as jobs (`ai_jobs`) tagged with the uploading account.
A worker on a separate machine with enough RAM polls the app over HTTPS with a bearer `WORKER_TOKEN`:
- `GET /api/worker/pending?owner=<account>` - cheap count, used by the tick
- `GET /api/worker/next?owner=<account>` - claims one job with a lease (expired leases are re-queued, max 3 attempts)
- `POST /api/worker/result` - `{id, lease_id, text|error}`; idempotent, stale leases ignored

Box setup (`worker/`):
- `worker/.env` (chmod 600): `APP_URL`, `WORKER_TOKEN`, `APP_ACCOUNT`, `OPENCODE_API_KEY`, `WORKER_THREADS`
- `worker/tick.py`: AI-less check, failure/work log lines (`worker/tick.log`, rotated); idle successful checks stay quiet. 0 pending -> exits; otherwise runs
  `worker/box_worker.py --drain`, which runs `opencode run` locally per job and exits when the queue is empty. The loop checks every15seconds while idle.
- Schedule: `nohup worker/tick_loop.sh >/dev/null 2>&1 &` for15-second checks. A standard once-per-minute crontab checks only every60seconds.

If the worker is offline, files are marked `queued_ai` and re-processed automatically once it is back. The UI shows worker online/offline,
last heartbeat, queued jobs and cached results.

## API
- Orders CRUD: `GET /api/orders?q=`, `POST /api/orders`, `GET/PUT/DELETE /api/orders/{id}`. UI: `/` (list + search + upload), `/orders/new`, `/orders/{id}` (edit/save/delete)
- `POST /api/extract?wait=0` -> `{batch_id}`; `GET /api/batches/{id}?view=api` -> `{summary, results:[{status, file, format, doc_type, cached,
  order, pos, lines, totals, validation, agreement:{status, models, disagreements}, versions}]}`
- `POST /api/upload`, `GET /api/orders`, `GET/PUT /api/orders/{id}`, `GET /api/files`, `GET /api/links`, `GET /api/ai-status`, `GET /healthz` (public)

## Run / test
    pip install -r requirements-dev.txt
    uvicorn app.main:app --reload
    pytest -q            # sample PO at tests/fixtures/sample_po.pdf (gitignored) or SAMPLE_PDF

## Render
`render.yaml`: web service, `/healthz` health check. Env: `APP_USERNAME`, `APP_PASSWORD_HASH` (bcrypt), `SECRET_KEY`, `AI_QUEUE=1`,
`WORKER_TOKEN`, `DATA_DIR`. On the free plan there is no disk: SQLite lives in `/tmp` and resets on redeploy/restart/spin-down;
the worker's polling keeps the instance awake while it runs.

## Local verification and live progress

Run `.venv/bin/python scripts/local_stack.py` after installing the development dependencies and configuring ignored `.env` and `worker/.env`. The local app runs at `http://127.0.0.1:8765`; the worker loop polls every15seconds while idle and drains queued jobs when active. `APP_ACCOUNT` must match `APP_USERNAME`. The loop holds one repository-specific flock for its lifetime.

The upload screen streams progress with SSE from `/api/batches/{id}/events` and reconciles through `/api/progress?bid={id}` after reconnects. It shows worker receipt, AI start/output/return, saved order, actual read/page attempts and files finished. Repairs can add reads; these counters measure completed requests, not model tokens or a guessed percentage.

Both synchronous and background uploads retain their input and batch metadata under persistent `DATA_DIR`. Interrupted batches replay after server restart; identical completed entries use cache. Worker results remain in an ignored local outbox until the server returns a recognised acknowledgement. Edited/deleted orders invalidate their source cache. Manual changes clear the original AI agreement badge.

Use `.venv/bin/python scripts/check_local_extraction.py /path/to/document` for real HTTP/worker extraction with retained reports. The checked PO fixture is restored from the supplied ZIP and remains ignored. See [assignment verification](docs/assignment-verification.md) for measured results and limits. No universal accuracy or production crash guarantee is implied by a passing local sample.

## System processing budget

The entire app has a hard budget of 20 files sent to AI, shared by single-file and ZIP uploads. Each document entering AI consumes one persistent slot; independent reads, repairs and crash retries share that slot. Cached results and documents classified without AI consume no new slot. Once 20 slots are used, further files are reported as skipped with the processing-limit reason and no AI job is sent. Finished or deleted orders do not replenish the budget. Existing AI-processed files count when the limit is introduced. The upload page shows used/remaining counts; `GET /api/processing-limit` reports the persisted budget.

## Documents and stored data

The document list shows every distinct uploaded file, including ZIP entries that have not been extracted. Repeated identical uploads with the same filename share a list entry; their individual records remain accessible by file ID. Open a document to see its current saved order header, POs, colour/size quantities, dates, rates, values and warnings, then use the existing order form to edit/save. Supporting documents show their stored references and original contents. Unfinished and failed files explicitly show that no extracted order is stored. Originals can be viewed/downloaded; cached duplicates link to the existing order. `GET /api/files/{id}/data` returns current saved data and original extraction separately.

ZIP entries are registered and originals retained before AI processing, within the existing ZIP size/security guards. Spreadsheet previews show up to 100 rows and 40 columns per sheet; download the original for the complete sheet. Original documents stored before this change are restored from retained batch inputs when available; missing originals are reported explicitly. Progress separates actual queued AI reads, unprepared files, retries and failed/cancelled reads.

## One-time queue cleanup

On the first startup with this update, `app.maintenance.clear_existing_queue_once()` cancels existing queued/leased AI jobs, unfinished file extractions and processing batches, preserves saved orders and available original documents, and starts a fresh 20-file allowance. A marker in the same persistent database prevents this cleanup/reset from running again on later deployments. Run `python scripts/clear_existing_queue_once.py` manually against the app's environment to invoke the same guarded operation. Completed batch results waiting for AI are reconciled to cancelled; future uploads are unaffected by repeat invocations.

## Editable supporting document data

Supporting files also use OpenCode to extract structured fields and tables when AI is configured. Tech-pack measurements/construction and BOM materials/consumption remain document data rather than purchase orders. The document page provides editable values, add/remove table rows and a Save document data button. Saves are schema-validated and read back before success is displayed. The original extraction stays preserved separately; manual edits clear AI verification and invalidate older cached data. These reads share the persistent 20-file budget. Classification-only files are freshly extracted on re-upload with AI enabled. Supporting-file AI output is marked as needing source review. `PUT /api/files/{id}/data` saves these values; order documents use the existing order form.
