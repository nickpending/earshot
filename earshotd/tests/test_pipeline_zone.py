"""C6: a window's day speakers are keyed by the day in the configured zone."""

import sqlite3
from zoneinfo import ZoneInfo

import pytest
from conftest import RD, T0
from fastapi.testclient import TestClient
from test_windowing import FakeModels, shifted_fixture

from earshotd import store
from earshotd.app import create_app
from earshotd.config import Config
from earshotd.pipeline import Pipeline

NEVER = 2**62  # a clock far enough on that the window is settled


@pytest.mark.parametrize(
    ("zone", "day"),
    [("Asia/Tokyo", "2026-10-02"), ("America/Los_Angeles", "2026-10-01")],
)
def test_c6_a_committed_window_is_keyed_and_listed_under_the_day_in_the_configured_zone(
    tmp_path, zone, day
):
    config = Config(tokens={RD: "read"}, data_dir=tmp_path, zone=ZoneInfo(zone))
    store.init(config.db_path)
    store.store_pages(config.db_path, shifted_fixture(0, 3 * 60_000, T0))
    assert Pipeline(config, FakeModels()).run_once(NEVER) is not None

    conn = sqlite3.connect(config.db_path)
    try:
        assert {r[0] for r in conn.execute("SELECT date FROM day_speakers")} == {day}
    finally:
        conn.close()
    r = TestClient(create_app(config)).get(
        "/v1/days", headers={"Authorization": f"Bearer {RD}"}
    )
    assert [d["date"] for d in r.json()] == [day]
