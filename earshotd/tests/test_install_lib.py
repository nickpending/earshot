"""deploy/install-lib.sh's functions, driven through real bash the way install.sh calls them."""

import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

import pytest
from test_proxy_cert import (
    cert_facts,
    fingerprint,
    make_proxy_cert,
    mode_of,
    openssl_out,
)

ROOT = Path(__file__).resolve().parents[2]
DEPLOY = ROOT / "deploy"
LIB = DEPLOY / "install-lib.sh"
EXAMPLE = DEPLOY / "earshotd.toml.example"
BASH = shutil.which("bash")
GOOD = "0123456789abcdef" * 4
REVISION = "0a363e9161cbc7ed1431c9597a8ceaf0c4f78fcf"

pytestmark = pytest.mark.skipif(BASH is None, reason="needs bash")


def bash(script: str, env: dict | None = None, cwd=None) -> subprocess.CompletedProcess:
    """Run `script` in a bash that has sourced the library, under install.sh's own options."""
    assert BASH
    full = f"set -euo pipefail; source {shlex.quote(str(LIB))}; {script}"
    return subprocess.run(
        [BASH, "-c", full],
        env=env,
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )


def wrappers(
    tmp_path: Path, names: list[str], stub: tuple[str, ...] = ()
) -> tuple[Path, Path]:
    """A bin directory of wrappers that append their own argv to a log, then run the real command.

    A name in `stub` is only logged and succeeds. Returns (bin directory, log file).
    """
    assert BASH
    bindir = tmp_path / "wrapbin"
    bindir.mkdir()
    log = tmp_path / "argv.log"
    log.touch()
    for name in names:
        real = shutil.which(name)
        if real is None:
            continue
        run = "exit 0" if name in stub else f'exec {shlex.quote(real)} "$@"'
        wrapper = bindir / name
        wrapper.write_text(
            f"#!{BASH}\nprintf '%s\\n' \"$0 $*\" >> {shlex.quote(str(log))}\n{run}\n"
        )
        wrapper.chmod(0o755)
    return bindir, log


COMMANDS = [
    "openssl", "sed", "cat", "chmod", "chown", "mv", "cp", "tr", "head", "awk",
    "grep", "dirname", "install", "rm", "mkdir", "touch", "printf", "od", "xxd",
]  # fmt: skip


def _seed(tmp_path: Path) -> tuple[Path, str, Path]:
    bindir, log = wrappers(tmp_path, COMMANDS)
    dest = tmp_path / "seeded.toml"
    done = bash(
        f"seed_config {shlex.quote(str(EXAMPLE))} {shlex.quote(str(dest))}",
        env={"PATH": str(bindir)},
    )
    assert done.returncode == 0, done.stderr
    return dest, log.read_text(), bindir


def test_seeding_replaces_each_placeholder_with_its_own_token_none_on_an_argv(tmp_path):
    dest, argv_log, _ = _seed(tmp_path)
    text = dest.read_text()
    assert "REPLACE_WITH" not in text
    tokens = {t["scope"]: t["token"] for t in tomllib.loads(text)["tokens"]}
    assert set(tokens) == {"upload", "read"}
    assert tokens["upload"] != tokens["read"]
    for token in tokens.values():
        assert re.fullmatch(r"[0-9a-f]{64}", token)
        assert token not in argv_log
    expected = (
        EXAMPLE.read_text()
        .replace("REPLACE_WITH_ANOTHER_openssl_rand_hex_32", tokens["read"])
        .replace("REPLACE_WITH_openssl_rand_hex_32", tokens["upload"])
    )
    assert text == expected
    assert "openssl rand -hex 32" in argv_log  # the wrappers were on the path


def test_an_existing_config_is_fixed_to_root_earshot_0640_though_nothing_is_seeded(
    tmp_path,
):
    bindir, log = wrappers(tmp_path, COMMANDS, stub=("chown",))
    cfg = tmp_path / "earshotd.toml"
    cfg.write_text("keep = 1\n")
    cfg.chmod(0o644)
    done = bash(
        f"config_step {shlex.quote(str(EXAMPLE))} {shlex.quote(str(cfg))}",
        env={"PATH": str(bindir)},
    )
    assert done.returncode == 0, done.stderr
    assert stat.S_IMODE(cfg.stat().st_mode) == 0o640
    assert f"root:earshot {cfg}" in log.read_text()
    assert cfg.read_text() == "keep = 1\n"


def _config(tmp_path: Path, listen: str | None, extra: tuple[str, ...] = ()) -> Path:
    cfg = tmp_path / "earshotd.toml"
    lines = [f'data_dir = "{tmp_path}"', *extra]
    if listen is not None:
        lines.append(f'listen = "{listen}"')
    lines += ["[[tokens]]", f'token = "{GOOD}"', 'scope = "read"']
    cfg.write_text("\n".join(lines) + "\n")
    cfg.chmod(0o600)
    return cfg


