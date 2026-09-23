"""
Tests for wiki_interest.resolve.

resolve_topic is built on api_client's primitives, all of which make real
HTTP calls -- every one is mocked via the `requests_mock` pytest fixture, no
real network calls are made. resolve_topic always calls, in order:
get_langlinks, get_wikidata_qid, get_wikidata_sitelinks (only if a QID was
found), then get_redirects once per resolved language against that
language's OWN wiki domain (e.g. pl.wikipedia.org, not the source project's
domain).
"""

import pytest
import requests

from wiki_interest.api_client import WIKIDATA_API
from wiki_interest.resolve import resolve_topic


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mediawiki_url(project: str) -> str:
    return f"https://{project}.org/w/api.php"


def _project_responder(langlinks: dict, qid):
    """
    A requests_mock json callback serving the source project's own mediawiki
    endpoint for both get_langlinks (prop=langlinks) and get_wikidata_qid /
    the QID lookup inside get_wikidata_sitelinks (prop=pageprops), keyed off
    the `prop` query param -- resolve_topic hits the same URL for both.
    """

    def _callback(request, context):
        prop = (request.qs.get("prop") or [None])[0]
        page = {"title": "Article"}
        if prop == "langlinks":
            page["langlinks"] = [{"lang": lang, "*": title} for lang, title in langlinks.items()]
        elif prop == "pageprops":
            if qid:
                page["pageprops"] = {"wikibase_item": qid}
        return {"query": {"pages": {"1": page}}}

    return _callback


def _sitelinks_json(qid: str, sitelinks: dict) -> dict:
    return {
        "entities": {
            qid: {
                "sitelinks": {
                    f"{lang}wiki": {"site": f"{lang}wiki", "title": title} for lang, title in sitelinks.items()
                }
            }
        }
    }


def _redirects_json(title: str, redirect_titles: list) -> dict:
    return {"query": {"pages": {"1": {"title": title, "redirects": [{"title": t} for t in redirect_titles]}}}}


def _register_redirects(requests_mock, lang: str, title: str, redirect_titles: list):
    url = _mediawiki_url(f"{lang}.wikipedia")
    requests_mock.get(url, json=_redirects_json(title, redirect_titles))


@pytest.fixture
def session():
    return requests.Session()


# ---------------------------------------------------------------------------
# 1. Happy path
# ---------------------------------------------------------------------------


def test_happy_path_all_langs_covered_and_agreeing(requests_mock, session):
    project_url = _mediawiki_url("en.wikipedia")
    langlinks = {"de": "Astronomie", "fr": "Astronomie", "pl": "Astronomia"}
    qid = "Q333"
    requests_mock.get(project_url, json=_project_responder(langlinks, qid))
    requests_mock.get(WIKIDATA_API, json=_sitelinks_json(qid, langlinks))  # sitelinks agree with langlinks

    _register_redirects(requests_mock, "de", "Astronomie", ["Astronomie (Redirect)"])
    _register_redirects(requests_mock, "fr", "Astronomie", ["Astronomie (alt)"])
    _register_redirects(requests_mock, "pl", "Astronomia", ["Astronomia (przekierowanie)"])

    result = resolve_topic(session, "en.wikipedia", "Astronomy", ["de", "fr", "pl"])

    assert result["qid"] == qid
    assert result["editions"] == langlinks
    assert result["missing"] == []
    assert result["match_confidence"] == "high"
    assert result["redirects"] == {
        "de": ["Astronomie (Redirect)"],
        "fr": ["Astronomie (alt)"],
        "pl": ["Astronomia (przekierowanie)"],
    }


def test_requests_to_source_project_ask_for_redirect_resolution(requests_mock, session):
    # If the `title` passed to resolve_topic is itself a redirect (e.g. a
    # user's natural phrasing lands on a redirect rather than the canonical
    # title), langlinks/pageprops queries without redirects=1 silently
    # return empty results for the redirect page itself -- verified against
    # the live API. Both requests resolve_topic sends to the source
    # project (langlinks, then pageprops) must ask for redirect resolution.
    project_url = _mediawiki_url("en.wikipedia")
    requests_mock.get(project_url, json=_project_responder({"de": "Astronomie"}, "Q333"))
    requests_mock.get(WIKIDATA_API, json=_sitelinks_json("Q333", {"de": "Astronomie"}))
    _register_redirects(requests_mock, "de", "Astronomie", [])

    resolve_topic(session, "en.wikipedia", "Astronomical", ["de"])

    # resolve_topic calls get_wikidata_qid directly, then get_wikidata_sitelinks
    # (which redundantly calls get_wikidata_qid again internally) -- a
    # pre-existing minor inefficiency, unrelated to this test's purpose.
    # So: 1 langlinks request + 2 pageprops requests = 3 total.
    source_project_requests = [r for r in requests_mock.request_history if r.url.startswith(project_url)]
    assert len(source_project_requests) == 3
    for request in source_project_requests:
        assert request.qs.get("redirects") == ["1"]


