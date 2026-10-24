#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
mkdir -p .tools
export IDF_TOOLS_PATH="$PWD/.tools/esp-tools"
command -v uv >/dev/null || { echo 'Run tools/setup.sh first to install the Python environment.' >&2; exit 1; }
idf_python="$(uv python find --system --no-project 3.11)"
export PATH="$(dirname -- "$idf_python"):$PATH"
if [[ ! -d .tools/esp-idf ]]; then
    git clone --branch v5.5.1 --recursive --depth 1 https://github.com/espressif/esp-idf.git .tools/esp-idf
fi
.tools/esp-idf/install.sh esp32s3
echo 'ESP-IDF installed locally. Run: bash tools/build-flash.sh /dev/ttyACM0'