def _render(tmp_path: Path, listen: str | None, extra: tuple[str, ...] = ()):
    cfg = _config(tmp_path, listen, extra)
    out = tmp_path / "out"
    out.mkdir(exist_ok=True)
    done = bash(
        "render_units"
        f" {shlex.quote(str(cfg))}"
        f" {shlex.quote(str(DEPLOY / 'earshotd.socket'))}"
        f" {shlex.quote(str(DEPLOY / 'nftables-earshot.conf'))}"
        f" {shlex.quote(str(out / 'earshotd.socket'))}"
        f" {shlex.quote(str(out / 'earshot.conf'))}",
        env={**os.environ, "EARSHOT_PY": sys.executable},
    )
    return done, out


PROXIES = 'proxy_addresses = ["192.0.2.10", "2001:db8::5"]'
TLS_NETWORK = (
    'tls_cert = "/x/server.crt"',
    'tls_key = "/x/server.key"',
    'tls_proxy_certs = ["/x/proxy.crt"]',
)


@pytest.mark.parametrize(
    ("listen", "stream"),
    [
        (None, "127.0.0.1:8080"),
        ("127.0.0.1:8080", "127.0.0.1:8080"),
        ("[::1]:8091", "[::1]:8091"),
    ],
)
def test_a_loopback_listen_renders_the_socket_and_no_firewall(tmp_path, listen, stream):
    done, out = _render(tmp_path, listen)
    assert done.returncode == 0, done.stderr
    assert "no firewall is needed" in done.stderr
    assert f"ListenStream={stream}\n" in (out / "earshotd.socket").read_text()
    assert [p.name for p in out.iterdir()] == ["earshotd.socket"]


def test_a_network_listen_without_proxy_addresses_is_refused_and_renders_nothing(
    tmp_path,
):
    done, out = _render(tmp_path, "0.0.0.0:9090", TLS_NETWORK)
    assert done.returncode != 0
    assert "proxy_addresses" in done.stderr
    assert list(out.iterdir()) == []


def test_a_network_listen_renders_one_accept_line_per_proxy_address(tmp_path):
    done, out = _render(tmp_path, "0.0.0.0:9090", (*TLS_NETWORK, PROXIES))
    assert done.returncode == 0, done.stderr
    assert "ListenStream=0.0.0.0:9090\n" in (out / "earshotd.socket").read_text()
    nft = (out / "earshot.conf").read_text()
    chain = nft[nft.index("chain input") :]
    lines = [
        ln.strip() for ln in chain.splitlines()[1:] if ln.strip() and ln.strip() != "}"
    ]
    assert lines == [
        "type filter hook input priority filter; policy accept;",
        'iif "lo" accept',
        "ip saddr 192.0.2.10 tcp dport 9090 accept",
        "ip6 saddr 2001:db8::5 tcp dport 9090 accept",
        "tcp dport 9090 drop",
    ]
    assert "8080" not in nft


def _nft_parses(path: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["nft", "-c", "-f", str(path)], capture_output=True, text=True, check=False
    )


def test_the_rendered_firewall_passes_nft_check(tmp_path):
    if shutil.which("nft") is None:
        pytest.skip("nft is not installed on this host")
    # nft -c reads the kernel ruleset, which needs CAP_NET_ADMIN; Ubuntu 24.04 also refuses the
    # unprivileged user namespace that would grant it. A ruleset known to be valid tells a host
    # that cannot check apart from a rendered file that does not parse.
    known_good = tmp_path / "known-good.conf"
    known_good.write_text("table inet probe {\n\tchain input {\n\t}\n}\n")
    probe = _nft_parses(known_good)
    if probe.returncode != 0:
        pytest.skip(f"nft -c cannot run as this user: {probe.stderr.strip()}")
    done, out = _render(tmp_path, "0.0.0.0:9090", (*TLS_NETWORK, PROXIES))
    assert done.returncode == 0, done.stderr
    checked = _nft_parses(out / "earshot.conf")
    assert checked.returncode == 0, checked.stderr


def test_the_firewall_template_holds_no_ip_address():
    text = (DEPLOY / "nftables-earshot.conf").read_text()
    assert not re.search(r"\b\d{1,3}(\.\d{1,3}){3}\b", text)
    assert not re.search(r"[0-9a-f]*:[0-9a-f]*:[0-9a-f:]+", text)


def test_a_listen_config_load_refuses_renders_nothing(tmp_path):
    done, out = _render(tmp_path, "127.0.0.1:0")
    assert done.returncode != 0
    assert "listen" in done.stderr
    assert list(out.iterdir()) == []


def _key_value(done: subprocess.CompletedProcess, key: str, value: str) -> None:
    assert done.returncode != 0
    assert key in done.stderr and value in done.stderr


def test_a_model_location_the_service_could_write_is_refused(tmp_path):
    mine = tmp_path / "models"
    mine.mkdir()
    mine.chmod(0o755)
    missing = tmp_path / "absent"
    cases = ["large-v3-turbo", str(missing), str(mine)]
    for value in cases:
        done = bash(
            f"check_model_location whisper_model {shlex.quote(value)}", cwd=tmp_path
        )
        _key_value(done, "whisper_model", value)


