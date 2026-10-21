# earshot — handoff

Everything learned on 2026-10-01 getting a Limitless pendant working with no cloud: what was
decided and why, what was ruled out and why, what was measured, and every trap hit on the way.
`DESIGN.md` is the design of record and `docs/protocol.md` is the wire reference; this file is the
context behind both. Read all three before building.

Roles, not machine names: "the Mac" is the Mac in Bluetooth range of the pendant, "the server" is
the Linux host that runs earshotd in the remote-server setup, "the proxy" is the host that fronts
it, and "the author" is whoever built this first.

## Purpose

The author owns a Limitless pendant. Limitless was acquired by Meta, and the pendant is losing
support. The author wants to keep wearing it with everything local: audio stays on their own
network, transcription runs on their own hardware, no third-party account. Two constraints were
set explicitly:

- **Compute runs on a Proxmox server, not on the Mac.** The Mac is the author's daily driver. It
  serves only for the Bluetooth piece, because nothing else can reach the pendant.
- **No extra hardware.** A Raspberry Pi relay was rejected as "repeat bs". The pendant stays in
  the author's office; moving it, or its charger, near the server was rejected.

## Decisions, and what decided each

| # | Decision | Decided by |
|---|---|---|
| D1 | Bluetooth runs on the Mac | Range scans 2026-10-01: the Mac (a Mac mini M4 Pro) hears the pendant at **-51 dBm** (and a Govee sensor at -46). The Proxmox host's radio, scanning with a raw-HCI script for 25 s, saw 23 devices but not the pendant (the same Govee read -90 there). The author rejected a Pi relay and moving the pendant. |
| D2 | Compute runs on the server, in an LXC | The author wants compute on Proxmox. A VM was considered only for USB Bluetooth passthrough, which D1 made moot. An LXC is the author's pattern for services. |
| D3 | CPU faster-whisper is enough | Benchmark on the Proxmox host (AMD Ryzen 9 9955HX, 16 cores / 32 threads) 2026-10-01: faster-whisper `large-v3-turbo`, int8, 16 threads, `vad_filter=True`, 130.3 s of real pendant audio transcribed in **2.2 s (58x realtime)**, accurate. A 16-hour day takes minutes. VAD also removed the junk Whisper invented over silence on the Mac. |
| D4 | Earshot.app is a Swift `.app` bundle, not a script | macOS refuses CoreBluetooth to a process with no app context: on an Intel Mac over ssh, bleak raised `BleakBluetoothNotAvailableError: Bluetooth is not authorized`. A bundle carrying `NSBluetoothAlwaysUsageDescription` gets one grant. The author also asked for a menu-bar status indicator, which a bundle provides. |
| D5 | Stable self-signed signing identity | omi's `desktop/context-for-claude/README.md` ("Why signing matters"): ad-hoc signatures change every build, so macOS revokes Screen Recording and microphone consent each time. The same applies to Bluetooth. |
| D6 | Vendor omi's Swift codec | `LimitlessProtocol.swift` is pure Foundation, MIT, with golden fixtures for the hard parsing cases. omi has 13.6k stars, is MIT, and was committed to the day this was written. Rewriting the codec would only add a second copy that can drift. |
| D7 | Flash drain, not live streaming | The pendant records to its own flash (95,232 pages) whether or not anything is connected; the drain is idempotent and tolerates disconnects. Live mode (`{0,1}`) loses audio whenever Bluetooth drops. |
| D8 | ACK only after fsync | ACK (msg 7) deletes the pendant's copy (omi `encodeAcknowledgeProcessedData`; `LimitlessFlashDrainEngine`: "an ACK deletes the pendant's copy"). |
| D9 | SQLite page store with `(session, page_index, page_ts_ms)` key | Idempotent re-upload; page indices restart at 0 after a factory reset (observed: newest page 2 right after reset). |
| D10 | TLS through a proxy, port locked to the proxy | Audio of the wearer's day must not cross the LAN in plaintext. The proxy already held a wildcard certificate and the proxy pattern. The lock is nftables inside the server, because the Proxmox firewall was off and enabling it datacenter-wide would touch every guest. |
| D12 | Transcripts served by a versioned read API | The author wants Limitless-style days, retrieved raw by AI assistants. SQLite is the one store; no transcript files on the side, so there is no second copy to drift. |
| D13 | Summaries are not earshot's job | The author, 2026-10-01: "we'll have bench-daemon do the summaries… earshot just does the transcription, structure, retrieval api". A summary job is a consumer of the read API. That job may move between daemons; earshot's API is the fixed boundary either way. |
| D14 | Don't clone Limitless's API | Earshot's days/transcript/range API covers what the assistants need; copying a dead vendor's shape ties earshot to it. Limitless originals stay reachable at `GET /v1/days/{date}/limitless`. |
| D15 | Import the archive's audio, not just its text | Limitless's API transcript labels 99% of lines "Unknown" (40,035 lines across 300 conversations sampled 2026-10-01; 415 named). Running the archive audio through earshot's own pipeline gives it the same speaker labels as new audio. Audio download verified: Ogg Opus, 16 kHz mono, at most 2 hours per request. |
| D16 | Speaker labels with pyannote community-1 | Benchmarked 2026-10-01 on AMI ES2004a (4 real speakers, first 10 min, DER with 0.25 s collar): pyannote 0.182 DER / 0.028 confusion / 5 speakers found; sherpa-onnx + TitaNet-large best 0.309 DER / 0.152 confusion / 25 speakers found (46 at its default). Adoption: whisperX 24.3k★, sherpa-onnx 15.1k★, pyannote-audio 10.6k★, all pushed in late September 2026. |
| D17 | Diarization on the server's CPU, not the Mac's GPU | pyannote on the 8-core LXC server: 2.4x realtime (2.9x at 16 cores, so cores don't fix it). On the Mac mini M4 Pro's GPU (MPS): 24.1x, identical accuracy. The server keeps up with live audio (about 15 min behind), and the author wants compute on Proxmox; the Mac's GPU is the measured fallback if the backlog ever needs it. |
| D18 | Rolling ~10-minute windows cut at silences | The pendant writes pages through silence, so "process a stretch once it ends" would never fire during continuous recording. Windows keep transcripts about 15 minutes behind live. |
| D19 | Day-level speakers and enrolled voices | pyannote labels are per window; linking each window's speakers to the day's voice fingerprints keeps one label per voice across the day, and matching against enrolled voices (the wearer first) puts names on lines. |
| D20 | The pendant deletes on the Mac's save, not on transcription | So it never fills up because the server is down or behind; the Mac's spool is the buffer. |
| D21 | Audio is never deleted on the server | About 7 MB per recorded hour; keeping it lets a better model redo everything later. |

## Rejected, and what each would have made worse

| Alternative | Why not |
|---|---|
| Stock omi app with the Limitless | Cloud. Even with custom speech-to-text (`SttProvider.localWhisper` / `onDeviceWhisper` / `custom`), the app still opens omi's backend socket with `custom_stt=enabled` (`app/lib/services/sockets/transcription_service.dart`), and conversations are stored in omi's cloud. |
| omi "Realtime Audio Bytes" webhook | Sent by omi's backend (`backend/routers/pusher.py`, `backend/utils/webhooks.py`), not by the phone, so the audio has already reached omi's cloud. |
| Self-hosted omi backend | `docs/doc/developer/backend/Backend_Setup.mdx` requires Firebase/GCP, Pinecone, OpenAI, Redis/Upstash and Google/Apple OAuth. Not local. |
| omibutfree ("Omi Local") | Supports Omi hardware only (no Limitless code in its tree), 28 stars, last push 2026-02-17. |
| girlflipwebsite-spec/limitless-pendant | Mac Python tool, read-only drain. 0 stars, created 2026-09-13, no license. Usable as a second protocol reference; its code can't be copied. |
| COREkunas/RePendant | Replacement firmware plus an Android app (Apache-2.0). Flashing risks bricking the pendant; long-recording transcription unfinished; 0 stars. |
| Wi-Fi sync | A search summary of the FCC filing lists 2.4 GHz Wi-Fi and 5 GHz NII bands (the FCC pages themselves returned 403/Cloudflare). But Limitless's own sync is pendant → phone over Bluetooth → cloud, omi's Limitless code never mentions Wi-Fi, and the router's 241 DHCP leases show no Limitless/pendant host. No known path exists. |
| Bluetooth on the Proxmox host (VM with USB passthrough) | Out of range (D1). |
| Raspberry Pi relay | Rejected by the author. |
| Pendant or charger near the server | Rejected by the author: the pendant stays in the office. |
| A second, Intel Mac as the Bluetooth host | The author chose the M4 Pro Mac. The Intel Mac would also hit the same Bluetooth permission (D4). |
| Transcription on the Mac | The author wants compute on Proxmox; D3 shows the server is fast enough. |
| sherpa-onnx diarization | D16: 31–39% DER on real speech and it invents dozens of speakers. Fast (15–34x realtime), but the labels would be noise. |
| Summaries inside earshot | D13. Also kept transcript text on the LAN unless a consumer chooses otherwise. |
| Limitless ZIP export instead of the API | No audio in it. omi's importer reads numbered speakers (`[1]`, `[2]`) from it, which suggests the ZIP keeps a speaker split the API drops; unconfirmed without a real export, and earshot re-labels from audio anyway. |
| Ask AI chats | The author doesn't want them. |
| Python/bleak daemon on the Mac | No app context, so no Bluetooth grant under launchd (D4); no menu bar. |
| SMB drop onto a NAS share | Depends on a Finder mount in the author's login session; when it's unmounted, writes fail. An HTTP upload with the spool as a buffer has no such dependency. |

## Pendant operations

**LEDs** (Limitless help, "What do the different Pendant LED / light colors mean?"):

| Light | Meaning |
|---|---|
| white | recording |
| orange / green | charging / fully charged |
| blue | ready to pair |
| purple | factory reset in progress |
| solid red | recovery mode: the reset's double-press was too slow. Hold the button 10+ s until it clears. |
| red blink 3×, pause, repeat | clock needs syncing before recording resumes (SetCurrentTime fixes it) |
| red blink 10× | flash full |
| red blink 4×, 5 s pause | too hot |
| breathing red | microphone init failure |

**Button.** Long press starts/stops recording (omi comment in `_tryParseButtonStatus`). SHORT,
LONG and DOUBLE presses all arrive over Bluetooth (observed). Short press has no on-device effect
that omi or Limitless documents.

**Battery.** About 5 %/h with the app off (10.7 mA) and 5.3 %/h connected (12.1 mA), so about 20 h
per charge; the app's share is about 12 % (A/B on 2026-10-06, the author's pendant).

