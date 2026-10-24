# HarnessLink implementation record

Source: the approved plan in the Codex conversation, 2026-10-20.

Goal: USB-powered ESP32-S3 push-to-talk terminal, English local STT/TTS,
Wi-Fi audio, persistent local Codex app-server conversation, brief spoken replies.

Tasks:
- [x] Bounded audio protocol and tests.
- [x] Codex stdio client, persistence, approvals and tests.
- [x] Speech adapters, WebSocket bridge, provisioning and diagnostics (source).
- [x] ESP-IDF firmware: provisioning, audio, button, transport, LED (source).
- [x] Documentation, simulated integration checks and independent code review.
- [ ] Compile, flash, and physical audio verification when tools/device are accessible.

Ruling: work in the user-provided HarnessLink directory; it contains only the
two source photos and is not a Git repository. No worktree or commits apply.

Ruling: preserve Codex permissions by omitting approval/sandbox overrides;
handle requests in the PC terminal. No spoken authorization of privileged actions.

Ruling: firmware must fit the conservative 4 MB flash profile and not require
PSRAM until actual flash/PSRAM identification is possible.

Environment: USB is visible in sysfs (303a:4001, cdc_acm, ttyACM0) but /dev/ttyACM0
is not exposed. ESP-IDF and Xtensa toolchain are absent. Shell DNS is restricted.
Installed Codex 0.163.0 schema was generated into /tmp for protocol verification.

Verification: `bash tools/check.sh` passes 24 Python tests and the host C primitive
test. `bash -n tools/*.sh` and Python compileall pass. Real subprocess tests cover
RPC responses, approvals, conversation persistence, failed/interrupted turns,
uncertain-interruption fencing, process death, and descendant pipe cleanup.
Device transport and speech models are simulated in session tests.

Independent review checked the pinned WebSocket header and I2S APIs. It identified
an interruption ambiguity; a regression test reproduced it and the client now
blocks further commands until uncertain state is inspected on the PC.
Final review also corrected esptool probe commands to the underscore spelling
required by ESP-IDF 5.5's esptool 4.x dependency. Local IDF setup selects a base
Python 3.11 interpreter rather than the host's Python 3.14 or an activated venv.

Physical verification remains blocked: build/flash script exits before mutation
because `/dev/ttyACM0` is absent; no ESP-IDF build or physical audio was completed.
The live Codex initialization probe was stopped without a response in this restricted
environment. Only fixture-based runtime RPC and generated-schema inspection are verified.
Real speech models and LAN WebSocket operation remain untested. The PC setup scripts
download dependencies/models and the ESP-IDF toolchain when run outside this sandbox.
