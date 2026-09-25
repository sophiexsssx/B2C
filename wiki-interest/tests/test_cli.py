"""
Tests for wiki_interest.cli: the analyze / report / resolve subcommands.

All HTTP calls are mocked via the `requests_mock` pytest fixture -- no real
network calls are made. WIKI_INTEREST_CACHE_DIR is monkeypatched to an
isolated tmp dir per test (same pattern as test_cache.py), so the real
wiki-interest/cache/ is never touched.

Pageview mocks return the SAME fixed monthly view count for every month in
a broad hardcoded range (2015-07..2028-12) -- api_client's
_materialize_zero_months matches by month regardless of what range the
mock itself was "asked" for, so one broad, uniform response correctly
backs any (start, end) a test picks, without needing to replicate cli.py's
reference-window date math in the test itself. Uniform views across
languages means yoy_growth computes to a real 0.0 (not None) for every
language -- enough to exercise ranking/capping without needing per-language
variation, which is metrics.py's job to get right, not this file's.
"""

import json
import re
import urllib.parse
from datetime import datetime, timezone

import pytest
import requests

import cli
from wiki_interest import cache
from wiki_interest.api_client import PAGEVIEWS_BASE, WIKIDATA_API

RESPONSE_BYTE_LIMIT = cli.RESPONSE_BYTE_LIMIT


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolated_cache_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("WIKI_INTEREST_CACHE_DIR", str(tmp_path))
    return tmp_path


@pytest.fixture
def session():
    return requests.Session()


def _mediawiki_url(project: str) -> str:
    return f"https://{project}.org/w/api.php"


def _broad_items(views: int) -> list:
    items = []
    year, month = 2015, 7
    while (year, month) <= (2028, 12):
        items.append({"timestamp": f"{year:04d}{month:02d}0100", "views": views})
        month += 1
        if month > 12:
            month = 1
            year += 1
    return items


def _mock_pageviews(requests_mock, article_views=100, site_views=100000):
    requests_mock.get(re.compile(re.escape(PAGEVIEWS_BASE) + r"/per-article/.*"), json={"items": _broad_items(article_views)})
    requests_mock.get(re.compile(re.escape(PAGEVIEWS_BASE) + r"/aggregate/.*"), json={"items": _broad_items(site_views)})


def _mock_resolution(requests_mock, langlinks: dict, qid="Q1"):
    """
    Mocks en.wikipedia (source project) langlinks + pageprops, Wikidata
    sitelinks agreeing with langlinks (-> match_confidence "high"), and an
    empty redirects list for every resolved language's own wiki.
    """

    def _en_callback(request, context):
        prop = (request.qs.get("prop") or [None])[0]
        page = {"title": "Topic"}
        if prop == "langlinks":
            page["langlinks"] = [{"lang": lang, "*": title} for lang, title in langlinks.items()]
        elif prop == "pageprops":
            page["pageprops"] = {"wikibase_item": qid}
        return {"query": {"pages": {"1": page}}}

    requests_mock.get(_mediawiki_url("en.wikipedia"), json=_en_callback)
    requests_mock.get(
        WIKIDATA_API,
        json={"entities": {qid: {"sitelinks": {f"{lang}wiki": {"site": f"{lang}wiki", "title": title} for lang, title in langlinks.items()}}}},
    )
    for lang, title in langlinks.items():
        requests_mock.get(_mediawiki_url(f"{lang}.wikipedia"), json={"query": {"pages": {"1": {"title": title}}}})


def _json_bytes(obj) -> int:
    return len(json.dumps(obj, separators=(",", ":")).encode("utf-8"))


# ---------------------------------------------------------------------------
# 1. Basic question = analyze + report = 2 calls
# ---------------------------------------------------------------------------


def test_basic_question_is_exactly_two_calls(requests_mock, session):
    _mock_resolution(requests_mock, {"de": "Thema", "pl": "Temat"})
    _mock_pageviews(requests_mock)

    # Call 1: analyze.
    analyze_response = cli.run_analyze("Topic", ["de", "pl"], start="2024-01", end="2024-12", session=session)
    assert "run_id" in analyze_response

    # Call 2: report -- reads the cached run, no refetch, no third call needed.
    report_response = cli.run_report(analyze_response["run_id"])

    assert report_response["run_id"] == analyze_response["run_id"]
    assert set(report_response["files"].keys()) == {"pdf", "png", "md"}
    for path in report_response["files"].values():
        from pathlib import Path

        assert Path(path).exists()
    assert isinstance(report_response["headline"], str) and report_response["headline"]


