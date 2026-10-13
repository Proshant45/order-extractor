#!/usr/bin/env bash
# Render build: python deps + (optional) opencode CLI binary into ./bin for EXTRACTOR=opencode
set -euo pipefail
pip install -r requirements.txt
if [ "${INSTALL_OPENCODE:-1}" = "1" ]; then
  mkdir -p bin .ochome
  curl -fsSL https://opencode.ai/install | HOME="$PWD/.ochome" bash -s -- --no-modify-path
  cp .ochome/.opencode/bin/opencode bin/opencode && chmod 755 bin/opencode
  rm -rf .ochome
  ./bin/opencode --version
fi
