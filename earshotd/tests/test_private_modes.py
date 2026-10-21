import os
import socket
import stat
from types import SimpleNamespace

import uvicorn

from earshotd import app as app_module
from earshotd import cli, lock, store, worker
from earshotd.config import Config

GOOD = "0123456789abcdef" * 4


def _mode(path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


def _umask() -> int:
    mask = os.umask(0)
    os.umask(mask)
    return mask


def test_every_file_the_store_creates_is_private_whatever_the_umask(tmp_path):
    os.umask(0o022)
    data = tmp_path / "data"
    db = data / "earshot.db"
    store.init(db)
    conn = store.connect(db)
    try:
        conn.execute("INSERT INTO voices (name, embedding) VALUES ('a', x'00')")
        for suffix in ("-wal", "-shm"):
            assert os.path.exists(f"{db}{suffix}")
        chunk = store.store_audio(db, data, "life1", 0, 1000, b"OggS")
        held = lock.acquire(data)
        try:
            assert _mode(data) == 0o700
            assert _mode(data / "limitless") == 0o700
            assert _mode(db) == 0o600
            assert _mode(f"{db}-wal") == 0o600
            assert _mode(f"{db}-shm") == 0o600
            assert _mode(chunk) == 0o600
            assert _mode(data / lock.NAME) == 0o600
        finally:
            held.close()
    finally:
        conn.close()


def _config_file(tmp_path, port) -> str:
    cfg = tmp_path / "earshotd.toml"
    cfg.write_text(
        f'data_dir = "{tmp_path / "data"}"\nlisten = "127.0.0.1:{port}"\n'
        f'[[tokens]]\ntoken = "{GOOD}"\nscope = "read"\n'
    )
    cfg.chmod(0o600)
    return str(cfg)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_app_main_runs_under_a_private_umask_and_binds_listen(tmp_path, monkeypatch):
    port = _free_port()
    monkeypatch.setenv("EARSHOTD_CONFIG", _config_file(tmp_path, port))
    monkeypatch.delenv("LISTEN_FDS", raising=False)
    monkeypatch.delenv("LISTEN_PID", raising=False)
    seen: dict = {}

    def run(app, **kwargs):
        seen.update(kwargs, umask=_umask())

    monkeypatch.setattr(uvicorn, "run", run)
    monkeypatch.setattr(worker, "start", lambda config: SimpleNamespace(status=None))
    os.umask(0o022)
    app_module.main()
    assert seen["umask"] == 0o077
    assert seen["host"] == "127.0.0.1" and seen["port"] == port
    assert "fd" not in seen


def test_cli_main_leaves_a_private_umask(tmp_path):
    config = Config(tokens={GOOD: "read"}, data_dir=tmp_path / "data")
    store.init(config.db_path)
    os.umask(0o022)
    assert cli.main(["rebuild"], config=config) == 0
    assert _umask() == 0o077