# ---------------------------------------------------------------------------
# 2. Every JSON response is valid and <=2KB
# ---------------------------------------------------------------------------


def test_analyze_and_report_responses_are_valid_json_and_under_2kb(requests_mock, session):
    _mock_resolution(requests_mock, {"de": "Thema", "pl": "Temat", "cs": "Predmet"})
    _mock_pageviews(requests_mock)

    analyze_response = cli.run_analyze("Topic", ["de", "pl", "cs"], start="2024-01", end="2024-12", session=session)
    reparsed = json.loads(json.dumps(analyze_response))  # round-trips cleanly -> valid JSON
    assert reparsed == analyze_response
    assert _json_bytes(analyze_response) <= RESPONSE_BYTE_LIMIT

    report_response = cli.run_report(analyze_response["run_id"])
    json.loads(json.dumps(report_response))
    assert _json_bytes(report_response) <= RESPONSE_BYTE_LIMIT


# ---------------------------------------------------------------------------
# 3. Output cap / omission with many languages
# ---------------------------------------------------------------------------


def test_analyze_caps_response_and_reports_omitted_count_for_many_languages(requests_mock, session):
    langs = [f"l{i:02d}" for i in range(40)]
    _mock_resolution(requests_mock, {lang: f"Title {lang}" for lang in langs})
    _mock_pageviews(requests_mock)

    response = cli.run_analyze("Topic", langs, start="2024-01", end="2024-12", session=session)

    assert _json_bytes(response) <= RESPONSE_BYTE_LIMIT
    assert response["omitted"] > 0
    kept = len(response["ranked"]) + len(response["unranked"])
    assert kept + response["omitted"] == len(langs)
    assert kept < len(langs)


def test_analyze_does_not_cap_when_well_under_budget(requests_mock, session):
    _mock_resolution(requests_mock, {"de": "Thema"})
    _mock_pageviews(requests_mock)

    response = cli.run_analyze("Topic", ["de"], start="2024-01", end="2024-12", session=session)

    assert response["omitted"] == 0
    assert len(response["ranked"]) + len(response["unranked"]) == 1


# ---------------------------------------------------------------------------
# 4. run_id is always server-generated
# ---------------------------------------------------------------------------


def test_run_id_is_server_generated_and_matches_the_saved_run(requests_mock, session):
    _mock_resolution(requests_mock, {"de": "Thema"})
    _mock_pageviews(requests_mock)

    response = cli.run_analyze("Topic", ["de"], start="2024-01", end="2024-12", session=session)

    assert re.match(r"^r_\d{8}_[0-9a-f]{6}$", response["run_id"])
    saved = cache.load_run_data(response["run_id"])
    assert saved["run_id"] == response["run_id"]
    assert saved["topic"] == "Topic"


def test_cli_rejects_caller_supplied_run_id_on_analyze():
    # cli._RaisingArgumentParser turns argparse's own SystemExit into a
    # normal exception -- see test_rank_by_invalid_choice_raises_value_error.
    parser = cli.build_parser()

    with pytest.raises(ValueError):
        parser.parse_args(["analyze", "--topic", "Topic", "--langs", "de", "--run-id", "caller-chosen-id"])


def test_run_analyze_has_no_run_id_parameter_at_all():
    import inspect

    assert "run_id" not in inspect.signature(cli.run_analyze).parameters


# ---------------------------------------------------------------------------
# 5. --article lang:"Title" overrides resolution for named languages only
# ---------------------------------------------------------------------------


def test_article_override_bypasses_resolution_only_for_named_languages(requests_mock, session):
    # Only "de" is registered as an auto-resolvable language (langlinks +
    # sitelinks); "pl" is provided entirely via --article and must never
    # need automatic resolution to succeed.
    _mock_resolution(requests_mock, {"de": "AutoResolvedTitle"})
    requests_mock.get(_mediawiki_url("pl.wikipedia"), json={"query": {"pages": {"1": {"title": "ManualTitle"}}}})
    _mock_pageviews(requests_mock)

    response = cli.run_analyze(
        "Topic",
        ["de", "pl"],
        start="2024-01",
        end="2024-12",
        article_overrides={"pl": "ManualTitle"},
        session=session,
    )

    all_results = response["ranked"] + response["unranked"]
    by_lang = {r["lang"]: r for r in all_results}
    assert by_lang["de"]["title"] == "AutoResolvedTitle"
    assert by_lang["pl"]["title"] == "ManualTitle"
    assert "pl" not in response["missing"]
    # The manual override is never cross-checked against Wikidata -- its
    # confidence/reason must say so, unlike the auto-resolved language.
    assert "manually specified" in by_lang["pl"]["reason"]
    assert "manually specified" not in by_lang["de"]["reason"]


