"""The Limitless client: explicit User-Agent, cursor paging, retry with backoff, loud failure."""

import pytest
from fake_limitless import KEY, lifelog, standard_archive

from earshotd import limitless


def client(fake, key=KEY, sleeps=None, **kw):
    sleeps = [] if sleeps is None else sleeps
    return limitless.Client(key, fake.base_url, sleep=sleeps.append, **kw)


def test_pages_through_every_lifelog_by_cursor(make_fake):
    fake = make_fake(standard_archive(extra=22))  # 24 lifelogs: 3 pages of at most 10
    pages = list(client(fake).lifelog_pages())
    assert [len(p) for p in pages] == [10, 10, 4]
    ids = [x["id"] for p in pages for x in p]
    assert len(ids) == len(set(ids)) == 24
    queries = [q for path, q in fake.requests if path == "/v1/lifelogs"]
    assert all(q["limit"] == "10" for q in queries)
    assert all(
        q[k] == "true"
        for q in queries
        for k in ("includeMarkdown", "includeHeadings", "includeContents")
    )


def test_sends_an_explicit_user_agent_and_the_key(make_fake):
    fake = make_fake(standard_archive(extra=0))
    list(client(fake).lifelog_pages())
    assert fake.user_agents and all(
        ua and not ua.startswith("Python-urllib") for ua in fake.user_agents
    )
    assert set(fake.keys) == {KEY}


def test_a_403_fails_loudly_and_is_never_an_empty_archive(make_fake):
    fake = make_fake(standard_archive())
    sleeps: list[float] = []
    # What Cloudflare does to the default urllib agent: reproduce it by sending that agent.
    c = client(fake, sleeps=sleeps)
    c.user_agent = "Python-urllib/3.12"
    with pytest.raises(limitless.LimitlessError, match="403"):
        list(c.lifelog_pages())
    assert sleeps == []  # a 403 is not retried


def test_a_bad_key_fails_loudly(make_fake):
    fake = make_fake(standard_archive())
    with pytest.raises(limitless.LimitlessError, match="401"):
        list(client(fake, key="wrong").lifelog_pages())


def test_429_and_5xx_are_retried_with_growing_backoff(make_fake):
    fake = make_fake(standard_archive(extra=0))
    fake.list_faults = [429, 503, 500]
    sleeps: list[float] = []
    pages = list(client(fake, sleeps=sleeps).lifelog_pages())
    assert sum(len(p) for p in pages) == 2
    assert len(sleeps) == 3 and sleeps == sorted(sleeps) and sleeps[0] < sleeps[-1]


def test_gives_up_after_the_attempts_and_says_what_failed(make_fake):
    fake = make_fake(standard_archive(extra=0))
    fake.list_faults = [503] * 50
    sleeps: list[float] = []
    with pytest.raises(limitless.LimitlessError, match="503"):
        list(client(fake, sleeps=sleeps, attempts=4).lifelog_pages())
    assert len(sleeps) == 3
    assert len([1 for p, _ in fake.requests if p == "/v1/lifelogs"]) == 4


def test_download_audio_returns_the_ogg_bytes_and_refuses_over_two_hours(make_fake):
    fake = make_fake([lifelog(0, 1_000, 2_000)])
    c = client(fake)
    data = c.download_audio(0, 60_000)
    assert data[:4] == b"OggS"
    with pytest.raises(limitless.LimitlessError, match="400"):
        c.download_audio(0, 2 * 3_600_000 + 1)
    assert fake.audio_requests()[0] == (0, 60_000)


def test_a_404_with_the_no_audio_body_raises_no_audio_and_is_not_retried(make_fake):
    fake = make_fake([lifelog(0, 1_000, 2_000)])
    fake.no_audio_at.add(0)
    sleeps: list[float] = []
    with pytest.raises(limitless.NoAudio) as e:
        client(fake, sleeps=sleeps).download_audio(0, 60_000)
    assert e.value.text == "No audio available for the requested time range"
    assert isinstance(e.value, limitless.LimitlessError)
    assert sleeps == [] and len(fake.audio_requests()) == 1


