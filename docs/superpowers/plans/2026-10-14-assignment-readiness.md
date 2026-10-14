# Assignment readiness implementation plan

Goal: match the supplied Texora order form and verify real document processing with the same local pull worker used on Grokbot.

Stack: existing FastAPI, SQLite, vanilla JavaScript, OpenCode CLI. Work stays in this checkout; no commit, push, or deployment without explicit approval. Accuracy and recovery take precedence over latency. The screenshot and 58-file ZIP supplied by the user are the acceptance references.

Current / next / later:
- Current: reproduce CRUD, validation, lease and restart failures with tests.
- Next: implement fixes, align the order form and run the full suite and browser checks.
- Later: run local HTTP app plus 15-second worker, measure real model results against manually checked sample values, exercise restart and delivery failure recovery.

Tasks:
1. CRUD and correctness (app/schema.py, app/db.py, app/main.py): distinct draft/saved state; strict quantities; preserve raw dates; prevent expired leases accepting results; expose uncertain AI agreement and compare separate styles correctly. Test create/update/read/delete, invalid fractional counts, expired renew/result and independent reads.
2. Durable uploads (app/db.py, app/main.py): store batch input and metadata on persistent DATA_DIR before starting processing, restart interrupted batches from retained files, cache already completed entries, retain queued_ai outcomes in progress. Test interrupted batch replay and no loss of uploaded bytes.
3. Worker (worker/*.py and tick_loop.sh, app/opencode_provider.py): 15-second polling, heartbeat while idle, validated delivery acknowledgement, bounded process output/read deadlines and clean process groups. Test delivery failures and partial/malformed output. Local secrets remain ignored.
4. UI (templates and static): screenshot proportions, sidebar, functional image input and auto-extraction, date inputs, editable quantity fallback and line duplication. Preserve incomplete and failed states; check CRUD and uploads through browser and API.
5. Acceptance: restore original ignored PO fixture from supplied ZIP; run all tests; extract representative documents with real OpenCode and compare known header/PO/quantity values; inspect the full ZIP for classification/truncation risk and retain reports. Record exact verification level and remaining limitations.

Review focus: no fabricated quantities, no masked AI disagreements, no silent truncation, no lost batches after restart, no false success on failed result delivery.

Progress extension requested during execution: stream durable, lease-validated worker stages over SSE at /api/batches/{id}/events. Show actual reads submitted/returned, pages and worker identities; no fabricated percentage or token progress. Browser reconnects and reconciles through existing progress/batch read APIs. Expose received/start/output/return/save separately.

Verification ledger: original58-file corpus inspected; first live PO70/72 exact checks, numeric lines correct,114.4s. Real serverSIGKILL recovered retained extraction, correct qty30 saved/read-back in28.5s. Free reader benchmark: Mimo58.5s71/72 (department punctuation), Step582.6s70/72. Keep Big Pickle A and choose measured Mimo B; recognised SAP parser provides confirmed style/department header, independent disagreements remain visible. 54tests passed before final empty204 regression. Final live PO and fullZIP replay underway.
