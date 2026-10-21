import ctypes
import errno
import json
import os
import socket
import subprocess
import sys
import time
from types import SimpleNamespace

import httpx
import pytest
import uvicorn

from earshotd import app as app_module
from earshotd import worker

GOOD = "0123456789abcdef" * 4

# Runs app.main in a child the way systemd would start it: the listening socket as fd 3, with
# LISTEN_FDS and LISTEN_PID. Only the worker (it loads models) is stubbed; uvicorn.run is real
# but first records which LISTEN_* variables the process still carries.
CHILD = """
import json, os, sys
from types import SimpleNamespace
import uvicorn
from earshotd import app, worker

sock_fd, listen_pid, env_out = int(sys.argv[1]), sys.argv[2], sys.argv[3]
real_run = uvicorn.run

def run(application, **kwargs):
    with open(env_out, "w") as f:
        json.dump(sorted(k for k in os.environ if k.startswith("LISTEN_")), f)
    real_run(application, **kwargs)

uvicorn.run = run
worker.start = lambda config: SimpleNamespace(status=None)
os.dup2(sock_fd, 3)
os.environ["LISTEN_FDS"] = "1"
os.environ["LISTEN_PID"] = str(os.getpid()) if listen_pid == "self" else listen_pid
app.main()
"""


def _listening() -> socket.socket:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen()
    return s


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _spawn(tmp_path, passed: socket.socket, listen_port: int, listen_pid: str):
    cfg = tmp_path / "earshotd.toml"
    cfg.write_text(
        f'data_dir = "{tmp_path / "data"}"\nlisten = "127.0.0.1:{listen_port}"\n'
        f'[[tokens]]\ntoken = "{GOOD}"\nscope = "read"\n'
    )
    cfg.chmod(0o600)
    env_out = tmp_path / "env.json"
    env = {k: v for k, v in os.environ.items() if not k.startswith("LISTEN_")}
    env["EARSHOTD_CONFIG"] = str(cfg)
    p = subprocess.Popen(
        [sys.executable, "-c", CHILD, str(passed.fileno()), listen_pid, str(env_out)],
        env=env,
        pass_fds=(passed.fileno(),),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    return p, env_out


def _health(p: subprocess.Popen, port: int) -> httpx.Response | None:
    """The answer to GET /v1/health, or None when the child exited or never answered."""
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline and p.poll() is None:
        try:
            return httpx.get(f"http://127.0.0.1:{port}/v1/health", timeout=1)
        except httpx.TransportError:
            time.sleep(0.1)
    return None


def _assert_liveness_only(r: httpx.Response | None) -> None:
    assert r is not None
    assert r.status_code == 200
    assert r.json() == {"ok": True}
    assert "server" not in r.headers


def _stop(p: subprocess.Popen) -> str:
    if p.poll() is None:
        p.kill()
    return p.communicate()[1]


def test_app_main_serves_the_socket_systemd_passed(tmp_path):
    held = _listening()
    port = held.getsockname()[1]
    p, env_out = _spawn(tmp_path, held, port, "self")
    try:
        _assert_liveness_only(_health(p, port))
        assert json.loads(env_out.read_text()) == []
    finally:
        err = _stop(p)
        held.close()
    assert "Traceback" not in err, err


def test_app_main_refuses_a_passed_socket_that_is_not_listen(tmp_path):
    held = _listening()
    p1 = held.getsockname()[1]
    p2 = _free_port()
    p, _ = _spawn(tmp_path, held, p2, "self")
    try:
        try:
            code = p.wait(timeout=20)
        except subprocess.TimeoutExpired:
            code = None  # it served instead of refusing
        assert code not in (None, 0)
    finally:
        err = _stop(p)
        held.close()
    assert f"127.0.0.1:{p1}" in err
    assert f"127.0.0.1:{p2}" in err
    assert "install.sh" in err


@pytest.mark.parametrize("passed", [False, True])
def test_a_loopback_listen_without_tls_settings_gives_uvicorn_no_tls_argument(
    tmp_path, monkeypatch, capsys, passed
):
    cfg = tmp_path / "earshotd.toml"
    cfg.write_text(
        f'data_dir = "{tmp_path / "data"}"\nlisten = "127.0.0.1:{_free_port()}"\n'
        f'[[tokens]]\ntoken = "{GOOD}"\nscope = "read"\n'
    )
    cfg.chmod(0o600)
    monkeypatch.setenv("EARSHOTD_CONFIG", str(cfg))
    runs: list[dict] = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: runs.append(kw))
    monkeypatch.setattr(worker, "start", lambda config: SimpleNamespace(status=None))
    # the real fd-3 handover is covered above; here only which uvicorn.run call is made matters
    monkeypatch.setattr(
        app_module, "_passed_socket", lambda host, port: 3 if passed else None
    )
    app_module.main()
    (kw,) = runs
    assert ("fd" in kw) is passed
    assert kw.get("fd") == (3 if passed else None)
    assert "ssl_context_factory" not in kw and not any(k.startswith("ssl") for k in kw)
    assert capsys.readouterr().err == ""


