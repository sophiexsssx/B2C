"""
Tests for wiki_interest.api_client.

All HTTP calls are mocked via the `requests_mock` pytest fixture (provided
by the `requests-mock` package) -- no real network calls are made. Env vars
are set with monkeypatch, never mutated directly.
"""

import calendar
import urllib.parse

import pytest
import requests

from wiki_interest import api_client
from wiki_interest.api_client import (
    PAGEVIEWS_BASE,
    WIKIDATA_API,
    AmbiguousNoDataError,
    ContactNotConfiguredError,
    OutOfRangeError,
    build_session,
    get_langlinks,
    get_pageviews_aggregate,
    get_pageviews_per_article,
    get_redirects,
    get_wikidata_sitelinks,
    resolve_cross_language,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mediawiki_url(project: str) -> str:
    return f"https://{project}.org/w/api.php"


def _start_ts(yyyymm: str) -> str:
    return f"{yyyymm.replace('-', '')}0100"


def _end_ts(yyyymm: str) -> str:
    # End-of-range must use the LAST day of the month, not the first -- see
    # test_per_article_end_month_uses_last_day_of_month for why.
    year, month = (int(p) for p in yyyymm.split("-"))
    last_day = calendar.monthrange(year, month)[1]
    return f"{year:04d}{month:02d}{last_day:02d}00"


def _per_article_url(project, article, start, end, access="all-access", agent="user"):
    encoded = urllib.parse.quote(article, safe="")
    return f"{PAGEVIEWS_BASE}/per-article/{project}/{access}/{agent}/{encoded}/monthly/{_start_ts(start)}/{_end_ts(end)}"


def _aggregate_url(project, start, end, access="all-access", agent="user"):
    return f"{PAGEVIEWS_BASE}/aggregate/{project}/{access}/{agent}/monthly/{_start_ts(start)}/{_end_ts(end)}"


@pytest.fixture
def session():
    # build_session() requires WIKITREND_CONTACT; the plain functions under
    # test just take a requests.Session, so a bare one is fine here.
    return requests.Session()


# ---------------------------------------------------------------------------
# 1. build_session
# ---------------------------------------------------------------------------


def test_build_session_raises_without_contact(monkeypatch):
    monkeypatch.delenv("WIKITREND_CONTACT", raising=False)
    with pytest.raises(ContactNotConfiguredError):
        build_session()


def test_build_session_sets_user_agent_with_contact(monkeypatch):
    monkeypatch.setenv("WIKITREND_CONTACT", "https://example.org/wiki-interest; sofia@example.org")
    session = build_session()
    assert "https://example.org/wiki-interest; sofia@example.org" in session.headers["User-Agent"]


def test_bare_session_gets_compliant_user_agent_enforced(requests_mock, session, monkeypatch):
    # `session` here is a plain requests.Session() (see the fixture below),
    # never passed through build_session() -- and requests.Session() already
    # carries its own default User-Agent ("python-requests/x.y.z"), which is
    # exactly the non-descriptive default Wikimedia may block. The shared
    # request boundary must overwrite it regardless of who built the session.
    monkeypatch.setenv("WIKITREND_CONTACT", "https://example.org/wiki-interest; sofia@example.org")
    assert "python-requests" in session.headers["User-Agent"]  # sanity: confirms the default is really there

    url = _mediawiki_url("en.wikipedia")
    requests_mock.get(url, json={"query": {"pages": {"1": {"title": "Python", "langlinks": []}}}})

    get_langlinks(session, "en.wikipedia", "Python")

    sent_ua = requests_mock.last_request.headers["User-Agent"]
    assert "python-requests" not in sent_ua
    assert "https://example.org/wiki-interest; sofia@example.org" in sent_ua


def test_bare_session_without_contact_raises_even_without_build_session(requests_mock, session, monkeypatch):
    # Confirms the enforcement doesn't depend on the caller having gone
    # through build_session() at all -- any entry point must raise.
    monkeypatch.delenv("WIKITREND_CONTACT", raising=False)
    with pytest.raises(ContactNotConfiguredError):
        get_langlinks(session, "en.wikipedia", "Python")
    assert requests_mock.call_count == 0  # must fail before ever making the request


# ---------------------------------------------------------------------------
# 2. get_pageviews_per_article / get_pageviews_aggregate
# ---------------------------------------------------------------------------


def test_per_article_fills_missing_months_with_zero(requests_mock, session):
    url = _per_article_url("en.wikipedia", "Astronomy", "2024-01", "2024-03")
    requests_mock.get(
        url,
        json={
            "items": [
                {"timestamp": "2024010100", "views": 100},
                {"timestamp": "2024030100", "views": 300},
            ]
        },
    )
    result = get_pageviews_per_article(session, "en.wikipedia", "Astronomy", "2024-01", "2024-03")
    assert result == [
        {"month": "2024-01", "views": 100},
        {"month": "2024-02", "views": 0},
        {"month": "2024-03", "views": 300},
    ]


def test_aggregate_fills_missing_months_with_zero(requests_mock, session):
    url = _aggregate_url("en.wikipedia", "2024-01", "2024-03")
    requests_mock.get(
        url,
        json={
            "items": [
                {"timestamp": "2024010100", "views": 1000},
            ]
        },
    )
    result = get_pageviews_aggregate(session, "en.wikipedia", "2024-01", "2024-03")
    assert result == [
        {"month": "2024-01", "views": 1000},
        {"month": "2024-02", "views": 0},
        {"month": "2024-03", "views": 0},
    ]


def test_per_article_end_month_uses_last_day_of_month(requests_mock, session):
    # The end timestamp must be the LAST day of the end month, not the first --
    # using day 01 silently truncates the whole end month to a near-empty
    # partial sum instead of its full total. Verified against the live API:
    # end="...2025120100" (day 01) returned 20 views for uk.wikipedia
    # "Астрономія" in December 2025, vs. 622 with end="...2025123100" (day 31).
    url = _per_article_url("en.wikipedia", "Astronomy", "2024-01", "2024-12")
    assert url.endswith("/2024010100/2024123100")
    requests_mock.get(url, json={"items": [{"timestamp": "2024123100", "views": 622}]})

    result = get_pageviews_per_article(session, "en.wikipedia", "Astronomy", "2024-01", "2024-12")

    assert result[-1] == {"month": "2024-12", "views": 622}


def test_aggregate_end_month_uses_last_day_of_month(requests_mock, session):
    # 2024 is a leap year -- February's last day is the 29th, not the 28th,
    # confirming this uses calendar.monthrange rather than a fixed day count.
    url = _aggregate_url("en.wikipedia", "2024-02", "2024-02")
    assert url.endswith("/2024020100/2024022900")
    requests_mock.get(url, json={"items": [{"timestamp": "2024020100", "views": 42}]})

    result = get_pageviews_aggregate(session, "en.wikipedia", "2024-02", "2024-02")

    assert result == [{"month": "2024-02", "views": 42}]


def test_per_article_empty_items_produces_full_zero_series(requests_mock, session):
    url = _per_article_url("en.wikipedia", "Astronomy", "2024-01", "2024-03")
    requests_mock.get(url, json={"items": []})
    result = get_pageviews_per_article(session, "en.wikipedia", "Astronomy", "2024-01", "2024-03")
    assert result == [
        {"month": "2024-01", "views": 0},
        {"month": "2024-02", "views": 0},
        {"month": "2024-03", "views": 0},
    ]


def test_aggregate_empty_items_produces_full_zero_series(requests_mock, session):
    url = _aggregate_url("en.wikipedia", "2024-01", "2024-02")
    requests_mock.get(url, json={"items": []})
    result = get_pageviews_aggregate(session, "en.wikipedia", "2024-01", "2024-02")
    assert result == [
        {"month": "2024-01", "views": 0},
        {"month": "2024-02", "views": 0},
    ]


def test_per_article_in_range_404_raises_ambiguous_no_data(requests_mock, session):
    url = _per_article_url("en.wikipedia", "XyzAbcDef12345", "2020-01", "2020-02")
    requests_mock.get(url, status_code=404, json={"detail": "not found", "status": 404})
    with pytest.raises(AmbiguousNoDataError):
        get_pageviews_per_article(session, "en.wikipedia", "XyzAbcDef12345", "2020-01", "2020-02")


def test_aggregate_in_range_404_raises_ambiguous_no_data(requests_mock, session):
    url = _aggregate_url("zz.wikipedia", "2020-01", "2020-02")
    requests_mock.get(url, status_code=404, json={"detail": "not found", "status": 404})
    with pytest.raises(AmbiguousNoDataError):
        get_pageviews_aggregate(session, "zz.wikipedia", "2020-01", "2020-02")


def test_per_article_out_of_range_raises_without_http_call(requests_mock, session):
    with pytest.raises(OutOfRangeError):
        get_pageviews_per_article(session, "en.wikipedia", "Astronomy", "2015-01", "2015-03")
    assert requests_mock.request_history == []


def test_aggregate_out_of_range_raises_without_http_call(requests_mock, session):
    with pytest.raises(OutOfRangeError):
        get_pageviews_aggregate(session, "en.wikipedia", "2010-06", "2010-12")
    assert requests_mock.request_history == []


def test_per_article_encodes_non_ascii_title(requests_mock, session):
    article = "Астрономія"
    url = _per_article_url("uk.wikipedia", article, "2024-01", "2024-01")
    requests_mock.get(url, json={"items": [{"timestamp": "2024010100", "views": 42}]})

    result = get_pageviews_per_article(session, "uk.wikipedia", article, "2024-01", "2024-01")

    assert result == [{"month": "2024-01", "views": 42}]
    assert len(requests_mock.request_history) == 1
    encoded_article = urllib.parse.quote(article, safe="")
    assert encoded_article in requests_mock.request_history[0].url
    # sanity: the encoded form actually differs from the raw title
    assert encoded_article != article


# ---------------------------------------------------------------------------
# 3. get_redirects pagination
# ---------------------------------------------------------------------------


def test_get_redirects_follows_rdcontinue_and_combines_pages(requests_mock, session):
    url = _mediawiki_url("en.wikipedia")
    first_page = {
        "query": {
            "pages": {
                "736": {
                    "title": "Astronomy",
                    "redirects": [
                        {"title": "Astronomical"},
                        {"title": "Astronomy (disambiguation)"},
                    ],
                }
            }
        },
        "continue": {"rdcontinue": "736|Astro", "continue": "||"},
    }
    second_page = {
        "query": {
            "pages": {
                "736": {
                    "title": "Astronomy",
                    "redirects": [
                        {"title": "Astrophysics (redirect)"},
                    ],
                }
            }
        }
        # no "continue" key -- pagination ends here
    }
    requests_mock.get(url, [{"json": first_page}, {"json": second_page}])

    result = get_redirects(session, "en.wikipedia", "Astronomy")

    assert sorted(result) == sorted(
        ["Astronomical", "Astronomy (disambiguation)", "Astrophysics (redirect)"]
    )
    assert requests_mock.call_count == 2
    # second request must carry the continuation token from the first response
    second_request_qs = requests_mock.request_history[1].qs
    assert second_request_qs.get("rdcontinue") == ["736|astro"] or second_request_qs.get(
        "rdcontinue"
    ) == ["736|Astro".lower()]


# ---------------------------------------------------------------------------
# 4. 429 handling (with Retry-After)
# ---------------------------------------------------------------------------


def test_get_json_retries_on_429_then_succeeds(requests_mock, session, monkeypatch):
    sleep_calls = []
    monkeypatch.setattr(api_client.time, "sleep", lambda seconds: sleep_calls.append(seconds))

    url = _mediawiki_url("en.wikipedia")
    ok_body = {"query": {"pages": {"1": {"title": "Python", "langlinks": [{"lang": "de", "*": "Python"}]}}}}
    requests_mock.get(
        url,
        [
            {"status_code": 429, "headers": {"Retry-After": "2"}, "json": {"error": "rate limited"}},
            {"status_code": 200, "json": ok_body},
        ],
    )

    result = get_langlinks(session, "en.wikipedia", "Python")

    assert result == {"de": "Python"}
    assert requests_mock.call_count == 2
    assert len(sleep_calls) == 1
    # Retry-After: 2 should be honored (plus up to 0.5s jitter)
    assert 2.0 <= sleep_calls[0] < 2.5


@pytest.mark.parametrize("bad_value", ["nan", "inf", "-inf"])
def test_get_json_non_finite_retry_after_falls_back_to_backoff(requests_mock, session, monkeypatch, bad_value):
    # float() accepts "nan"/"inf"/"-inf" without raising, and NaN's comparison
    # semantics (nan <= 0 and nan > X are both False) mean it silently defeats
    # a plain range check -- and time.sleep(nan) raises ValueError for real.
    sleep_calls = []
    monkeypatch.setattr(api_client.time, "sleep", lambda seconds: sleep_calls.append(seconds))

    url = _mediawiki_url("en.wikipedia")
    ok_body = {"query": {"pages": {"1": {"title": "Python", "langlinks": [{"lang": "de", "*": "Python"}]}}}}
    requests_mock.get(
        url,
        [
            {"status_code": 429, "headers": {"Retry-After": bad_value}, "json": {"error": "rate limited"}},
            {"status_code": 200, "json": ok_body},
        ],
    )

    get_langlinks(session, "en.wikipedia", "Python")

    assert len(sleep_calls) == 1
    assert api_client.BACKOFF_BASE_SECONDS <= sleep_calls[0] < api_client.BACKOFF_BASE_SECONDS + 0.5


def test_get_json_over_cap_retry_after_falls_back_to_backoff(requests_mock, session, monkeypatch):
    sleep_calls = []
    monkeypatch.setattr(api_client.time, "sleep", lambda seconds: sleep_calls.append(seconds))

    url = _mediawiki_url("en.wikipedia")
    ok_body = {"query": {"pages": {"1": {"title": "Python", "langlinks": [{"lang": "de", "*": "Python"}]}}}}
    huge_retry_after = str(api_client.MAX_RETRY_AFTER_SECONDS + 1)
    requests_mock.get(
        url,
        [
            {"status_code": 429, "headers": {"Retry-After": huge_retry_after}, "json": {"error": "rate limited"}},
            {"status_code": 200, "json": ok_body},
        ],
    )

    get_langlinks(session, "en.wikipedia", "Python")

    # A Retry-After beyond the cap must not be honored -- falls back to the
    # attempt-0 exponential backoff (BACKOFF_BASE_SECONDS, plus jitter), not
    # anywhere near the huge value the server sent.
    assert len(sleep_calls) == 1
    assert api_client.BACKOFF_BASE_SECONDS <= sleep_calls[0] < api_client.BACKOFF_BASE_SECONDS + 0.5


def test_get_json_past_retry_after_falls_back_to_backoff(requests_mock, session, monkeypatch):
    sleep_calls = []
    monkeypatch.setattr(api_client.time, "sleep", lambda seconds: sleep_calls.append(seconds))

    url = _mediawiki_url("en.wikipedia")
    ok_body = {"query": {"pages": {"1": {"title": "Python", "langlinks": [{"lang": "de", "*": "Python"}]}}}}
    past_date = "Wed, 21 Oct 2015 07:28:00 GMT"  # long in the past
    requests_mock.get(
        url,
        [
            {"status_code": 429, "headers": {"Retry-After": past_date}, "json": {"error": "rate limited"}},
            {"status_code": 200, "json": ok_body},
        ],
    )

    get_langlinks(session, "en.wikipedia", "Python")

    # A past Retry-After must not be honored as a ~0s immediate retry --
    # falls back to exponential backoff like any other unusable value.
    assert len(sleep_calls) == 1
    assert api_client.BACKOFF_BASE_SECONDS <= sleep_calls[0] < api_client.BACKOFF_BASE_SECONDS + 0.5


def test_get_json_naive_http_date_retry_after_treated_as_utc(requests_mock, session, monkeypatch):
    sleep_calls = []
    monkeypatch.setattr(api_client.time, "sleep", lambda seconds: sleep_calls.append(seconds))

    from datetime import datetime, timedelta, timezone

    url = _mediawiki_url("en.wikipedia")
    ok_body = {"query": {"pages": {"1": {"title": "Python", "langlinks": [{"lang": "de", "*": "Python"}]}}}}
    # No timezone marker -- must be treated as UTC, not the test runner's local time.
    naive_future = (datetime.now(timezone.utc) + timedelta(seconds=10)).strftime("%a, %d %b %Y %H:%M:%S")
    requests_mock.get(
        url,
        [
            {"status_code": 429, "headers": {"Retry-After": naive_future}, "json": {"error": "rate limited"}},
            {"status_code": 200, "json": ok_body},
        ],
    )

    get_langlinks(session, "en.wikipedia", "Python")

    assert len(sleep_calls) == 1
    # ~10s +/- jitter/test-runtime slack; if this were wrongly compared against
    # local time on a non-UTC machine it would be off by hours, not seconds.
    assert 8.0 <= sleep_calls[0] < 11.0


# ---------------------------------------------------------------------------
# 5. 5xx handling
# ---------------------------------------------------------------------------


def test_get_json_retries_on_503_then_succeeds(requests_mock, session, monkeypatch):
    sleep_calls = []
    monkeypatch.setattr(api_client.time, "sleep", lambda seconds: sleep_calls.append(seconds))

    url = _mediawiki_url("en.wikipedia")
    ok_body = {"query": {"pages": {"1": {"title": "Python", "langlinks": [{"lang": "fr", "*": "Python"}]}}}}
    requests_mock.get(
        url,
        [
            {"status_code": 503, "json": {"error": "service unavailable"}},
            {"status_code": 200, "json": ok_body},
        ],
    )

    result = get_langlinks(session, "en.wikipedia", "Python")

    assert result == {"fr": "Python"}
    assert requests_mock.call_count == 2
    assert len(sleep_calls) == 1


# ---------------------------------------------------------------------------
# 6. Retries exhausted
# ---------------------------------------------------------------------------


def test_get_json_raises_after_retries_exhausted(requests_mock, session, monkeypatch):
    sleep_calls = []
    monkeypatch.setattr(api_client.time, "sleep", lambda seconds: sleep_calls.append(seconds))

    url = _mediawiki_url("en.wikipedia")
    responses = [{"status_code": 503, "json": {"error": "down"}} for _ in range(api_client.MAX_RETRIES)]
    requests_mock.get(url, responses)

    with pytest.raises(requests.HTTPError):
        get_langlinks(session, "en.wikipedia", "Python")

    assert requests_mock.call_count == api_client.MAX_RETRIES
    # No sleep after the final failed attempt -- the result is discarded via
    # `raise last_error` immediately after, so waiting there is pure waste.
    assert len(sleep_calls) == api_client.MAX_RETRIES - 1


# ---------------------------------------------------------------------------
# 7. get_langlinks / get_wikidata_sitelinks happy paths
# ---------------------------------------------------------------------------


def test_get_langlinks_happy_path(requests_mock, session):
    url = _mediawiki_url("en.wikipedia")
    requests_mock.get(
        url,
        json={
            "query": {
                "pages": {
                    "23033": {
                        "title": "Python (programming language)",
                        "langlinks": [
                            {"lang": "cs", "*": "Python (programovací jazyk)"},
                            {"lang": "de", "*": "Python (Programmiersprache)"},
                            {"lang": "fr", "*": "Python (langage)"},
                            {"lang": "pl", "*": "Python (język programowania)"},
                        ],
                    }
                }
            }
        },
    )

    result = get_langlinks(session, "en.wikipedia", "Python (programming language)")

    assert result == {
        "cs": "Python (programovací jazyk)",
        "de": "Python (Programmiersprache)",
        "fr": "Python (langage)",
        "pl": "Python (język programowania)",
    }


def test_get_wikidata_sitelinks_happy_path(requests_mock, session):
    """
    Mirrors notes/api.md Q3's two-call shape: resolve the QID via
    pageprops on the source wiki (_get_wikidata_qid), then fetch sitelinks
    from Wikidata (wbgetentities). Both calls are mocked with requests-mock.
    """
    mediawiki_url = _mediawiki_url("en.wikipedia")
    requests_mock.get(
        mediawiki_url,
        json={
            "query": {
                "pages": {
                    "23033": {
                        "title": "Python (programming language)",
                        "pageprops": {"wikibase_item": "Q28865"},
                    }
                }
            }
        },
    )
    requests_mock.get(
        WIKIDATA_API,
        json={
            "entities": {
                "Q28865": {
                    "sitelinks": {
                        "enwiki": {"site": "enwiki", "title": "Python (programming language)"},
                        "dewiki": {"site": "dewiki", "title": "Python (Programmiersprache)"},
                        "be_x_oldwiki": {"site": "be_x_oldwiki", "title": "Пайтон"},
                        "commonswiki": {"site": "commonswiki", "title": "Category:Python"},
                    }
                }
            }
        },
    )

    result = get_wikidata_sitelinks(session, "en.wikipedia", "Python (programming language)")

    assert result == {
        "en": "Python (programming language)",
        "de": "Python (Programmiersprache)",
        "be-tarask": "Пайтон",  # site-key alias normalized
    }
    assert "commons" not in result  # non-Wikipedia sitelinks excluded
    assert requests_mock.call_count == 2  # pageprops call + wbgetentities call


def test_get_wikidata_sitelinks_no_wikibase_item_skips_second_call(requests_mock, session):
    """
    If the page has no pageprops.wikibase_item (not linked to any Wikidata
    item), get_wikidata_sitelinks must return {} without attempting the
    wbgetentities call at all.
    """
    mediawiki_url = _mediawiki_url("en.wikipedia")
    requests_mock.get(
        mediawiki_url,
        json={
            "query": {
                "pages": {
                    "999": {
                        "title": "Some Article With No Wikidata Item",
                        # no "pageprops" key -- page isn't linked to any Wikidata item
                    }
                }
            }
        },
    )
    # Deliberately do NOT register WIKIDATA_API: if get_wikidata_sitelinks
    # attempted a second call, requests_mock would raise NoMockAddress and
    # fail this test -- so a passing test proves the second call never happened.

    result = get_wikidata_sitelinks(session, "en.wikipedia", "Some Article With No Wikidata Item")

    assert result == {}
    assert requests_mock.call_count == 1


# ---------------------------------------------------------------------------
# 8. resolve_cross_language
# ---------------------------------------------------------------------------


def test_resolve_cross_language_uses_fallback_only_for_missing_langs(requests_mock, session, monkeypatch):
    mediawiki_url = _mediawiki_url("en.wikipedia")
    requests_mock.get(
        mediawiki_url,
        json={
            "query": {
                "pages": {
                    "1": {
                        "title": "Astronomy",
                        "langlinks": [
                            {"lang": "de", "*": "Astronomie"},
                            {"lang": "fr", "*": "Astronomie"},
                        ],
                    }
                }
            }
        },
    )

    fallback_calls = []

    def fake_sitelinks(sess, project, title):
        fallback_calls.append((project, title))
        return {"uk": "Астрономія"}

    monkeypatch.setattr(api_client, "get_wikidata_sitelinks", fake_sitelinks)

    found, missing = resolve_cross_language(session, "en.wikipedia", "Astronomy", ["de", "fr", "uk"])

    assert found == {"de": "Astronomie", "fr": "Astronomie", "uk": "Астрономія"}
    assert missing == []
    # fallback should have been consulted (only "uk" was missing after langlinks)
    assert fallback_calls == [("en.wikipedia", "Astronomy")]


def test_resolve_cross_language_skips_fallback_when_langlinks_covers_everything(
    requests_mock, session, monkeypatch
):
    mediawiki_url = _mediawiki_url("en.wikipedia")
    requests_mock.get(
        mediawiki_url,
        json={
            "query": {
                "pages": {
                    "1": {
                        "title": "Astronomy",
                        "langlinks": [
                            {"lang": "de", "*": "Astronomie"},
                            {"lang": "fr", "*": "Astronomie"},
                        ],
                    }
                }
            }
        },
    )

    def fail_if_called(sess, project, title):
        raise AssertionError("fallback should not be called when langlinks covers all target langs")

    monkeypatch.setattr(api_client, "get_wikidata_sitelinks", fail_if_called)

    found, missing = resolve_cross_language(session, "en.wikipedia", "Astronomy", ["de", "fr"])

    assert found == {"de": "Astronomie", "fr": "Astronomie"}
    assert missing == []


def test_resolve_cross_language_reports_missing_when_neither_source_has_it(
    requests_mock, session, monkeypatch
):
    mediawiki_url = _mediawiki_url("en.wikipedia")
    requests_mock.get(
        mediawiki_url,
        json={
            "query": {
                "pages": {
                    "1": {
                        "title": "Astronomy",
                        "langlinks": [
                            {"lang": "de", "*": "Astronomie"},
                        ],
                    }
                }
            }
        },
    )

    def fake_sitelinks(sess, project, title):
        return {}  # Wikidata has no entry for the missing language either

    monkeypatch.setattr(api_client, "get_wikidata_sitelinks", fake_sitelinks)

    found, missing = resolve_cross_language(session, "en.wikipedia", "Astronomy", ["de", "xx"])

    assert found == {"de": "Astronomie"}
    assert missing == ["xx"]
