import os
import signal
import socket
import sqlite3
import subprocess
import sys
import time

import httpx
from conftest import CT, UP


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _start(cfg_path, port):
    env = dict(os.environ, EARSHOTD_CONFIG=str(cfg_path))
    p = subprocess.Popen(
        [sys.executable, "-c", "from earshotd.app import main; main()"],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    for _ in range(100):
        try:
            httpx.get(
                f"http://127.0.0.1:{port}/", timeout=0.5
            )  # any answer means it is up
            return p
        except httpx.TransportError:
            time.sleep(0.1)
    p.kill()
    raise RuntimeError("earshotd did not start")


def _post(port, body):
    return httpx.post(
        f"http://127.0.0.1:{port}/v1/pages",
        content=body,
        headers={"Authorization": f"Bearer {UP}", "Content-Type": CT},
    )


def test_200_survives_sigkill_and_restart(tmp_path, body):
    port = _free_port()
    cfg = tmp_path / "earshotd.toml"
    cfg.write_text(
        f'data_dir = "{tmp_path / "data"}"\nlisten = "127.0.0.1:{port}"\n'
        f'[[tokens]]\ntoken = "{UP}"\nscope = "upload"\n'
    )
    cfg.chmod(0o600)  # config.load refuses a config others can read
    p = _start(cfg, port)
    try:
        r = _post(port, body)
        assert r.status_code == 200 and r.json() == {"stored": 36, "duplicates": 0}
        os.kill(p.pid, signal.SIGKILL)
        p.wait()
    finally:
        if p.poll() is None:
            p.kill()

    p = _start(cfg, port)
    try:
        conn = sqlite3.connect(tmp_path / "data" / "earshot.db")
        assert conn.execute("SELECT count(*) FROM pages").fetchone()[0] == 36
        conn.close()
        assert _post(port, body).json() == {"stored": 0, "duplicates": 36}
    finally:
        p.kill()
        p.wait()