**Factory reset** (Limitless help, "How do I factory reset my Pendant?"): deletes recordings
not yet synced. From the button: press and release, then immediately press and hold; purple
appears; keep holding; release when purple goes out; light turns blue = ready to pair. Forget the
pendant in the phone's Bluetooth settings first, and keep the Limitless/omi apps closed so the
phone doesn't grab it.

**Pairing.** After the reset, the Mac's first encrypted write succeeded with no prompt. The
pendant does not appear in macOS's Bluetooth settings, since it is not an audio or input device;
that's expected.

## Evidence trail

Scripts are in `spikes/` (Python, run with `uv run --with bleak …`). None of them sends msg 7.

| Script | Proved |
|---|---|
| `blescan.py` | Raw-HCI scanner (no bluez), run as root on the Proxmox host: its radio works but can't hear the pendant. Brings hci0 up and puts it back down. Kernel 6.x requires the 16-byte `hci_filter` struct (`<IIIH2x>`); the 14-byte pack raises EINVAL. |
| `macscan.py` | CoreBluetooth scan from the Mac: pendant `Pendant` at -51 dBm. |
| `probe.py` | Connect, GetDeviceStatus, storage-state parse. Before the reset: `Insufficient Encryption`. After: storage `{newest: 2, session: 1, free: 95230, total: 95232}`. |
| `monitor.py` | Clock set, then status every 3 s plus button events. Pages advance about 1 per 3 s with speech and more slowly in silence; LONG/SHORT/DOUBLE all arrive. |
| `pull.py` | Non-destructive drain: msg 8 `{1,0}`, collect pages, msg 8 `{0,0}`. First run pulled 113 of about 183 pages (gotcha 4); after the fix, 1,748 of 1,748 in 107 s (about 16 pages/s). Logs the download window. |
| `pull.py` ×2 during continuous `say` | Recording continues during download: the 107 s window held 107.4 s of audio (5,369 frames) and transcribed as the spoken sentence. `newest` rose 1731 → 1806 across that download. |
| `transcribe.py` | PyAV decode + resample + mlx-whisper on the Mac. Produced "The quick brown fox jumps over the lazy dog… Second speech block… counting 1 2 3 4 5 6 7 8 9 10". |

