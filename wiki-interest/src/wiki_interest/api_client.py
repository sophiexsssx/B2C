"""
Client for the Wikimedia Pageviews API, MediaWiki action API, and Wikidata API.

Request shapes and error behavior here are verified against real endpoints --
see notes/api.md at the project root for the curl calls and responses this
was built from. Disk caching is a later milestone; every function here is a
plain, stateless network call so it's easy to wrap with a cache.
"""

import os
import random
import time
import urllib.parse
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import requests

PAGEVIEWS_BASE = "https://wikimedia.org/api/rest_v1/metrics/pageviews"
WIKIDATA_API = "https://www.wikidata.org/w/api.php"

# First month the Pageviews API has data for (notes/api.md Q7).
DATA_START_YYYYMM = "2015-07"

MAX_RETRIES = 5
BACKOFF_BASE_SECONDS = 1.0
# A server-supplied Retry-After beyond this is treated as unusable (falls
# back to exponential backoff instead), so a huge or misconfigured value
# can't stall the client indefinitely.
MAX_RETRY_AFTER_SECONDS = 60.0

# Wikidata site-key -> langlinks-style language code, for the handful of
# wikis where the two APIs name the same edition differently (notes/api.md Q3).
_WIKIDATA_SITEID_ALIASES = {
    "be_x_old": "be-tarask",
    "zh_classical": "lzh",
    "zh_yue": "yue",
    "zh_min_nan": "nan",
    "no": "nb",
    "als": "gsw",
}
# Wikidata sitelink keys for non-Wikipedia projects, after stripping "wiki".
_NON_WIKIPEDIA_SITEIDS = {"commons", "meta", "species", "mediawiki", "wikifunctions", "abstract"}


class ContactNotConfiguredError(RuntimeError):
    """Raised when WIKITREND_CONTACT is not set -- we refuse to send a bare User-Agent."""


class OutOfRangeError(RuntimeError):
    """Raised when the requested range starts before the Pageviews API's data (July 2015)."""


class AmbiguousNoDataError(RuntimeError):
    """
    Raised for an in-range 404 from the Pageviews API. Per Wikimedia's own
    error message, this means either zero views for the period or data
    "not loaded yet" -- it is NOT proof the article/period has no data.
    Callers should treat it as retryable for recent periods and as
    likely-zero for older, fully-in-range periods.
    """


class _NotFoundResponse(Exception):
    """Internal: signals a 404 up to the caller that knows how to interpret it."""


def build_session() -> requests.Session:
    """
    Build a requests.Session with a contact-bearing User-Agent set once.

    Wikimedia has required a descriptive User-Agent with contact info on
    every request since Feb 2010; non-descriptive/absent ones "may be
    blocked without notice" regardless of status code (notes/api.md Q6).
    The contact string comes from WIKITREND_CONTACT (a URL and/or email);
    we fail loudly rather than silently sending a bare default UA.
    """
    contact = os.environ.get("WIKITREND_CONTACT")
    if not contact:
        raise ContactNotConfiguredError(
            "WIKITREND_CONTACT is not set. Wikimedia requires a descriptive "
            "User-Agent with contact info on every request -- set e.g. "
            "WIKITREND_CONTACT='https://example.org/wiki-interest; you@example.org'"
        )
    session = requests.Session()
    session.headers["User-Agent"] = f"WikiInterestBot/1.0 ({contact})"
    return session


def get_pageviews_per_article(session, project, article, start, end, access="all-access", agent="user"):
    """
    Fetch monthly pageviews for one article on `project` (e.g. "en.wikipedia").

    `start`/`end` are "YYYY-MM" strings, inclusive. `agent="user"` (the
    default) excludes known bots and automated traffic -- a heuristic
    Wikimedia classification, not a verified-human guarantee.

    Returns a complete monthly series: a list of {"month": "YYYY-MM", "views": int}
    covering every month in range, with any month the API omitted (because it
    had zero views) filled in as 0 -- the API does not return explicit zero
    entries.

    Raises OutOfRangeError if `start` is before the Pageviews API's data.
    Raises AmbiguousNoDataError for an in-range 404 (see its docstring).
    """
    _check_in_range(start)
    encoded_article = urllib.parse.quote(article, safe="")
    url = (
        f"{PAGEVIEWS_BASE}/per-article/{project}/{access}/{agent}/"
        f"{encoded_article}/monthly/{_to_timestamp(start)}/{_to_timestamp(end)}"
    )
    items = _fetch_pageview_items(session, url, article, project, start, end)
    return _materialize_zero_months(items, start, end)