def _find_trusting_me(tmp_path: Path) -> str:
    """A PATH whose find treats the test user as root too, so only the write bits decide."""
    assert BASH
    real = shutil.which("find")
    assert real
    bindir = tmp_path / "findbin"
    bindir.mkdir()
    wrapper = bindir / "find"
    wrapper.write_text(
        f"#!{BASH}\n"
        "args=()\n"
        "while [ $# -gt 0 ]; do\n"
        '  if [ "$1" = -user ] && [ "${2-}" = root ]; then\n'
        f"    args+=( '(' -user root -o -user {os.getuid()} ')' ); shift 2\n"
        '  else args+=("$1"); shift; fi\n'
        "done\n"
        f'exec {shlex.quote(real)} "${{args[@]}}"\n'
    )
    wrapper.chmod(0o755)
    return f"{bindir}{os.pathsep}{os.environ['PATH']}"


@pytest.fixture
def private_dir():
    """A fresh 0755 directory under the user's home, removed afterwards.

    Linux puts tmp_path below world-writable /tmp, which the check refuses as a parent, so a tree
    whose own modes must decide lives under the home directory, whose parents nobody else can write.
    """
    private = Path(tempfile.mkdtemp(prefix="earshot-test-", dir=Path.home()))
    private.chmod(0o755)
    yield private
    shutil.rmtree(private, ignore_errors=True)


def test_a_directory_or_parent_others_or_the_group_can_write_is_refused(
    tmp_path, private_dir
):
    env = {**os.environ, "PATH": _find_trusting_me(tmp_path)}
    open_dir = private_dir / "open"
    open_dir.mkdir()
    shut = open_dir / "models"
    shut.mkdir()
    shut.chmod(0o755)
    # 0o1777 is /tmp's own mode: sticky does not make a world-writable directory safe to load from.
    for mode, victim in [
        (0o777, open_dir),
        (0o775, open_dir),
        (0o1777, open_dir),
        (0o777, shut),
    ]:
        open_dir.chmod(mode)
        done = bash(
            f"check_model_location whisper_model {shlex.quote(str(victim))}", env=env
        )
        assert done.returncode != 0, (oct(mode), victim)
        assert "whisper_model" in done.stderr
        assert f"{open_dir} is not root-owned" in done.stderr, done.stderr


def _tight_tree(top: Path) -> Path:
    """A model-like tree at `top`: 0755 directories and 0644 files, a subdirectory among them."""
    (top / "embedding").mkdir(parents=True)
    (top / "embedding" / "pytorch_model.bin").write_bytes(b"weights")
    (top / "config.yaml").write_text("pipeline: {}\n")
    for path in [top, top / "embedding"]:
        path.chmod(0o755)
    for path in [top / "config.yaml", top / "embedding" / "pytorch_model.bin"]:
        path.chmod(0o644)
    return top


def test_a_root_owned_directory_nobody_else_can_write_is_accepted(
    tmp_path, private_dir
):
    env = {**os.environ, "PATH": _find_trusting_me(tmp_path)}
    tight = _tight_tree(private_dir / "tight")
    done = bash(
        f"check_model_location whisper_model {shlex.quote(str(tight))}", env=env
    )
    assert done.returncode == 0, done.stderr


def test_a_tree_find_cannot_fully_read_is_refused_through_check_models(
    tmp_path, private_dir
):
    if os.geteuid() == 0:
        pytest.skip(
            "root reads a mode-000 directory, so this user cannot hide one from find"
        )
    good = _tight_tree(private_dir / "diarization")
    leaky = _tight_tree(private_dir / "whisper")
    hidden = leaky / "hidden"
    hidden.mkdir()
    (hidden / "weights.bin").write_bytes(b"swapped")
    (hidden / "weights.bin").chmod(0o666)
    hidden.chmod(0o000)
    cfg = _config(tmp_path, None)
    cfg.write_text(
        f'whisper_model = "{leaky}"\ndiarization_model = "{good}"\n' + cfg.read_text()
    )
    env = {
        **os.environ,
        "EARSHOT_PY": sys.executable,
        "PATH": _find_trusting_me(tmp_path),
    }
    try:
        done = bash(f"check_models {shlex.quote(str(cfg))}", env=env, cwd=tmp_path)
    finally:
        hidden.chmod(0o755)
    assert done.returncode != 0
    assert (
        f'refusing whisper_model = "{leaky}": find could not read all of {leaky}'
        in done.stderr
    ), done.stderr
    assert "diarization_model" not in done.stderr


def test_a_parent_find_cannot_read_is_refused(tmp_path, private_dir):
    # A parent find fails only on a broken filesystem, which a test cannot make; a find that fails
    # on -maxdepth 0 stands in for it, and everything else goes to the find that trusts the user.
    trusting = _find_trusting_me(tmp_path)
    bindir = tmp_path / "failbin"
    bindir.mkdir()
    (bindir / "find").write_text(
        f"#!{BASH}\n"
        'case " $* " in *" -maxdepth 0 "*) echo "find: simulated read error" >&2; exit 1;; esac\n'
        f'PATH={shlex.quote(trusting)} exec find "$@"\n'
    )
    (bindir / "find").chmod(0o755)
    tight = _tight_tree(private_dir / "tight")
    env = {**os.environ, "PATH": f"{bindir}{os.pathsep}{trusting}"}
    done = bash(
        f"rc=0; check_model_location whisper_model {shlex.quote(str(tight))} || rc=1;"
        " exit $rc",
        env=env,
    )
    assert done.returncode == 1, done.stderr
    assert f"find could not read its parent {private_dir}" in done.stderr, done.stderr


