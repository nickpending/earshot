#!/usr/bin/env bash
# Install earshotd on a Debian or Ubuntu server. Run as root from a checkout of the repo:
#   sudo deploy/install.sh [/path/to/speaker-diarization-community-1-snapshot]
# With no argument, the gated pyannote model is downloaded at the revision pinned in
# earshotd/src/earshotd/models.py, with a Hugging Face read token typed at a hidden prompt; an
# installed copy that matches the pins is kept without asking. A snapshot directory installs from
# that instead, offline. Idempotent. Never touches any user's shell files. Requires uv on root's
# PATH (/usr/local/bin/uv, installed with UV_NO_MODIFY_PATH=1) and openssl; nft only when
# `listen` in /etc/earshot/earshotd.toml is a network address.
# The logic lives in deploy/install-lib.sh, which earshotd/tests/test_install_lib.py tests.
set -euo pipefail

SNAPSHOT="${1:-}"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
CONFIG=/etc/earshot/earshotd.toml
MODELS=/opt/earshot/models
TLS_DIR=/etc/earshot/tls
# shellcheck source=deploy/install-lib.sh
. "$REPO/deploy/install-lib.sh"
[ "$(id -u)" -eq 0 ] || { echo "run as root" >&2; exit 1; }
[ -z "$SNAPSHOT" ] || [ -d "$SNAPSHOT" ] || { echo "snapshot dir not found: $SNAPSHOT" >&2; exit 1; }
require_tools

id earshot >/dev/null 2>&1 || useradd --system --home-dir /var/lib/earshot --shell /usr/sbin/nologin earshot

install -d -o earshot -g earshot -m 0750 /var/lib/earshot
install -d -o root -g earshot -m 0750 /etc/earshot

# Config: seeded from the example only if absent, with freshly generated tokens (never the
# example's placeholders, which are public in the repo); root:earshot 0640 on every run.
config_step "$REPO/deploy/earshotd.toml.example" "$CONFIG"

# The proxy hop's server side: earshotd's own key and certificate, generated here when absent and
# never copied. It prints server.crt's fingerprint to copy to the proxy host; the proxy's proxy.crt
# (deploy/proxy-cert.sh, run on that host) goes into the same directory.
server_tls "$TLS_DIR"

# Everything below /var/lib/earshot is private to the service user, files an older install made too.
tighten_data /var/lib/earshot
for old in /var/lib/earshot/models /var/lib/earshot/hf; do
  [ ! -e "$old" ] || echo "note: $old is no longer used and is service-writable; remove it once earshotd runs" >&2
done

# Code and venv under /opt/earshot (uv-managed Python kept there too).
install -d -o root -g root -m 0755 /opt/earshot
cp "$REPO/earshotd/pyproject.toml" "$REPO/earshotd/uv.lock" /opt/earshot/
rm -rf /opt/earshot/src
cp -r "$REPO/earshotd/src" /opt/earshot/src
UV_PYTHON_INSTALL_DIR=/opt/earshot/python UV_PROJECT_ENVIRONMENT=/opt/earshot/.venv \
  uv sync --project /opt/earshot --frozen --no-dev
chmod -R a+rX /opt/earshot

# Models: root-owned, so a compromised service cannot rewrite what it loads. The diarization
# model is the five files pinned in earshotd/src/earshotd/models.py, checked by sha256 before they
# replace anything (given a snapshot dir, installed from it; else a matching installed copy is
# kept, else downloaded with a token read at a hidden prompt). Whisper is fetched as root at its
# pinned revision (the unit runs HF_HUB_OFFLINE=1 and cannot download).
install -d -o root -g root -m 0755 "$MODELS"
install_diarization "$MODELS" "$SNAPSHOT"
fetch_whisper "$MODELS"
chown -R root:root "$MODELS"
chmod -R a+rX,go-w "$MODELS"

# Nothing is enabled, started or restarted before this passes: config.load refuses a config that
# is exposed or has placeholder tokens or a bad listen (a network address with neither the TLS
# settings nor insecure_plain_http), each model must be root-owned, and the TLS files must load
# the way earshotd loads them.
config_values "$CONFIG" >/dev/null
check_models "$CONFIG" || { echo "fix the lines above in $CONFIG, then rerun install.sh" >&2; exit 1; }
check_tls "$CONFIG" || { echo "fix the TLS files or lines above in $CONFIG, then rerun install.sh" >&2; exit 1; }

# Firewall, only on a network listen: the daemon's port (from listen) only from loopback and the
# proxy_addresses in the config, loaded now and at boot. render_units refuses a network listen with
# no proxy_addresses; firewall_step refuses one without nft. A loopback listen installs none.
RENDERED="$(mktemp -d)"
trap 'rm -rf "$RENDERED"' EXIT
render_units "$CONFIG" "$REPO/deploy/earshotd.socket" "$REPO/deploy/nftables-earshot.conf" \
  "$RENDERED/earshotd.socket" "$RENDERED/earshot.conf"
firewall_step "$RENDERED/earshot.conf" /etc/nftables.d /etc/nftables.conf

# Service: earshotd.socket holds the listening socket, earshotd runs with no network of its own.
install -m 0644 "$RENDERED/earshotd.socket" /etc/systemd/system/earshotd.socket
install -m 0644 "$REPO/deploy/earshotd.service" /etc/systemd/system/earshotd.service
systemctl daemon-reload
systemctl enable earshotd.socket earshotd
# The old unit held the port itself: stop it before the socket takes the port over.
systemctl stop earshotd
systemctl restart earshotd.socket
systemctl start earshotd
echo "installed. Next: deploy/verify.sh local (on the server), reboot, then verify.sh local again; then, with EARSHOT_VERIFY_HOST set to the server's address, verify.sh proxy from the proxy host (also EARSHOT_VERIFY_TLS_DIR) and verify.sh outside from any other host."
