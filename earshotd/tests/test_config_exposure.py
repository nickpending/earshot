import os

import pytest

from earshotd.config import ConfigError, load

GOOD = "0123456789abcdef" * 4


def _write(tmp_path, mode, token=GOOD):
    cfg = tmp_path / "earshotd.toml"
    cfg.write_text(
        f'data_dir = "{tmp_path}"\n[[tokens]]\ntoken = "{token}"\nscope = "read"\n'
    )
    cfg.chmod(mode)
    return cfg


@pytest.mark.parametrize("mode", [0o644, 0o660, 0o604])
def test_a_config_others_can_read_or_the_group_can_write_is_refused(tmp_path, mode):
    cfg = _write(tmp_path, mode)
    with pytest.raises(ConfigError) as e:
        load(cfg)
    message = str(e.value)
    assert str(cfg) in message
    assert f"{mode:04o}" in message
    assert f"uid {os.geteuid()}" in message


def test_the_mode_is_checked_before_any_token_is_parsed(tmp_path):
    cfg = _write(tmp_path, 0o644, token="REPLACE_WITH_openssl_rand_hex_32")
    with pytest.raises(ConfigError) as e:
        load(cfg)
    assert "0644" in str(e.value)
    assert "64 lowercase hex" not in str(e.value)


def test_a_config_owned_by_someone_else_is_refused(tmp_path, monkeypatch):
    cfg = _write(tmp_path, 0o600)
    owner = cfg.stat().st_uid
    if owner == 0:
        pytest.skip("the test user is root, so every owner passes")
    monkeypatch.setattr(os, "geteuid", lambda: owner + 1)
    with pytest.raises(ConfigError) as e:
        load(cfg)
    assert str(cfg) in str(e.value)
    assert f"uid {owner}" in str(e.value)


@pytest.mark.parametrize("mode", [0o640, 0o600])
def test_a_private_config_loads(tmp_path, mode):
    cfg = _write(tmp_path, mode)
    assert load(cfg).tokens == {GOOD: "read"}


def test_a_network_listen_in_a_file_is_refused_until_it_has_tls_or_the_opt_in(tmp_path):
    cfg = tmp_path / "earshotd.toml"
    body = f'data_dir = "{tmp_path}"\nlisten = "0.0.0.0:8080"\n'
    tokens = f'[[tokens]]\ntoken = "{GOOD}"\nscope = "read"\n'
    cfg.write_text(body + tokens)
    cfg.chmod(0o600)
    with pytest.raises(ConfigError, match="insecure_plain_http"):
        load(cfg)
    cfg.write_text(body + "insecure_plain_http = true\n" + tokens)
    assert load(cfg).insecure_plain_http
    cfg.write_text(
        body
        + 'tls_cert = "/e/server.crt"\ntls_key = "/e/server.key"\n'
        + 'tls_proxy_certs = ["/e/proxy.crt"]\n'
        + tokens
    )
    assert load(cfg).tls
