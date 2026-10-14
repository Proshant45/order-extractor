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
4. **AI read**: each order document is read by opencode free models and saved as an order straight away (read A).
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
- `worker/tick.py`: AI-less check, one log line per run (`worker/tick.log`, rotated). 0 pending -> exits; otherwise runs
  `worker/box_worker.py --drain`, which runs `opencode run` locally per job and exits when the queue is empty.
- Schedule: `* * * * * flock -n /tmp/oe_tick.lock .venv/bin/python worker/tick.py` in crontab, or `nohup worker/tick_loop.sh &` where cron is unavailable.

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
