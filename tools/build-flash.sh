#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
port="${1:-/dev/ttyACM0}"
if [[ ! -c "$port" ]]; then
    echo "Serial device is not exposed: $port" >&2
    echo 'Run this from a PC terminal with USB access; use ls /dev/serial/by-id/ to identify the board.' >&2
    exit 1
fi
if ! command -v idf.py >/dev/null; then
    if [[ ! -f .tools/esp-idf/export.sh ]]; then
        echo 'ESP-IDF unavailable. First run: bash tools/install-idf.sh' >&2
        exit 1
    fi
    export IDF_TOOLS_PATH="$PWD/.tools/esp-tools"
    idf_python="$(uv python find --system --no-project 3.11)"
    export PATH="$(dirname -- "$idf_python"):$PATH"
    source .tools/esp-idf/export.sh
fi
cd firmware
# Identify hardware, then compile before performing the authorized full erase.
python -m esptool --chip esp32s3 --port "$port" chip_id
python -m esptool --chip esp32s3 --port "$port" flash_id
if [[ ! -f sdkconfig ]]; then
    idf.py set-target esp32s3
fi
idf.py build
idf.py -p "$port" erase-flash flash
echo 'Firmware flashed. Close serial monitors and run harnesslink provision from the project root.'
