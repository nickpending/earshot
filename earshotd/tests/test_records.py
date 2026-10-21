import json
from pathlib import Path

import pytest

from earshotd import records

FIX = Path(__file__).resolve().parents[2] / "fixtures" / "pendant-pages"


def raw(n: int) -> bytes:
    return bytes.fromhex((FIX / f"{n}.storagebuffer.hex").read_text().strip())


def kinds(n: int) -> set[str]:
    return {r.kind for r in records.decode(raw(n)).records}


def test_audio_pages_carry_their_frames():
    d = records.decode(raw(3937))
    assert (d.session, d.page_index) == (994, 3937)
    assert d.audio is not None and d.audio.frame_count == 72
    assert d.audio.page_ts_ms == 1790911718910
    assert records.decode(raw(3936)).audio.frame_count == 1
    assert records.decode(raw(49123)).audio.frame_count == 72


def test_frames_are_u16_length_prefixed():
    a = records.decode(raw(3936)).audio
    n = int.from_bytes(a.frames[:2], "little")
    assert len(a.frames) == 2 + n


@pytest.mark.parametrize("n", [3935, 4891, 49052])
def test_pages_without_opus_have_no_audio(n):
    assert records.decode(raw(n)).audio is None


def test_log_record_is_split_and_uncoloured():
    log = next(r for r in records.decode(raw(3936)).records if r.kind == "log")
    assert log.data == {
        "level": "inf",
        "module": "storage",
        "text": "Session num:993 started with err:0",
    }
    assert log.ts_ms == 1790911683828


def test_battery_is_signed():
    bat = [r for r in records.decode(raw(4891)).records if r.kind == "battery"]
    assert bat[0].data == {"mv": 3898, "ma": -13, "temp_c": 25, "pct": 69}


def test_motion_and_thread_stats():
    d = records.decode(raw(3937))
    m = next(r for r in d.records if r.kind == "motion")
    assert set(m.data) == {"x", "y", "z"} and 0.9 < m.data["z"] < 1.1
    t = next(r for r in records.decode(raw(3936)).records if r.kind == "thread_stats")
    assert t.data["threads"][1] == {
        "name": "BT RX",
        "stack_used": 1156,
        "stack_size": 2200,
        "cpu_pct": 0,
    }


def test_marker_page():
    assert "marker" in kinds(3935)


def test_crc_failed_page():
    d = records.decode(raw(49052))
    assert [r.kind for r in d.records] == ["crc_failed"]
    assert (d.session, d.page_index) == (8875, 49052)


def test_unrecognised_wrapper_field_is_unknown_with_hex():
    u = [r for r in records.decode(raw(4891)).records if r.kind == "unknown"]
    assert [r.data for r in u] == [{"field": 13, "hex": "0801"}] * 5


def test_session_end_then_start_across_a_marker():
    def sessions(n: int) -> list[str]:
        return [
            r.data["event"]
            for r in records.decode(raw(n)).records
            if r.kind == "session"
        ]

    assert sessions(3935) == ["end"]
    assert sessions(3936) == ["start", "end"]
    assert sessions(3937) == ["start"]


def test_button_long_press_and_other_press():
    off = [r for r in records.decode(raw(61432)).records if r.kind == "button"]
    assert [r.data["long_press"] for r in off] == [True, True]
    assert [r.data["count"] for r in off] == [1, 2]
    assert off[0].data["epoch_ms"] == off[0].ts_ms == 1791306012590
    assert off[0].data["fields"] == {"1": 2, "2": 1, "8": 800}
    on = next(r for r in records.decode(raw(61433)).records if r.kind == "button")
    assert on.data == {
        "long_press": False,
        "count": 2,
        "epoch_ms": 1791306023044,
        "fields": {"1": 1, "2": 1, "3": 1, "7": 900},
    }


def test_usb_connect_event():
    usb = [r for r in records.decode(raw(66064)).records if r.kind == "usb"]
    assert [(r.ts_ms, r.data) for r in usb] == [(1791326794481, {"event": 1})]


def test_no_page_index_is_an_error():
    with pytest.raises(records.DecodeError, match="page index"):
        records.decode(bytes([0x10, 0x01]))


def test_truncated_buffer_is_an_error():
    with pytest.raises(records.DecodeError):
        records.decode(raw(3937)[:-5])


def test_data_json_roundtrips():
    r = records.decode(raw(4891)).records[0]
    assert json.loads(r.data_json()) == r.data


def test_named_record_of_unexpected_shape_is_kept_as_unknown():
    # wrapper {1: 0, 6: {2: "x"}}: battery whose millivolts is bytes, not a number
    wrapper = bytes([0x08, 0x00, 0x32, 0x03, 0x12, 0x01, 0x78])
    page = bytes([0x08, 0x01, 0x1A, len(wrapper)]) + wrapper
    sb = bytes([0x10, 0x07, 0x28, 0x2A, 0x32, len(page)]) + page
    d = records.decode(sb)
    assert [(r.kind, r.data) for r in d.records] == [
        ("unknown", {"field": 6, "hex": "120178"})
    ]