def test_article_override_still_fetches_its_own_redirects(requests_mock, session):
    _mock_resolution(requests_mock, {"de": "AutoResolvedTitle"})
    requests_mock.get(
        _mediawiki_url("pl.wikipedia"),
        json={"query": {"pages": {"1": {"title": "ManualTitle", "redirects": [{"title": "ManualAlt"}]}}}},
    )
    _mock_pageviews(requests_mock)

    cli.run_analyze("Topic", ["pl"], start="2024-01", end="2024-12", article_overrides={"pl": "ManualTitle"}, session=session)

    redirect_requests = [r for r in requests_mock.request_history if r.url.startswith(_mediawiki_url("pl.wikipedia")) and r.qs.get("prop") == ["redirects"]]
    assert len(redirect_requests) == 1


# ---------------------------------------------------------------------------
# 6. resolve subcommand
# ---------------------------------------------------------------------------


def test_run_resolve_returns_resolve_topic_shape(requests_mock, session):
    _mock_resolution(requests_mock, {"de": "Thema", "pl": "Temat"})

    result = cli.run_resolve("Topic", ["de", "pl"], session=session)

    assert result["match_confidence"] == "high"
    assert result["editions"] == {"de": "Thema", "pl": "Temat"}
    assert result["missing"] == []


def test_resolve_response_stays_well_under_2kb(requests_mock, session):
    _mock_resolution(requests_mock, {"de": "Thema", "pl": "Temat"})

    result = cli.run_resolve("Topic", ["de", "pl"], session=session)

    assert _json_bytes(result) <= RESPONSE_BYTE_LIMIT


# ---------------------------------------------------------------------------
# 7. missing languages are surfaced, not silently dropped
# ---------------------------------------------------------------------------


def test_missing_language_is_listed_and_produces_no_result_entry(requests_mock, session):
    _mock_resolution(requests_mock, {"de": "Thema"})  # "xx" covered by nobody
    _mock_pageviews(requests_mock)

    response = cli.run_analyze("Topic", ["de", "xx"], start="2024-01", end="2024-12", session=session)

    assert response["missing"] == ["xx"]
    all_langs = {r["lang"] for r in response["ranked"] + response["unranked"]}
    assert all_langs == {"de"}


# ---------------------------------------------------------------------------
# 8. report reads the cached run, no refetch
# ---------------------------------------------------------------------------


def test_report_does_not_refetch_pageviews(requests_mock, session):
    _mock_resolution(requests_mock, {"de": "Thema"})
    _mock_pageviews(requests_mock)

    analyze_response = cli.run_analyze("Topic", ["de"], start="2024-01", end="2024-12", session=session)
    request_count_after_analyze = len(requests_mock.request_history)

    cli.run_report(analyze_response["run_id"])

    assert len(requests_mock.request_history) == request_count_after_analyze


def test_report_format_filter_returns_only_requested_files(requests_mock, session):
    _mock_resolution(requests_mock, {"de": "Thema"})
    _mock_pageviews(requests_mock)

    analyze_response = cli.run_analyze("Topic", ["de"], start="2024-01", end="2024-12", session=session)
    report_response = cli.run_report(analyze_response["run_id"], formats=["md"])

    assert set(report_response["files"].keys()) == {"md"}


# ---------------------------------------------------------------------------
# Helpers for the review-fix tests below
# ---------------------------------------------------------------------------


