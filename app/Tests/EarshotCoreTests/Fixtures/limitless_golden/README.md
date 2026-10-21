# limitless_golden

The `*.json` files here are omi's golden fixtures, copied unchanged from BasedHardware/omi at
`056936e0ed0667ebb269b0a17fcbcc4c37275f8c`, `app/test/fixtures/limitless_golden/` (MIT, see
`Sources/EarshotCore/OMI-LICENSE-NOTICE.md`). They stay `.json`: the repo ignores `*.bin`.

`CodecGoldenTests.swift` replays each fixture: `packets` (hex) go through the BLE wrapper parser, fragment
reassembly and the pendant-message parser, and the surfaced pages must equal `expectedPages`. `encoders.json` pins the
byte-exact TX vectors. Fixtures with empty `expectedPages` and audio subfields (`audio_page_zero_frames`,
`fixed64_fields_page`, `unknown_wiretype_page`) are the pages that must never surface, so they are never ACKed.
