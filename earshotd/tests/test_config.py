"""The config refuses limitless_key, and the shipped example parses."""

import dataclasses
import os
import re
import tomllib
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from conftest import T0, tzfile

from earshotd.config import ConfigError, load, parse

GOOD = "0123456789abcdef" * 4
SECRET = "k-secret-value"
EXAMPLE = Path(__file__).resolve().parents[2] / "deploy" / "earshotd.toml.example"


def raw(tmp_path, **extra) -> dict:
    return {
        "data_dir": str(tmp_path),
        "tokens": [{"token": GOOD, "scope": "read"}],
        **extra,
    }


@pytest.mark.parametrize("value", [SECRET, ""])
def test_c6_limitless_key_in_the_config_refuses_to_start(tmp_path, value):
    with pytest.raises(ConfigError) as e:
        parse(raw(tmp_path, limitless_key=value))
    assert "limitless_key" in str(e.value) and "remove the line" in str(e.value)
    assert SECRET not in str(e.value)


def test_c6_a_config_file_holding_limitless_key_is_refused_on_load(tmp_path):
    cfg = tmp_path / "earshotd.toml"
    cfg.write_text(f'data_dir = "{tmp_path}"\nlimitless_key = "{SECRET}"\n')
    os.chmod(cfg, 0o600)
    with pytest.raises(ConfigError) as e:
        load(cfg)
    assert "limitless_key" in str(e.value) and "remove the line" in str(e.value)
    assert SECRET not in str(e.value)


def test_c9_the_example_config_parses_once_its_tokens_are_replaced():
    text = EXAMPLE.read_text()
    tokens = iter([GOOD, "f" * 64])
    text = re.sub(r'"REPLACE_WITH[^"]*"', lambda _m: f'"{next(tokens)}"', text)
    config = parse(tomllib.loads(text))
    assert set(config.tokens.values()) == {"upload", "read"}
    assert config.proxy_addresses == () and config.host_port == ("127.0.0.1", 8080)


def test_proxy_addresses_load_as_given_and_default_to_empty(tmp_path):
    both = parse(raw(tmp_path, proxy_addresses=["192.0.2.10", "2001:db8::5"]))
    assert both.proxy_addresses == ("192.0.2.10", "2001:db8::5")
    assert parse(raw(tmp_path)).proxy_addresses == ()


@pytest.mark.parametrize(
    ("value", "named"),
    [
        ("192.0.2.10", "proxy_addresses"),
        ({"a": 1}, "proxy_addresses"),
        (["192.0.2.0/24"], "192.0.2.0/24"),
        (["proxy.example"], "proxy.example"),
        (["fe80::1%eth0"], "fe80::1%eth0"),
        ([5], "5"),
        ([""], "''"),
        (["192.0.2.10", "not-an-ip"], "not-an-ip"),
    ],
)
def test_a_proxy_addresses_value_that_is_not_a_list_of_ip_addresses_is_refused(
    tmp_path, value, named
):
    with pytest.raises(ConfigError) as e:
        parse(raw(tmp_path, proxy_addresses=value))
    assert "proxy_addresses" in str(e.value) and named in str(e.value)


def test_adding_proxy_addresses_changes_nothing_else_in_a_mutual_tls_config(tmp_path):
    mutual_tls = raw(
        tmp_path,
        listen="0.0.0.0:8080",
        tls_cert="/etc/earshot/tls/server.crt",
        tls_key="/etc/earshot/tls/server.key",
        tls_proxy_certs=["/etc/earshot/tls/proxy.crt"],
    )
    nowhere = {
        "host_zone": tmp_path / "no-such-file"
    }  # a zone read from a file differs per parse
    before = parse(mutual_tls, **nowhere)
    after = parse({**mutual_tls, "proxy_addresses": ["192.0.2.10"]}, **nowhere)
    assert after.proxy_addresses == ("192.0.2.10",)
    assert dataclasses.replace(after, proxy_addresses=()) == before


def clock(config, ms=T0) -> str:
    return datetime.fromtimestamp(ms / 1000, config.zone).strftime("%Y-%m-%d %H:%M:%S")


