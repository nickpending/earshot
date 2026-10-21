import re

import pytest
from fastapi.routing import APIRoute

from earshotd.app import create_app


def _routes(config):
    for route in create_app(config).routes:
        if isinstance(route, APIRoute):
            for method in route.methods:
                yield method, re.sub(r"\{[^}]+\}", "2026-10-01", route.path)


def test_every_route_with_a_trailing_slash_answers_without_a_redirect(client, config):
    # #16: behind the TLS proxy a slash redirect pointed the client at plain http, so a client
    # that followed it re-sent its audio and token in the clear. No path may answer with one.
    routes = list(_routes(config))
    assert routes
    for method, path in routes:
        r = client.request(method, path + "/", follow_redirects=False)
        assert not 300 <= r.status_code < 400, (
            f"{method} {path}/ answered {r.status_code}"
        )


@pytest.mark.parametrize(
    "method,path", [("POST", "/v1/pages/"), ("GET", "/v1/health/")]
)
def test_a_trailing_slash_is_a_plain_404(client, method, path):
    r = client.request(method, path, follow_redirects=False)
    assert r.status_code == 404
    assert "location" not in r.headers
