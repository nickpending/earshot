# Limitless pendant BLE protocol

Everything earshot needs to talk to a Limitless pendant (model LSPT01, FCC ID 2BL99-LSPT01).
Source of truth is BasedHardware/omi (MIT), pinned at
`056936e0ed0667ebb269b0a17fcbcc4c37275f8c` (2026-10-01). Paths below are in that tree. Facts
marked **observed** were measured against the author's pendant on 2026-10-01; the rest are read from
omi's code.

Fetch the reference tree:

```sh
git clone https://github.com/BasedHardware/omi.git omi
git -C omi checkout 056936e0ed0667ebb269b0a17fcbcc4c37275f8c
```

| File | What it is |
|---|---|
| `app/ios/Runner/Limitless/LimitlessProtocol.swift` | Pure Foundation codec: encoders, wrapper/page/status parsers, opus extraction. Vendor this. |
| `app/ios/Runner/Limitless/LimitlessFlashDrainEngine.swift` | The drain state machine (status → download → reassemble → append → ACK → record). Port its logic. |
| `app/ios/Runner/Batch/LimitlessBatchAudioWriter.swift` | omi's spool writer (fsync barrier before ACK). |
| `desktop/macos/Desktop/Sources/Bluetooth/Connections/LimitlessDeviceConnection.swift` | macOS CoreBluetooth connection: init sequence, unpair, realtime stream, battery. |
| `desktop/macos/Desktop/Sources/Bluetooth/DeviceUUIDs.swift` | UUIDs. |
| `app/lib/services/devices/connectors/limitless_connection.dart` | The original Dart connector; button parsing (`_tryParseButtonStatus`). |
| `app/test/fixtures/limitless_golden/*.json` + `app/test/unit/limitless_golden_test.dart` | Golden fixtures for the page parser (truncation, out-of-order fragments, unknown wire types, overflow varints). |
| `app/android/app/src/test/kotlin/com/friend/ios/limitless/LimitlessProtocolTest.kt` | Kotlin parity tests. |

## GATT (observed)

| Service | Characteristic | Properties | Use |
|---|---|---|---|
| `632DE001-604C-446B-A80F-7963E950F3FB` | `632DE002-…` (TX) | write, write-without-response | commands to the pendant |
| | `632DE003-…` (RX) | notify | everything from the pendant |
| `180F` Battery | `2A19` | read, notify | battery percent (read `0x63` = 99%) |
| `8D53DC1D-1DB7-4CD3-868B-8A527460AA84` SMP | `DA2E7828-FBCE-4E01-AE9E-261174997C48` | notify, write-without-response | MCUmgr firmware update. **Never write to it.** |

- Advertised local name: `Pendant` (observed). The advertisement carries the `632DE001` service.
- A connected pendant stops advertising (observed: a second client's scan finds nothing while the
  first is connected).
- ATT MTU negotiated by macOS: 498 (observed).

## Pairing and encryption (observed)

The TX characteristic requires an encrypted link. Writing from an unpaired central fails with
`GATT Protocol Error: Insufficient Encryption` (15). The pendant pairs with one central at a time;
to move it, factory reset it (see `HANDOFF.md`, Pendant operations). After the reset, the Mac's
first write succeeded with no prompt shown, and later connects keep working.

## Framing

All values are protobuf wire format: varint tag `(field << 3) | wireType`; wire type 0 = varint,
2 = length-delimited.

### BLE wrapper (both directions)

| Field | Type | Meaning |
|---|---|---|
| 1 | varint | message index. Outbound: a counter the central owns, +1 per write. Inbound: the reassembly key. |
| 2 | varint | fragment sequence (0-based) |
| 3 | varint | total fragments |
| 4 | bytes | payload (or this fragment of it) |

Outbound writes always send `seq=0, numFrags=1`. Inbound payloads can span fragments: buffer by
index, concatenate by seq once `count == numFrags`.

### Request trailer (msg 30)

Every command payload ends with field 30: `{1: requestId (varint), 2: 0}`. The central owns the
`requestId` counter, +1 per command.

## Commands (central → pendant)

The payload is one command field plus the msg-30 trailer, wrapped.