`fixtures/say-test.pages.v1`: 36 real pages (2400 frames, 48 s) pulled from the pendant on
2026-10-01, in the upload format of `DESIGN.md`, trimmed to the synthetic `say` speech only (no
room audio). `session` is set to 1, from the storage state, because the pull spike didn't record
StorageBuffer field 2. Transcribed through the same path it reads: "…is testing whether the
pendant writes audio while someone is… recording test for the pendant… the quick brown fox jumps
over the lazy dog… Second speech block… counting 1 2 3 4 5 6 7 8 9 10." Page indices 105–183 with
gaps (missed pages, not silence), so it also exercises gap handling. Use it as earshotd's
real-data acceptance input.

## Gotchas hit

1. **48 kHz decode.** PyAV's opus decoder ignored `ctx.sample_rate = 16000` and output 960-sample
   48 kHz frames. Treated as 16 kHz, the audio ran 3× long (390 s instead of 130 s) and Whisper
   printed "Thank you." for every 30 s window. Fix: `av.AudioResampler(format="flt",
   layout="mono", rate=16000)`.
2. **Whisper repetition loop.** With default `condition_on_previous_text=True`, mlx-whisper
   repeated one line across four segments. Set it to False.
3. **Hallucination over silence.** Without VAD, Whisper invented text over room noise. faster-
   whisper's `vad_filter=True` removed it.
