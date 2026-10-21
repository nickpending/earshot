from conftest import RD, UP, up
from test_upload_v2 import CT2, ORDER, v2_body


def rd(token=RD):
    return {"Authorization": f"Bearer {token}"}


def seed(client):
    client.post("/v1/pages", content=v2_body(*ORDER), headers=up(ctype=CT2))


def test_battery_in_range_ordered(client):
    seed(client)
    r = client.get("/v1/pendant/records?kind=battery", headers=rd())
    assert r.status_code == 200
    bat = r.json()
    assert len(bat) == 5
    assert [x["ts_ms"] for x in bat] == sorted(x["ts_ms"] for x in bat)
    assert set(bat[0]) == {"kind", "ts_ms", "session", "page_index", "data"}
    assert bat[0]["data"] == {"mv": 3898, "ma": -13, "temp_c": 25, "pct": 69}
    lo, hi = bat[1]["ts_ms"], bat[3]["ts_ms"]
    r = client.get(
        f"/v1/pendant/records?kind=battery&from={lo}&to={hi}&limit=2", headers=rd()
    )
    assert [x["ts_ms"] for x in r.json()] == [x["ts_ms"] for x in bat[1:3]]


def test_kind_filter(client):
    seed(client)
    everything = client.get("/v1/pendant/records", headers=rd()).json()
    assert {"battery", "log", "marker"} <= {x["kind"] for x in everything}
    marks = client.get("/v1/pendant/records?kind=marker", headers=rd()).json()
    assert marks and {x["kind"] for x in marks} == {"marker"}


def test_scopes(client):
    assert client.get("/v1/pendant/records", headers=rd(UP)).status_code == 403
    assert client.get("/v1/pendant/records").status_code == 401


def test_untimed_crc_failed_page_listed_without_a_range(client):
    seed(client)
    crc = client.get("/v1/pendant/records?kind=crc_failed", headers=rd()).json()
    assert [(x["page_index"], x["ts_ms"]) for x in crc] == [(49052, None)]
    ranged = client.get(
        "/v1/pendant/records?kind=crc_failed&from=0&to=9999999999999", headers=rd()
    ).json()
    assert ranged == []