def get_pageviews_aggregate(session, project, start, end, access="all-access", agent="user"):
    """
    Fetch total monthly pageviews for an entire wiki (site-level, not
    per-article) -- used to normalize an article's share of total traffic.

    Same "YYYY-MM" range, zero-materialization, and 404-ambiguity handling
    as get_pageviews_per_article.
    """
    _check_in_range(start)
    url = f"{PAGEVIEWS_BASE}/aggregate/{project}/{access}/{agent}/monthly/{_to_timestamp(start)}/{_to_timestamp(end)}"
    items = _fetch_pageview_items(session, url, None, project, start, end)
    return _materialize_zero_months(items, start, end)


def get_redirects(session, project, title):
    """
    Return every redirect title pointing to `title` on `project`'s wiki.

    Uses rdlimit=max and follows the `rdcontinue` token until none remains,
    so heavily-redirected articles aren't silently truncated (verified:
    "United States" has 301 redirects -- notes/api.md Q4).
    """
    url = _mediawiki_api_url(project)
    params = {"action": "query", "titles": title, "prop": "redirects", "rdlimit": "max", "format": "json"}
    redirects = []
    while True:
        data = _get_json(session, url, params=params)
        for page in data.get("query", {}).get("pages", {}).values():
            redirects.extend(r["title"] for r in page.get("redirects", []))
        rdcontinue = data.get("continue", {}).get("rdcontinue")
        if not rdcontinue:
            return redirects
        params["rdcontinue"] = rdcontinue


def get_langlinks(session, project, title, limit=500):
    """
    Primary cross-language lookup: MediaWiki langlinks for `title`.

    Single API call, no normalization needed -- verified to match Wikidata
    sitelinks once site-key naming is normalized, for the one article tested
    (notes/api.md Q3). Returns {lang_code: title}.
    """
    url = _mediawiki_api_url(project)
    params = {"action": "query", "titles": title, "prop": "langlinks", "lllimit": limit, "format": "json"}
    data = _get_json(session, url, params=params)
    result = {}
    for page in data.get("query", {}).get("pages", {}).values():
        for link in page.get("langlinks", []):
            result[link["lang"]] = link["*"]
    return result


def get_wikidata_sitelinks(session, project, title):
    """
    Fallback cross-language lookup via Wikidata sitelinks, for languages
    langlinks doesn't cover. Two calls: resolve the Wikidata QID for `title`
    on `project`, then fetch its sitelinks. Returns {lang_code: title},
    limited to Wikipedia editions (sister projects like Commons excluded),
    with known legacy site-key aliases normalized to langlinks' codes.
    """
    qid = _get_wikidata_qid(session, project, title)
    if qid is None:
        return {}
    params = {"action": "wbgetentities", "ids": qid, "props": "sitelinks", "format": "json"}
    data = _get_json(session, WIKIDATA_API, params=params)
    sitelinks = data.get("entities", {}).get(qid, {}).get("sitelinks", {})
    result = {}
    for site_id, info in sitelinks.items():
        if not site_id.endswith("wiki"):
            continue
        lang = site_id[: -len("wiki")]
        if lang in _NON_WIKIPEDIA_SITEIDS:
            continue
        result[_WIKIDATA_SITEID_ALIASES.get(lang, lang)] = info["title"]
    return result


def resolve_cross_language(session, project, title, target_langs):
    """
    Resolve `title` into each language in `target_langs`.

    Tries langlinks first (cheap, single call); falls back to Wikidata
    sitelinks only for the languages langlinks didn't cover.

    Returns (found: {lang: title}, missing: [lang, ...]).
    """
    found = get_langlinks(session, project, title)
    missing = [lang for lang in target_langs if lang not in found]
    if missing:
        fallback = get_wikidata_sitelinks(session, project, title)
        for lang in list(missing):
            if lang in fallback:
                found[lang] = fallback[lang]
                missing.remove(lang)
    return {lang: found[lang] for lang in target_langs if lang in found}, missing


# -- internals ----------------------------------------------------------------


def _mediawiki_api_url(project: str) -> str:
    """"en.wikipedia" -> "https://en.wikipedia.org/w/api.php"."""
    return f"https://{project}.org/w/api.php"