4. **Spike parser crash.** `probe.storage_state` indexes past the end of some data packets. In
   `pull.py` the exception escaped the notify callback and dropped those packets, so about a
   third of the pages were lost from the pull. Not a pendant gap: with the call wrapped, a pull got
   every page. The same scanner also returns garbage `oldest` values from data packets. A real
   parser walks fields with bounds checks and never throws inside the BLE callback.
5. **Connected pendant stops advertising.** A second scan while one client is connected finds
   nothing (`find_device_by_filter` returned None). The app must be the only client; stop any
   spike before launching it.
6. **faster-whisper vs PyAV.** `model.transcribe(path)` failed with `open() got an unexpected
   keyword argument 'metadata_errors'`. Pass a float32 numpy array instead.
7. **Encryption.** `Insufficient Encryption` means the pendant is paired to another central.
   Only a factory reset frees it.
8. **Uncontrolled grep.** Searching a router's dhcpd leases for "rewind" (Limitless's company
   name) matched dhcpd's own `rewind binding state` keyword. Search only for tokens that can't
   occur in the file's own syntax.
9. **An installer must not append to shell rc files.** On a host whose dotfiles are managed, an
   installer that appends to `~/.zshrc` or `~/.bashrc` makes the next non-interactive apply abort.
   Install uv for earshotd with `UV_NO_MODIFY_PATH=1`, run earshotd as its own system user from
   `/opt/earshot`, and put nothing in anyone's shell files.
