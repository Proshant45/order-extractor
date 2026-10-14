# OpenRouter verification — 2026-10-14

The supplied key authenticated successfully. It was read from a private temporary file for checks and is excluded from Git. OpenRouter reported a free-tier daily allowance of 50 requests. Quota counts API requests, including chunks and repairs, separately from the app's persistent 20-document limit.

## Catalog and model selection

The live catalog contained 14 explicit `:free` variants with all listed prices equal to zero at the time checked. Model availability and account quotas can change. The app revalidates catalog pricing, required text/image inputs and a minimum 32,768-token context before inference; requests additionally enforce zero prompt/completion prices.

| Model | Catalog capabilities | Actual check |
| --- | --- | --- |
| `dots-studio/dots-3-note-preview:free` | Text + images; 512,000 context; JSON output supported | Text and four page images passed; selected as default |
| `google/gemma-4-31b-it:free` | Text + images; 262,144 context; JSON output supported | Catalog checked; fallback routing unit-tested; real extraction not benchmarked |
| `google/gemma-4-26b-a4b-it:free` | Text + images; 262,144 context; JSON output supported | Both probes received provider HTTP 429; not selected as default |

[OpenRouter free variants](https://openrouter.ai/docs/guides/routing/model-variants/free), [account limits](https://openrouter.ai/docs/api/reference/limits), and [provider price constraints](https://openrouter.ai/docs/guides/routing/provider-selection) describe the supported routing/limit controls.

## Real API checks

A controlled synthetic PO had known buyer, PO, style, S/M quantities, line quantity, rate, line value, printed totals and destination. Dots returned JSON with HTTP 200, finish reason `stop` and reported cost zero.

| Input | Time | Outcome |
| --- | --- | --- |
| Text probe | 27.9 s | 11/11 expected values/arithmetic checks passed |
| Four separate page images in one request | 27.5 s | 11/11 checks passed; all images contributed source fields |
| App upload through real API | 22.8 s including CRUD checks | One read, `text_openrouter_checked`, 11/11 checks passed |

The isolated app check used a temporary database and authenticated API requests: upload, extract, read stored draft, edit quantities/buyer, save, read back exact changes, delete, and verify HTTP 404. Direct API status and rendered dashboard were checked. It consumed one document budget slot and did not use a worker heartbeat.

## Recovery checks and limits

Provider unit tests cover rejecting paid/nonzero-priced models, preserving all four images, caching completed reads, direct progress, worker exclusion, quota cooldown, catalog outages, free fallback, embedded API failures, truncated output, and rejecting unpersisted stale-lease results. The persistent 20-file budget and existing extraction/CRUD tests remain regression checks.

The final regression suite passed **104 tests**. Python compilation, JavaScript/shell syntax and `git diff --check` passed. Independent source/mock review approved the backend/provider after the outage, stale-acknowledgment and fallback-cache findings were fixed.

These probes are synthetic, not an accuracy benchmark of the entire GEAL ZIP. Passing arithmetic checks cannot prove every source field correct. Real free-model latency varies, and the fallback model may also be busy. Persistent storage is required for restart recovery.

Verification level: unit-tested and real external API extraction/CRUD tested in an isolated app. The new OpenRouter deployment on Render remains unverified until the user adds `OPENROUTER_API_KEY` and deploys.

## Follow-up: actual failed GEAL files

Read-only Render logs confirmed both the ALDI AMD-02 spreadsheet and BESTSELLER BOM failed on incomplete replies. The ALDI tech pack had already stored 81 fields, 6 tables and 15 table rows; its `classified` database status did not mean classification without extracted data.

A bounded request using the actual ALDI text returned `finish_reason=length`, empty content and 1,576 reasoning tokens within a 2,048-token output allowance. The model catalog advertised optional reasoning. Disabling it produced complete zero-cost replies with zero reasoning tokens on the actual BOM and ALDI sources. OpenRouter documents that reasoning shares the output budget in its [reasoning guidance](https://openrouter.ai/docs/guides/best-practices/reasoning-tokens).

The complete BOM response exposed a separate AI column-alignment error. The app now preserves the original workbook cells directly when spreadsheet table validation fails, including native numeric values, original row numbers, embedded tabs/newlines and blank positions. The actual BOM was stored and read back with 12 fields and 93 nonempty native source rows. Cached completed responses were reused during follow-up checks.

The actual ALDI contract produced a stored draft after two complete zero-cost reads. Read-back totals were 672,389 pieces and 2,290,543.968 value, matching the printed totals. It remains `needs_review` because pack/unit rates and repeated-PO date/total conflicts are flagged; matching document totals are not presented as proof of full accuracy.

These write/read checks used an isolated temporary database. Startup recovery is regression-tested to schedule only the old truncation failures with retained inputs and preserve their existing budget slots. Production retry results after this follow-up deployment have not yet been verified.