def test_every_refused_model_line_is_named_in_one_run(tmp_path):
    cfg = _config(tmp_path, None)
    # top-level keys must come before the [[tokens]] table
    cfg.write_text(
        'whisper_model = "large-v3-turbo"\n'
        f'diarization_model = "{tmp_path}"\n' + cfg.read_text()
    )
    done = bash(
        f"check_models {shlex.quote(str(cfg))}",
        env={**os.environ, "EARSHOT_PY": sys.executable},
        cwd=tmp_path,
    )
    assert done.returncode != 0
    assert 'whisper_model = "large-v3-turbo"' in done.stderr
    assert f'diarization_model = "{tmp_path}"' in done.stderr


def test_a_module_in_the_current_directory_is_never_imported_by_install(tmp_path):
    # root runs install.sh from some directory: nothing there may run before the venv's packages
    trap = tmp_path / "cwd"
    trap.mkdir()
    marker = tmp_path / "ran.txt"
    for name in ("earshotd.py", "faster_whisper.py"):
        (trap / name).write_text(f"open({str(marker)!r}, 'a').write('ran')\n")
    cfg = _config(tmp_path, None)
    env = {**os.environ, "EARSHOT_PY": sys.executable}
    models = tmp_path / "models"
    models.mkdir()
    bash(f"config_values {shlex.quote(str(cfg))}", env=env, cwd=trap)
    bash(
        f"fetch_whisper {shlex.quote(str(models))}",
        env=env,
        cwd=trap,
    )
    assert not marker.exists()


def test_tightening_strips_group_and_other_below_the_directory_only(tmp_path):
    top = tmp_path / "data"
    sub = top / "limitless"
    sub.mkdir(parents=True)
    f1, f2 = top / "earshot.db", sub / "a.ogg"
    f1.write_text("x")
    f2.write_text("y")
    f1.chmod(0o644)
    sub.chmod(0o755)
    f2.chmod(0o664)
    top.chmod(0o750)
    # runuser is wrapped: it logs its argv, drops "-u <user> --" and runs the rest as the test user
    bindir = tmp_path / "runuserbin"
    bindir.mkdir()
    log = tmp_path / "runuser.log"
    assert BASH
    wrapper = bindir / "runuser"
    wrapper.write_text(
        f"#!{BASH}\nprintf '%s\\n' \"$*\" >> {shlex.quote(str(log))}\n"
        '[ "$1" = -u ] && [ "$3" = -- ] || exit 64\nshift 3\nexec "$@"\n'
    )
    wrapper.chmod(0o755)
    env = {**os.environ, "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}"}
    done = bash(f"tighten_data {shlex.quote(str(top))}", env=env)
    assert done.returncode == 0, done.stderr
    assert log.read_text().startswith("-u earshot -- find ")
    assert stat.S_IMODE(f1.stat().st_mode) == 0o600
    assert stat.S_IMODE(sub.stat().st_mode) == 0o700
    assert stat.S_IMODE(f2.stat().st_mode) == 0o600
    assert stat.S_IMODE(top.stat().st_mode) == 0o750


RECORDER = """#!{python}
import json, os, sys
hf = os.environ.get("HF_HOME")
json.dump(
    {{
        "argv": sys.argv[1:],
        "hf_home": hf,
        "hf_empty_dir": bool(hf) and os.path.isdir(hf) and not os.listdir(hf),
    }},
    open({out!r}, "w"),
)
"""


def test_install_sh_defines_no_whisper_pin():
    text = (DEPLOY / "install.sh").read_text()
    assert "WHISPER_REVISION" not in text and "WHISPER_REPO" not in text


def test_whisper_is_fetched_with_only_the_models_directory_through_a_throwaway_hf_home(
    tmp_path,
):
    out = tmp_path / "recorded.json"
    recorder = tmp_path / "python"
    recorder.write_text(RECORDER.format(python=sys.executable, out=str(out)))
    recorder.chmod(0o755)
    models = tmp_path / "models"
    models.mkdir()
    done = bash(
        f"fetch_whisper {shlex.quote(str(models))}",
        env={**os.environ, "EARSHOT_PY": str(recorder)},
    )
    assert done.returncode == 0, done.stderr
    rec = json.loads(out.read_text())
    assert rec["argv"][-1] == str(models)
    assert rec["hf_empty_dir"]
    assert rec["hf_home"] != "/var/lib/earshot/hf"
    assert not os.path.exists(rec["hf_home"])


FAKE_FASTER_WHISPER = """
import json
def download_model(size_or_id, output_dir=None, revision=None, **rest):
    json.dump(
        {"id": size_or_id, "output_dir": output_dir, "revision": revision, "rest": rest},
        open(%r, "w"),
    )
    return output_dir
"""


