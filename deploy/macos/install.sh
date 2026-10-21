#!/usr/bin/env bash
# Install earshotd on a Mac as a per-user launchd agent. Run as the logged-in user (not root) from a
# checkout of the repo:
#   deploy/macos/install.sh [/path/to/speaker-diarization-community-1-snapshot]
# With no argument, the gated pyannote model is downloaded at the revision pinned in
# earshotd/src/earshotd/models.py, with a Hugging Face read token typed at a hidden prompt; an
# installed copy that matches the pins is kept without asking. A snapshot directory installs from
# that instead, offline. Idempotent: a rerun updates the code and the job and keeps the config, the
# data and a matching diarization copy. Never touches any shell file. Requires uv, openssl and
# /usr/bin/sandbox-exec.
#
# Everything lives under ~/Library/Application Support/earshotd (app, python, models, data, tmp);
# the config is ~/.config/earshot/earshotd.toml, the log ~/Library/Logs/earshotd.log, the job
# ~/Library/LaunchAgents/casa.mal.earshotd.plist. deploy/macos/launchd.py renders the plist and the
# sandbox profile; the shared logic (seeding, pinned model fetches) is deploy/install-lib.sh.
set -euo pipefail

SNAPSHOT="${1:-}"
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
LABEL=casa.mal.earshotd
ROOT="$HOME/Library/Application Support/earshotd"
CONFIG="$HOME/.config/earshot/earshotd.toml"
LOG="$HOME/Library/Logs/earshotd.log"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
PROFILE="$ROOT/earshotd.sb"
EARSHOT_PY="$ROOT/app/.venv/bin/python"
# shellcheck source=deploy/install-lib.sh
. "$REPO/deploy/install-lib.sh"

[ "$(uname -s)" = Darwin ] || { echo "this installs the macOS service; on Linux use deploy/install.sh" >&2; exit 1; }
[ "$(id -u)" -ne 0 ] || { echo "run as your own user, not root: the job belongs to your login session" >&2; exit 1; }
[ -z "$SNAPSHOT" ] || [ -d "$SNAPSHOT" ] || { echo "snapshot dir not found: $SNAPSHOT" >&2; exit 1; }
command -v uv >/dev/null || { echo "uv not found; install it from https://docs.astral.sh/uv/" >&2; exit 1; }
command -v openssl >/dev/null || { echo "openssl not found" >&2; exit 1; }
[ -x /usr/bin/sandbox-exec ] || { echo "/usr/bin/sandbox-exec is missing: the service is not run unconfined" >&2; exit 1; }

# Everything this script creates is private to the user.
umask 077
for dir in "$ROOT" "$ROOT/app" "$ROOT/python" "$ROOT/models" "$ROOT/data" "$ROOT/tmp" "$HOME/.config/earshot"; do
  mkdir -p "$dir"
  chmod 0700 "$dir"
done
mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"

# Code and venv under the root, with the uv-managed Python kept there too, so the sandboxed job
# reads its interpreter from inside its own root. The bytecode is compiled now: the job cannot
# write __pycache__ into its read-only venv.
cp "$REPO/earshotd/pyproject.toml" "$REPO/earshotd/uv.lock" "$ROOT/app/"
rm -rf "$ROOT/app/src"
cp -R "$REPO/earshotd/src" "$ROOT/app/src"
UV_PYTHON_INSTALL_DIR="$ROOT/python" UV_PYTHON_PREFERENCE=only-managed UV_PROJECT_ENVIRONMENT="$ROOT/app/.venv" \
  uv sync --project "$ROOT/app" --frozen --no-dev --compile-bytecode
# That compiles site-packages only. The job runs python -I, which ignores the PYTHON* variables,
# and the sandbox denies its __pycache__ writes: compile the interpreter's stdlib and earshotd's
# own code here too.
"$EARSHOT_PY" -I -m compileall -q \
  "$("$EARSHOT_PY" -I -c 'import sysconfig; print(sysconfig.get_path("stdlib"))')" "$ROOT/app/src" >/dev/null

# Config: seeded from the example only if absent, with freshly generated tokens (never the
# example's placeholders, which are public in the repo), then pointed at the root. A config that
# exists is kept as it is. Mode 0600 on every run (earshotd refuses one others can read).
if [ ! -f "$CONFIG" ]; then
  seed_config "$REPO/deploy/earshotd.toml.example" "$CONFIG"
  "$EARSHOT_PY" -I "$REPO/deploy/macos/launchd.py" paths "$CONFIG" "$ROOT"
  echo "seeded $CONFIG with generated upload and read tokens; read them from that file" >&2
fi
chmod 0600 "$CONFIG"

# Models: the diarization model is the five files pinned in earshotd/src/earshotd/models.py, checked
# by sha256 before they replace anything (from a snapshot dir; else a matching installed copy is
# kept; else downloaded with a token read at a hidden prompt). Whisper is fetched at its pinned
# revision (the job runs HF_HUB_OFFLINE=1 and cannot download).
install_diarization "$ROOT/models" "$SNAPSHOT"
fetch_whisper "$ROOT/models"

# Nothing is loaded into launchd unless this passes: the config must load, listen must be 127.0.0.1
# or ::1, and every model must be under the root. It writes the sandbox profile and the plist.
"$EARSHOT_PY" -I "$REPO/deploy/macos/launchd.py" render "$CONFIG" "$ROOT" "$HOME" "$LOG" "$LABEL" "$PLIST" "$PROFILE" \
  || { echo "fix the line above in $CONFIG, then rerun deploy/macos/install.sh" >&2; exit 1; }
[ ! -e "$LOG" ] || chmod 0600 "$LOG"

# Replace the job: boot out the old one, wait until launchd has let go of it, then load the new.
DOMAIN="gui/$(id -u)"
launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
tries=0
while launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; do
  tries=$((tries + 1))
  [ "$tries" -le 60 ] || { echo "launchd still holds $LABEL after 30 seconds" >&2; exit 1; }
  sleep 0.5
done
launchctl bootstrap "$DOMAIN" "$PLIST"

listen=$(config_values "$CONFIG" | head -n 1)
host=${listen%:*}
case $host in *:*) host="[$host]" ;; esac
echo "installed. The first start loads the models and takes tens of seconds."
echo "health: curl http://$host:${listen##*:}/v1/health"
echo "log: $LOG"