def _mock_resolution_for(requests_mock, source_project: str, langlinks: dict, qid="Q1"):
    """
    Like _mock_resolution, but the source Wikipedia project is a parameter
    instead of hardcoded "en.wikipedia" -- for --source-lang tests.
    """

    def _source_callback(request, context):
        prop = (request.qs.get("prop") or [None])[0]
        page = {"title": "Topic"}
        if prop == "langlinks":
            page["langlinks"] = [{"lang": lang, "*": title} for lang, title in langlinks.items()]
        elif prop == "pageprops":
            page["pageprops"] = {"wikibase_item": qid}
        return {"query": {"pages": {"1": page}}}

    requests_mock.get(_mediawiki_url(source_project), json=_source_callback)
    requests_mock.get(
        WIKIDATA_API,
        json={"entities": {qid: {"sitelinks": {f"{lang}wiki": {"site": f"{lang}wiki", "title": title} for lang, title in langlinks.items()}}}},
    )
    for lang, title in langlinks.items():
        requests_mock.get(_mediawiki_url(f"{lang}.wikipedia"), json={"query": {"pages": {"1": {"title": title}}}})


def _mock_404_for_article(requests_mock, project: str, article: str):
    """
    Registers a 404 (Pageviews API's real in-range-404 response shape, per
    test_api_client.py) for exactly `article`'s per-article endpoint on
    `project`, regardless of the exact start/end timestamps the CLI's
    reference-window math ends up requesting -- narrow enough to not shadow
    _mock_pageviews's broad per-article mock for every OTHER article, and
    registered after it in a test so requests_mock's most-recently-added-
    matcher-wins ordering picks this one first for matching requests.
    """
    encoded = urllib.parse.quote(article, safe="")
    pattern = re.compile(re.escape(f"{PAGEVIEWS_BASE}/per-article/{project}/") + r"[^/]+/[^/]+/" + re.escape(encoded) + r"/monthly/.*")
    requests_mock.get(pattern, status_code=404, json={"detail": "not found", "status": 404})


def _per_article_requested(requests_mock, project: str, title: str) -> bool:
    """Whether any request in requests_mock's history hit `title`'s per-article endpoint on `project`."""
    encoded = urllib.parse.quote(title, safe="")
    pattern = re.compile(re.escape(f"/per-article/{project}/") + r"[^/]+/[^/]+/" + re.escape(encoded) + r"/monthly/")
    return any(pattern.search(r.url) for r in requests_mock.request_history)


# ---------------------------------------------------------------------------
# 9. Per-article 404 handling (redirect vs. main article)
# ---------------------------------------------------------------------------


def test_redirect_404_is_treated_as_zero_and_does_not_abort_the_language(requests_mock, session):
    _mock_resolution(requests_mock, {"de": "Thema"})
    # Give "de" one redirect ("ThemaAlt") whose own pageviews call 404s.
    requests_mock.get(
        _mediawiki_url("de.wikipedia"),
        json={"query": {"pages": {"1": {"title": "Thema", "redirects": [{"title": "ThemaAlt"}]}}}},
    )
    _mock_pageviews(requests_mock)
    _mock_404_for_article(requests_mock, "de.wikipedia", "ThemaAlt")

    response = cli.run_analyze("Topic", ["de"], start="2024-01", end="2024-12", session=session)

    all_results = response["ranked"] + response["unranked"]
    assert len(all_results) == 1
    result = all_results[0]
    # The redirect's 404 must not abort the language: a real, non-null
    # computation comes back, not "no_data".
    assert result["yoy_growth_flag"] != "no_data"
    assert isinstance(result["spikes_removed"], int)
    assert response["missing"] == []


def test_main_article_404_marks_only_that_language_no_data_others_unaffected(requests_mock, session):
    _mock_resolution(requests_mock, {"de": "ArticleDE", "pl": "ArticlePL"})
    _mock_pageviews(requests_mock)
    # "de"'s own main-article pageviews call 404s; "pl" resolves and fetches fine.
    _mock_404_for_article(requests_mock, "de.wikipedia", "ArticleDE")

    response = cli.run_analyze("Topic", ["de", "pl"], start="2024-01", end="2024-12", session=session)

    all_results = response["ranked"] + response["unranked"]
    by_lang = {r["lang"]: r for r in all_results}
    assert set(by_lang) == {"de", "pl"}
    assert by_lang["de"]["yoy_growth_flag"] == "no_data"
    assert by_lang["de"]["confidence"] == "low"
    assert "404" in by_lang["de"]["reason"] or "article" in by_lang["de"]["reason"].lower()
    # The other language must still be processed normally, not aborted.
    assert by_lang["pl"]["yoy_growth_flag"] != "no_data"
    assert response["missing"] == []  # both resolved fine; "de" just had no page data