# ---------------------------------------------------------------------------
# 2. Language covered by neither source
# ---------------------------------------------------------------------------


def test_language_covered_by_neither_source_is_missing(requests_mock, session):
    project_url = _mediawiki_url("en.wikipedia")
    langlinks = {"de": "Astronomie"}  # "xx" covered by nobody
    qid = "Q333"
    requests_mock.get(project_url, json=_project_responder(langlinks, qid))
    requests_mock.get(WIKIDATA_API, json=_sitelinks_json(qid, {"de": "Astronomie"}))

    _register_redirects(requests_mock, "de", "Astronomie", [])

    result = resolve_topic(session, "en.wikipedia", "Astronomy", ["de", "xx"])

    assert result["editions"] == {"de": "Astronomie"}
    assert result["missing"] == ["xx"]


# ---------------------------------------------------------------------------
# 3. langlinks / sitelinks disagreement
# ---------------------------------------------------------------------------


def test_disagreement_between_langlinks_and_sitelinks_is_low_confidence(requests_mock, session):
    # A direct title contradiction between the two sources is positive
    # evidence something is wrong -- worse than merely unverified (single
    # source), so this is "low", not "medium".
    project_url = _mediawiki_url("en.wikipedia")
    langlinks = {"pl": "Astronomia"}
    sitelinks = {"pl": "Astronomia (disambiguation)"}  # different title for the same language
    qid = "Q333"
    requests_mock.get(project_url, json=_project_responder(langlinks, qid))
    requests_mock.get(WIKIDATA_API, json=_sitelinks_json(qid, sitelinks))

    _register_redirects(requests_mock, "pl", "Astronomia", [])

    result = resolve_topic(session, "en.wikipedia", "Astronomy", ["pl"])

    assert result["match_confidence"] == "low"
    assert "pl" in result["reason"]


def test_language_found_by_only_one_source_is_medium_confidence(requests_mock, session):
    # langlinks covers "pl" but sitelinks doesn't have it at all (missing,
    # not disagreeing) -- unverified, so "medium", not silently "high".
    project_url = _mediawiki_url("en.wikipedia")
    langlinks = {"pl": "Astronomia"}
    qid = "Q333"
    requests_mock.get(project_url, json=_project_responder(langlinks, qid))
    requests_mock.get(WIKIDATA_API, json=_sitelinks_json(qid, {}))  # sitelinks has nothing

    _register_redirects(requests_mock, "pl", "Astronomia", [])

    result = resolve_topic(session, "en.wikipedia", "Astronomy", ["pl"])

    assert result["editions"] == {"pl": "Astronomia"}
    assert result["match_confidence"] == "medium"
    assert "pl" in result["reason"]


# ---------------------------------------------------------------------------
# 4. No Wikidata item at all
# ---------------------------------------------------------------------------


def test_no_wikidata_item_is_low_confidence_and_skips_sitelinks_call(requests_mock, session):
    project_url = _mediawiki_url("en.wikipedia")
    langlinks = {"de": "Astronomie"}
    requests_mock.get(project_url, json=_project_responder(langlinks, qid=None))
    # Deliberately do NOT register WIKIDATA_API: if get_wikidata_sitelinks were
    # called anyway, requests_mock would raise NoMockAddress and fail this test.

    _register_redirects(requests_mock, "de", "Astronomie", [])

    result = resolve_topic(session, "en.wikipedia", "Astronomy", ["de"])

    assert result["qid"] is None
    assert result["match_confidence"] == "low"
    assert not any(WIKIDATA_API in r.url for r in requests_mock.request_history)


# ---------------------------------------------------------------------------
# 5. Redirects fetched against each edition's OWN wiki domain
# ---------------------------------------------------------------------------


def test_redirects_fetched_against_resolved_language_own_domain(requests_mock, session):
    project_url = _mediawiki_url("en.wikipedia")
    langlinks = {"pl": "Astronomia"}
    requests_mock.get(project_url, json=_project_responder(langlinks, qid=None))

    pl_url = _mediawiki_url("pl.wikipedia")
    requests_mock.get(pl_url, json=_redirects_json("Astronomia", ["Astronomia (przekierowanie)"]))

    result = resolve_topic(session, "en.wikipedia", "Astronomy", ["pl"])

    assert result["redirects"] == {"pl": ["Astronomia (przekierowanie)"]}

    redirect_requests = [r for r in requests_mock.request_history if r.url.startswith(pl_url)]
    assert len(redirect_requests) == 1
    assert redirect_requests[0].qs.get("prop") == ["redirects"]
    # never queried the source project's own domain for redirects
    assert not any(r.url.startswith(project_url) and "prop=redirects" in r.url for r in requests_mock.request_history)
