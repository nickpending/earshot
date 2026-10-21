#!/usr/bin/env bash
# C11 probes against the earshotd server. Usage: verify.sh <role>
#   local    on the server: unit enabled+active as earshot, config-only settings, nft loaded when listen is a network address, data dir private;
#            when the config sets the TLS settings, health and the worker over mutual TLS, a handshake
#            without a client certificate refused, and plain http refused
#   proxy    from the proxy host, over mutual TLS with no token: /v1/health answers 200 with exactly {"ok": true}
#            and no Server header; /v1/status and /v1/openapi.json answer 401; a handshake without a
#            client certificate is refused and plain http gets no answer
#   outside  from any host that is not the proxy: a direct connection to the port is refused or dropped
# Run `local` after a reboot of the server for the no-manual-step check.
# proxy and outside need EARSHOT_VERIFY_HOST, the server's address; they fail without it (local probes
# 127.0.0.1 and never reads it). EARSHOT_VERIFY_PORT is the port (default 8080, listen's own default).
# proxy needs EARSHOT_VERIFY_TLS_DIR: the directory holding proxy.crt and proxy.key (deploy/proxy-cert.sh
# on the proxy host) and server.crt (copied from the server's /etc/earshot/tls); it fails without it.
# EARSHOT_PY is the earshotd venv's python, which reads the TLS settings through earshotd.config.load.
set -uo pipefail
HOST="${EARSHOT_VERIFY_HOST:-}"
PORT="${EARSHOT_VERIFY_PORT:-8080}"
EARSHOT_PY="${EARSHOT_PY:-/opt/earshot/.venv/bin/python}"
CONFIG=/etc/earshot/earshotd.toml
fail=0
ok()  { echo "ok   $*"; }
bad() { echo "FAIL $*"; fail=1; }

# worker_state <config> <host:port> [curl option...]: prints the worker state /v1/status reports,
# over https instead of http when VERIFY_SCHEME=https. /v1/status needs the read token, so it comes
# from the config and reaches curl on stdin (-K -), never on an argv.
worker_state() {
  local tok cfg=$1 hostport=$2
  shift 2
  tok="$(python3 -c '
import sys, tomllib
for t in tomllib.load(sys.stdin.buffer).get("tokens", []):
    if t.get("scope") == "read":
        print(t["token"])
        break
' < "$cfg")" || return 1
  [ -n "$tok" ] || { echo "no read token in $cfg; /v1/status needs one" >&2; return 1; }
  printf 'header = "Authorization: Bearer %s"\n' "$tok" \
    | curl -s -m 10 -K - "$@" "${VERIFY_SCHEME:-http}://$hostport/v1/status" \
    | python3 -c 'import json, sys; print(json.load(sys.stdin)["worker"]["state"])'
}

# http_code <curl option...> <url>: the status code curl got, 000 when there was no HTTP response
# (a refused or failed handshake included).
http_code() {
  curl -s -m 10 -o /dev/null -w '%{http_code}' "$@"
}

# local_probes <config> <port>: health and the worker through the loopback address. With the TLS
# settings (read through earshotd.config.load, never from the file here) they go over mutual TLS as
# the server itself (its own certificate is admitted as a client), and two more probes follow: a
# handshake without a client certificate and plain http must both get no HTTP response.
local_probes() {
  local cfg=$1 port=$2 tls cert key h w code base
  local opts=()
  base="127.0.0.1:$port"
  tls="$("$EARSHOT_PY" -I -c '
import sys
from earshotd.config import load

config = load(sys.argv[1])
if config.tls:
    print(config.tls_cert)
    print(config.tls_key)
' "$cfg")" || { bad "earshotd.config.load could not read the TLS settings from $cfg"; return; }
  if [ -n "$tls" ]; then
    { IFS= read -r cert; IFS= read -r key; } <<< "$tls"
    base="earshotd:$port"
    opts=(--resolve "earshotd:$port:127.0.0.1" --cacert "$cert" --cert "$cert" --key "$key")
    export VERIFY_SCHEME=https
  else
    unset VERIFY_SCHEME
  fi
  h="$(curl -s -m 10 -w '\n%{http_code}' ${opts[@]+"${opts[@]}"} "${VERIFY_SCHEME:-http}://$base/v1/health")"
  [ "${h##*$'\n'}" = 200 ] && ok "local health 200" || bad "local health not 200"
  w="$(worker_state "$cfg" "$base" ${opts[@]+"${opts[@]}"} 2>&1)"
  [ "$w" = running ] && ok "worker running" || bad "worker.state is not running (stalled or absent): $w"
  [ -n "$tls" ] || return 0
  code="$(http_code --resolve "earshotd:$port:127.0.0.1" --cacert "$cert" "https://$base/v1/health")"
  [ "$code" = 000 ] && ok "handshake without a client certificate refused" \
    || bad "a handshake without a client certificate got HTTP $code"
  code="$(http_code "http://127.0.0.1:$port/v1/health")"
  [ "$code" = 000 ] && ok "plain http refused" || bad "plain http to port $port got HTTP $code"
}

