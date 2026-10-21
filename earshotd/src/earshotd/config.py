"""Configuration read from one TOML file (default /etc/earshot/earshotd.toml)."""

from __future__ import annotations

import ipaddress
import os
import re
import tomllib
from dataclasses import dataclass
from datetime import UTC, tzinfo
from pathlib import Path
from zoneinfo import ZoneInfo

DEFAULT_PATH = "/etc/earshot/earshotd.toml"
# The host's own zone, the day's zone when the config sets no timezone.
HOST_ZONE = "/etc/localtime"
SCOPES = ("upload", "read")
# The form `openssl rand -hex 32` produces; anything else, a placeholder included, refuses to start.
_TOKEN = re.compile(r"[0-9a-f]{64}")
_OPTIONAL = (
    "listen",
    "whisper_model",
    "diarization_model",
    "speaker_link_threshold",
    "voice_match_threshold",
)


_TLS_SETTINGS = ("tls_cert", "tls_key", "tls_proxy_certs")


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Config:
    tokens: dict[str, str]  # token -> scope
    data_dir: Path
    # Loopback unless the operator opens it; a network address belongs only behind a firewall.
    listen: str = "127.0.0.1:8080"
    whisper_model: str = "large-v3-turbo"
    diarization_model: str = ""
    speaker_link_threshold: float = 0.5
    voice_match_threshold: float = 0.5
    # The proxy hop is mutual TLS: all three together, or none (server-only TLS is not a mode).
    tls_cert: Path | None = None
    tls_key: Path | None = None
    tls_proxy_certs: tuple[Path, ...] = ()
    # The one way to serve a network address in plain http; the firewall is then its only guard.
    insecure_plain_http: bool = False
    # Hosts the firewall admits to the port on a network listen. earshotd itself does not use it:
    # deploy/install.sh renders the firewall from this file.
    proxy_addresses: tuple[str, ...] = ()
    # The one zone a day is cut in, resolved once by parse; a Config built by hand cuts in UTC.
    zone: tzinfo = UTC

    @property
    def tls(self) -> bool:
        return self.tls_cert is not None

    @property
    def db_path(self) -> Path:
        return self.data_dir / "earshot.db"

    @property
    def host_port(self) -> tuple[str, int]:
        host, _, port = self.listen.rpartition(":")
        return host or "127.0.0.1", int(port)


def parse(raw: dict, *, host_zone: str | os.PathLike = HOST_ZONE) -> Config:
    tokens: dict[str, str] = {}
    for entry in raw.get("tokens", []):
        token, scope = entry.get("token"), entry.get("scope")
        if not token or scope not in SCOPES:
            raise ConfigError(f"token entry needs a token and a scope in {SCOPES}")
        if not _TOKEN.fullmatch(token):
            raise ConfigError(
                f"{scope} token is not 64 lowercase hex characters (openssl rand -hex 32)"
            )
        if token in tokens:
            raise ConfigError("duplicate token: a token carries exactly one scope")
        tokens[token] = scope
    if "data_dir" not in raw:
        raise ConfigError("data_dir is required")
    if "limitless_key" in raw:
        raise ConfigError(
            "limitless_key does not belong in this file: remove the line;"
            " `earshot import-limitless` asks for the key when it runs"
        )
    kwargs = {k: raw[k] for k in _OPTIONAL if k in raw}
    if "listen" in kwargs:
        _check_listen(kwargs["listen"])
    tls, insecure = _tls_settings(raw), raw.get("insecure_plain_http", False)
    if not isinstance(insecure, bool):
        raise ConfigError("insecure_plain_http must be true or false")
    if tls and insecure:
        raise ConfigError(
            "insecure_plain_http = true together with tls_cert, tls_key and tls_proxy_certs:"
            " remove insecure_plain_http, or remove the three tls settings"
        )
    config = Config(
        tokens=tokens,
        data_dir=Path(raw["data_dir"]),
        insecure_plain_http=insecure,
        proxy_addresses=_proxy_addresses(raw),
        zone=_zone(raw, host_zone),
        **tls,
        **kwargs,
    )
    if not (config.tls or insecure or is_loopback(config.host_port[0])):
        raise ConfigError(
            f"listen {config.listen!r} is a network address: set tls_cert, tls_key and"
            " tls_proxy_certs so the proxy hop is mutual TLS, or set insecure_plain_http = true"
            " to serve plain http behind a firewall"
        )
    return config


