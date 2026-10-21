"""tls.server_context: the files it refuses, and the context it builds from good ones."""

import os
import shutil
import ssl
from pathlib import Path

import pytest
from test_install_lib import make_server_tls
from test_proxy_cert import make_proxy_cert

from earshotd import tls
from earshotd.config import Config, ConfigError

GOOD = "0123456789abcdef" * 4


def tls_config(
    tmp_path: Path, server: Path, *proxies: Path, listen: str = "127.0.0.1:8080"
) -> Config:
    """A config naming a server pair and the proxy certificates in the given directories."""
    return Config(
        tokens={GOOD: "read"},
        data_dir=tmp_path / "data",
        listen=listen,
        tls_cert=server / "server.crt",
        tls_key=server / "server.key",
        tls_proxy_certs=tuple(p / "proxy.crt" for p in proxies),
    )


@pytest.fixture(scope="module")
def generated(tmp_path_factory):
    """One generated server pair and proxy pair, copied for each test that changes a mode."""
    root = tmp_path_factory.mktemp("generated")
    return make_server_tls(root), make_proxy_cert(root, "proxy")


@pytest.fixture
def pair(tmp_path, generated):
    server, proxy = (tmp_path / "server", tmp_path / "proxy")
    shutil.copytree(generated[0], server)
    shutil.copytree(generated[1], proxy)
    return server, proxy


@pytest.mark.parametrize("key_mode", [0o644, 0o660, 0o604])
def test_a_key_others_can_read_or_the_group_can_write_is_refused(
    tmp_path, pair, key_mode
):
    server, proxy = pair
    (server / "server.key").chmod(key_mode)
    with pytest.raises(ConfigError) as e:
        tls.server_context(tls_config(tmp_path, server, proxy))
    assert str(server / "server.key") in str(e.value)
    assert f"{key_mode:04o}" in str(e.value)


def test_a_key_owned_by_someone_else_is_refused(tmp_path, pair, monkeypatch):
    server, proxy = pair
    owner = (server / "server.key").stat().st_uid
    if owner == 0:
        pytest.skip("the test user is root, so every owner passes")
    monkeypatch.setattr(os, "geteuid", lambda: owner + 1)
    with pytest.raises(ConfigError) as e:
        tls.server_context(tls_config(tmp_path, server, proxy))
    assert str(server / "server.key") in str(e.value)
    assert f"uid {owner}" in str(e.value)


@pytest.mark.parametrize("cert_mode", [0o664, 0o646])
@pytest.mark.parametrize("which", ["server", "proxy"])
def test_a_certificate_the_group_or_others_can_write_is_refused(
    tmp_path, pair, cert_mode, which
):
    server, proxy = pair
    victim = server / "server.crt" if which == "server" else proxy / "proxy.crt"
    victim.chmod(cert_mode)
    with pytest.raises(ConfigError) as e:
        tls.server_context(tls_config(tmp_path, server, proxy))
    assert str(victim) in str(e.value)
    assert f"{cert_mode:04o}" in str(e.value)


def test_a_proxy_certificate_that_does_not_exist_is_refused(tmp_path, pair):
    server, proxy = pair
    (proxy / "proxy.crt").unlink()
    with pytest.raises(ConfigError) as e:
        tls.server_context(tls_config(tmp_path, server, proxy))
    assert str(proxy / "proxy.crt") in str(e.value)


def test_a_proxy_certificate_file_holding_no_pem_is_refused(tmp_path, pair):
    server, proxy = pair
    (proxy / "proxy.crt").write_text("not a certificate\n")
    with pytest.raises(ConfigError) as e:
        tls.server_context(tls_config(tmp_path, server, proxy))
    assert str(proxy / "proxy.crt") in str(e.value)


def test_a_key_that_does_not_belong_to_the_certificate_is_refused(tmp_path, pair):
    server, proxy = pair
    other = make_server_tls(tmp_path, "other")
    (server / "server.key").write_bytes((other / "server.key").read_bytes())
    (server / "server.key").chmod(0o600)
    with pytest.raises(ConfigError) as e:
        tls.server_context(tls_config(tmp_path, server, proxy))
    assert str(server / "server.key") in str(e.value)


@pytest.mark.parametrize("key_mode", [0o600, 0o640])
def test_good_files_build_a_tls13_context_that_requires_a_client_certificate(
    tmp_path, pair, key_mode
):
    server, proxy = pair
    (server / "server.key").chmod(key_mode)
    context = tls.server_context(tls_config(tmp_path, server, proxy))
    assert isinstance(context, ssl.SSLContext)
    assert context.minimum_version == ssl.TLSVersion.TLSv1_3
    assert context.verify_mode == ssl.CERT_REQUIRED
