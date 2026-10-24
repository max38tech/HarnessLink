#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
PYTHONPATH=bridge "${PYTHON:-python3}" -m unittest discover -s tests -v
test_binary="$(mktemp /tmp/harnesslink-core.XXXXXX)"
trap 'rm -f -- "$test_binary"' EXIT
gcc -std=c11 -Wall -Wextra -Werror -Ifirmware/main tests/test_firmware_core.c -o "$test_binary"
"$test_binary"
echo 'Python behavior tests and portable firmware primitives passed. This does not compile ESP-IDF firmware.'