# ---------------------------------------------------------------------------
# 10. Errors come back as exactly one JSON line on stdout, never a traceback
# ---------------------------------------------------------------------------


def test_main_prints_single_json_error_line_on_timeout(requests_mock, capsys):
    requests_mock.get(_mediawiki_url("en.wikipedia"), exc=requests.exceptions.Timeout)

    exit_code = cli.main(["resolve", "--article", "Topic", "--langs", "de"])

    captured = capsys.readouterr()
    assert exit_code == 1
    stdout_lines = [line for line in captured.out.split("\n") if line]
    assert len(stdout_lines) == 1
    payload = json.loads(stdout_lines[0])
    assert isinstance(payload["error"], str) and payload["error"]
    assert isinstance(payload["hint"], str) and payload["hint"]
    assert "Traceback" not in captured.out
    assert "Traceback" not in captured.err


def test_main_prints_single_json_error_line_on_missing_run_id(capsys):
    exit_code = cli.main(["report", "--run-id", "r_never_analyzed"])

    captured = capsys.readouterr()
    assert exit_code == 1
    stdout_lines = [line for line in captured.out.split("\n") if line]
    assert len(stdout_lines) == 1
    payload = json.loads(stdout_lines[0])
    assert isinstance(payload["error"], str) and payload["error"]
    assert isinstance(payload["hint"], str) and payload["hint"]
    assert "run" in payload["hint"].lower()
    assert "Traceback" not in captured.out
    assert "Traceback" not in captured.err


def test_main_prints_single_json_error_line_on_bad_argument_not_argparse_usage_text(capsys):
    # An argparse-level failure (invalid choice) must get the same
    # single-JSON-line-on-stdout, exit-1 treatment as any other error --
    # not argparse's own usage message on stderr + SystemExit(2).
    exit_code = cli.main(["analyze", "--topic", "Topic", "--langs", "de", "--rank-by", "bogus"])

    captured = capsys.readouterr()
    assert exit_code == 1
    stdout_lines = [line for line in captured.out.split("\n") if line]
    assert len(stdout_lines) == 1
    payload = json.loads(stdout_lines[0])
    assert isinstance(payload["error"], str) and payload["error"]
    assert isinstance(payload["hint"], str) and payload["hint"]
    assert captured.err == ""
    assert "usage:" not in captured.out.lower()
    assert "Traceback" not in captured.out
    assert "Traceback" not in captured.err


def test_main_help_still_exits_zero_and_prints_help_text(capsys):
    # --help must keep its normal argparse behavior (help text + exit 0),
    # unlike a genuine argument ERROR -- only error() is overridden.
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["--help"])

    assert exc_info.value.code == 0
    captured = capsys.readouterr()
    assert "usage:" in captured.out.lower()


# ---------------------------------------------------------------------------
# 11. --source-lang (default "en")
# ---------------------------------------------------------------------------


def test_run_analyze_source_lang_queries_that_wikipedia_not_english(requests_mock, session):
    _mock_resolution_for(requests_mock, "uk.wikipedia", {"de": "Thema"})
    _mock_pageviews(requests_mock)

    response = cli.run_analyze("Тема", ["de"], start="2024-01", end="2024-12", source_lang="uk", session=session)

    en_requests = [r for r in requests_mock.request_history if r.url.startswith(_mediawiki_url("en.wikipedia"))]
    uk_requests = [r for r in requests_mock.request_history if r.url.startswith(_mediawiki_url("uk.wikipedia"))]
    assert en_requests == []
    assert len(uk_requests) > 0

    all_results = response["ranked"] + response["unranked"]
    assert len(all_results) == 1
    assert all_results[0]["lang"] == "de"
    assert all_results[0]["title"] == "Thema"


def test_run_resolve_source_lang_queries_that_wikipedia_not_english(requests_mock, session):
    _mock_resolution_for(requests_mock, "uk.wikipedia", {"de": "Thema"})

    result = cli.run_resolve("Тема", ["de"], source_lang="uk", session=session)

    assert result["editions"] == {"de": "Thema"}
    en_requests = [r for r in requests_mock.request_history if r.url.startswith(_mediawiki_url("en.wikipedia"))]
    assert en_requests == []


# ---------------------------------------------------------------------------
# 12. Validation: --rank-by choices, start > end, end clamped to last complete month
# ---------------------------------------------------------------------------


