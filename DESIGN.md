# earshot — design of record

Use a Limitless pendant with no cloud: audio comes off the pendant over Bluetooth to a Mac in range,
transcription and speaker labelling run on hardware you own (that same Mac, or a server you run),
nothing leaves your network. earshot
captures, transcribes, stores and serves; it does not summarize. Protocol details live in
`docs/protocol.md`; the history, evidence and rejected alternatives live in `docs/HANDOFF.md`.

## Shape

One Mac (the default, Setup 1 in the README):

```
pendant ──BLE──▶ Earshot.app ──http, loopback──▶ earshotd, on the same Mac
```

Remote server (Setup 2):

```
pendant ──BLE──▶ the Mac: Earshot.app ──HTTPS──▶ the proxy (Caddy) ──mutual TLS──▶ the server: earshotd
```

Either way:

```
Limitless cloud archive ──HTTPS──▶ earshotd's host: earshot import-limitless ──▶ same store, same pipeline
earshotd ◀── read API ── assistants and other read clients, the summary job (a daemon consumer, outside earshot)
```

| Component | Runs on | Language | Owns |
|---|---|---|---|
| Earshot.app | the Mac in Bluetooth range of the pendant (measured on a Mac mini M4 Pro, macOS 26) | Swift 6, SwiftPM, Command Line Tools | the pendant: connection, clock, status, flash drain, local spool, upload |
| earshotd | the same Mac, or a Linux server (measured on an 8-core unprivileged LXC server) | Python 3.12, uv, FastAPI | the audio store, transcription, speaker labels, the read API |
| `earshot import-limitless` | earshotd's host | Python, same package as earshotd | one-time pull of the Limitless archive into earshotd's store |
| route | the proxy, in the remote-server variant (Caddy) | Caddyfile | TLS for the public name, proxy to the server |

**Not in earshot: summaries.** Daily summaries and anything else that interprets transcripts are
consumers of the read API, run as daemon jobs that write their output where you read it.
earshot never stores them. That keeps one job per component and keeps transcript text inside
your network unless a consumer chooses to send it out.

## Timing

Measured on the author's pendant, 2026-10-01:

- The pendant records continuously to its own flash, including during a download: a
  107-second download window held exactly 107 s of recorded audio, and it transcribed cleanly.
- In range of the Mac, Earshot.app drains every 90 s. A download moves about 16 pages/s
  (1,748 pages in 107 s), so a full day away from the office drains in about 10–15 minutes on
  return.
- Pages are written about one per 3 s with speech and more slowly in silence; silence still
  writes pages. Flash holds 95,232 pages, about 79 hours at the speech rate and more in practice.
- An 8-core unprivileged LXC server processes audio (Whisper + pyannote) at about 2.4x realtime, so transcripts run about
  15 minutes behind live when the pendant is in range.

## Earshot.app

A menu-bar app (`LSUIElement`, no Dock icon) that owns the pendant.

**Connection.** Scan for service `632DE001-604C-446B-A80F-7963E950F3FB`. The pendant is chosen
from the menu: with nothing chosen the app only lists the devices it sees (name, signal, first four
hex digits of the identifier) and connects to none; once one is chosen, its CoreBluetooth identifier
is kept in the app's defaults and no other device is ever connected. Connect to it, subscribe to RX
`…E003`, write to TX `…E002`. Reconnect on disconnect, with backoff. On every connect, send
SetCurrentTime (msg 6) before anything else. Forget Pendant clears the choice and cancels the
connection.

**Status (menu bar).** The icon and title say disconnected, connected, downloading (a drain cycle is
running; the pendant keeps recording), or connected with audio stopped (the icon gains a minus badge).
The menu lists, one line each:

