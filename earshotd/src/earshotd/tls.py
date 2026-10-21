"""The TLS context earshotd serves the proxy hop with: TLS 1.3, a client certificate required."""

from __future__ import annotations

import os
import ssl
from pathlib import Path

from .config import Config, ConfigError

_PEM = "-----BEGIN CERTIFICATE-----"


def _read(path: Path, *, key: bool) -> bytes:
    """The file's bytes, refused (naming the path) when unreadable or exposed.

    A key is refused when others can read it, its group can write it, or anyone but root or this
    process owns it, as config.load treats the config. A certificate or trust file is public, so
    it is refused only when its group or others can write it or someone else owns it: that is how
    an attacker would swap the pin.
    """
    kind, bits, rule = (
        ("key", 0o027, "readable by others or writable by its group")
        if key
        else ("certificate", 0o022, "writable by its group or others")
    )
    try:
        with open(path, "rb") as f:
            st = os.fstat(f.fileno())
            if st.st_mode & bits or st.st_uid not in (0, os.geteuid()):
                raise ConfigError(
                    f"{path} is mode {st.st_mode & 0o777:04o}, owner uid {st.st_uid}: a TLS"
                    f" {kind} must not be {rule}, and must be owned by root or the user"
                    " running earshotd"
                )
            return f.read()
    except OSError as e:
        raise ConfigError(f"{path}: {e.strerror or e}") from e


def server_context(config: Config) -> ssl.SSLContext:
    """The server side of the proxy hop, built from config's tls_cert, tls_key and tls_proxy_certs.

    It trusts exactly its own certificate (so root on the server can probe it with the server's
    key) and each proxy certificate: no CA, each certificate is its own pin. Raises ConfigError
    naming the path of whatever it refuses.
    """
    assert config.tls_cert and config.tls_key and config.tls_proxy_certs
    _read(config.tls_key, key=True)
    trusted = [config.tls_cert, *config.tls_proxy_certs]
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    context.verify_mode = ssl.CERT_REQUIRED
    # Each certificate is its own pin. Without this OpenSSL looks the client certificate's issuer
    # up by subject name, and proxy certificates from proxy-cert.sh share one subject, so only
    # the first of several would verify.
    context.verify_flags |= ssl.VERIFY_X509_PARTIAL_CHAIN
    for path in trusted:
        pem = _read(path, key=False).decode("ascii", "replace")
        if _PEM not in pem:
            raise ConfigError(f"{path} holds no PEM certificate")
        try:
            context.load_verify_locations(cadata=pem)
        except ssl.SSLError as e:
            raise ConfigError(f"{path} is not a usable certificate: {e}") from e
    try:
        context.load_cert_chain(config.tls_cert, config.tls_key)
    except (ssl.SSLError, OSError) as e:
        raise ConfigError(
            f"{config.tls_key} does not load with {config.tls_cert}: {e}"
        ) from e
    return context