def _zone(raw: dict, host_zone: str | os.PathLike) -> tzinfo:
    """The `timezone` setting as a zone; the host's own zone when it is absent, UTC when the host has none."""
    if "timezone" not in raw:
        if not os.path.lexists(host_zone):
            return UTC  # only a host with no zone file at all cuts days in UTC
        try:
            with open(host_zone, "rb") as f:
                return ZoneInfo.from_file(f)
        except (OSError, ValueError) as e:
            raise ConfigError(
                f"timezone is not set and the host zone file {os.fspath(host_zone)} cannot be"
                f' used ({e}): set timezone explicitly, e.g. timezone = "Europe/Berlin"'
            ) from None
    name = raw["timezone"]
    try:
        if not isinstance(name, str) or not name:
            raise ValueError(name)
        return ZoneInfo(name)
    except (ValueError, LookupError, OSError):
        raise ConfigError(
            f"timezone {name!r} is not a zone name (an IANA name such as Europe/Berlin)"
        ) from None


def _tls_settings(raw: dict) -> dict:
    """The three TLS settings as Config fields; empty when none is given."""
    given = [k for k in _TLS_SETTINGS if k in raw]
    if not given:
        return {}
    absent = [k for k in _TLS_SETTINGS if k not in raw]
    if absent:
        raise ConfigError(
            f"{', '.join(given)} set without {', '.join(absent)}: the proxy hop is mutual TLS,"
            " so tls_cert, tls_key and tls_proxy_certs are given all together or not at all"
        )
    for key in ("tls_cert", "tls_key"):
        if not isinstance(raw[key], str) or not raw[key]:
            raise ConfigError(f"{key} must be a path")
    certs = raw["tls_proxy_certs"]
    if not (
        isinstance(certs, list)
        and certs
        and all(isinstance(c, str) and c for c in certs)
    ):
        raise ConfigError(
            "tls_proxy_certs must be a non-empty list of certificate paths"
        )
    return {
        "tls_cert": Path(raw["tls_cert"]),
        "tls_key": Path(raw["tls_key"]),
        "tls_proxy_certs": tuple(Path(c) for c in certs),
    }


def _proxy_addresses(raw: dict) -> tuple[str, ...]:
    """proxy_addresses as single IP addresses (a range, a host name or a scoped address is refused)."""
    given = raw.get("proxy_addresses", [])
    if not isinstance(given, list):
        raise ConfigError("proxy_addresses must be a list of IP addresses")
    addresses = []
    for entry in given:
        try:
            if not isinstance(entry, str) or "%" in entry:
                raise ValueError(entry)
            addresses.append(str(ipaddress.ip_address(entry)))
        except ValueError:
            raise ConfigError(
                f"proxy_addresses entry {entry!r} is not a single IP address"
                " (no range, host name or interface scope)"
            ) from None
    return tuple(addresses)


def is_loopback(host: str) -> bool:
    """True for 127.0.0.0/8, ::1 and localhost (brackets stripped): the hosts served plain."""
    host = host.strip("[]").lower()
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _check_listen(listen: object) -> None:
    _, sep, port = listen.rpartition(":") if isinstance(listen, str) else ("", "", "")
    if not (sep and port.isascii() and port.isdigit() and 1 <= int(port) <= 65535):
        raise ConfigError(f"listen {listen!r} must be host:port with a port of 1-65535")


def load(
    path: str | os.PathLike | None = None, *, host_zone: str | os.PathLike = HOST_ZONE
) -> Config:
    path = path or os.environ.get("EARSHOTD_CONFIG", DEFAULT_PATH)
    with open(path, "rb") as f:
        # The file holds the tokens: refuse one others can read or the group can write, or one
        # owned by anyone but root or this process (OpenSSH StrictModes), before parsing it.
        st = os.fstat(f.fileno())
        if st.st_mode & 0o027 or st.st_uid not in (0, os.geteuid()):
            raise ConfigError(
                f"{path} is mode {st.st_mode & 0o777:04o}, owner uid {st.st_uid}: it must not be"
                " readable by others or writable by its group, and must be owned by root or"
                " the user running earshotd"
            )
        return parse(tomllib.load(f), host_zone=host_zone)
