# Order Extractor

Upload purchase orders, sales contracts, order sheets, invoices, control sheets, tech packs and BOMs individually or as a ZIP. Order documents become stored, editable drafts with headers, POs, colour/size quantities, dates, rates and values. Supporting documents have editable fields and tables. Login is required.

## OpenRouter setup

Set `OPENROUTER_API_KEY` in Render Environment and deploy the latest commit. The key takes priority over old `AI_QUEUE` and `EXTRACTOR` settings. Render calls the API directly; no separate worker machine is needed. Keep the key in the environment, never in Git.

The default text and image model is `dots-studio/dots-3-note-preview:free`. Override with `OPENROUTER_MODEL` or `OPENROUTER_VISION_MODEL`. Every requested model must have an explicit `:free` ID, zero pricing for every listed catalog component, and the required input capabilities. Requests also cap prompt/completion prices at zero. Paid models are rejected before inference. Provider outages can fall back to `google/gemma-4-31b-it:free`, subject to the same checks; `OPENROUTER_FALLBACK_MODEL` overrides it.

The supplied account reported a **50-request daily free quota** during verification. Each chunk, repair or fallback uses an API request; the separate 20-file budget counts documents. Free models can be busy or rate-limited. Files remain visible as waiting for AI and are retained for automatic retry after a persisted cooldown. Truncated output is rejected.

See [OpenRouter verification](docs/openrouter-verification.md) for measured results and limitations.

## Extraction and progress

PDFs use up to four pages per chunk, split where a new PO starts when text headers are readable. Dense pages use smaller groups to respect the 30,000-character text cap. Scanned chunks include every page image in order. Long spreadsheet/text inputs are split with rows retained. Configure `AI_CHUNK_PAGES` to 3 or 4 and `AI_CHUNK_MAX_CHARS` as needed. The default prepares and processes one chunk at a time.

Normal chunks use one read. Invalid output or failed checks triggers one repair read. Checks include size sums, quantity × rate, printed totals, PO coverage and recognised deterministic layouts. Whole-PO totals are checked again after merging. Completed reads are reused during file retries. `checked` means the available checks passed; it does not guarantee every source field is correct. Unresolved mismatches remain visible for review.

Optional model reasoning is disabled for extraction when supported, keeping output capacity available for JSON. Empty, truncated or invalid JSON can use one verified free fallback; partial output is rejected and errors include the model's finish/token diagnostics. Supporting-document schema failures get one chunk re-read. For XLS/XLSX table layout failures, the app instead stores all nonempty native workbook rows with original row numbers and column letters, preserving blank positions and multiline cells. These editable source tables replace AI tables and carry a review warning.

On deployment with OpenRouter enabled, old truncation failures with retained originals are scheduled for retry using their existing processing slots. Saved orders, unrelated failures and the completed tech pack remain intact.

The upload page streams actual progress through SSE, with polling after reconnects. It shows requested pages, model, start/return times, failed reads, repairs and stored results. Counters track requests and files, not guessed model progress.

## System processing budget

The app has a persistent budget of **20 files sent to AI**, shared by all single-file and ZIP uploads. A document consumes one slot; chunks, repairs, provider changes and crash retries share that slot. Cached results and classification without AI consume no new slot. Once 20 are used, further documents are skipped with the limit reason. Finishing or deleting an order does not replenish the budget. The upload page and `GET /api/processing-limit` show used/remaining counts.

## Documents and CRUD

The Documents list links every distinct uploaded file, including ZIP entries awaiting extraction. Repeated identical uploads with the same filename are grouped; records remain accessible by ID. Order documents show their stored header, POs, quantities, values and warnings, with a link to the order editor. Supporting documents show structured fields and tables that can be edited, added, removed and saved. Original contents are available to view/download.

Saves are schema-validated and read back before the UI displays success. Manual edits clear AI verification and invalidate the old source cache. Original extraction is preserved separately. Failed and unfinished files explicitly show that no final extracted data is available. Spreadsheet previews show up to 100 rows and 40 columns per sheet; download the original for the complete sheet.

Uploads are streamed to disk. Defaults allow 25 MB per single file and 100 MB per ZIP. ZIP guards cover entry/total sizes, compression ratios, traversal, encryption and nesting. Corrupt or unsupported entries receive an explicit reason. Entries are indexed and originals retained before processing. Interrupted batches replay after restart when their input and database remain on persistent storage.

## Deploy on Render

The blueprint uses a persistent disk, `/healthz` health check and a single web process. Configure `APP_USERNAME`, `APP_PASSWORD_HASH` (bcrypt), `SECRET_KEY`, `OPENROUTER_API_KEY` and `DATA_DIR`. Defaults are `EXTRACTOR=openrouter` and `AI_QUEUE=0`. OpenCode is not installed by the default build.

On a Render plan without persistent storage, SQLite and retained inputs are lost on restart/redeploy; retry cannot recover lost storage. The API key must be added through Render Environment, not committed to the repository.

## API

- Orders: `GET /api/orders?q=`, `POST /api/orders`, `GET/PUT/DELETE /api/orders/{id}`.
- Upload: `POST /api/upload?wait=0` returns a tracked batch; `GET /api/batches/{id}` returns its outcome.
- Structured extraction: `POST /api/extract?wait=0`, then `GET /api/batches/{id}?view=api`.
- Stored document data: `GET /api/files`, `GET /api/files/{id}/data`, `PUT /api/files/{id}/data` for supporting documents.
- Source: `GET /api/files/{id}/source` (append `?download=1`).
- Progress/status: `GET /api/progress?bid={id}`, `/api/batches/{id}/events`, `/api/ai-status`, `/api/processing-limit`.

## One-time queue cleanup

`app.maintenance.clear_existing_queue_once()` cancels existing queued/leased reads, unfinished files and processing batches on its first invocation. It preserves saved orders and available originals and resets the 20-file allowance. A persistent marker prevents repeated cleanup on later deployments. `python scripts/clear_existing_queue_once.py` invokes the same guarded operation manually. The OpenRouter migration does not request another database wipe.

## Run and test

```bash
pip install -r requirements-dev.txt
uvicorn app.main:app --reload
pytest -q
```

Tests use temporary databases and disable real API keys by default. The sample PO fixture is gitignored; use `SAMPLE_PDF` when needed.

## Optional legacy OpenCode worker

Explicitly set `EXTRACTOR=queue`, `AI_QUEUE=1`, `WORKER_TOKEN` and remove `OPENROUTER_API_KEY` to use an existing box setup. `worker/.env` requires `APP_URL`, `WORKER_TOKEN`, `APP_ACCOUNT` and legacy model settings. `worker/tick_loop.sh` checks every 15 seconds and drains jobs with `worker/box_worker.py`. `AI_READ_MODE=dual` enables legacy independent reads. This mode is optional; OpenRouter does not call OpenCode. Legacy local verification scripts remain in `scripts/`.
