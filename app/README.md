# Earshot.app

Menu-bar app that owns the Limitless pendant: connects over Bluetooth, sets the pendant clock, shows
status, drains flash pages into an fsynced local spool, ACKs only what is durable, and uploads the spool to earshotd.
Design: `DESIGN.md` (Earshot.app, Upload contract) and `docs/protocol.md`.

## Layout

| Path | What |
|---|---|
| `Sources/EarshotCore` | Everything testable, no UI and no CoreBluetooth: vendored omi codec, `DrainEngine`, `Spool`, `Uploader`, `Config`, the display side (`PageDigest`, `StatusFacts`, `Alerts`), the `PendantTransport` boundary. |
| `Sources/Earshot` | The app: SwiftUI `MenuBarExtra`, `CoreBluetoothTransport`, `StatusModel`, `Notifier` (UserNotifications), `LoginItem` (SMAppService). |
| `Resources` | `Info.plist` (`NSBluetoothAlwaysUsageDescription`, `LSUIElement`) and the Bluetooth entitlement. |
| `Tests/EarshotCoreTests` | Swift Testing suites; omi's golden fixtures under `Fixtures/limitless_golden`. |
| `../scripts/build-app.sh` | Builds and signs `Earshot.app`. |

`Sources/EarshotCore/LimitlessProtocol.swift` is vendored from omi at `056936e0` (MIT, see
`OMI-LICENSE-NOTICE.md`). Do not edit it; change behaviour around it.

## The ACK rule

Pendant msg 7 deletes the pendant's copy of every page up to the index. `DrainEngine` writes it only after
`sink.sync()` (fsync of the spool segment) returned, and only up to the last page of the contiguous run of pages it
received and saved: a page whose notification was lost, or whose index (StorageBuffer field 5) cannot be read, is
never ACKed past. Every page is saved as raw bytes and none is classified. Every way a cycle ends
(caught up, stall, append failure, fsync failure) sends SetMode `{0,0}` so the pendant keeps recording.

## The menu

`StatusModel` decodes what the menu shows from the pages the drain already saved: `PageDigest` reads battery records,
opus frames and the button's idle/wake lines; `StatusFacts` keeps the rolling picture and renders the lines;
`Alerts` fires the three notifications (battery <= 15 %, no audio for 10 minutes, no upload for 6 hours) once per
condition. The drain never calls the decoder, and the pendant is sent nothing between cycles. A cycle ends as caught up
when every page below the newest is in hand and the link is quiet for 5 s. See `DESIGN.md`, Earshot.app.

## Config

`~/.config/earshot/config.toml`, the only source of the server and the token:

```toml
server_url = "https://earshot.example.com"
token = "<upload-scope token from /etc/earshot/earshotd.toml on the server>"
```

`server_url` must be `https` unless earshotd runs on this Mac (`127.0.0.1`, `localhost`, `::1`): the app refuses
plain `http` to any other host, since every upload carries the token.

The file holds the token, so it must be owned by you and readable by no one else (`chmod 600 ~/.config/earshot/config.toml`).
The app refuses a config that another user owns or that any group or other permission bit is set on, before it parses
a key, and the menu shows `Config:` with the path, the mode and the fix. It never changes the file's mode or contents.

## Spool

`~/Library/Application Support/Earshot/spool/`: append-only `seg-<n>.pages.v2.open` segments of v2 records,
`{u32 little-endian length, raw StorageBuffer}`, each page's bytes exactly as the pendant sent them. The drain never
interprets a page; earshotd decodes it for storage. A cycle closes the active segment (`.pages.v2`), and the uploader POSTs
closed segments oldest first as `application/vnd.earshot.pages.v2`, deleting each only after a 200 whose
`stored + duplicates` equals its record count. Segments written in v1 before the update (`.pages.v1`;
`fixtures/say-test.pages.v1` is the reference) still upload, as `application/vnd.earshot.pages.v1`.
Failures keep the segment and retry with doubling backoff (5 s up to 15 min); the menu shows the backlog.

## Test

```sh
cd app
swift format lint --strict --recursive .
swift build
swift test
```

On Command Line Tools without Xcode, plain `swift test` cannot find `Testing.framework`; pass the framework flags
(derive them from `xcode-select -p`, as below):

```sh
D="$(xcode-select -p)/Library/Developer"
swift test -Xswiftc -F -Xswiftc "$D/Frameworks" -Xlinker -rpath -Xlinker "$D/Frameworks" -Xlinker -rpath -Xlinker "$D/usr/lib"
```

Tests inject a fake pendant (`FakePendant`) and a loopback fake of `POST /v1/pages` (`FakeEarshotd`); they never touch
Bluetooth, the keychain or the network.

## Build and sign

```sh
scripts/build-app.sh          # writes dist/Earshot.app
```

The script builds release, assembles the bundle, and signs it with the self-signed `Earshot Signing` identity, creating
it on first run in `~/Library/Keychains/earshot-signing.keychain-db` with a generated password kept 0600 in
`~/.config/earshot/signing-keychain-password`. A stable identity keeps the code requirement, and so the Bluetooth
grant, the same across rebuilds; an ad-hoc signature changes every build. The bundle is signed with the hardened
runtime (`--options runtime`) and the script fails the build if the signed bundle lacks the runtime flag. The keychain
joins the user search list only while `codesign` runs.

| Variable | Default |
|---|---|
| `EARSHOT_KEYCHAIN` | `~/Library/Keychains/earshot-signing.keychain-db` |
| `EARSHOT_KEYCHAIN_PASSWORD_FILE` | `~/.config/earshot/signing-keychain-password` |
| `EARSHOT_OUT_DIR` | `<repo>/dist` |

To try the script without touching the real keychain, point the first two at paths in a temp directory.

First launch: macOS asks once for Bluetooth. The app never registers itself as a login item: turn on `Launch at Login`
in the menu if you want it (it shows the system's state, and unchecking it unregisters).
Then open the menu, choose the pendant under `Choose Pendant` (each entry shows the advertised name, signal in dBm and
the first four hex digits of its identifier; the strongest signal is first), and the app connects to that pendant and
no other. Until one is chosen the title says `No pendant chosen` and nothing is connected or drained. `Forget Pendant`
clears the choice. The choice is kept per Mac in the app's defaults.
