import pytest

from earshotd.config import ConfigError, parse

GOOD = "0123456789abcdef" * 4


@pytest.mark.parametrize(
    "token",
    ["REPLACE_WITH_openssl_rand_hex_32", "u" * 64, GOOD[:-1], GOOD.upper(), GOOD + "0"],
)
def test_a_token_not_from_openssl_rand_hex_32_refuses_to_start(tmp_path, token):
    raw = {"data_dir": str(tmp_path), "tokens": [{"token": token, "scope": "upload"}]}
    with pytest.raises(ConfigError, match="64 lowercase hex"):
        parse(raw)


def test_a_generated_token_is_accepted(tmp_path):
    raw = {"data_dir": str(tmp_path), "tokens": [{"token": GOOD, "scope": "read"}]}
    assert parse(raw).tokens == {GOOD: "read"}


@pytest.mark.parametrize("raw_listen", [None, ":8080"])
def test_earshotd_listens_on_loopback_unless_told_otherwise(tmp_path, raw_listen):
    raw = {"data_dir": str(tmp_path), "tokens": [{"token": GOOD, "scope": "read"}]}
    if raw_listen is not None:
        raw["listen"] = raw_listen
    assert parse(raw).host_port == ("127.0.0.1", 8080)


@pytest.mark.parametrize(
    "bad", ["127.0.0.1:0", "127.0.0.1:65536", "127.0.0.1", "127.0.0.1:http"]
)
def test_a_listen_that_is_not_host_and_a_port_in_range_refuses_to_start(tmp_path, bad):
    raw = {
        "data_dir": str(tmp_path),
        "tokens": [{"token": GOOD, "scope": "read"}],
        "listen": bad,
    }
    with pytest.raises(ConfigError, match="listen"):
        parse(raw)


def _raw(tmp_path, **extra):
    return {
        "data_dir": str(tmp_path),
        "tokens": [{"token": GOOD, "scope": "read"}],
        **extra,
    }


TLS = {
    "tls_cert": "/etc/earshot/tls/server.crt",
    "tls_key": "/etc/earshot/tls/server.key",
    "tls_proxy_certs": ["/etc/earshot/tls/proxy.crt"],
}


def test_an_explicit_network_address_is_kept_with_tls_settings(tmp_path):
    config = parse(_raw(tmp_path, listen="0.0.0.0:8080", **TLS))
    assert config.host_port == ("0.0.0.0", 8080)
    assert str(config.tls_cert) == TLS["tls_cert"]
    assert str(config.tls_key) == TLS["tls_key"]
    assert [str(p) for p in config.tls_proxy_certs] == TLS["tls_proxy_certs"]
    assert config.tls and not config.insecure_plain_http


def test_an_explicit_network_address_is_kept_by_the_plain_http_opt_in(tmp_path):
    config = parse(_raw(tmp_path, listen="0.0.0.0:8080", insecure_plain_http=True))
    assert config.host_port == ("0.0.0.0", 8080)
    assert config.insecure_plain_http and not config.tls
    assert config.tls_cert is None and config.tls_proxy_certs == ()


@pytest.mark.parametrize("listen", ["0.0.0.0:8080", "192.0.2.43:8080"])
def test_a_network_listen_with_neither_tls_nor_the_opt_in_is_refused(tmp_path, listen):
    with pytest.raises(ConfigError) as e:
        parse(_raw(tmp_path, listen=listen))
    for name in ("tls_cert", "tls_key", "tls_proxy_certs", "insecure_plain_http"):
        assert name in str(e.value)


@pytest.mark.parametrize(
    ("given", "missing"),
    [
        (["tls_cert"], "tls_key"),
        (["tls_cert", "tls_key"], "tls_proxy_certs"),
        (["tls_proxy_certs"], "tls_cert"),
    ],
)
def test_partial_tls_settings_are_refused_on_any_listen(tmp_path, given, missing):
    for listen in ("0.0.0.0:8080", "127.0.0.1:8080"):
        raw = _raw(tmp_path, listen=listen, **{k: TLS[k] for k in given})
        with pytest.raises(ConfigError, match=missing):
            parse(raw)


@pytest.mark.parametrize("bad", [[], "/etc/earshot/tls/proxy.crt", [""], [7]])
def test_tls_proxy_certs_must_be_a_non_empty_list_of_paths(tmp_path, bad):
    raw = _raw(tmp_path, listen="0.0.0.0:8080", **{**TLS, "tls_proxy_certs": bad})
    with pytest.raises(ConfigError, match="tls_proxy_certs"):
        parse(raw)


@pytest.mark.parametrize("bad", ["true", 1, "yes"])
def test_insecure_plain_http_must_be_a_bool(tmp_path, bad):
    with pytest.raises(ConfigError, match="insecure_plain_http"):
        parse(_raw(tmp_path, listen="0.0.0.0:8080", insecure_plain_http=bad))


def test_the_plain_http_opt_in_together_with_tls_is_refused(tmp_path):
    raw = _raw(tmp_path, listen="0.0.0.0:8080", insecure_plain_http=True, **TLS)
    with pytest.raises(ConfigError) as e:
        parse(raw)
    assert "insecure_plain_http" in str(e.value) and "tls_cert" in str(e.value)


@pytest.mark.parametrize(
    "listen",
    ["127.0.0.1:8080", "127.0.0.2:8080", "[::1]:8080", "localhost:8080", ":8080"],
)
def test_a_loopback_listen_needs_no_tls_and_stays_plain(tmp_path, listen):
    config = parse(_raw(tmp_path, listen=listen))
    assert not config.tls and not config.insecure_plain_http
    assert config.tls_cert is None and config.tls_key is None
    assert config.tls_proxy_certs == ()


def test_tls_settings_apply_on_loopback_too(tmp_path):
    assert parse(_raw(tmp_path, listen="127.0.0.1:8080", **TLS)).tls