10. **Synthetic voices fool diarization.** Three of four setups merged two of three macOS `say`
    voices into one speaker. Test speaker separation on real speech (AMI ES2004a), never TTS.
11. **pyannote setup.** `pyannote/speaker-diarization-community-1` is gated: the Hugging Face
    account behind the token must accept its license (403 until then). A snapshot downloaded once
    is then copied to the other host; `Pipeline.from_pretrained(<snapshot dir>)` loads it with no
    token and no network (checked on the Mac, pyannote.audio 4.0.7). Install torch from
    `https://download.pytorch.org/whl/cpu` on the server or uv pulls the multi-GB CUDA build.
    torchaudio and torchcodec need the same index as direct dependencies, and torch must match
    torchaudio's newest CPU build (pinned torch/torchaudio 2.11, torchcodec 0.17). Otherwise PyPI's
    Linux wheels want `libcudart.so.13` and the worker stalls on its first window. macOS tests
    can't see this; only the deploy to the server does.
12. **Limitless audio cap.** `/v1/download-audio` refuses ranges over 2 hours with HTTP 400
    `Time range cannot exceed 2 hours`.
13. **Limitless 403 on Python's default agent.** On 2026-10-03, `api.limitless.ai` returned 403 to
    `urllib` with the default `Python-urllib` User-Agent, and 200 to curl with the same key. It's a
    Cloudflare bot rule, not the key.
14. **`log` is a zsh builtin.** In zsh, `log show ...` runs the builtin (it prints `too many
    arguments`, or nothing when piped) instead of macOS's unified-log tool, so the app looks
    silent. Call `/usr/bin/log show --predicate 'subsystem == "casa.mal.Earshot"' --last 1h`.
15. **Marker pages.** The pendant writes pages whose audio field holds only metadata (page 3935:
    `48 01`). omi's codec drops them, and before marker-pages (0f171b7) the drain refused to ACK
    past one, so from 2026-10-03 every 90 s drain re-downloaded the whole flash. The drain now
    logs `marker page N` and steps over them (DESIGN.md, Invariant).
16. **pyannote's NaN and zero centroids.** community-1 returns a NaN speaker embedding when none
    of a speaker's embeddings survive its clustering filter (0.5-1.5 s of speech in 30 s of
    silence reproduces it), and all-zero ones for speakers it could not cluster. Unfiltered they
    became `S<n>` day speakers that swallowed other voices and could never be named: on the
    author's server 20 of 26 transcribed days held one and no line carried a name.
    `diarize.diarize` now drops such a speaker from the fingerprints (their lines get speaker
    `null`). Stores transcribed before that fix need `earshot rebuild` (stop earshotd first; it
    refuses while the worker runs).
17. **systemd reopened the data directory.** `install.sh` creates `/var/lib/earshot` as 0750,
    but the unit's `StateDirectory=` resets it to 0755 on every start, so from 2026-10-02 to
    2026-10-14 any user on the author's server could read the store, its backups and the
    Limitless audio. The unit now sets `StateDirectoryMode=0750`, and `verify.sh local` fails on
    any other mode.

## How to verify against the real pendant

The pendant is the test fixture that can't be faked:

- Connection and status: the storage state's `newest` advances while the light is white.
- Drain safety: `total_pages - free_pages == newest - oldest` (observed four times: 2, 184, 1731
  and 1806, each matching). A drain that doesn't ACK leaves both sides unchanged; after an ACK,
  `oldest` moves past the ACKed index and `free_pages` rises by the same count.
- Audio: play `say -r 170 "<known sentence>"` near the pendant for 30 s, drain, upload, and grep
  the day's transcript for the sentence.

## Open

_none_
