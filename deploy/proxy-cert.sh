#!/usr/bin/env bash
# Generate the proxy's key and client certificate for the earshotd hop. Run it on the host that
# runs the proxy; it needs only bash and openssl, so copy this one file there, no checkout.
#   proxy-cert.sh <dir> [group]
# Writes <dir>/proxy.key and <dir>/proxy.crt when proxy.key is absent, and on every run sets the
# modes. The key never leaves this host: copy only proxy.crt to the earshotd server
# (/etc/earshot/tls/proxy.crt) and server.crt from the server back here. With a group the key is
# root:<group> 0640 so the proxy's user can read it; without one it stays 0600. Rotate by
# deleting proxy.key and running this again, then copying the new proxy.crt to the server.
# deploy/install-lib.sh server_tls makes the server's pair the same way; the openssl call is
# repeated here so this file runs on its own.
set -euo pipefail

dir=${1:?usage: proxy-cert.sh <dir> [group]}
group=${2-}
command -v openssl >/dev/null || { echo "openssl not installed" >&2; exit 1; }

if [ ! -e "$dir/proxy.key" ]; then
  (umask 077; mkdir -p "$dir")
  tmp=$(umask 077; mktemp -d "$dir/.new.XXXXXX")
  openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:P-256 -noenc -days 3650 \
    -subj /CN=earshot-proxy -keyout "$tmp/proxy.key" -out "$tmp/proxy.crt" \
    -addext subjectAltName=DNS:earshot-proxy \
    -addext basicConstraints=critical,CA:FALSE \
    -addext keyUsage=digitalSignature \
    -addext extendedKeyUsage=clientAuth \
    || { rm -rf "$tmp"; exit 1; }
  # The key moves last: its presence is what stops the next run from regenerating.
  mv "$tmp/proxy.crt" "$dir/proxy.crt"
  mv "$tmp/proxy.key" "$dir/proxy.key"
  rmdir "$tmp"
fi
[ -f "$dir/proxy.crt" ] || {
  echo "$dir/proxy.key exists but $dir/proxy.crt is missing: remove proxy.key to regenerate both" >&2
  exit 1
}

if [ -n "$group" ]; then
  chown "root:$group" "$dir"
  chown "root:$group" "$dir/proxy.key"
  chmod 0750 "$dir"
  chmod 0640 "$dir/proxy.key"
else
  chmod 0750 "$dir"
  chmod 0600 "$dir/proxy.key"
fi
chmod 0644 "$dir/proxy.crt"

echo "certificate to copy to the earshotd server: $dir/proxy.crt"
openssl x509 -in "$dir/proxy.crt" -noout -fingerprint -sha256
