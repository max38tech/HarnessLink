#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
command -v uv >/dev/null || { echo 'Install uv, then rerun this script.' >&2; exit 1; }
uv venv --python 3.11 .venv
uv pip install --python .venv/bin/python -e .
.venv/bin/harnesslink init
.venv/bin/harnesslink download-models
echo 'PC bridge installed. Next: firmware build/flash and USB provisioning; see README.md.'
