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