def _get_wikidata_qid(session, project, title):
    """Resolve the Wikidata item id (QID) for `title` on `project`, or None if it has none."""
    url = _mediawiki_api_url(project)
    params = {"action": "query", "titles": title, "prop": "pageprops", "ppprop": "wikibase_item", "format": "json"}
    data = _get_json(session, url, params=params)
    for page in data.get("query", {}).get("pages", {}).values():
        qid = page.get("pageprops", {}).get("wikibase_item")
        if qid:
            return qid
    return None


def _check_in_range(start: str) -> None:
    if start < DATA_START_YYYYMM:
        raise OutOfRangeError(
            f"Requested start {start!r} is before Pageviews API data begins ({DATA_START_YYYYMM})"
        )


def _fetch_pageview_items(session, url, article, project, start, end):
    try:
        data = _get_json(session, url)
    except _NotFoundResponse:
        subject = f"{article!r} on {project}" if article else project
        raise AmbiguousNoDataError(
            f"No data returned for {subject} in {start}..{end} -- this may mean zero views "
            "for the period, or that data is not loaded yet (not proof there is no data)."
        )
    return data.get("items", [])


def _to_timestamp(yyyymm: str) -> str:
    """"2025-01" -> "2025010100" (first day, hour 00 -- the format this API uses)."""
    year, month = yyyymm.split("-")
    return f"{year}{month}0100"


def _from_timestamp(timestamp: str) -> str:
    """"2025010100" -> "2025-01"."""
    return f"{timestamp[0:4]}-{timestamp[4:6]}"


def _month_range(start: str, end: str):
    """Yield "YYYY-MM" strings from start to end, inclusive."""
    year, month = (int(part) for part in start.split("-"))
    end_year, end_month = (int(part) for part in end.split("-"))
    while (year, month) <= (end_year, end_month):
        yield f"{year:04d}-{month:02d}"
        month += 1
        if month > 12:
            month = 1
            year += 1


def _materialize_zero_months(items: list, start: str, end: str) -> list:
    """Fill a complete monthly series for [start, end], defaulting omitted months to 0 views."""
    by_month = {_from_timestamp(item["timestamp"]): item["views"] for item in items}
    return [{"month": month, "views": by_month.get(month, 0)} for month in _month_range(start, end)]


def _get_json(session: requests.Session, url: str, params: dict = None) -> dict:
    """
    GET url(+params) and return parsed JSON, retrying on 429 and 5xx with
    exponential backoff (honoring Retry-After when present). Raises
    _NotFoundResponse on 404 so callers can decide what that means; raises
    for any other non-2xx status via response.raise_for_status().
    """
    last_error = None
    for attempt in range(MAX_RETRIES):
        response = session.get(url, params=params, timeout=15)
        if response.status_code == 429 or response.status_code >= 500:
            last_error = requests.HTTPError(f"{response.status_code} from {response.url}", response=response)
            if attempt < MAX_RETRIES - 1:
                _sleep_for_retry(attempt, response.headers.get("Retry-After"))
            continue
        if response.status_code == 404:
            raise _NotFoundResponse()
        response.raise_for_status()
        return response.json()
    raise last_error


def _sleep_for_retry(attempt: int, retry_after: str) -> None:
    """Sleep before a retry: honor Retry-After if present and usable, else exponential backoff with jitter."""
    seconds = _parse_retry_after(retry_after) if retry_after else None
    if seconds is None:
        seconds = BACKOFF_BASE_SECONDS * (2**attempt)
    time.sleep(seconds + random.uniform(0, 0.5))


def _parse_retry_after(retry_after: str):
    """
    Parse Retry-After (an integer number of seconds or an HTTP-date) into a
    wait in seconds. Returns None -- so the caller falls back to exponential
    backoff -- if the value is invalid, already in the past, or exceeds
    MAX_RETRY_AFTER_SECONDS (a huge or misconfigured value shouldn't be able
    to stall the client indefinitely).
    """
    try:
        seconds = float(retry_after)
    except ValueError:
        seconds = _parse_retry_after_http_date(retry_after)
    if seconds is None or seconds <= 0 or seconds > MAX_RETRY_AFTER_SECONDS:
        return None
    return seconds


def _parse_retry_after_http_date(retry_after: str):
    """Parse an HTTP-date Retry-After value; a timezone-naive result is treated as UTC."""
    try:
        target = parsedate_to_datetime(retry_after)
    except (TypeError, ValueError):
        return None
    if target.tzinfo is None:
        target = target.replace(tzinfo=timezone.utc)
    return (target - datetime.now(timezone.utc)).total_seconds()