def test_c4_a_configured_timezone_cuts_the_day(tmp_path):
    config = parse(raw(tmp_path, timezone="Asia/Tokyo"))
    assert clock(config) == "2026-10-02 08:08:02"


@pytest.mark.parametrize("value", ["Not/AZone", "", 5, "../../etc/passwd"], ids=repr)
def test_c4_a_timezone_that_is_not_a_zone_refuses_naming_key_and_value(tmp_path, value):
    with pytest.raises(ConfigError) as e:
        parse(raw(tmp_path, timezone=value))
    assert "timezone" in str(e.value) and repr(value) in str(e.value)


def test_c4_without_timezone_the_day_is_cut_in_the_host_zone(tmp_path):
    kolkata = tzfile(tmp_path, "Asia/Kolkata")
    assert clock(parse(raw(tmp_path), host_zone=kolkata)) == "2026-10-02 04:38:02"


def test_c4_without_timezone_and_without_a_host_zone_file_the_day_is_cut_in_utc(
    tmp_path,
):
    config = parse(raw(tmp_path), host_zone=tmp_path / "no-such-file")
    assert clock(config) == "2026-10-01 23:08:02"


def test_c4_a_configured_timezone_wins_over_the_host_zone(tmp_path):
    kolkata = tzfile(tmp_path, "Asia/Kolkata")
    config = parse(raw(tmp_path, timezone="Asia/Tokyo"), host_zone=kolkata)
    assert clock(config) == "2026-10-02 08:08:02"


def example_config(tmp_path, uncomment_timezone: bool):
    tokens = iter([GOOD, "f" * 64])
    text = re.sub(
        r'"REPLACE_WITH[^"]*"', lambda _m: f'"{next(tokens)}"', EXAMPLE.read_text()
    )
    if uncomment_timezone:
        text, n = re.subn(r"^# (timezone = )", r"\1", text, flags=re.MULTILINE)
        assert n == 1
    cfg = tmp_path / "earshotd.toml"
    cfg.write_text(text)
    cfg.chmod(0o600)
    return load(cfg, host_zone=tzfile(tmp_path, "Asia/Kolkata"))


def test_c10_the_shipped_example_leaves_the_day_to_the_host_zone(tmp_path):
    assert clock(example_config(tmp_path, False)) == "2026-10-02 04:38:02"


def test_c10_the_example_timezone_line_names_a_zone_when_uncommented(tmp_path):
    named = re.search(r'^# timezone = "([^"]+)"', EXAMPLE.read_text(), re.MULTILINE)
    assert named
    expected = datetime.fromtimestamp(T0 / 1000, ZoneInfo(named[1]))
    assert clock(example_config(tmp_path, True)) == expected.strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    assert named[1] != "Asia/Kolkata"


def test_c10_the_example_says_a_changed_timezone_needs_a_rebuild():
    assert "earshot rebuild" in EXAMPLE.read_text()


def test_c4_a_host_zone_file_that_cannot_be_read_refuses_instead_of_cutting_in_utc(
    tmp_path,
):
    if os.geteuid() == 0:
        pytest.skip("root reads a mode-000 file")
    unreadable = tzfile(tmp_path, "Asia/Kolkata")
    unreadable.chmod(0)
    with pytest.raises(ConfigError) as e:
        parse(raw(tmp_path), host_zone=unreadable)
    assert str(unreadable) in str(e.value) and "timezone" in str(e.value)


def test_c4_a_dangling_host_zone_symlink_refuses(tmp_path):
    dangling = tmp_path / "localtime"
    dangling.symlink_to(tmp_path / "no-such-zone-file")
    with pytest.raises(ConfigError) as e:
        parse(raw(tmp_path), host_zone=dangling)
    assert str(dangling) in str(e.value) and "timezone" in str(e.value)


def test_c4_a_host_zone_file_that_is_not_a_tz_file_refuses(tmp_path):
    junk = tmp_path / "localtime"
    junk.write_text("not a zone file\n")
    with pytest.raises(ConfigError) as e:
        parse(raw(tmp_path), host_zone=junk)
    assert str(junk) in str(e.value) and "timezone" in str(e.value)