def test_the_fetch_hands_download_model_the_repo_directory_and_revision(tmp_path):
    out = tmp_path / "kwargs.json"
    pkg = tmp_path / "fake" / "faster_whisper"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "utils.py").write_text(FAKE_FASTER_WHISPER % str(out))
    models = tmp_path / "models"
    models.mkdir()
    # python runs isolated (-I ignores PYTHONPATH), so the fake goes in through a wrapper that
    # insists on -I and prepends the fake package directory to sys.path inside it
    py = tmp_path / "py"
    py.write_text(
        f"#!{sys.executable}\n"
        "import os, sys\n"
        "args = sys.argv[1:]\n"
        "assert args[0] == '-I' and args[1] == '-c', args\n"
        f"code = 'import sys; sys.path.insert(0, ' + repr({str(tmp_path / 'fake')!r})"
        " + ')\\n' + args[2]\n"
        f"os.execv({sys.executable!r}, ['python', '-I', '-c', code, *args[3:]])\n"
    )
    py.chmod(0o755)
    done = bash(
        f"fetch_whisper {shlex.quote(str(models))}",
        env={**os.environ, "EARSHOT_PY": str(py)},
    )
    assert done.returncode == 0, done.stderr
    assert json.loads(out.read_text()) == {
        "id": "mobiuslabsgmbh/faster-whisper-large-v3-turbo",
        "output_dir": str(models / "faster-whisper-large-v3-turbo"),
        "revision": REVISION,
        "rest": {},
    }


def _scratch(tmp_path: Path, name: str) -> Path:
    """A directory of its own for one run's wrapper bin and argv log."""
    scratch = tmp_path / f"scratch-{name}"
    scratch.mkdir(exist_ok=True)
    return scratch


def _server_tls(scratch: Path, tls_dir: Path):
    """Run server_tls on tls_dir with chown and runuser only logged; returns (done, argv log)."""
    bindir, log = wrappers(scratch, [*COMMANDS, "runuser"], stub=("chown", "runuser"))
    env = {**os.environ, "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}"}
    done = bash(f"server_tls {shlex.quote(str(tls_dir))}", env=env)
    return done, log


def make_server_tls(tmp_path: Path, name: str = "tls") -> Path:
    """A fresh server key and certificate in tmp_path/<name>, as install.sh makes them."""
    out = tmp_path / name
    done, _ = _server_tls(_scratch(tmp_path, f"make-{name}"), out)
    assert done.returncode == 0, done.stderr
    return out


def test_server_tls_generates_the_servers_own_pair_once_and_resets_its_modes(tmp_path):
    os.umask(0o022)
    tls = tmp_path / "etc-earshot-tls"
    done, log = _server_tls(_scratch(tmp_path, "one"), tls)
    assert done.returncode == 0, done.stderr
    key, crt = tls / "server.key", tls / "server.crt"
    assert mode_of(key) == 0o640 and mode_of(crt) == 0o644 and mode_of(tls) == 0o750
    recorded = log.read_text()
    assert f"root:earshot {tls}\n" in recorded
    assert f"root:earshot {key}\n" in recorded
    assert f"root:root {crt}\n" in recorded
    key_text = openssl_out("pkey", "-in", str(key), "-noout", "-text")
    assert "prime256v1" in key_text or "P-256" in key_text
    facts = cert_facts(crt)
    assert facts["subject"] == "CN=earshotd"
    assert facts["self_signed"]
    assert facts["san"] == "DNS:earshotd"
    assert facts["ca_critical_false"]
    assert (
        facts["eku"] == "TLS Web Server Authentication, TLS Web Client Authentication"
    )
    assert abs(facts["days"] - 3650) <= 1
    assert fingerprint(crt) in done.stderr + done.stdout
    for line in key.read_text().splitlines()[1:-1]:
        assert line not in recorded  # no argv ever holds key material
    assert "PRIVATE KEY" not in recorded

    before = (key.read_bytes(), crt.read_bytes())
    key.chmod(0o644)
    crt.chmod(0o600)
    again, _ = _server_tls(_scratch(tmp_path, "two"), tls)
    assert again.returncode == 0, again.stderr
    assert (key.read_bytes(), crt.read_bytes()) == before
    assert mode_of(key) == 0o640 and mode_of(crt) == 0o644
    assert fingerprint(crt) in again.stderr + again.stdout


def _check_tls(tmp_path: Path, server: Path, proxy_crt: Path, key_mode: int = 0o640):
    (server / "server.key").chmod(key_mode)
    cfg = _config(
        tmp_path,
        None,
        (
            f'tls_cert = "{server / "server.crt"}"',
            f'tls_key = "{server / "server.key"}"',
            f'tls_proxy_certs = ["{proxy_crt}"]',
        ),
    )
    return bash(
        f"check_tls {shlex.quote(str(cfg))}",
        env={**os.environ, "EARSHOT_PY": sys.executable},
    )


