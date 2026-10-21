#!/usr/bin/env bash
# Functions deploy/install.sh runs. Sourced, never executed; earshotd/tests/test_install_lib.py
# drives each one with real bash. Written for bash 3.2 and up (no mapfile, no ${var,,}).

# The earshotd venv's python: the only interpreter that may read the config (earshotd.config.load).
EARSHOT_PY="${EARSHOT_PY:-/opt/earshot/.venv/bin/python}"

# seed_config <example> <dest>: write <dest> from <example> with each REPLACE_WITH placeholder
# replaced by its own fresh token. A token lives only in shell variables and the printf builtin,
# so no process is ever handed one as an argument.
seed_config() {
  local example=$1 dest=$2 content upload_token read_token
  upload_token=$(openssl rand -hex 32) || return 1
  read_token=$(openssl rand -hex 32) || return 1
  IFS= read -r -d '' content < "$example" || true
  content=${content//REPLACE_WITH_ANOTHER_openssl_rand_hex_32/$read_token}
  content=${content//REPLACE_WITH_openssl_rand_hex_32/$upload_token}
  (umask 027; printf '%s' "$content" > "$dest")
}

# config_step <example> <config>: seed the config when absent, then on every run, seeded or not,
# make it root:earshot 0640 (earshotd refuses to load a config others can read).
config_step() {
  local example=$1 cfg=$2
  if [ ! -f "$cfg" ]; then
    seed_config "$example" "$cfg"
    echo "seeded $cfg with generated upload and read tokens; read them from that file" >&2
  fi
  chown root:earshot "$cfg"
  chmod 0640 "$cfg"
}

# config_values <config>: print, one per line, exactly as earshotd.config.load returns them: listen
# as host:port, whisper_model, diarization_model, `loopback` or `network` for the listen host, then
# one `ip <address>` or `ip6 <address>` line per proxy_addresses entry. Fails with load's error
# when it refuses the config.
config_values() {
  "$EARSHOT_PY" -I -c '
import ipaddress
import sys
from earshotd.config import ConfigError, is_loopback, load

try:
    config = load(sys.argv[1])
except ConfigError as e:
    sys.exit(f"earshotd config: {e}")
host, port = config.host_port
print(f"{host}:{port}")
print(config.whisper_model)
print(config.diarization_model)
print("loopback" if is_loopback(host) else "network")
for address in config.proxy_addresses:
    family = "ip6" if ipaddress.ip_address(address).version == 6 else "ip"
    print(f"{family} {address}")
' "$1"
}

# server_tls <dir>: earshotd's side of the proxy hop. When <dir>/server.key is absent, generate the
# server's EC P-256 key and a self-signed certificate (CN and SAN earshotd, serverAuth and
# clientAuth, 3650 days) on this host; the key is never copied anywhere. On every run, new files
# or not, set <dir> root:earshot 0750, server.key root:earshot 0640 and every *.crt root:root 0644
# (earshotd refuses a key others can read and a certificate others can write), then print
# server.crt's path and SHA-256 fingerprint: copy that certificate to the proxy host, and put the
# proxy's proxy.crt (deploy/proxy-cert.sh) in <dir>. Rotate by deleting server.key and rerunning.
server_tls() {
  local dir=$1 tmp crt
  if [ ! -e "$dir/server.key" ]; then
    (umask 077; mkdir -p "$dir") || return 1
    tmp=$(umask 077; mktemp -d "$dir/.new.XXXXXX") || return 1
    openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:P-256 -noenc -days 3650 \
      -subj /CN=earshotd -keyout "$tmp/server.key" -out "$tmp/server.crt" \
      -addext subjectAltName=DNS:earshotd \
      -addext basicConstraints=critical,CA:FALSE \
      -addext keyUsage=digitalSignature \
      -addext extendedKeyUsage=serverAuth,clientAuth \
      || { rm -rf "$tmp"; return 1; }
    # The key moves last: its presence is what stops the next run from regenerating.
    mv "$tmp/server.crt" "$dir/server.crt" || return 1
    mv "$tmp/server.key" "$dir/server.key" || return 1
    rmdir "$tmp"
  fi
  if [ ! -f "$dir/server.crt" ]; then
    echo "$dir/server.key exists but $dir/server.crt is missing: remove server.key to regenerate both" >&2
    return 1
  fi
  chown root:earshot "$dir"
  chmod 0750 "$dir"
  chown root:earshot "$dir/server.key"
  chmod 0640 "$dir/server.key"
  for crt in "$dir"/*.crt; do
    chown root:root "$crt"
    chmod 0644 "$crt"
  done
  echo "certificate to copy to the proxy host: $dir/server.crt" >&2
  openssl x509 -in "$dir/server.crt" -noout -fingerprint -sha256 >&2
}

# check_tls <config>: when the config sets the TLS settings, build the context earshotd builds at
# start (earshotd.tls.server_context), so a key others can read or a certificate that is missing
# or writable by others stops the install, naming the file, before anything is enabled. A config
# without TLS settings passes.
check_tls() {
  "$EARSHOT_PY" -I -c '
import sys
from earshotd.config import ConfigError, load
from earshotd.tls import server_context

try:
    config = load(sys.argv[1])
    if config.tls:
        server_context(config)
except ConfigError as e:
    sys.exit(f"earshotd tls: {e}")
' "$1"
}

# render_units <config> <socket-template> <nft-template> <socket-out> <nft-out>: the socket's
# ListenStream and the firewall's dport both come from the config's listen. On a loopback listen
# only the socket is written and no firewall is needed. On a network listen the template's marker
# line becomes one accept line per proxy_addresses entry, and an empty proxy_addresses is refused.
# Writes nothing when the config is refused.
render_units() {
  local cfg=$1 socket_tpl=$2 nft_tpl=$3 socket_out=$4 nft_out=$5
  local vals line n=0 listen port verdict accepts= socket nft
  vals=$(config_values "$cfg") || return 1
  while IFS= read -r line; do
    n=$((n + 1))
    case $n in
      1) listen=$line; port=${listen##*:} ;;
      2|3) ;;
      4) verdict=$line ;;
      *) accepts="${accepts:+$accepts$'\n\t\t'}${line%% *} saddr ${line#* } tcp dport $port accept" ;;
    esac
  done <<< "$vals"
  IFS= read -r -d '' socket < "$socket_tpl" || true
  IFS= read -r -d '' nft < "$nft_tpl" || true
  socket=${socket/ListenStream=127.0.0.1:8080/ListenStream=$listen}
  case $socket in
    *"ListenStream=$listen"*) ;;
    *) echo "$socket_tpl has no ListenStream=127.0.0.1:8080 line to render" >&2; return 1 ;;
  esac
  if [ "$verdict" = loopback ]; then
    printf '%s' "$socket" > "$socket_out"
    echo "listen $listen is loopback: no firewall is needed" >&2
    return 0
  fi
  if [ -z "$accepts" ]; then
    echo "listen $listen is a network address: set proxy_addresses = [\"<the proxy host's address>\"] in $cfg so the firewall admits only the proxy" >&2
    return 1
  fi
  nft=${nft//dport 8080/dport $port}
  case $nft in
    *"dport $port"*PROXY_ACCEPT_LINES*|*PROXY_ACCEPT_LINES*"dport $port"*) ;;
    *) echo "$nft_tpl has no dport 8080 match and PROXY_ACCEPT_LINES marker to render" >&2; return 1 ;;
  esac
  nft=${nft/PROXY_ACCEPT_LINES/$accepts}
  printf '%s' "$socket" > "$socket_out"
  printf '%s' "$nft" > "$nft_out"
}

# firewall_step <rendered-nft> <nftables.d dir> <nftables.conf>: load the rendered firewall now and
# at boot. A missing <rendered-nft> (render_units wrote none: loopback listen) loads nothing and
# does not need nft. Otherwise nft must exist, the file is checked before it is loaded, and
# nftables.service is enabled so it loads again at boot (Debian's layout).
firewall_step() {
  local rendered=$1 dir=$2 main=$3
  [ -f "$rendered" ] || return 0
  command -v nft >/dev/null || { echo "nft not installed (apt install nftables): listen is a network address, so the firewall must load" >&2; return 1; }
  install -d "$dir"
  install -m 0644 "$rendered" "$dir/earshot.conf"
  grep -qF "include \"$dir/*.conf\"" "$main" 2>/dev/null \
    || echo "include \"$dir/*.conf\"" >> "$main"
  nft -c -f "$dir/earshot.conf"
  nft -f "$dir/earshot.conf"
  systemctl enable nftables
}

# require_tools: uv and openssl must be on root's PATH. nft is not required here: only a network
# listen needs it, and firewall_step asks for it then.
require_tools() {
  command -v uv >/dev/null || { echo "uv not on root's PATH; expected /usr/local/bin/uv (install with UV_NO_MODIFY_PATH=1 so no shell files change)" >&2; return 1; }
  command -v openssl >/dev/null || { echo "openssl not installed (apt install openssl)" >&2; return 1; }
}

# check_model_location <config key> <value>: succeed only when <value> is an existing directory
# whose every file, folder and parent is root-owned and not group- or other-writable, so the
# service user can never rewrite a model it loads. A bare model name fails: it would download.
check_model_location() {
  local key=$1 value=$2 dir p bad
  if [ ! -d "$value" ]; then
    echo "refusing $key = \"$value\": not an existing directory; point it at a model directory under /opt/earshot/models" >&2
    return 1
  fi
  dir=$(cd -P "$value" && pwd -P) || return 1
  # check_models calls this inside `||`, where bash ignores set -e: a find that could not read part
  # of the tree must refuse here, or the unread part would pass unchecked.
  if ! bad=$(find "$dir" \( ! -user root -o \( ! -type l -a \( -perm -020 -o -perm -002 \) \) \) -print); then
    echo "refusing $key = \"$value\": find could not read all of $dir, so not every file in it was checked; models must be root-owned under /opt/earshot/models" >&2
    return 1
  fi
  if [ -n "$bad" ]; then
    echo "refusing $key = \"$value\": ${bad%%$'\n'*} is not root-owned or is writable by group or other; models must be root-owned under /opt/earshot/models" >&2
    return 1
  fi
  p=$dir
  while [ "$p" != / ]; do
    p=${p%/*}
    [ -n "$p" ] || p=/
    if ! bad=$(find "$p" -maxdepth 0 \( ! -user root -o -perm -020 -o -perm -002 \) -print); then
      echo "refusing $key = \"$value\": find could not read its parent $p, so the parent was not checked; models must be root-owned under /opt/earshot/models" >&2
      return 1
    fi
    if [ -n "$bad" ]; then
      echo "refusing $key = \"$value\": its parent $p is not root-owned or is writable by group or other; models must be root-owned under /opt/earshot/models" >&2
      return 1
    fi
  done
}

# check_models <config>: check_model_location for whisper_model and diarization_model, naming
# every refused line in one run.
check_models() {
  local vals listen whisper diarization rc=0
  vals=$(config_values "$1") || return 1
  { IFS= read -r listen; IFS= read -r whisper; IFS= read -r diarization; } <<< "$vals"
  check_model_location whisper_model "$whisper" || rc=1
  check_model_location diarization_model "$diarization" || rc=1
  return $rc
}

# tighten_data <dir>: remove group and other permission from everything below <dir>, leaving the
# directory itself and every owner bit as they are. It runs as the earshot user, which owns the
# tree: a compromised service that swaps an entry for a symlink mid-run can then only make chmod
# touch files earshot owns, never a root file. Symlinks are skipped as well.
tighten_data() {
  runuser -u earshot -- find "$1" -mindepth 1 ! -type l -exec chmod go= {} +
}

# fetch_whisper <models-dir>: download the whisper model at the revision pinned in
# earshotd/models.py into <models-dir>/<its name>, through a throwaway HF_HOME that is removed
# afterwards.
fetch_whisper() {
  local models=$1 hf rc=0
  hf=$(mktemp -d) || return 1
  HF_HOME=$hf HF_HUB_OFFLINE=0 "$EARSHOT_PY" -I -c '
import sys
from faster_whisper.utils import download_model
from earshotd.models import WHISPER

print(download_model(WHISPER.repo, output_dir=f"{sys.argv[1]}/{WHISPER.name}", revision=WHISPER.revision))
' "$models" || rc=$?
  rm -rf "$hf"
  return $rc
}

# install_diarization <models-dir> [<snapshot dir>]: put the pyannote model, checked file by file
# against earshotd/models.py's pins, at <models-dir>/speaker-diarization-community-1, through a
# throwaway HF_HOME that is removed afterwards. With a snapshot directory it installs from that,
# with no prompt and no network. Without one, an installed copy that matches the pins is kept
# and nothing is asked or fetched; otherwise the pinned revision is downloaded with a Hugging Face
# token read from the terminal (or, with no terminal, the first line of stdin).
install_diarization() {
  local models=$1 snapshot=${2:-} name=speaker-diarization-community-1 hf problems rc=0
  hf=$(mktemp -d) || return 1
  if [ -n "$snapshot" ]; then
    HF_HOME=$hf HF_HUB_OFFLINE=0 "$EARSHOT_PY" -I -m earshotd.models place "$snapshot" "$models" || rc=$?
  elif [ -d "$models/$name" ] && problems=$("$EARSHOT_PY" -I -m earshotd.models check "$models/$name"); then
    echo "installed copy $models/$name matches the pinned revision; keeping it"
  else
    if [ -d "$models/$name" ]; then
      echo "installed copy $models/$name does not match the pinned revision:"
      echo "$problems"
    else
      echo "no installed copy at $models/$name"
    fi
    echo "fetching the pinned revision from Hugging Face (the model is gated: accept its terms on huggingface.co first)"
    HF_HOME=$hf HF_HUB_OFFLINE=0 "$EARSHOT_PY" -I -m earshotd.models fetch "$models" || rc=$?
  fi
  rm -rf "$hf"
  return $rc
}
