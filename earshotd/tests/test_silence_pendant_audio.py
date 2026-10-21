"""scripts/silence-pendant-audio.py: the rewrite of one StorageBuffer and the script's refusals."""

import importlib.util
from pathlib import Path

from earshotd import records

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "silence-pendant-audio.py"
SILENT = bytes.fromhex("b8" + "ff" * 40)


def load_script():
    spec = importlib.util.spec_from_file_location("silence_under_test", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def varint(v: int) -> bytes:
    out = bytearray()
    while v > 0x7F:
        out.append(v & 0x7F | 0x80)
        v >>= 7
    out.append(v)
    return bytes(out)


def ld(num: int, payload: bytes) -> bytes:
    return varint(num << 3 | 2) + varint(len(payload)) + payload


def vi(num: int, value: int) -> bytes:
    return varint(num << 3) + varint(value)


FRAME_47 = b"\xb8" + bytes(range(1, 47))
FRAME_12 = b"\xb8" + bytes(range(100, 111))
NOT_A_FRAME = b"\xb8" + bytes(range(50, 58))
LOG = ld(10, ld(1, b"[00:00:01.000,000] <inf> main: hello"))


def storage_buffer(*wrappers: bytes) -> bytes:
    flash = vi(1, 1000) + b"".join(ld(3, w) for w in wrappers)
    return vi(2, 994) + vi(5, 7) + ld(6, flash)


def audio_wrapper(first: bytes, second: bytes) -> bytes:
    return ld(2, ld(1, first) + ld(1, second) + ld(2, NOT_A_FRAME))


def test_both_frames_become_the_silent_frame_and_the_length_prefixes_grow():
    original = storage_buffer(LOG, audio_wrapper(FRAME_47, FRAME_12))
    # inner 2 x (2 + 41) + 11 = 97 = 0x61; wrapper payload 99 = 0x63; flash payload 3 + 2 + 40 + 2 + 99.
    inner = bytes.fromhex("0a29" + "b8" + "ff" * 40) * 2 + b"\x12\x09" + NOT_A_FRAME
    wrapper = bytes.fromhex("1a63" + "1261") + inner
    log_wrapper = b"\x1a\x28" + LOG
    flash_payload = bytes.fromhex("08e807") + log_wrapper + wrapper
    expected = (
        bytes.fromhex("10e207" + "2807") + b"\x32" + varint(len(flash_payload)) + flash_payload
    )
    assert len(LOG) == 40
    out = load_script().rewrite(original)
    assert out == expected
    assert load_script().rewrite(out) == out
    d = records.decode(out)
    assert (d.session, d.page_index, d.audio.page_ts_ms, d.audio.frame_count) == (994, 7, 1000, 2)


def test_a_page_without_frames_is_returned_byte_identical():
    plain = storage_buffer(LOG, ld(2, NOT_A_FRAME))
    assert load_script().rewrite(plain) == plain


def write_page(directory: Path, raw: bytes) -> Path:
    path = directory / "7.storagebuffer.hex"
    path.write_text(raw.hex() + "\n")
    return path


def test_main_rewrites_a_page_and_check_then_passes(tmp_path):
    original = storage_buffer(LOG, audio_wrapper(FRAME_47, FRAME_12))
    path = write_page(tmp_path, original)
    script = load_script()
    assert script.main([str(tmp_path), "--check"]) == 1
    assert path.read_text() == original.hex() + "\n"
    assert script.main([str(tmp_path)]) == 0
    assert path.read_text() == script.rewrite(original).hex() + "\n"
    assert script.main([str(tmp_path), "--check"]) == 0


def test_a_rewrite_that_changes_a_record_exits_nonzero_and_leaves_the_file(
    tmp_path, monkeypatch, capsys
):
    original = storage_buffer(LOG, audio_wrapper(FRAME_47, FRAME_12))
    path = write_page(tmp_path, original)
    before = path.read_bytes()
    script = load_script()
    dropping = storage_buffer(audio_wrapper(SILENT, SILENT))
    monkeypatch.setattr(script, "rewrite", lambda raw: dropping)
    assert script.main([str(tmp_path)]) != 0
    assert path.read_bytes() == before
    err = capsys.readouterr().err
    assert path.name in err and "record list" in err