def test_check_tls_passes_a_config_without_tls_settings(tmp_path):
    cfg = _config(tmp_path, None)
    done = bash(
        f"check_tls {shlex.quote(str(cfg))}",
        env={**os.environ, "EARSHOT_PY": sys.executable},
    )
    assert done.returncode == 0, done.stderr


def test_check_tls_passes_good_files_and_names_the_file_it_refuses(tmp_path):
    server = make_server_tls(tmp_path)
    proxy = make_proxy_cert(tmp_path, "proxy")
    good = _check_tls(tmp_path, server, proxy / "proxy.crt")
    assert good.returncode == 0, good.stderr

    missing = proxy / "absent.crt"
    done = _check_tls(tmp_path, server, missing)
    assert done.returncode != 0
    assert str(missing) in done.stderr

    done = _check_tls(tmp_path, server, proxy / "proxy.crt", key_mode=0o644)
    assert done.returncode != 0
    assert str(server / "server.key") in done.stderr and "0644" in done.stderr


def test_install_sh_checks_tls_before_it_enables_anything():
    text = (DEPLOY / "install.sh").read_text()
    check = text.index("check_tls ")
    for verb in ("enable", "stop", "restart", "start"):
        assert check < text.index(f"systemctl {verb}"), verb
    assert "server_tls " in text


def test_server_tls_does_not_replace_a_key_whose_certificate_is_gone(tmp_path):
    tls = make_server_tls(tmp_path)
    (tls / "server.crt").unlink()
    done, _ = _server_tls(_scratch(tmp_path, "again"), tls)
    assert done.returncode != 0
    assert "server.crt" in done.stderr


def _stubs(tmp_path: Path, logged: tuple[str, ...], real: tuple[str, ...]):
    """A bin directory holding only wrappers: `logged` names are logged and succeed, `real`
    names are logged then run for real. Returns (bin directory, log file)."""
    assert BASH
    bindir = tmp_path / "stubbin"
    bindir.mkdir()
    log = tmp_path / "calls.log"
    log.touch()
    for name in (*logged, *real):
        run = (
            "exit 0"
            if name in logged
            else f'exec {shlex.quote(shutil.which(name) or "")} "$@"'
        )
        wrapper = bindir / name
        wrapper.write_text(
            f"#!{BASH}\nprintf '%s\\n' \"{name} $*\" >> {shlex.quote(str(log))}\n{run}\n"
        )
        wrapper.chmod(0o755)
    return bindir, log


def _firewall(
    tmp_path: Path, listen: str | None, extra: tuple[str, ...], with_nft: bool
):
    """Render the units for a config, then run firewall_step on the result with only stubs on PATH."""
    done, out = _render(tmp_path, listen, extra)
    assert done.returncode == 0, done.stderr
    logged = ("uv", "openssl", "systemctl") + (("nft",) if with_nft else ())
    bindir, log = _stubs(tmp_path, logged, ("install", "grep"))
    nftd, main = tmp_path / "nftables.d", tmp_path / "nftables.conf"
    main.write_text("# main\n")
    ran = bash(
        f"require_tools; firewall_step {shlex.quote(str(out / 'earshot.conf'))}"
        f" {shlex.quote(str(nftd))} {shlex.quote(str(main))}",
        env={"PATH": str(bindir)},
    )
    return ran, log.read_text(), nftd, main, out


def test_a_loopback_listen_loads_no_firewall_and_does_not_need_nft(tmp_path):
    ran, calls, nftd, _, _ = _firewall(tmp_path, None, (), with_nft=False)
    assert ran.returncode == 0, ran.stderr
    assert "nft" not in calls and "systemctl" not in calls
    assert not nftd.exists()


def test_a_network_listen_without_nft_is_refused_before_nft_or_systemctl_run(tmp_path):
    ran, calls, nftd, main, _ = _firewall(
        tmp_path, "0.0.0.0:9090", (*TLS_NETWORK, PROXIES), with_nft=False
    )
    assert ran.returncode != 0
    assert "apt install nftables" in ran.stderr
    assert "nft " not in calls and "systemctl" not in calls
    assert not nftd.exists() and main.read_text() == "# main\n"


def test_a_network_listen_checks_then_loads_the_rendered_firewall_and_enables_nftables(
    tmp_path,
):
    ran, calls, nftd, main, out = _firewall(
        tmp_path, "0.0.0.0:9090", (*TLS_NETWORK, PROXIES), with_nft=True
    )
    assert ran.returncode == 0, ran.stderr
    conf = nftd / "earshot.conf"
    assert conf.read_text() == (out / "earshot.conf").read_text()
    sequence = [ln for ln in calls.splitlines() if ln.startswith(("nft", "systemctl"))]
    assert sequence == [
        f"nft -c -f {conf}",
        f"nft -f {conf}",
        "systemctl enable nftables",
    ]
    assert main.read_text().count(f'include "{nftd}/*.conf"') == 1
    again = bash(
        f"firewall_step {shlex.quote(str(out / 'earshot.conf'))}"
        f" {shlex.quote(str(nftd))} {shlex.quote(str(main))}",
        env={"PATH": str(tmp_path / "stubbin")},
    )
    assert again.returncode == 0, again.stderr
    assert main.read_text().count("include") == 1