| Msg | Name | Body | Notes |
|---|---|---|---|
| 6 | SetCurrentTime | `{1: epoch ms}` | Send on every connect. Before it, page timestamps are ms-since-boot (observed: `74964`); after it, epoch ms (observed: `1790896272480` = 2026-10-01 16:11 PDT). Limitless docs: a pendant that needs a clock blinks red 3× and won't resume recording. |
| 21 | GetDeviceStatus | empty | Reply carries storage state (below). |
| 8 | SetMode | `{1: batchMode, 2: realTime}` | `{1,0}` = push flash pages (download); `{0,1}` = live stream; `{0,0}` = record to flash (normal). Always return to `{0,0}` after a drain. Recording continues during `{1,0}` (observed: a 107 s download window held 107.4 s of recorded audio). |
| 7 | AcknowledgeProcessedData | `{1: upToIndex}` | **Deletes every flash page ≤ upToIndex on the pendant.** Never send for a page that isn't fsynced. |
| 15 | UnpairBluetooth | `{1: doNotReset}` | omi uses it to release the pendant. earshot doesn't need it. |

The desktop connector's "enable data stream" is msg 8 with `{1:0, 2:1}`, i.e. live mode.

## Replies (pendant → central)

### Storage state

Inside a notification: wrapper field 4 (`0x22`) → field 5 (`0x2a`) → field 5 (`0x2a`), whose
varints are:

| Field | Meaning | Observed |
|---|---|---|
| 1 | oldest flash page | 0 or absent right after reset |
| 2 | newest flash page | 2 just after reset, 184 about 15 min later |
| 3 | current storage session | 1 |
| 4 | free capture pages | 95,230 / 95,048 |
| 5 | total capture pages | 95,232 |

omi's parser (`parseDeviceStatus`) scans for the byte pattern rather than walking fields. The spike
port of it threw on some unrelated data packets (see `HANDOFF.md`, gotchas); a real port must walk
fields with bounds checks.

### Device status reply, beyond storage (observed 2026-10-06)

The GetDeviceStatus reply's field 4 -> field 5 also carries: field 3 `{1: 5000}` (the VAD
threshold setting), field 4 `100` (LED brightness), field 7 **device mode** (empty while a button long
press has the pendant idle; `{1: 1}` or `{1: 2}` while recording, and the pendant pushes an unprompted
reply when it changes), field 8 `1`, and field 9 **battery** `{2: mV, 3: mA (signed, omitted at 0),
4: degrees C, 5: percent, 6: ?}`. Fixtures: `fixtures/pendant-pages/status-*.notification.hex`. On the
three non-empty replies captured on 2026-10-06, the value tracked the recording (VAD) state: `{1: 1}`
at 15:07:57 while flash logged NOT_RECORDING, `{1: 2}` in the push at 15:08:04 as recording started and
at 15:10:20 while recording. So 1 reads as on and not hearing speech, 2 as on and recording speech;
three samples, not established beyond that.

Earshot.app reads field 7 as: present and empty (or `{1: 0}`) = off, `{1: n}` with n non-zero = on, field
absent = unknown (no claim). It reads field 9's current as absent = not charging. The same reader handles
polled and pushed replies.

### Flash page (download mode)

The reassembled payload is a PendantMessage; field 2 is a StorageBuffer:

| StorageBuffer field | Meaning |
|---|---|
| 2 | session |
| 4 | seq |
| 5 | page index (the ACK key) |
| 6 | flash page bytes (absent on a header-only page; see the parity rule) |

Flash page bytes: field 1 (`0x08`) = timestamp ms (pendant clock), field 2 (`0x10`) = varint, then
repeated field 3 (`0x1a`) audio wrappers. Inside a wrapper, field 2 (`0x12`) holds audio data that
nests length-delimited fields recursively; an opus frame is any length-delimited field of
10–200 bytes whose first byte is a valid TOC: `b8 78 f8 b0 70 f0`.

