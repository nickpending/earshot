"""deploy/proxy-cert.sh run alone, as it runs on a proxy host with no checkout beside it."""

import os
import re
import shlex
import shutil
import stat
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "deploy" / "proxy-cert.sh"
BASH = shutil.which("bash")
OPENSSL = shutil.which("openssl")

pytestmark = pytest.mark.skipif(
    BASH is None or OPENSSL is None, reason="needs bash and openssl"
)


def openssl_out(*args: str) -> str:
    assert OPENSSL
    done = subprocess.run([OPENSSL, *args], capture_output=True, text=True, check=True)
    return done.stdout


def fingerprint(crt: Path) -> str:
    """The line `openssl x509 -fingerprint -sha256` prints for the certificate."""
    return openssl_out(
        "x509", "-in", str(crt), "-noout", "-fingerprint", "-sha256"
    ).strip()


def cert_facts(crt: Path) -> dict:
    """What a test asserts about a generated certificate, read back through openssl."""
    text = openssl_out("x509", "-in", str(crt), "-noout", "-text")
    when = {}
    for name, flag in (("notBefore", "-startdate"), ("notAfter", "-enddate")):
        line = openssl_out("x509", "-in", str(crt), "-noout", flag).strip()
        when[name] = datetime.strptime(
            line.split("=", 1)[1].replace("GMT", "+0000"), "%b %d %H:%M:%S %Y %z"
        )
    subject = openssl_out("x509", "-in", str(crt), "-noout", "-subject").strip()
    issuer = openssl_out("x509", "-in", str(crt), "-noout", "-issuer").strip()
    return {
        "text": text,
        "subject": subject.split("=", 1)[1].replace(" ", ""),
        "self_signed": subject.split("=", 1)[1] == issuer.split("=", 1)[1],
        "days": (when["notAfter"] - when["notBefore"]) / timedelta(days=1),
        "san": extension_line(text, "Subject Alternative Name"),
        "ca_critical_false": bool(
            re.search(r"Basic Constraints: critical\s*\n\s*CA:FALSE", text)
        ),
        "eku": extension_line(text, "Extended Key Usage"),
    }


def extension_line(text: str, name: str) -> str:
    """The value line under `name` in `openssl x509 -text` output."""
    found = re.search(rf"{name}:.*\n\s*(.+)", text)
    assert found, f"no {name} in the certificate"
    return found.group(1).strip()


def mode_of(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def stub_chown(tmp_path: Path) -> tuple[dict, Path]:
    """An env whose chown only appends its argv to a log (the test user is not root)."""
    assert BASH
    bindir = tmp_path / "chownbin"
    bindir.mkdir(exist_ok=True)
    log = tmp_path / "chown.log"
    log.touch()
    chown = bindir / "chown"
    chown.write_text(f"#!{BASH}\nprintf '%s\\n' \"$*\" >> {shlex.quote(str(log))}\n")
    chown.chmod(0o755)
    return {**os.environ, "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}"}, log


def run_proxy_cert(
    tmp_path: Path, out: Path, group: str | None = None
) -> tuple[subprocess.CompletedProcess, Path]:
    """Copy the script alone into an empty directory and run it there against <out>."""
    assert BASH
    alone = tmp_path / "alone"
    alone.mkdir(exist_ok=True)
    shutil.copy(SCRIPT, alone / "proxy-cert.sh")
    env, log = stub_chown(tmp_path)
    args = [BASH, "proxy-cert.sh", str(out), *([group] if group else [])]
    done = subprocess.run(
        args, cwd=alone, env=env, capture_output=True, text=True, check=False
    )
    return done, log


def make_proxy_cert(tmp_path: Path, name: str) -> Path:
    """A fresh proxy key and certificate in tmp_path/<name>; returns the directory."""
    out = tmp_path / name
    done, _ = run_proxy_cert(tmp_path, out)
    assert done.returncode == 0, done.stderr
    return out


def test_a_proxy_key_and_certificate_are_generated_by_the_script_alone(tmp_path):
    os.umask(0o022)
    out = tmp_path / "first"
    done, _ = run_proxy_cert(tmp_path, out)
    assert done.returncode == 0, done.stderr
    key, crt = out / "proxy.key", out / "proxy.crt"
    assert mode_of(key) == 0o600 and mode_of(crt) == 0o644
    assert mode_of(out) == 0o750
    key_text = openssl_out("pkey", "-in", str(key), "-noout", "-text")
    assert "prime256v1" in key_text or "P-256" in key_text
    facts = cert_facts(crt)
    assert facts["subject"] == "CN=earshot-proxy"
    assert facts["self_signed"]
    assert facts["san"] == "DNS:earshot-proxy"
    assert facts["ca_critical_false"]
    assert facts["eku"] == "TLS Web Client Authentication"
    assert abs(facts["days"] - 3650) <= 1
    assert str(crt) in done.stdout
    assert fingerprint(crt) in done.stdout


def test_a_group_makes_the_key_readable_by_it_and_a_repeat_changes_nothing(tmp_path):
    os.umask(0o022)
    first = make_proxy_cert(tmp_path, "first")
    before = {n: (first / n).read_bytes() for n in ("proxy.key", "proxy.crt")}
    grouped = tmp_path / "grouped"
    done, log = run_proxy_cert(tmp_path, grouped, "caddy")
    assert done.returncode == 0, done.stderr
    assert f"root:caddy {grouped / 'proxy.key'}" in log.read_text()
    assert mode_of(grouped / "proxy.key") == 0o640
    assert mode_of(grouped / "proxy.crt") == 0o644
    # the group cannot read the first directory's key; a rerun there must not regenerate it
    again, _ = run_proxy_cert(tmp_path, first)
    assert again.returncode == 0, again.stderr
    assert {n: (first / n).read_bytes() for n in before} == before
    assert mode_of(first / "proxy.key") == 0o600
    assert fingerprint(first / "proxy.crt") in again.stdout


def test_a_certificate_without_its_key_is_not_silently_replaced(tmp_path):
    out = make_proxy_cert(tmp_path, "first")
    (out / "proxy.crt").unlink()
    done, _ = run_proxy_cert(tmp_path, out)
    assert done.returncode != 0
    assert "proxy.crt" in done.stderr