def test_app_main_ignores_a_passed_socket_that_is_for_another_pid(tmp_path):
    held = _listening()
    p2 = _free_port()
    p, _ = _spawn(tmp_path, held, p2, str(os.getpid()))
    try:
        _assert_liveness_only(_health(p, p2))
    finally:
        _stop(p)
        held.close()


class FakeLaunchd:
    """launch_activate_socket as libSystem answers it: an errno, or a malloc'd fd array and count."""

    def __init__(self, err: int = 0, fds: tuple[int, ...] = ()):
        self.err, self.fds = err, fds
        self.asked: list[bytes] = []
        self.freed: list[object] = []

    def launch_activate_socket(self, name, fds_ref, count_ref):
        self.asked.append(name)
        if self.err:
            return self.err
        libc = ctypes.CDLL(None)
        libc.malloc.restype = ctypes.c_void_p
        raw = libc.malloc(4 * max(len(self.fds), 1))
        array = (ctypes.c_int * max(len(self.fds), 1)).from_address(raw)
        for i, fd in enumerate(self.fds):
            array[i] = fd
        ctypes.memmove(
            ctypes.addressof(fds_ref._obj), ctypes.byref(ctypes.c_void_p(raw)), 8
        )
        count_ref._obj.value = len(self.fds)
        return 0

    def free(self, pointer):
        self.freed.append(pointer)


def _no_systemd(monkeypatch):
    monkeypatch.delenv("LISTEN_FDS", raising=False)
    monkeypatch.delenv("LISTEN_PID", raising=False)


@pytest.mark.skipif(sys.platform != "darwin", reason="launchd is macOS")
def test_a_hand_run_on_a_mac_gets_no_passed_socket_and_no_error(monkeypatch):
    # launch_activate_socket really answers ESRCH here: this process is not a launchd job
    _no_systemd(monkeypatch)
    assert app_module._passed_socket("127.0.0.1", 8080) is None


def test_esrch_from_launchd_means_bind_listen_yourself(monkeypatch):
    _no_systemd(monkeypatch)
    fake = FakeLaunchd(err=errno.ESRCH)
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(app_module, "_launchd_lib", lambda: fake)
    assert app_module._passed_socket("127.0.0.1", 8080) is None
    assert fake.asked == [b"Listeners"]


def test_the_launchd_lookup_is_never_attempted_off_a_mac(monkeypatch):
    _no_systemd(monkeypatch)

    def boom():
        raise AssertionError("launchd consulted off darwin")

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(app_module, "_launchd_lib", boom)
    assert app_module._passed_socket("127.0.0.1", 8080) is None


def test_an_unrelated_launchd_error_exits_naming_it(monkeypatch):
    _no_systemd(monkeypatch)
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(
        app_module, "_launchd_lib", lambda: FakeLaunchd(err=errno.ENOENT)
    )
    with pytest.raises(SystemExit) as e:
        app_module._passed_socket("127.0.0.1", 8080)
    message = str(e.value.code)
    assert "launch_activate_socket" in message and "No such file" in message


def test_a_launchd_count_other_than_one_exits(monkeypatch):
    _no_systemd(monkeypatch)
    monkeypatch.setattr(sys, "platform", "darwin")
    fake = FakeLaunchd(fds=(os.dup(0), os.dup(0)))
    monkeypatch.setattr(app_module, "_launchd_lib", lambda: fake)
    with pytest.raises(SystemExit) as e:
        app_module._passed_socket("127.0.0.1", 8080)
    assert "launch_activate_socket" in str(e.value.code)
    assert "2 sockets" in str(e.value.code)
    assert len(fake.freed) == 1
    for fd in fake.fds:
        with pytest.raises(OSError):  # each fd launchd handed over was closed
            os.fstat(fd)


def test_a_launchd_socket_goes_through_the_same_address_check(monkeypatch):
    _no_systemd(monkeypatch)
    monkeypatch.setattr(sys, "platform", "darwin")
    held = _listening()
    other = _free_port()
    fake = FakeLaunchd(fds=(held.fileno(),))
    monkeypatch.setattr(app_module, "_launchd_lib", lambda: fake)
    try:
        with pytest.raises(SystemExit) as e:
            app_module._passed_socket("127.0.0.1", other)
        message = str(e.value.code)
        assert f"127.0.0.1:{held.getsockname()[1]}" in message
        assert f"127.0.0.1:{other}" in message
        assert "deploy/macos/install.sh" in message
    finally:
        held.close()
