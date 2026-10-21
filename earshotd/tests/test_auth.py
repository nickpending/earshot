import pytest
from conftest import RD, UP, page_count, up


@pytest.mark.parametrize("token", [None, "nope" * 16])
def test_missing_or_unknown_token_401(client, config, body, token):
    r = client.post("/v1/pages", content=body, headers=up(token=token))
    assert r.status_code == 401
    assert page_count(config) == 0


def test_read_token_on_upload_403(client, config, body):
    r = client.post("/v1/pages", content=body, headers=up(token=RD))
    assert r.status_code == 403
    assert page_count(config) == 0


def test_non_bearer_scheme_401(client, config, body):
    h = up(token=None)
    h["Authorization"] = f"Basic {UP}"
    assert client.post("/v1/pages", content=body, headers=h).status_code == 401
    assert page_count(config) == 0


@pytest.mark.parametrize("cut", [5, 17, 19, 21, -1, -100])
def test_truncated_body_400_writes_nothing(client, config, body, cut):
    r = client.post("/v1/pages", content=body[:cut], headers=up())
    assert r.status_code == 400
    assert "error" in r.json()
    assert page_count(config) == 0
