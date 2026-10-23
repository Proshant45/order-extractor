#!/usr/bin/env bash
# Render build: python deps only. The optional Codex backend (EXTRACTOR=codex) runs on the box worker,
# which uses its own `codex login`; Render itself calls OpenRouter directly.
set -euo pipefail
pip install -r requirements.txt