def test_rank_by_invalid_choice_raises_value_error():
    # build_parser() uses a raising ArgumentParser (see cli._RaisingArgumentParser)
    # so an argument error becomes a normal exception main() can turn into
    # the same {"error", "hint"} JSON line as any other failure, rather
    # than argparse's own SystemExit + usage-message-on-stderr.
    parser = cli.build_parser()

    with pytest.raises(ValueError):
        parser.parse_args(["analyze", "--topic", "Topic", "--langs", "de", "--rank-by", "bogus"])


def test_rank_by_valid_choices_are_accepted():
    parser = cli.build_parser()

    for choice in cli.RANK_BY_CHOICES:
        args = parser.parse_args(["analyze", "--topic", "Topic", "--langs", "de", "--rank-by", choice])
        assert args.rank_by == choice


def test_validate_and_normalize_period_raises_when_start_after_end():
    with pytest.raises(ValueError):
        cli._validate_and_normalize_period("2024-06", "2024-01")


def test_validate_yyyymm_rejects_trailing_newline():
    # Python's $ (unlike \Z) matches just before a trailing newline, so
    # "2024-01\n" would otherwise slip past validation and then compare as
    # a different, wrong string everywhere start/end are used.
    with pytest.raises(ValueError):
        cli._validate_yyyymm("--start", "2024-01\n")


def test_run_analyze_raises_value_error_when_start_after_end(session):
    # No network mocking needed -- validation happens before any fetch.
    with pytest.raises(ValueError):
        cli.run_analyze("Topic", ["de"], start="2024-06", end="2024-01", session=session)


def test_validate_and_normalize_period_clamps_current_or_future_end():
    today = datetime(2026, 9, 24, tzinfo=timezone.utc)

    start, end, note = cli._validate_and_normalize_period("2024-01", "2026-09", today=today)

    assert end == "2026-08"
    assert note and "clamp" in note.lower()


def test_validate_and_normalize_period_no_clamp_at_last_complete_month_boundary():
    today = datetime(2026, 9, 24, tzinfo=timezone.utc)

    start, end, note = cli._validate_and_normalize_period("2024-01", "2026-08", today=today)

    assert end == "2026-08"
    assert note is None


# ---------------------------------------------------------------------------
# 13. Redirect cap (MAX_REDIRECTS_PER_LANGUAGE)
# ---------------------------------------------------------------------------


def test_redirect_cap_limits_counted_redirects_and_reason_mentions_it(requests_mock, session, monkeypatch):
    monkeypatch.setattr(cli, "MAX_REDIRECTS_PER_LANGUAGE", 3)
    _mock_resolution(requests_mock, {"de": "Thema"})
    redirect_titles = [f"Redirect{i}" for i in range(5)]
    requests_mock.get(
        _mediawiki_url("de.wikipedia"),
        json={"query": {"pages": {"1": {"title": "Thema", "redirects": [{"title": t} for t in redirect_titles]}}}},
    )
    _mock_pageviews(requests_mock)

    response = cli.run_analyze("Topic", ["de"], start="2024-01", end="2024-12", session=session)

    result = (response["ranked"] + response["unranked"])[0]
    assert "3 of 5" in result["reason"]

    for title in redirect_titles[:3]:
        assert _per_article_requested(requests_mock, "de.wikipedia", title)
    for title in redirect_titles[3:]:
        assert not _per_article_requested(requests_mock, "de.wikipedia", title)


# ---------------------------------------------------------------------------
# 14. report --summary overrides the rendered summary without persisting it
# ---------------------------------------------------------------------------


def test_report_summary_override_is_not_persisted_to_the_cached_run(requests_mock, session):
    _mock_resolution(requests_mock, {"de": "Thema"})
    _mock_pageviews(requests_mock)

    analyze_response = cli.run_analyze("Topic", ["de"], start="2024-01", end="2024-12", session=session)
    run_id = analyze_response["run_id"]

    overridden = cli.run_report(run_id, summary="Custom summary text")
    with open(overridden["files"]["md"], encoding="utf-8") as f:
        overridden_markdown = f.read()
    assert "Custom summary text" in overridden_markdown

    default = cli.run_report(run_id)
    with open(default["files"]["md"], encoding="utf-8") as f:
        default_markdown = f.read()
    assert "Custom summary text" not in default_markdown

    saved = cache.load_run_data(run_id)
    assert "summary" not in saved
