# HarnessLink protocol v1

Transport: one device on an IPv4 LAN WebSocket, port 8765 by default.
No compression. JSON control messages are UTF-8 text frames. Audio messages
are unfragmented binary frames containing raw 16 kHz mono signed PCM16 little-endian.
The bridge caps each received message at 64 KiB and the incoming queue at four frames.
Firmware accepts server messages up to 3,200 bytes and reassembles transport-buffer chunks.

Device authenticates within ten seconds:

```json
{"type":"hello","version":1,"token":"PRIVATE_DEVICE_TOKEN"}
```

Invalid authentication closes with 1008. A second connected device closes with 1013.
On success, bridge sends `{"type":"ready","version":1,"sample_rate":16000}`.
Credentials are never put in URLs. This prototype uses plaintext `ws://` over a trusted
home LAN; the token and audio are not protected against a LAN observer. No port forwarding.

## Recording

1. Device sends `{"type":"record_start","id":1}`; IDs increase within a connection.
2. Bridge interrupts and waits for previous work to finish, then sends `{"type":"recording","id":1}`.
3. Only after that acknowledgment, device sends binary PCM frames, normally 640 bytes / 20 ms.
4. Device sends `{"type":"record_end","id":1}` on release or at the 60-second limit.

`record_cancel` discards partial audio. The bridge rejects odd byte counts, recordings
over 1,920,000 bytes, and recordings left open beyond 65 wall-clock seconds.
Normal mode ignores recordings shorter than 250 ms or with RMS below 100 after DC removal.
Diagnostic capture mode retains silence so microphone faults can be inspected.

The bridge sends `status` with `state` equal to `transcribing`, `processing`,
`synthesizing`, or `approval`. Error controls have `type:error` and a PC-directed
`message`; recoverable errors are followed by `ready`.

## Playback

1. Bridge sends `{"type":"play_start","id":1,"sample_rate":16000,"bytes":4000}`.
2. Bridge sends up to 3,200 binary bytes (100ms); device writes duplicated stereo
   samples to the amplifier, then sends `{"type":"play_ack","id":1,"seq":0}`.
3. Bridge sends the next chunk only after the exact ID/sequence acknowledgment.
4. Bridge sends `{"type":"play_end","id":1}` after the last acknowledgment.
5. Device drains output and sends `{"type":"play_done","id":1}`; bridge sends `ready`.

Acknowledgments mean bytes were accepted into the I²S output path. `play_done` means
the bounded DMA queue has drained. Playback waits at most five seconds per acknowledgment.
BOOT immediately clears device playback and requests a new recording. The bridge sends
`play_cancel` before acknowledging the recording. Binary frames already in transit are
ignored by the device while it is waiting to record.

On disconnect, both sides discard incomplete recordings and playback. The bridge attempts
to interrupt an active Codex turn. If completion cannot be confirmed, it blocks further
commands until the PC thread has been inspected and the bridge restarted. No audio or
Codex command is automatically replayed on reconnect.