@pytest.mark.parametrize(
    "status, body",
    [
        (404, {"error": "not found"}),
        (404, {"error": "No audio available for the requested time range."}),
        (404, {"message": "No audio available for the requested time range"}),
        (400, {"error": "No audio available for the requested time range"}),
        (401, {"error": "No audio available for the requested time range"}),
        (403, {"error": "No audio available for the requested time range"}),
    ],
)
def test_any_other_failure_is_a_plain_limitless_error(make_fake, status, body):
    fake = make_fake([lifelog(0, 1_000, 2_000)])
    fake.audio_errors[0] = (status, body)
    with pytest.raises(limitless.LimitlessError, match=str(status)) as e:
        client(fake).download_audio(0, 60_000)
    assert not isinstance(e.value, limitless.NoAudio)


def test_a_malformed_listing_is_an_error_not_an_empty_archive(make_fake):
    fake = make_fake([])
    c = client(fake)
    assert list(c.lifelog_pages()) == [
        []
    ]  # a genuinely empty archive is one empty page

    c._get = lambda *_a, **_k: b'{"data": {}}'  # type: ignore[method-assign]
    with pytest.raises(limitless.LimitlessError, match="lifelogs"):
        list(c.lifelog_pages())


def test_a_repeating_cursor_is_an_error(make_fake):
    fake = make_fake([])
    c = client(fake)
    c._get = lambda *_a, **_k: (  # type: ignore[method-assign]
        b'{"data": {"lifelogs": []}, "meta": {"lifelogs": {"nextCursor": "same"}}}'
    )
    with pytest.raises(limitless.LimitlessError, match="cursor"):
        list(c.lifelog_pages())


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_c1_a_redirect_fails_the_call_and_the_key_reaches_nobody_else(
    make_fake, status
):
    a = make_fake([lifelog(0, 1_000, 2_000)])
    b = make_fake([lifelog(1, 1_000, 2_000)])
    a.redirect = (status, b.base_url + "/v1/lifelogs")
    sleeps: list[float] = []
    c = client(a, sleeps=sleeps)
    with pytest.raises(limitless.LimitlessError, match=f"/v1/lifelogs: HTTP {status}"):
        list(c.lifelog_pages())
    assert len(a.requests) == 1
    with pytest.raises(
        limitless.LimitlessError, match=f"/v1/download-audio: HTTP {status}"
    ):
        c.download_audio(0, 60_000)
    assert len(a.requests) == 2  # one request per call, never retried
    assert sleeps == []
    assert b.requests == [] and b.keys == []


@pytest.mark.parametrize(
    "base",
    [
        "http://example.com",
        "http://127.0.0.2:8080",
        "ftp://127.0.0.1",
        "https:///v1",
        "http://user@127.0.0.1@evil.com/",
        "http://127.0.0.1:80@evil.com/",
        "https://u:secretpw@api.limitless.ai",
        "http://127.0.0.1:8080/?a=b",
        "http://127.0.0.1:8080/#f",
    ],
)
def test_c2_a_base_the_key_must_not_go_to_is_refused_at_construction(base):
    with pytest.raises(limitless.LimitlessError) as e:
        limitless.Client(KEY, base)
    message = str(e.value)
    assert KEY not in message and "secretpw" not in message and "user@" not in message
    if "@" in base or "?" in base or "#" in base:
        assert "no userinfo, query or fragment" in message
    else:
        assert "must be https" in message


@pytest.mark.parametrize(
    "base",
    [
        "https://api.limitless.ai",
        "https://example.com",
        "http://127.0.0.1:8080",
        "http://localhost:8080",
        "http://[::1]:8080",
    ],
)
def test_c2_an_https_or_loopback_base_constructs(base):
    limitless.Client(KEY, base)