Parity rule (omi `parseStorageBuffer`, split by Earshot): omi's codec drops any page that has audio
subfields but yields zero frames. Earshot keeps that for a page whose audio field (field 2 inside a
field-3 wrapper) holds a blob of 10 bytes or more that yields no valid frame: it is a parse failure,
never surfaced and never ACKed past. A page with no audio bytes advances the ACK: Earshot surfaces
it with zero frames and its page index, writes no spool record for it, and the drain moves past it.
That covers a page whose audio fields hold only small metadata (under 10 bytes; page 3935, field 2
holding only `48 01`, stalled the ACK for two days before the split) and a page with no flash page
(field 6 absent or empty). Header-only shape, observed on page 49052 (71 consecutive pages,
49052-49122, identical on every cycle for five hours): StorageBuffer fields 1-5 and 7, field 3 = 3,
field 7 = 1, no field 6; what those two values mean is not known. A page with no audio subfields (a
diagnostic page, e.g. 4891) surfaces with zero frames in omi's codec, so the drain advances too.
Every page the drain refuses to ACK past, and every one it surfaces this way, is logged at notice
with its page index.

Observed page sizes: about 72 frames per full page; partial pages occur (1, 20, 44, 54 frames).
Pages are written about 1 per 3 s with speech and more slowly in silence. Silence still produces
pages; recording is not voice-gated.

### Flash page records (observed 2026-10-05)

Decoded from a read-only download of all 5,406 pages on the pendant (49052-54457). Field numbers
are inside each field-3 wrapper of the flash page; field 1 of every wrapper is a varint offset in
ms from the page timestamp (or all-ones when unset). Counts are from that download. earshotd stores a named
record whose payload does not fit the shape below as `unknown` with its hex, so a firmware change to one
record cannot make an upload fail.

| Wrapper field | Count | What it is | Shape |
|---|---|---|---|
| 2 | 334,181 | Audio: opus frames, nested length-delimited (the only record omi reads). A wrapper whose field 2 holds `48 01` (field 9 = 1) and no frame is a marker (page 3935). | bytes |
| 10 | 25,245 | Firmware log line (Zephyr): `{1: {11: "<uptime>] <level> <module>: <text>"}}`, ANSI colour codes included. | text |
| 8 | 13,075 | Accelerometer gravity vector in g, about one per 1.3 s: `{1: {1: x, 2: y, 3: z}}`, fixed32 floats. | floats |
| 11 | 815 | Zephyr thread stats: field 9 repeated `{1: name, 2: stack used, 3: stack size, 4: cpu %}`, plus fields 5-7. | message |
| 6 | 660 | Battery: `{2: millivolts, 3: milliamps (signed; negative = discharging), 4: degrees C, 5: percent}`. Observed 4234 mV, -10 mA, 26 C, 100 % unplugged; +35 mA while charging. | varints |
| 9 | 321 | Recording state: `{}` (off) beside `Recording state set to NOT_RECORDING`, `{1: 1}` beside `RECORDING`. | varint |
| 12 | 643 | Storage session boundary: `{1: 1}` = session start, `{2: 1}` = session end (decoded as `session`). Checked 2026-10-06 over 3,481 records on the author's server: 1,735 of 1,740 end-to-start pairs cross into a new StorageBuffer session (field 2) and 1,728 start-to-end pairs stay in one; a start sits about 1 s before `Session num:N started`, and every end is within 1.6 s of a marker (median 60 ms). Sessions split at each recording-state change. | varint |
| 13 | 332 | A tick about every 60 s inside a storage session, counted from the session's start: 179 of 180 sessions longer than 61 s have one at 60 s (the author's server, 2026-10-06). Payload `{1: 1}` (863) or empty (16), with no split by recording state. Meaning not established; stored as `unknown`. | varint |
| 5 | 16 | Bluetooth link events: `{1: 2}` and `{1: 6}` at connect, `{1: 3, 4: 19}` at disconnect (log: `Disconnected (reason 19)`), `{1: 1}` at boot. | varints |
| 3 | 4 | Button event (decoded as `button`): `{1: 2, 2: 1, 4: n, 6: epoch ms, 8: 800}` and `{1: 1, 2: 1, 3: 1, 4: n, 6: epoch ms, 7: 900}`. Field 1 = 2 is a long press: all 12 such records on the author's server by 2026-10-06 sit beside `Switching to idle mode` or `Waking up from idle mode after event BUTTON_LONG_PRESS`. Field 1 = 1 was seen once, 1.5 s after a wake, so it is some other press. Field 4 counts presses, field 6 equals the record time. Fields 2, 3, 7 and 8 are kept raw. | varints |
| 7 | 1 | USB event (decoded as `usb`): `{1: 1}` at 15:46:34 on 2026-10-06, 1 s before `Scheduling battery metrics update for event USB_CONNECTED` (charger plugged in, page 66064). Unplug not yet seen. | varint |

