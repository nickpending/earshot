# earshot

Earshot is a Limitless pendant used with no cloud. A menu-bar app on a Mac, Earshot.app, drains the pendant over
Bluetooth into a local spool and uploads it to `earshotd`, a server you run. `earshotd` transcribes the audio with
faster-whisper, labels who spoke with pyannote, and serves the result through a read API. Your audio and transcripts
stay on machines you own, and `earshotd` turns off the telemetry its libraries would otherwise send (pyannote,
onnxruntime, Hugging Face).

## What you need

- A Limitless pendant.
- A Mac with Bluetooth, in range of the pendant, running macOS with the Command Line Tools installed
  (`xcode-select --install`). It builds and runs Earshot.app.
- [uv](https://docs.astral.sh/uv/) to run `earshotd`.
- A Hugging Face account that has accepted the terms of the gated
  [pyannote/speaker-diarization-community-1](https://huggingface.co/pyannote/speaker-diarization-community-1) model.

## Pick a setup

- **One Mac (the default).** `earshotd` and the app run on the same Mac and talk over loopback. Nothing leaves the
  machine and no firewall or proxy is involved. On an M4 Pro, transcription plus speaker labelling ran at about 1.5x
  realtime, so one Mac keeps up with a full day of audio. Pick this unless you already have a server you want to use.
- **Remote Linux server.** `earshotd` runs under systemd on a Linux server, behind a TLS proxy you operate, and the
  Mac only runs the app. Pick this if the Mac is not always on or you want the transcripts kept on a server.

## The pyannote model

`earshotd` loads the diarization model from a local directory and never from the Hugging Face Hub, so no token is
needed at runtime. Both installers download it for you at the revision and file hashes pinned in
[`earshotd/src/earshotd/models.py`](earshotd/src/earshotd/models.py): they ask for a Hugging Face read token at a
hidden prompt and never write it anywhere.

Only for a host with no route to Hugging Face, fetch it on another machine first, from a checkout of this repository,
then pass the resulting directory to the installer:

```sh
mkdir -p ~/earshot/models
uv run --project earshotd python -I -m earshotd.models fetch ~/earshot/models
# installs ~/earshot/models/speaker-diarization-community-1
```

## Setup 1: one Mac

`deploy/macos/install.sh` installs `earshotd` as a launchd agent in your login session. It starts when you log in,
restarts if it dies, and runs confined: launchd holds the loopback socket, and a `sandbox-exec` profile gives the
process no network of its own, no other program, no mach services beyond the three the pipeline uses, writes only to
its data and temp directories, and no reads in your home beyond its own files. Run it from a checkout of this
repository, as yourself (not root), with [uv](https://docs.astral.sh/uv/) and `openssl` installed.

1. Install the service. It asks for a Hugging Face read token at a hidden prompt and downloads the pinned model
   (or pass a directory fetched as in "The pyannote model"):

   ```sh
   deploy/macos/install.sh
   ```

   It copies the code and a Python into `~/Library/Application Support/earshotd`, fetches the pinned models there,
   seeds `~/.config/earshot/earshotd.toml` with a freshly generated `upload` token and `read` token and `listen =
   "127.0.0.1:8080"`, and loads the job `casa.mal.earshotd`. Every file is private to you. A rerun updates the code
   and the job and keeps your config, your data and a matching diarization copy. The service listens on `127.0.0.1`
   or `::1` only; the script refuses any other `listen`.

2. Read the tokens from `~/.config/earshot/earshotd.toml`.

3. Check that it answers. The first start loads the models and takes tens of seconds:

   ```sh
   curl http://127.0.0.1:8080/v1/health
   ```

   The service logs to `~/Library/Logs/earshotd.log` (it is not rotated).

4. Build and configure the app as described in [app/README.md](app/README.md) (Build and sign, then Config). In
   `~/.config/earshot/config.toml` set:

   ```toml
   server_url = "http://127.0.0.1:8080"
   token = "<the upload token from ~/.config/earshot/earshotd.toml>"
   ```

   Then make it private; the app refuses a config others can read:

   ```sh
   chmod 600 ~/.config/earshot/config.toml
   ```

   Plain `http` is accepted only because the server is on this Mac.

The `earshot` command (see "Naming speakers") lives in the service's venv at
`~/Library/Application Support/earshotd/app/.venv/bin/earshot`. It runs as you, outside the sandbox, against the same
config:

```sh
EARSHOTD_CONFIG=~/.config/earshot/earshotd.toml "$HOME/Library/Application Support/earshotd/app/.venv/bin/earshot" voice add <name> --from <ms> --to <ms>
```

To stop and remove the job, keeping your data:

```sh
launchctl bootout gui/$(id -u)/casa.mal.earshotd
rm ~/Library/LaunchAgents/casa.mal.earshotd.plist
```

Your transcripts stay in `~/Library/Application Support/earshotd/data`, and `deploy/macos/install.sh` loads the job
again. Delete `~/Library/Application Support/earshotd` too only when you want the data gone.

## Setup 2: remote Linux server

`deploy/install.sh` installs `earshotd` under systemd on a Linux server. Read it first. It is tested on Debian and
Ubuntu (it persists the firewall through `/etc/nftables.conf` and `nftables.service`); other distributions are
untested, and contributions that make them work are welcome.

On a loopback `listen` (the default) it needs no firewall. On a network `listen` it needs nftables
(`sudo apt install nftables`) and loads a firewall that admits the listen port only from loopback and the hosts you
name in `proxy_addresses` in `/etc/earshot/earshotd.toml`; it refuses to install without them.

The hop from the proxy to `earshotd` is mutual TLS (1.3), not plain http. `earshotd` serves TLS itself and completes
a handshake only with a client that presents the proxy's certificate, so tokens, audio and transcripts are encrypted
on that segment and a host that merely spoofs the proxy's address reaches no HTTP. The firewall is a second layer.
Each side generates its own private key and only certificates are copied between hosts; the keys never leave the
host that made them. `earshotd` refuses to start on a network `listen` without the TLS lines below.

1. On the server, install the tools `install.sh` needs, and uv for root without touching any shell file:

   ```sh
   sudo apt install git curl openssl
   curl -LsSf https://astral.sh/uv/install.sh | sudo env UV_INSTALL_DIR=/usr/local/bin UV_NO_MODIFY_PATH=1 sh
   git clone https://github.com/nickpending/earshot && cd earshot
   ```

   Have a Hugging Face read token ready, from an account that has accepted the terms of the model above.
2. On the proxy host, create the proxy's key and certificate. Copy `deploy/proxy-cert.sh` there (it needs only bash
   and openssl) and run it with a directory and, optionally, the group your proxy runs as, so that user can read
   the key:

   ```sh
   sudo ./proxy-cert.sh /etc/caddy/earshot caddy
   ```

   This writes `proxy.key` (never copy it) and `proxy.crt` there and prints the certificate's path and SHA-256
   fingerprint.
3. On the server, as root, from a checkout of this repository:

   ```sh
   sudo deploy/install.sh
   ```

   It asks for your Hugging Face token at a hidden prompt and downloads the diarization model at the pinned revision;
   the token is never written to a file, an argument or the environment. On a host with no route to Hugging Face,
   pass a directory fetched elsewhere (see "The pyannote model") instead:
   `sudo deploy/install.sh /path/to/speaker-diarization-community-1-snapshot`. Every file is checked against the
   pinned sha256 before it replaces anything, and a rerun keeps an installed copy that matches without asking.

   The script seeds `/etc/earshot/earshotd.toml` from `deploy/earshotd.toml.example` with generated tokens, generates
   `server.key` and `server.crt` in `/etc/earshot/tls` (printing the certificate's fingerprint), and starts the
   service on loopback.
4. Exchange certificates, checking each fingerprint against the one printed on the host that made it. Copy the
   server's `/etc/earshot/tls/server.crt` to the proxy host (for example `/etc/caddy/earshot/server.crt`), and the
   proxy's `proxy.crt` to the server's `/etc/earshot/tls/proxy.crt`.
5. On the server, edit `/etc/earshot/earshotd.toml`: set `listen` to the address the proxy reaches (for example
   `0.0.0.0:8080`), uncomment the three `tls_cert`, `tls_key` and `tls_proxy_certs` lines, and add
   `proxy_addresses = ["<the proxy host's IP address>"]` (IPv4 or IPv6 addresses, no ranges), then rerun
   `sudo deploy/install.sh`. It asks for no token, and it stops before enabling anything if a TLS file will not
   load, if `nft` is missing, or if `proxy_addresses` is empty, naming what to fix. No file in the checkout is
   edited. To pin several proxies, list each one's `proxy.crt` in
   `tls_proxy_certs`.
6. Point your proxy at `https://<server>:8080` with this contract, and set the app's `server_url` to the proxy's
   `https` address with the `upload` token from `/etc/earshot/earshotd.toml`. The app refuses plain `http` to
   anything but this Mac. Build and configure the app per [app/README.md](app/README.md).
   - Speak `https` to `earshotd`.
   - Verify the server's certificate against `server.crt`, as the name `earshotd` (the certificate is not issued for
     the server's address).
   - Present `proxy.crt` and `proxy.key` as the client certificate.

   Measured with Caddy v2.10.2, this route (the paths are where you put the files):

   ```
   reverse_proxy https://<server>:8080 {
   	transport http {
   		tls_server_name earshotd
   		tls_trust_pool file /etc/caddy/earshot/server.crt
   		tls_client_auth /etc/caddy/earshot/proxy.crt /etc/caddy/earshot/proxy.key
   	}
   }
   ```

   Other proxies get the contract above, not a tested snippet.
7. Check the hop with `deploy/verify.sh`, which takes a role. `local` runs on the server and probes 127.0.0.1.
   `proxy` runs on the proxy host and `outside` on any other host; both need `EARSHOT_VERIFY_HOST`, the server's
   address, and refuse to probe without it. `proxy` also needs `EARSHOT_VERIFY_TLS_DIR` and read access to the key:

   ```sh
   deploy/verify.sh local                                                       # on the server
   EARSHOT_VERIFY_HOST=<server address> EARSHOT_VERIFY_TLS_DIR=/etc/caddy/earshot \
     deploy/verify.sh proxy                                                     # on the proxy host
   EARSHOT_VERIFY_HOST=<server address> deploy/verify.sh outside                # on any other host
   ```

Two settings are sized to the host. The memory bound is `MemoryMax=6G` in `earshotd.service`, sized to an 8 GiB
server; install.sh rewrites that unit on every run, so change it with a drop-in (`sudo systemctl edit earshotd`,
then `[Service]` and `MemoryMax=...`), which install.sh never touches. The day is cut in the zone the optional
`timezone` key in `earshotd.toml` names (an IANA name such as `Europe/Berlin`), else in the server's own zone, and in
UTC on a server with none: set `timezone` if the server's zone is UTC and your days are not. Changing it on a store
that already holds days needs `earshot rebuild`.

Certificates are valid for 3650 days and nothing renews them: when one expires the proxy answers 502. To rotate,
delete that side's key, rerun its script (`proxy-cert.sh`, or `install.sh` on the server) and copy the new
certificate across; list the old and new `proxy.crt` in `tls_proxy_certs` until the proxy has switched.

If the proxy cannot do mutual TLS, `insecure_plain_http = true` in place of the TLS lines serves plain http on the
network address. It is an opt-in for a segment you trust, with the firewall as the only protection, and `earshotd`
prints a line saying so at every start.

## Naming speakers

Until you enrol a voice, transcript lines carry the labels S1, S2 and so on, per day. To name a speaker, find a range
of stored audio where only that person speaks and enrol it:

```sh
earshot voice add <name> --from <ms> --to <ms>
```

`earshot` is the CLI that ships with `earshotd`. On the Mac service use the venv path shown in Setup 1. On the Linux
server run it as the service user against the service's config:

```sh
sudo -u earshot env EARSHOTD_CONFIG=/etc/earshot/earshotd.toml /opt/earshot/.venv/bin/earshot voice add <name> --from <ms> --to <ms>
```

It stores a voiceprint of that speaker, not a trained model, and matches later speech against it. Voiceprints stay in
your own store, in the `data_dir` of your `earshotd`.

## Reading transcripts

Every route but `/v1/health` needs the `read` token as `Authorization: Bearer <token>`. The main ones:

- `GET /v1/days?from=YYYY-MM-DD&to=YYYY-MM-DD`: the days that have transcripts, newest first.
- `GET /v1/days/<date>/transcript`: one day's lines; send `Accept: text/markdown` for one
  `- HH:MM:SS **speaker** text` line each.
- `GET /v1/transcript?from=<ms>&to=<ms>`: any range, crossing days (at most 50,000 lines per call).
- `GET /v1/status`: pages stored, pages waiting for transcription, and whether the worker is running.

The full list is the Read API table in [DESIGN.md](DESIGN.md).

## Importing your Limitless archive

If you used the Limitless cloud, `earshot import-limitless` pulls your lifelogs and their audio into the same store
and transcribes them through the same pipeline. It asks for your Limitless API key at a hidden prompt (or reads it
from the first line of stdin) and never stores it. A rerun downloads only what is missing. Run it with the same
command form as `voice add` above, for example on the Linux server:

```sh
sudo -u earshot env EARSHOTD_CONFIG=/etc/earshot/earshotd.toml /opt/earshot/.venv/bin/earshot import-limitless
```

## More

- [DESIGN.md](DESIGN.md): how the app and `earshotd` are built, and the upload contract.
- [docs/protocol.md](docs/protocol.md): the Limitless pendant's Bluetooth protocol as this project uses it.
- [app/README.md](app/README.md): building, configuring and testing Earshot.app.

## Credits and license

Earshot is released under the [MIT license](LICENSE). The pendant protocol codec is vendored from
[omi](https://github.com/BasedHardware/omi), also MIT; its notice is
[app/Sources/EarshotCore/OMI-LICENSE-NOTICE.md](app/Sources/EarshotCore/OMI-LICENSE-NOTICE.md). Transcription uses
[faster-whisper](https://github.com/SYSTRAN/faster-whisper) and speaker labelling uses
[pyannote.audio](https://github.com/pyannote/pyannote-audio).