def test_install_sh_asks_nft_nothing_at_the_top_and_takes_the_snapshot_as_optional():
    text = (DEPLOY / "install.sh").read_text()
    assert "command -v nft" not in text
    assert 'SNAPSHOT="${1:-}"' in text and "${1:?" not in text


STUB_PY = """#!{python}
import os, sys
a = sys.argv[1:]
if a[:3] == ["-I", "-m", "earshotd.models"] or (a[:2] == ["-I", "-c"] and "download_model" in a[2]):
    sys.exit(0)
os.execv({python!r}, [{python!r}, *a])
"""
REAL_TOOLS = ["cp", "rm", "mkdir", "mktemp", "openssl", "mv", "rmdir", "grep", "chmod", "dirname", "cat", "tr", "head", "sed", "awk", "xxd", "od", "touch"]  # fmt: skip


def _run_install_sh(
    tmp_path: Path,
    listen: str | None,
    extra: tuple[str, ...] = (),
    with_nft: bool = False,
):
    """install.sh itself, run through real bash in a scratch root: its /etc, /opt and /var/lib are
    rewritten into the root, and nft, systemctl, install, uv, chown, useradd, runuser and id are
    logging wrappers (the real python reads the config, the model steps are no-ops). Returns
    (done, the logged calls)."""
    assert BASH
    root = tmp_path / "root"
    repo = root / "repo"
    (repo / "deploy").mkdir(parents=True)
    (repo / "earshotd").mkdir()
    for name in (
        "install-lib.sh",
        "earshotd.socket",
        "earshotd.service",
        "nftables-earshot.conf",
    ):
        shutil.copy(DEPLOY / name, repo / "deploy" / name)
    shutil.copy(EXAMPLE, repo / "deploy" / EXAMPLE.name)
    for name in ("pyproject.toml", "uv.lock"):
        shutil.copy(ROOT / "earshotd" / name, repo / "earshotd" / name)
    (repo / "earshotd" / "src").mkdir()
    (repo / "earshotd" / "src" / "x.py").write_text("")
    script = repo / "deploy" / "install.sh"
    script.write_text(
        re.sub(
            r"(?<![\w.$])/(etc|opt|var)/",
            rf"{root}/\1/",
            (DEPLOY / "install.sh").read_text(),
        )
    )
    models_dir = root / "opt/earshot/models"
    for name in ("faster-whisper-large-v3-turbo", "speaker-diarization-community-1"):
        (models_dir / name).mkdir(parents=True)
    tls = root / "etc/earshot/tls"
    tls.mkdir(parents=True)
    (root / "etc/systemd/system").mkdir(parents=True)
    log = tmp_path / "calls.log"
    log.touch()
    bindir = tmp_path / "stubbin"
    bindir.mkdir()

    def wrapper(name: str, body: str) -> None:
        path = bindir / name
        path.write_text(
            f"#!{BASH}\nprintf '%s\\n' \"{name} $*\" >> {shlex.quote(str(log))}\n{body}\n"
        )
        path.chmod(0o755)

    for name in ("nft", "systemctl", "uv", "useradd", "chown"):
        if name != "nft" or with_nft:
            wrapper(name, "exit 0")
    wrapper("id", 'if [ "${1:-}" = -u ]; then echo 0; fi')
    wrapper("runuser", 'shift 3; exec "$@"')
    real_install = shutil.which("install")
    wrapper(
        "install",
        'args=(); while [ $# -gt 0 ]; do case $1 in -o|-g) shift 2 ;; *) args+=("$1"); shift ;; esac; done\n'
        f'exec {shlex.quote(real_install or "")} "${{args[@]}}"',
    )
    real_find = shutil.which("find")
    (bindir / "find").write_text(
        f'#!{BASH}\nfor a in "$@"; do [ "$a" = -user ] && exit 0; done\nexec {shlex.quote(real_find or "")} "$@"\n'
    )
    (bindir / "find").chmod(0o755)
    for name in REAL_TOOLS:
        real = shutil.which(name)
        if real:
            (bindir / name).write_text(f'#!{BASH}\nexec {shlex.quote(real)} "$@"\n')
            (bindir / name).chmod(0o755)
    py = tmp_path / "py"
    py.write_text(STUB_PY.format(python=sys.executable))
    py.chmod(0o755)

    lines = [
        f'data_dir = "{root}/var/lib/earshot"',
        f'whisper_model = "{models_dir}/faster-whisper-large-v3-turbo"',
        f'diarization_model = "{models_dir}/speaker-diarization-community-1"',
        *extra,
    ]
    if listen is not None:
        lines.insert(0, f'listen = "{listen}"')
    cfg = root / "etc/earshot/earshotd.toml"
    cfg.write_text(
        "\n".join(lines) + f'\n[[tokens]]\ntoken = "{GOOD}"\nscope = "read"\n'
    )
    cfg.chmod(0o640)
    proxy = make_proxy_cert(tmp_path, "proxy")
    shutil.copy(proxy / "proxy.crt", tls / "proxy.crt")
    done = subprocess.run(
        [BASH, str(script)],
        env={"PATH": str(bindir), "EARSHOT_PY": str(py), "TMPDIR": str(tmp_path)},
        capture_output=True,
        text=True,
        check=False,
    )
    return done, log.read_text().splitlines(), root