# nft_check <config>: the earshot nft table must be loaded when listen is a network address (read
# through earshotd.config.load); a loopback listen has no firewall and needs none.
nft_check() {
  local verdict
  verdict="$("$EARSHOT_PY" -I -c '
import sys
from earshotd.config import is_loopback, load

print("loopback" if is_loopback(load(sys.argv[1]).host_port[0]) else "network")
' "$1")" || { bad "earshotd.config.load could not read listen from $1"; return; }
  if [ "$verdict" = loopback ]; then
    ok "no firewall needed (loopback listen)"
  else
    nft list table inet earshot >/dev/null 2>&1 && ok "nft table loaded" || bad "nft table missing"
  fi
}

# need_host: the server's address must be named; no address is ever defaulted to.
need_host() {
  [ -n "$HOST" ] && return 0
  bad "EARSHOT_VERIFY_HOST is not set: name the server's address"
  return 1
}

role() {
  case "${1:-}" in
  local)
    systemctl is-enabled --quiet earshotd && ok "unit enabled" || bad "unit not enabled"
    systemctl is-active --quiet earshotd && ok "unit active" || bad "unit not active"
    [ "$(systemctl show -p User --value earshotd)" = earshot ] && ok "runs as earshot" || bad "wrong user"
    systemctl show -p Environment --value earshotd | grep -q "EARSHOTD_CONFIG=$CONFIG" \
      && ok "config path from unit" || bad "unit does not point at $CONFIG"
    grep -q REPLACE_WITH "$CONFIG" \
      && bad "config still has placeholder tokens" || ok "no placeholder tokens"
    local_probes "$CONFIG" "$PORT"
    nft_check "$CONFIG"
    m="$(stat -c %a /var/lib/earshot)"
    [ "$m" = 750 ] && ok "data dir 0750" || bad "data dir is $m, transcripts readable by other users"
    systemctl is-active --quiet earshotd.socket && ok "earshotd.socket active" || bad "earshotd.socket not active"
    [ "$(systemctl show -p PrivateNetwork --value earshotd)" = yes ] \
      && ok "unit has no network of its own (PrivateNetwork=yes)" || bad "earshotd does not set PrivateNetwork=yes"
    # A symlink's own mode is always 777 and grants nothing; its target is checked where it lives.
    exposed="$(find /var/lib/earshot -mindepth 1 ! -type l -perm /077 2>&1 | head -n 3)"
    [ -z "$exposed" ] && ok "no file below /var/lib/earshot readable by group or other" \
      || bad "readable by group or other: $exposed"
    foreign="$(find /opt/earshot/models \( ! -user root -o \( ! -type l -perm /022 \) \) 2>&1 | head -n 3)"
    [ -d /opt/earshot/models ] && [ -z "$foreign" ] && ok "models root-owned, not writable by group or other" \
      || bad "models missing, not root-owned or writable: $foreign"
    ;;
  proxy)
    need_host || exit 1
    dir="${EARSHOT_VERIFY_TLS_DIR:-}"
    if [ -z "$dir" ]; then
      bad "EARSHOT_VERIFY_TLS_DIR is not set: name the directory holding proxy.crt, proxy.key and server.crt"
      exit 1
    fi
    for f in proxy.crt proxy.key server.crt; do
      [ -r "$dir/$f" ] || { bad "EARSHOT_VERIFY_TLS_DIR=$dir has no readable $f"; exit 1; }
    done
    base="https://earshotd:$PORT"
    resolve=(--resolve "earshotd:$PORT:$HOST")
    mutual=("${resolve[@]}" --cacert "$dir/server.crt" --cert "$dir/proxy.crt" --key "$dir/proxy.key")
    code="$(http_code "${mutual[@]}" "$base/v1/health")"
    [ "$code" = 200 ] && ok "health from the proxy 200 over mutual TLS (no auth)" || bad "health from the proxy got $code"
    b="$(curl -s -m 10 "${mutual[@]}" "$base/v1/health")"
    printf '%s' "$b" | python3 -c 'import json,sys; sys.exit(json.load(sys.stdin) != {"ok": True})' 2>/dev/null \
      && ok "health body is exactly {\"ok\": true}" || bad "health body is not exactly {\"ok\": true}: $b"
    hdrs="$(curl -s -m 10 -o /dev/null -D - "${mutual[@]}" "$base/v1/health")"
    printf '%s' "$hdrs" | grep -qi '^server:' \
      && bad "health response carries a Server header" || ok "health response has no Server header"
    for path in /v1/status /v1/openapi.json; do
      code="$(http_code "${mutual[@]}" "$base$path")"
      [ "$code" = 401 ] && ok "$path from the proxy 401 (no token)" || bad "$path from the proxy got $code, want 401"
    done
    code="$(http_code "${resolve[@]}" --cacert "$dir/server.crt" "$base/v1/health")"
    [ "$code" = 000 ] && ok "handshake without a client certificate refused" \
      || bad "a handshake without a client certificate got HTTP $code"
    code="$(http_code "http://$HOST:$PORT/v1/health")"
    [ "$code" = 000 ] && ok "plain http to $HOST:$PORT gets no answer" || bad "plain http to $HOST:$PORT got HTTP $code"
    ;;
  outside)
    need_host || exit 1
    if curl -s -m 5 -o /dev/null "http://$HOST:$PORT/v1/health"; then
      bad "outside host reached $HOST:$PORT directly"
    else
      ok "outside host direct connection refused or dropped"
    fi
    ;;
  *) echo "usage: $0 local|proxy|outside" >&2; exit 2 ;;
  esac
  exit $fail
}

# Sourced (by the tests), the functions are defined and no role runs.
if [ "${BASH_SOURCE[0]}" = "$0" ]; then role "$@"; fi