| Field | Source and rule |
|---|---|
| Battery | The status reply's percent and charging state, refreshed every cycle (a saved page's battery record counts only when newer). `Battery N%`; `, charging` when milliamps > 0; otherwise `, about H h left` from the least-squares percent slope over the last hour of discharging records in saved pages, with no estimate under 15 minutes of data, after a charge within the hour, or when the percent is not falling. |
| Audio | `Capturing audio` while opus frames were heard in the last 5 minutes; `Off (button) since HH:MM` while the status reply's device mode says off, HH:MM being the first cycle that saw it (no page needed), until the first reply that says on; a reply with no mode field claims nothing; `No audio since HH:MM` when frames stopped without one; `Out of range` when disconnected; `Audio unknown` before the first saved page. |
| Last heard | `Audio last heard HH:MM`: the timestamp of the newest saved page that carried opus frames. |
| Today | `Audio today: H.H h`: opus frames x 20 ms in the local day. |
| Sync | `Synced N min ago` from the last upload earshotd answered 200 to, or `Sync failed: <reason>` while the latest attempt failed. |
| On pendant, Backlog | Pages on the pendant (`total_pages - free_pages`, from the drain's status) and the spool pages not yet accepted by earshotd. |

Any alert condition below that is true is also listed in the menu, notification or not.

**Device mode and battery.** They come from the GetDeviceStatus reply the drain already requests every
cycle (`DeviceStatus`, EarshotCore; `docs/protocol.md`, Device status reply, beyond storage): field 7
is the mode (empty means off by the button, `{1: n}` means recording, absent means unknown) and field 9
the battery. The pendant also pushes a reply unprompted when its mode changes; the RX handler reads it
like a polled one, so the menu can change between cycles, and nothing is sent to get it. The idle-mode
log line in saved pages is history only: an idle pendant fills no pages, so the line only arrives after
recording resumes. The status reader is display-only: it never changes what the drain saves or ACKs.

**Display decoder.** `PageDigest` (EarshotCore) decodes a saved StorageBuffer into battery records
(wrapper field 6), the opus frame count (field 2, nested), the idle and wake log lines (field 10)
and the page timestamp, per `docs/protocol.md`, Flash page records. It exists only for the menu: the
drain never calls it. `StatusModel` hands the decoder copies of the raw bytes after the spool
accepted them (`TeeSink`), once a cycle has finished, so a decoder bug can mislabel the menu and cannot
change what is saved, fsynced or ACKed. Decoding is in the app, not earshotd, so the menu works with
earshotd down. The recording-state flag (field 9) is not read: it follows voice activity, not the
button.

**Notifications.** Three macOS notifications, each edge-triggered, for what the wearer can act on:

| Alert | Fires when | Fires again after |
|---|---|---|
| Battery low | latest reading (status reply, or a newer page record) <= 15 % and not charging | a charging reading (milliamps > 0) is seen |
| Pendant off | the status reply's mode is off for 10 minutes, or mode on or unknown with no opus frames for 10 minutes in saved pages; not charging (milliamps <= 0 in the reply); judged after a cycle that reached the newest page or found nothing to drain; names the button (`long-press to resume`) when the mode is off, otherwise says no audio is being captured | audio is heard again |
| Not syncing | no upload has returned 200 for 6 hours while the app runs (from launch when none has); says earshotd is unreachable when the last upload failed, otherwise that the pendant is out of range | an upload returns 200 |

Alert state (fired or armed) is kept in the app's own defaults, so a relaunch while the condition
holds does not repeat an alert; it re-arms as the table says. Permission is asked the first time an
alert is due. When notifications are denied or refused, nothing
is posted, the refusal is logged once at notice, and the menu still shows the condition. No alert
depends on the accelerometer: worn and on the desk cannot be told apart.

**Drain cycle.** Port omi's `LimitlessFlashDrainEngine` (see `docs/protocol.md`, Drain cycle).
Every 90 s while connected: status → SetMode `{1,0}` (send stored pages) → reassemble pages →
append each page's raw bytes to spool → fsync → ACK → SetMode `{0,0}` (stop sending). Recording
never stops. Between cycles the app sends the pendant nothing: no status poll, no battery read (the
battery comes from the status reply the cycle itself requests). **Early end.** The pendant is still writing its newest page and may
never send it, so once every page below the newest is in hand and the link has been quiet for 5 s,
the cycle ends as caught up (`caughtUp`), ACKs through the last page received after the fsync, and
leaves the newest page for the next cycle. A hole below the newest keeps the 30 s stall.
**Bounds.** Reassembly holds at most 8 incomplete messages of 64 fragments and 64 KiB each, and a
fragment whose numFrags or seq contradicts itself drops its message (a notice is logged; the page
stays on the pendant). The RX inbox holds at most 4,096 packets. A cycle ends after saving 4,096
pages (`pageLimit`), ACKs through the last contiguous page as for any end, and the next cycle
begins at once rather than after the 90 s interval.

**Invariant: ACK (msg 7) deletes the pendant's copy. A page is ACKed only after its raw bytes are
fsynced to the local spool.** The drain does not interpret page contents (the menu decodes copies afterwards): every StorageBuffer the
pendant sends is saved byte for byte, whatever it holds (audio, a marker, telemetry, a CRC-failed
page with no flash page), and earshotd decodes it. The ACK covers the contiguous run of page indices
(StorageBuffer field 5) saved so far; a page whose index cannot be read is saved but does not extend
the run, so nothing past a missing page is ACKed. The pendant deletes on the Mac's save, not on
transcription, so it never fills up because the server is down or behind. Every drain ends with one
notice line reporting pages saved and ACKed-through; a page received without a readable index is
logged at notice too.

**Spool.** `~/Library/Application Support/Earshot/spool/`, append-only segment files
(`seg-<n>.pages.v2`) of v2 records, `{u32 length, raw StorageBuffer}`, in the upload format below.
fsync the file (and its directory on creation) before ACK. Survives app restart. Segments written
in v1 before the move to raw pages (`seg-<n>.pages.v1`) still upload, as v1. The directory is mode
`0700`, set at every start whether the app created it or found it at another mode.

**Upload.** POST closed spool segments to earshotd, `application/vnd.earshot.pages.v2` for a v2
segment and `application/vnd.earshot.pages.v1` for a leftover v1 one. Delete a segment only after a
2xx that covers every page in it. If earshotd is unreachable, keep the segment and retry with
backoff; the spool is the buffer. Nothing is dropped. The uploader never follows a redirect: a 3xx is
the response, the redirect target receives no request, the segment is kept, and the failure says the
redirect was refused (the body and the token go only to the configured server).

**Codec.** Vendor `LimitlessProtocol.swift` from omi at
`056936e0ed0667ebb269b0a17fcbcc4c37275f8c` with omi's MIT notice, and port omi's golden fixtures
(`app/test/fixtures/limitless_golden/`) into `swift test`.

**Config.** `~/.config/earshot/config.toml`: `server_url`, `token`. Neither is ever in the repo. The app
refuses, before parsing any key, a file any group or other permission bit is set on or that another user
owns, and never changes it.

**Bundle and signing.** SwiftPM builds the executable; a build script assembles `Earshot.app`
(Info.plist with `NSBluetoothAlwaysUsageDescription`, `LSUIElement = true`) and signs it with a
dedicated, stable self-signed identity, "Earshot Signing", with the hardened runtime (the build fails
without the runtime flag; the designated requirement is unchanged). Launch at login is opt-in: a
`Launch at Login` menu item over `SMAppService`; starting the app reads the registration and never
changes it.
Why: macOS refuses CoreBluetooth to a process with no app context, and an ad-hoc signature
changes every build, which makes macOS treat each build as a new app and drop its Bluetooth
grant.

**Never** write to the SMP service (`8D53DC1D-…`); it is firmware update.

## Upload contract

`POST /v1/pages`

- `Authorization: Bearer <token>` (upload scope)
- `Content-Type: application/vnd.earshot.pages.v2` (raw pages) or
  `application/vnd.earshot.pages.v1` (parsed pages); both stay accepted.

**v2 body.** Zero or more raw pages back to back, each `{u32 little-endian length, that many
bytes of StorageBuffer}`, the StorageBuffer exactly as the pendant sent it. earshotd stores every
raw page in `raw_pages` (keyed by `(session, page_index, sha256 of the raw bytes)`), decodes the
page's opus frames into the `pages` table as v1 does (so transcription is unchanged), and decodes
every other record into `pendant_records` (docs/protocol.md, 'Flash page records'). A body that
is truncated mid-record, or whose StorageBuffer has no page index (field 5) or does not parse, is
a 400 naming the byte offset or the problem; nothing from it is stored. A re-sent raw page is a
duplicate. Because raw pages are kept, `earshotd redecode` can re-run the decoder over all of
them.

**v1 body.** Zero or more page records, little-endian, back to back:

```
u32 session        StorageBuffer field 2
u32 page_index     StorageBuffer field 5
u64 page_ts_ms     flash page field 1 (epoch ms once the clock is set)
u16 frame_count
frame_count × { u16 len, len bytes opus }
```

Responses:

| Status | Body | Meaning |
|---|---|---|
| 200 | `{"stored": n, "duplicates": m}` | every page in the body is durable (new or already present) |
| 400 | `{"error": "..."}` | malformed body; nothing stored |
| 401 / 403 | | missing or unknown token / wrong scope |
| 408 | `{"error": "..."}` | body not received within 120 seconds; nothing stored |
| 413 | `{"error": "..."}` | body over 4 MiB (4194304 bytes); nothing stored. A `Content-Length` over the cap is refused before any body is read |
| 503 | `{"error": "..."}`, `Retry-After: 30` | two uploads already in flight; nothing stored, retry later |

The cap and the limits are fixed in code. The app closes a segment at 1 MiB (plus at most the one
record that crosses it, about 4 KiB), so every segment it spools sits well under the 4 MiB cap.

Idempotent (v1): a page is keyed by `(session, page_index, page_ts_ms)`. Re-sending a page is a
duplicate, not an error. The key includes the timestamp because page indices restart at 0 after
a factory reset (observed: newest page 2 right after reset).

## Read API

For assistants and other read clients, and the summary job. Versioned under `/v1`, JSON unless asked otherwise,
times in epoch ms, days cut in the configured zone (the `timezone` setting, defaulting to the host's own zone). Every route except health takes
`Authorization: Bearer <token>` with the `read` scope. FastAPI serves the OpenAPI document at
`/v1/openapi.json`, which needs a read token too, so a read client can discover the API from the
service itself.

| Route | Returns |
|---|---|
| `GET /v1/days?from=YYYY-MM-DD&to=YYYY-MM-DD` | `[{"date", "segments", "first_ms", "last_ms", "sources"}]`, newest first |
| `GET /v1/days/{date}/transcript` | `{"date", "segments": [Segment]}`; with `Accept: text/markdown`, one `- HH:MM:SS **speaker** text` line per segment; at most 50,000 segments, a busier day is a 400 |
| `GET /v1/transcript?from=<ms>&to=<ms>` | the same `Segment` list over any range, crossing days; at most 50,000 segments, a larger range is a 400 `{"error": ...}` and is read in shorter ranges |
| `GET /v1/days/{date}/limitless` | the Limitless originals for that day: `[{"lifelog_id", "title", "start_ms", "end_ms", "starred", "markdown"}]`; empty for days after the archive |
| `GET /v1/pendant/records?kind=<kind>&from=<ms>&to=<ms>&limit=<n>` | the pendant's non-audio records: `[{"kind", "ts_ms", "session", "page_index", "data"}]` by `ts_ms`; `kind`, `from`, `to` optional, `limit` default 1000, at most 10000 |
| `GET /v1/health` | `{"ok": true}`, no auth, for an uptime monitor, which polls it through the proxy and expects a 2xx; runs no count and reads no row, and is a 500 when the store will not open or lacks its schema |
| `GET /v1/status` | `{"pages", "untranscribed_pages", "last_page_ts_ms", "worker", "import": {...}}`, read scope |

`Segment` = `{"start_ms", "end_ms", "text", "source": "pendant" | "limitless", "speaker", "name"}`.
`speaker` is the day-level label (`S1`, `S2`, …), stable for one voice across a whole day;
`name` is set when the voice matches an enrolled voice (for example `Ada`), else null.

A day's segments are final only once all of that day's audio is transcribed; an unfinished day
returns what exists so far.

**Tokens.** `/etc/earshot/earshotd.toml` lists tokens with scopes: `upload` (Earshot.app) and
`read` (assistants, consumers). A token never carries both. Generated on the server with
`openssl rand -hex 32`.

## earshotd

uv project, FastAPI, native under systemd as its own system user from `/opt/earshot` (no Docker:
Docker-CE 28+ breaks in an unprivileged Proxmox LXC). The installer touches no
shell file: an installer that appends to a login shell's rc file breaks hosts whose dotfiles are managed, so uv goes in
with `UV_NO_MODIFY_PATH=1`.

**Store.** SQLite at `/var/lib/earshot/earshot.db` (`journal_mode=WAL`, `synchronous=FULL`),
the single source of truth:

- `pages(session, page_index, page_ts_ms, frames BLOB, transcribed_at NULL, PRIMARY KEY
  (session, page_index, page_ts_ms))`. A 200 on upload is returned only after the transaction
  commits.
- `raw_pages(session, page_index, sha256, raw BLOB, PRIMARY KEY (session, page_index, sha256))`:
  every StorageBuffer a v2 upload carried, byte for byte, the only copy once the pendant ACKs.
- `pendant_records(session, page_index, sha256, n, kind, ts_ms, data JSON, PRIMARY KEY
  (session, page_index, sha256, n))`: the non-audio records decoded from `raw_pages`. `kind` is
  `log` (`level`, `module`, `text`), `battery` (`mv`, `ma`, `temp_c`, `pct`), `motion` (`x`, `y`,
  `z`), `thread_stats`, `recording`, `bluetooth`, `marker`, `crc_failed` (a page whose flash
  failed its CRC; `ts_ms` NULL) or `unknown` (`field`, `hex`: a wrapper field the decoder does
  not name yet, e.g. 12 and 13). `ts_ms` is the page timestamp plus the record's offset.
  Rebuildable from `raw_pages`.
- `limitless_lifelogs(lifelog_id PRIMARY KEY, title, start_ms, end_ms, starred, markdown,
  contents_json)` and `limitless_audio(lifelog_id, start_ms, end_ms, path, transcribed_at,
  done_ms, UNIQUE (lifelog_id, start_ms, end_ms))`, the imported archive. Audio files under
  `/var/lib/earshot/limitless/` (Ogg Opus as Limitless sent it). `done_ms` is how far the
  pipeline has committed into a chunk (NULL: not started); `transcribed_at` is set with the last
  window.
- `segments(id, start_ms, end_ms, text, source, speaker, name, model)`, indexed on `start_ms`.
  A segment's day is the local date of its `start_ms`.
- `day_speakers(date, speaker, centroid BLOB)`: one voice fingerprint per day-level speaker.
- `voices(name PRIMARY KEY, embedding BLOB)`: enrolled voices.

Audio is never deleted (about 7 MB per recorded hour), so a better model later can redo
everything.

**Pipeline.** One worker loop in the same process, same steps for pendant and imported audio:

1. **Window.** Take untranscribed audio in time order. Close a window at about 10 minutes, cut
   at the longest silence in its last 60 s so no sentence is split, and only once the cut point
   is at least 2 minutes old (so it isn't still arriving). A gap of more than 120 s in page
   timestamps always closes a window. Silence still writes pages, so a stretch never "ends" on
   its own; windows are what make transcription run during continuous recording. A window loads
   at most 22 minutes of audio (66,000 frames, or 13,332,000 bytes of stored frames, whichever
   comes first; fixed, not configured): when the pages stored in one range hold more, the
   window closes as `cap` over what was loaded, once its last page is 2 minutes old, and the
   rest waits for the next window.
2. **Decode.** Opus frames via PyAV; the decoder outputs 48 kHz, so resample to 16 kHz mono
   float32.
3. **Transcribe.** faster-whisper `large-v3-turbo`, `device="cpu"`, `compute_type="int8"`,
   `language="en"`, `vad_filter=True`, `condition_on_previous_text=False`. Pass a numpy array,
   not a path (the faster-whisper/PyAV pairing installed 2026-10-01 fails on `av.open(...,
   metadata_errors=...)`).
4. **Diarize.** pyannote.audio 4.x, `pyannote/speaker-diarization-community-1` (gated on Hugging
   Face; the account that downloads it accepts the license), CPU torch, 8 threads. Loaded from a local
   directory (`diarization_model`), never from the Hub: a local snapshot
   needs no secret and no network at startup. The revision and the sha256 of
   each of the five files the model loads are pinned in `earshotd/src/earshotd/models.py`, the
   only place either revision is written.
   `deploy/install.sh` installs exactly those five files, checked, to
   `/opt/earshot/models/speaker-diarization-community-1`, root-owned: from a snapshot directory,
   or downloaded with a token read at a hidden prompt that is never stored. Assign each Whisper
   segment the speaker whose turns overlap it most.
5. **Link speakers across windows.** Compare each window speaker's voice fingerprint (pyannote's
   embedding) to the day's `day_speakers` centroids by cosine similarity; above the threshold it
   is that day speaker, otherwise a new `S<n>`. Then compare to `voices`; above the threshold set
   `name`. Thresholds are tuned on AMI ES2004a and recorded in config. A NaN or all-zero
   fingerprint from pyannote (a speaker none of whose embeddings survive its clustering filter,
   or one it could not cluster) is treated as no fingerprint: the speaker is dropped where
   diarization returns, never linked, stored or named, and their lines get speaker `null`.
   `voice add` refuses a range whose dominant speaker has none.
6. **Commit.** Insert the window's segments and mark its audio transcribed in one transaction.

**Redecode.** `earshot redecode` empties `pendant_records` and rebuilds it, and any missing audio
`pages` rows, from every `raw_pages` row through the same decoder the upload uses, in one
transaction. It reads raw pages one at a time, so its memory does not grow with the archive.
Existing `pages` rows and transcriptions are untouched. Run it after the decoder learns a record
type. It names at most 100 raw pages that fail to decode, then counts the rest in one last line;
it exits 1 if any failed. Like rebuild, it refuses, changing nothing, while earshotd runs: its one
transaction holds the store for tens of seconds on a real archive, beside a worker whose commits time
out, so it takes the worker's lock without waiting. Stop earshotd, redecode, start it.

**Rebuild.** `earshot rebuild` clears everything derived from audio in one transaction:
`segments` and `day_speakers` are emptied, and `pages.transcribed_at` and
`limitless_audio.transcribed_at`/`done_ms` go back to NULL, so the running worker transcribes,
diarizes and links all stored audio again with the current code. It keeps `voices`, `raw_pages`,
the `pages` frames, `pendant_records`, `limitless_lifelogs`, the `limitless_audio` paths and
`limitless_audio_absent`. It refuses, changing nothing, while earshotd runs: the worker holds an
exclusive lock on `data_dir/worker.lock` and rebuild takes the same lock without waiting. Stop
earshotd, rebuild, start it. Until the worker catches up the read API returns fewer transcripts.

**Voice enrollment.** `earshot voice add <name> --from <ms> --to <ms>`: computes the embedding of
that speaker's speech in that range and stores it in `voices`. Enrolling the wearer is the first use.
A range holding more than 22 minutes of stored audio (counting the 60 s loaded before `--from`)
is refused, changing nothing.

**Config.** `/etc/earshot/earshotd.toml`: `tokens` (with scopes), `listen` (default `127.0.0.1:8080`; a remote server sets `0.0.0.0:8080` behind mutual TLS and the nftables lock),
`data_dir`, `timezone` (optional, an IANA name: days are cut in it, else in the host's own zone from `/etc/localtime`, else UTC;
an unknown name refuses at load, and a change on a store that holds days needs `earshot rebuild`), `whisper_model`, `diarization_model` (local pyannote snapshot directory), speaker-link and voice-match thresholds. `listen` must be `host:port` with a port of 1-65535.
The proxy hop is mutual TLS: `tls_cert`, `tls_key` and `tls_proxy_certs` (a non-empty list) are given all
together or not at all, and `config.parse` refuses a `listen` whose host is not loopback (127.0.0.0/8, `::1`,
`localhost`) unless they are set or `insecure_plain_http = true` is (a bool, default false), which serves plain
http on a network address with the firewall as its only protection; the opt-in together with the TLS settings
is refused, and `earshotd` prints a stderr line at start saying the hop is plain http by that opt-in. A
loopback `listen` with no TLS settings is served plain, as the one-Mac setup does. With the TLS settings
`earshotd` serves TLS 1.3 only, on the passed socket and the bound one alike, and requires a client
certificate that is its own or one of `tls_proxy_certs` (each certificate is its own pin, no CA); a client
without one gets no HTTP response. Before the models load it refuses, naming the path, a key others can read,
its group can write or anyone but root or `earshotd` owns, and a certificate or trust file its group or others
can write; `install.sh` runs the same check before enabling anything. `install.sh` generates the server's key and
certificate in `/etc/earshot/tls` and `deploy/proxy-cert.sh` generates the proxy's on the proxy host: private keys
never leave the host that made them, and only certificates (valid 3650 days, rotated on demand) are exchanged.
`config.load` refuses to start on a config that others can read, its group can write, or that
anyone but root or the user running earshotd owns (OpenSSH StrictModes); the error names the path,
its octal mode and its owner uid. Install leaves it `root:earshot` `0640`. The Limitless key is
never in the config: `config.parse` refuses a config that holds `limitless_key` (naming the key,
never its value), because earshotd, the daemon that parses untrusted audio, loads that file.

**Confinement.** earshotd runs socket-activated with no network of its own: `earshotd.socket`
holds the listening socket (its `ListenStream` is rendered by `deploy/install.sh` from `listen`,
as is the firewall's port) and passes it as fd 3, and `earshotd.service` sets `PrivateNetwork=yes`
so the daemon's namespace holds only loopback and a compromised audio parser has no route off the
host. The directives (no capabilities, devices, kernel access or other processes; `UMask=0077`;
the system-call filter) are in `deploy/earshotd.service`, which says why each directive left out
(`MemoryDenyWriteExecute`, `ProcSubset`, `~@resources`) breaks the pipeline. `MemoryMax=6G` is fitted
to an 8 GiB server and is sized to the host with a systemd drop-in (`systemctl edit earshotd`): a percentage is no limit inside an LXC. When
`LISTEN_FDS` is `1` and `LISTEN_PID` is its own pid, `earshotd` serves fd 3, and refuses to start,
naming both addresses, if that socket is not `listen`; without them it binds `listen` as before
(macOS, tests, a hand run). Both `earshotd` and the `earshot` CLI set `umask 077`, and every file
in `data_dir` is created private whatever the umask: `data_dir` and `limitless/` `0700`, the
database with its `-wal` and `-shm`, `worker.lock` and each `.ogg` `0600`; install removes group
and other permissions from older files. Models live root-owned under `/opt/earshot/models`
(`speaker-diarization-community-1` and `faster-whisper-large-v3-turbo`, whisper fetched at a pinned
revision), and install refuses to enable the service while either configured model is not a
directory whose files and parents are root-owned and writable by no one else.

**Network.** In the remote-server variant the proxy (Caddy here) terminates TLS for its public name and
proxies to `https://<server>:8080` over mutual TLS: it verifies earshotd's `server.crt` under the name `earshotd`
and presents its own `proxy.crt` and `proxy.key`, and earshotd completes no handshake with a client that lacks
that certificate, so a host that spoofs the proxy's address reaches no HTTP. `proxy_addresses` in the config is the
firewall's input: nftables on the server allows the listen port only from those addresses (and loopback), a second
layer under the TLS that needs nothing from the hypervisor. Audio and transcripts never cross the network in
plaintext while the config sets the TLS settings; setting `insecure_plain_http = true` instead withdraws that
claim, and the firewall alone then guards the hop.

## Limitless import

`earshot import-limitless`, run on earshotd's host, writing through earshotd's own store code. The author's
archive was 2,432 conversations over 143 days (2025-11-04 → 2026-09-15), about 407 recorded hours, on
2026-10-01; it has until the end of 2026 to be pulled out.

1. Page through `GET https://api.limitless.ai/v1/lifelogs` (`X-API-Key`, `limit=10`, cursor,
   `includeMarkdown`, `includeHeadings`, `includeContents`) and upsert every lifelog into
   `limitless_lifelogs` by `lifelog_id`.
2. For each lifelog, download its audio from `GET /v1/download-audio?startMs&endMs&audioSource=pendant`
   in chunks of at most 2 hours (the API refuses longer: `Time range cannot exceed 2 hours`), write
   the Ogg file, fsync, record it in `limitless_audio`.
   The file is written under a `.part` name, fsynced, renamed and the directory fsynced before its
   row is committed; each file must be Ogg Opus, 16 kHz (read from OpusHead), mono, or the
   lifelog fails.
3. Retry 429, 5xx and dropped connections with exponential backoff (1 s doubling, `Retry-After`
   honoured, 6 attempts). Re-running skips every chunk whose row is committed and whose file
   exists, so an interrupted import resumes. Any other failure (a 403 included) is not retried.
   One answer is a settled result, not a failure: `404` with the body exactly
   `{"error":"No audio available for the requested time range"}` is the no-audio result. The chunk
   is stored in `limitless_audio_absent` (lifelog id, start, end, the API's error text, checked
   time), is never requested again on later runs, is reported on its own "no audio at Limitless"
   line, and does not make the run fail. Any other 404 body, and every other 4xx, stays a failure.
   A lifelog that still fails is named by `lifelog_id` in the final report and the exit status is
   1; so is a listing that fails, one that lists nothing, or a stored count that differs from the
   API's. The report prints "API listed N lifelogs, stored M". `--api-base` points the command at
   a stand-in server for tests. The key is asked for at run time and never stored: a hidden
   prompt on a terminal, else the first line of stdin. The client sends it only to the base it
   was given: it follows no redirect (a 3xx fails the call), takes plain http only to
   127.0.0.1, localhost or ::1, and refuses a base with userinfo, a query or a fragment. Each new
   chunk file is named by the SHA-256 of its lifelog id, so no id can steer the path; rows
   written earlier keep their id-named paths.
4. Imported audio enters the same pipeline with `source = "limitless"`, so archive and pendant
   transcripts share one format and one set of speaker labels. Each pick takes the oldest
   untranscribed chunk and windows it from `done_ms` with the pendant rules (a clock far enough on
   that no cut is "still arriving"), one transaction per window. A ready pendant window always
   goes before imported audio: the archive is about 170 CPU-hours at 2.4x realtime, and
   letting it go first would leave live transcripts a week behind instead of about 15 minutes.
5. Send an explicit `User-Agent`. Limitless sits behind Cloudflare, which answers Python's
   default urllib agent with 403.

Skipped: Limitless "Ask AI" chats.

Limitless's own transcript lines label 99% of lines "Unknown" (40,035 lines sampled across 300
conversations), so they are kept as `markdown`/`contents_json` originals only, never served as
segments.

## Open

_none_
