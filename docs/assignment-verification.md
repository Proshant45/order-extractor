# Assignment verification

Status: local implementation, unit tests, and real HTTP/browser writes verified. User approved commit and push on2026-10-14. Render deployment and remote Grokbot rollout remain unverified.

The screenshot supplied on2026-10-14 is the form reference. The supplied GEAL ZIP contains58files. The corpus audit is in ignored `data/acceptance/dataset-audit.json`; retained local input/results are under ignored `data/local` and `data/acceptance`.

Local app: `http://127.0.0.1:8765`. Start with `.venv/bin/python scripts/local_stack.py`. It loads ignored `.env`, runs the existing pull worker structure, checks every15seconds while idle, and restarts the local app/loop if they stop. Local login credentials are `APP_USERNAME` and `LOCAL_PASSWORD` in `.env`; never copy these into version control. The remote worker must receive the updated source and dependency manifest separately.

Verified outcomes:
- Browser create draft, edit/save, delete and404read-back. Row duplication/deletion, hidden-size retention, quantity-only lines, printed quantity preservation, image persistence, desktop/mobile layout. Reference screenshots: ignored `screenshots/order-form.png` and `screenshots/order-form-mobile.png`.
- Real worker plus HTTP extraction/read-back of Aditya Birla PO3110121365:72/72checked fields passed, including30size cells, five line quantities/rates/values, PO totals and principal headers.132.4seconds from submission to final batch response. The result correctly remains `needs_review` because independent destination/department text differs. This is one source-checked document, not a claim of universal AI accuracy.
- Actual local serverSIGKILL during a queued extraction: retained batch recovered and saved/read-back verified buyer and quantity30in28.5seconds. `data/acceptance/recovery.json` records it. The QAorder was deleted and404verified afterward.
- SSE returned200and streamed progress/complete in the browser. Observed1/2AIreads returned while the other was running, then2/2and order saved. Progress uses actual claimed leases and OpenCode step/text events; read totals count submitted attempts and may increase during repairs. File counts distinguish saved/classified/skipped/failed/queued outcomes.
- Full58-file real extraction run is tracked as batch `28253c0afdb5f936`; see the live UI/API for its current state. Do not imply finished/accurate until its final results are reviewed.

Run `.venv/bin/python -m pytest -q`. Tests require an environment that permits TestClient's thread/event-loop bridge; the managed sandbox hung at startup here, while the same suite passed outside it. Current installed FastAPI/TestClient emit deprecation warnings.

Timing varies on free models. A trivial JSON response took4seconds. The six-page PO took114.4seconds initially (70/72header-inclusive checks), then132.4seconds after fixes (72/72). Mimo's independent benchmark was58.5seconds with71/72exact checks (department apostrophe difference); live Mimo took124seconds. Choose on accuracy and observed reliability, not a promised latency.

Recovery requires persistent `DATA_DIR`. The Render blueprint specifies a persistent disk; actual deployed disk/plan is unverified. Ephemeral storage cannot preserve uploads/orders across an instance replacement. No machine-power-loss or production network-fault guarantee has been established.

OpenCode `--format json` emits CLIevents; it does not guarantee the model's answer conforms to the order schema. Completed answers are parsed, schema-validated and checked separately. [CLI documentation](https://opencode.ai/docs/cli/).

Manual QA still needed: field-level ground truth for the other buyer documents; actual Render/Grokbot rollout and persistent-disk configuration; long offline/reconnect behavior on the deployed host. Some missing/ambiguous source fields require a person to complete them.

Reference controls corrected after user review: native Buyer/Brand/Factory/Department/Size/Destination/Color and currency dropdowns, calendar inputs and SEE COLOR control. Choices come from existing saved orders and allow new entries. Dropdown selections, raw dates, custom values and hidden quantities passed DOM checks and a real local API write/read-back.62tests passed before commit preparation. Final visual comparison of these dropdown revisions was not browser-verified.