StorageBuffer field 3 was 2 on 3,916 pages and 3 on 1,419; field 2 (session) took 323 values.

**Header-only pages.** A StorageBuffer with field 7 = 1 and no field 6 is a page whose flash
failed its CRC. The firmware logs `Flash CRC failed: idx N. Buffer is all 0xff` and
`[Batch] -EBADMSG: CRC error on flash page. Send metadata and s...` for each one. Pages
49052-49122 (written before 10:44:41 on 2026-10-05, while spike scripts were connecting) were
blank flash; there was nothing in them to lose.

**Recording off (button).** A long press logs `Switching to idle mode after event
BUTTON_LONG_PRESS` and `Device mode set to IDLE_MODE`; the next long press logs `Waking up from idle
mode after event BUTTON_LONG_PRESS` and restarts audio (`Starting audio with omni and PDM mics
enabled`). Pages keep being written while idle, but they carry no opus frames (pages 61432 and 61440
on 2026-10-06 held 0 frames).

**Recording state (field 9) is not the button.** `Recording state set to RECORDING / NOT_RECORDING`
flips every few seconds (180 times overnight on 2026-10-06), each flip starting a new storage
session; pages on both sides keep carrying about 75 frames, so audio continues through it. It tracks
voice-activity segments, not whether the pendant is recording.

**Settings** (logged at boot): `vad_threshold_ms: 5000`, `led_brightness: 100`,
`allow_recording_on_usb: 0`, `sync_mode: 1`, `show_vad_status_led: 0`, `show_debug_led: 0`.

**Reboot.** The pendant booted once on 2026-10-05 (`Initialization successful`, `Recovered time
from device state`). The last log line before the boot banner is the drain's batch upload
reaching index 49040, next to the CRC-failed run; whether reading those pages crashed it is not
established.

### Button events

Notification shape: field 4 (`0x22`) → field 8 (`0x42`) → field 1 (`0x08`) = event.

| Value | Event | Observed |
|---|---|---|
| 1 | SHORT | yes |
| 2 | LONG | yes; omi: "Limitless uses this to start/stop recording on device" |
| 3 | DOUBLE | yes |

## Audio (observed)

- Codec: Opus, every frame TOC `0xb8`, 35–57 bytes (mean 41.6), 20 ms per frame.
- Encoded at 16 kHz mono (omi's writer names files `opus_fs320_16000_1`).
- libopus via FFmpeg/PyAV decodes to **48 kHz** float planar unless told otherwise; resample to
  16 kHz before Whisper. Treating 48 kHz output as 16 kHz plays it 3× slow and Whisper prints
  "Thank you." over and over.
- About 2 KB/s, so roughly 7 MB per recorded hour.

## Download throughput (observed)

1,748 pages in 107 s, about 16 pages/s, over a macOS CoreBluetooth link at MTU 498.

## Drain cycle (omi `LimitlessFlashDrainEngine`)

Constants: cycle every 90 s, first cycle 5 s after connect, status timeout 8 s, stall 30 s (stall
check every 5 s), ACK every 25 pages.

1. SetCurrentTime, then GetDeviceStatus.
2. If `newest >= oldest`: `endPage = newest`, SetMode `{1,0}`.
3. Per reassembled page: append its frames to the spool. Track `lastAppendedPageIndex`.
4. Every 25 pages: fsync the spool, then ACK `lastAppendedPageIndex`.
5. Finish when `maxSeenPageIndex >= endPage`, on stall, or on append failure: fsync, ACK what was
   written, SetMode `{0,0}`.