def _service_calls(calls: list[str]) -> list[str]:
    return [c for c in calls if c.startswith(("nft ", "systemctl "))]


def _network_lines(tmp_path: Path, proxies: bool) -> tuple[str, ...]:
    tls = tmp_path / "root/etc/earshot/tls"
    lines = (
        f'tls_cert = "{tls}/server.crt"',
        f'tls_key = "{tls}/server.key"',
        f'tls_proxy_certs = ["{tls}/proxy.crt"]',
    )
    return (*lines, PROXIES) if proxies else lines


def test_install_sh_on_a_loopback_listen_needs_no_nft_and_loads_no_firewall(tmp_path):
    done, calls, _ = _run_install_sh(tmp_path, None)
    assert done.returncode == 0, done.stderr
    assert "no firewall is needed" in done.stderr
    assert [c for c in calls if c.startswith("nft ")] == []
    assert _service_calls(calls) == [
        "systemctl daemon-reload",
        "systemctl enable earshotd.socket earshotd",
        "systemctl stop earshotd",
        "systemctl restart earshotd.socket",
        "systemctl start earshotd",
    ]


def test_install_sh_on_a_network_listen_without_nft_stops_before_any_nft_or_systemctl(
    tmp_path,
):
    done, calls, _ = _run_install_sh(
        tmp_path, "0.0.0.0:9090", _network_lines(tmp_path, proxies=True), with_nft=False
    )
    assert done.returncode != 0
    assert "apt install nftables" in done.stderr
    assert _service_calls(calls) == []


def test_install_sh_on_a_network_listen_without_proxy_addresses_stops_before_any_nft_or_systemctl(
    tmp_path,
):
    done, calls, _ = _run_install_sh(
        tmp_path, "0.0.0.0:9090", _network_lines(tmp_path, proxies=False), with_nft=True
    )
    assert done.returncode != 0
    assert "proxy_addresses" in done.stderr
    assert _service_calls(calls) == []


def test_install_sh_on_a_network_listen_loads_the_firewall_before_it_enables_anything(
    tmp_path,
):
    done, calls, root = _run_install_sh(
        tmp_path, "0.0.0.0:9090", _network_lines(tmp_path, proxies=True), with_nft=True
    )
    assert done.returncode == 0, done.stderr
    conf = f"{root}/etc/nftables.d/earshot.conf"
    assert _service_calls(calls) == [
        f"nft -c -f {conf}",
        f"nft -f {conf}",
        "systemctl enable nftables",
        "systemctl daemon-reload",
        "systemctl enable earshotd.socket earshotd",
        "systemctl stop earshotd",
        "systemctl restart earshotd.socket",
        "systemctl start earshotd",
    ]
    assert "ip saddr 192.0.2.10 tcp dport 9090 accept" in Path(conf).read_text()


def test_install_sh_stops_before_any_systemctl_when_a_tls_file_will_not_load(tmp_path):
    lines = _network_lines(tmp_path, proxies=True)
    missing = tuple(ln.replace("proxy.crt", "absent.crt") for ln in lines)
    done, calls, _ = _run_install_sh(tmp_path, "0.0.0.0:9090", missing, with_nft=True)
    assert done.returncode != 0
    assert "absent.crt" in done.stderr
    assert _service_calls(calls) == []


def _nft_check(tmp_path: Path, listen: str | None, extra: tuple[str, ...]):
    """verify.sh sourced, its nft check run against a config with nft's table listing failing."""
    cfg = _config(tmp_path, listen, extra)
    bindir, log = _stubs(tmp_path, (), ())
    nft = bindir / "nft"
    nft.write_text(
        f"#!{BASH}\nprintf '%s\\n' \"nft $*\" >> {shlex.quote(str(log))}\nexit 1\n"
    )
    nft.chmod(0o755)
    assert BASH
    done = subprocess.run(
        [
            BASH,
            "-c",
            'source "$1" && nft_check "$2"',
            "bash",
            str(DEPLOY / "verify.sh"),
            str(cfg),
        ],
        env={
            **os.environ,
            "EARSHOT_PY": sys.executable,
            "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
        },
        capture_output=True,
        text=True,
        check=False,
    )
    return done, log.read_text()


def test_verify_local_needs_no_nft_table_on_a_loopback_listen(tmp_path):
    done, calls = _nft_check(tmp_path, None, ())
    assert done.returncode == 0, done.stderr
    assert "ok" in done.stdout and "no firewall needed" in done.stdout
    assert "FAIL" not in done.stdout and calls == ""


def test_verify_local_fails_a_network_listen_whose_nft_table_is_missing(tmp_path):
    done, calls = _nft_check(tmp_path, "0.0.0.0:9090", (*TLS_NETWORK, PROXIES))
    assert "FAIL nft table missing" in done.stdout
    assert "list table inet earshot" in calls
