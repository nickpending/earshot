# Real pendant pages

StorageBuffer messages (PendantMessage field 2) captured read-only from the author's pendant on
2026-10-05, one hex string per file. Field 5 is the page index, field 6 the flash page.

| Page | What it is |
|---|---|
| 3935 | marker: one audio field (field 2) holding only a flag, no opus frame |
| 3936 | audio: 1 opus frame |
| 3937 | audio: 72 opus frames |
| 4891 | telemetry: Zephyr thread stats, no audio field |
| 49052 | header only: StorageBuffer fields 1, 2, 3, 4, 5 and 7 (field 3 = 3, field 7 = 1), no flash page (field 6); captured 2026-10-05 17:25, one of 71 in a row (49052-49122) |
| 49123 | audio: the first normal page after that run (field 3 = 2, field 6 present) |
| 61432 | recording off by button: 0 opus frames, log lines `Switching to idle mode after event BUTTON_LONG_PRESS` and `Device mode set to IDLE_MODE`, a button record (wrapper field 3), battery; captured 2026-10-06 10:02 |
| 61433 | recording back on: log `Waking up from idle mode after event BUTTON_LONG_PRESS`, then audio restarts |
| 66064 | charger plugged in: a USB record (wrapper field 7, `{1: 1}`), then log `Scheduling battery metrics update for event USB_CONNECTED`; exported from the server's `raw_pages` on 2026-10-06 |

## Synthetic audio

The opus frames in the five audio pages (3936, 3937, 49123, 61433, 66064) are synthetic silence: no
byte of the captured audio is in the repository. `scripts/silence-pendant-audio.py` replaced every
frame with one 41-byte silent CELT frame (`b8` then forty `ff`) and rewrote the length prefixes on
the way to each; everything else is as captured, and `records.decode` gives the same session, page
index, page timestamp, records and frame count. Sizes in bytes, original then new: 3936 922 -> 916,
3937 4016 -> 3803, 49123 4012 -> 4003, 61433 4049 -> 3958, 66064 4047 -> 4004. A newly captured page
with audio is run through the script before it is committed
(`uv run --project earshotd python scripts/silence-pendant-audio.py`); `earshotd/tests/test_fixture_audio.py`
fails on any other frame in this directory.

`fixtures/say-test.pages.v1` (the macOS `say` voice through the pendant) stays: diarizing all 48 s of
it on the server found one speaker at cosine 0.048 against the operator's enrolled voice (his real
labels score 0.64-0.91, threshold 0.5).

Page 3935 stalled Earshot.app's ACK for two days: omi's codec reads its audio field as a parse
failure (audio subfields, zero frames), so it is never surfaced and the drain never ACKs past it.

Page 49052 stalled the drain again from 12:30 on 2026-10-05: with no field 6 there are no audio bytes
to parse, but marker-pages' classifier needs a flash page and treated it as a parse failure.

## Device status replies (GetDeviceStatus, msg 21)

Whole RX notifications (BLE wrapper included), 2026-10-06. Inside wrapper field 4 -> field 5:
field 5 is storage (as omi reads it), field 7 is the device mode (empty while the button has the
pendant idle, `{1: 1}` or `{1: 2}` while recording), field 9 is battery `{2: mV, 3: mA, 4: degrees C,
5: percent, 6: ?}`.

| File | State |
|---|---|
| status-recording.notification.hex | recording, captured raw at 15:10 (field 7 = `{1: 2}`) |
| status-idle.notification.hex | idle after a long press at 15:04; re-encoded from the decoded capture taken at 15:06 (60 bytes, the original's length), because the raw bytes were not saved |
