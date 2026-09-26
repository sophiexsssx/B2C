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
from wiki_interest import api_client, cache
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
# 3b. avg_monthly_views / last12_avg_monthly_views / --rank-by avg_monthly_views
# ---------------------------------------------------------------------------


def test_analyze_results_include_avg_monthly_views_fields(requests_mock, session):
    _mock_resolution(requests_mock, {"de": "Thema"})
    _mock_pageviews(requests_mock, article_views=250)

    response = cli.run_analyze("Topic", ["de"], start="2024-01", end="2024-12", session=session)

    result = (response["ranked"] + response["unranked"])[0]
    assert result["avg_monthly_views"] == pytest.approx(250, abs=1)
    assert result["last12_avg_monthly_views"] == pytest.approx(250, abs=1)


def test_rank_by_avg_monthly_views_sorts_by_raw_volume_not_growth(requests_mock, session):
    _mock_resolution(requests_mock, {"de": "Thema", "pl": "Temat"})
    _mock_pageviews(requests_mock)  # uniform views for the broad mock -- override per-language below

    # "de" has a much higher volume but identical (uniform) growth to "pl" --
    # ranking by avg_monthly_views must put the higher-volume language first,
    # which the default yoy_growth ranking (both 0.0, alphabetical/stable
    # order) wouldn't distinguish.
    import re

    from wiki_interest.api_client import PAGEVIEWS_BASE

    requests_mock.get(re.compile(re.escape(PAGEVIEWS_BASE) + r"/per-article/de\.wikipedia/.*"), json={"items": _broad_items(5000)})
    requests_mock.get(re.compile(re.escape(PAGEVIEWS_BASE) + r"/per-article/pl\.wikipedia/.*"), json={"items": _broad_items(100)})

    response = cli.run_analyze("Topic", ["de", "pl"], start="2024-01", end="2024-12", rank_by="avg_monthly_views", session=session)

    ranked_langs = [r["lang"] for r in response["ranked"]]
    assert ranked_langs == ["de", "pl"]


def test_rank_by_avg_monthly_views_is_a_valid_argparse_choice():
    parser = cli.build_parser()

    args = parser.parse_args(["analyze", "--topic", "Topic", "--langs", "de", "--rank-by", "avg_monthly_views"])

    assert args.rank_by == "avg_monthly_views"


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
# 12b. --start/--end also accept a full YYYY-MM-DD (M6: real Haiku run guessed
#      this format and had to retry -- see evals/results/haiku-blind.md eval 6)
# ---------------------------------------------------------------------------


def test_normalize_month_input_reduces_full_date_to_month():
    assert cli._normalize_month_input("2026-04-01") == "2026-04"
    assert cli._normalize_month_input("2026-04-30") == "2026-04"


def test_normalize_month_input_leaves_plain_month_unchanged():
    assert cli._normalize_month_input("2026-04") == "2026-04"


def test_normalize_month_input_leaves_invalid_input_unchanged_for_validate_yyyymm_to_reject():
    # Not this function's job to raise -- _validate_yyyymm still rejects
    # whatever doesn't come out as a canonical YYYY-MM.
    assert cli._normalize_month_input("not-a-date") == "not-a-date"


def test_validate_and_normalize_period_accepts_yyyy_mm_dd():
    start, end, note = cli._validate_and_normalize_period("2026-04-01", "2026-04-30")

    assert (start, end) == ("2026-04", "2026-04")
    assert note is None


def test_run_analyze_accepts_yyyy_mm_dd_start_and_end(requests_mock, session):
    _mock_resolution(requests_mock, {"de": "Thema"})
    _mock_pageviews(requests_mock)

    response = cli.run_analyze("Topic", ["de"], start="2024-01-01", end="2024-12-31", session=session)

    assert response["period"]["start"] == "2024-01"
    assert response["period"]["end"] == "2024-12"


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


# ---------------------------------------------------------------------------
# 15. Site-aggregate 404 follows the same date-aware policy as a redirect's,
#     and doesn't abort run_analyze's per-language loop
# ---------------------------------------------------------------------------


def _mock_404_for_aggregate(requests_mock, project: str):
    """Like _mock_404_for_article, but for the site-aggregate endpoint (no article path segment)."""
    pattern = re.compile(re.escape(f"{PAGEVIEWS_BASE}/aggregate/{project}/") + r"[^/]+/[^/]+/monthly/.*")
    requests_mock.get(pattern, status_code=404, json={"detail": "not found", "status": 404})


def test_is_recent_range_covers_current_month_and_a_post_month_end_grace_window():
    # The current (in-progress) month is always "recent".
    assert cli._is_recent_range("2026-09", datetime(2026, 9, 24, tzinfo=timezone.utc)) is True

    # The month that JUST completed is still "recent" (retryable) for a
    # short grace window into the following month, since run_analyze
    # always clamps `end` to a complete month before these functions are
    # ever called -- without this, "recent" could never trigger there at all.
    assert cli._is_recent_range("2026-08", datetime(2026, 9, 3, tzinfo=timezone.utc)) is True

    # Once well past that grace window, the same "just completed" month is
    # no longer "recent" -- old behavior for a month that's actually old.
    assert cli._is_recent_range("2026-08", datetime(2026, 9, 24, tzinfo=timezone.utc)) is False

    # A month further back than "just completed" is never "recent",
    # regardless of how early in the current month it is.
    assert cli._is_recent_range("2026-07", datetime(2026, 9, 3, tzinfo=timezone.utc)) is False


def test_fetch_site_monthly_404_on_older_range_is_treated_as_zero(requests_mock, session):
    _mock_404_for_aggregate(requests_mock, "de.wikipedia")

    result = cli._fetch_site_monthly(session, "de.wikipedia", "2020-01", "2020-12", "user")

    assert len(result) == 12
    assert all(v == 0 for v in result.values())


def test_fetch_site_monthly_404_on_recent_range_propagates(requests_mock, session):
    today = datetime(2026, 9, 24, tzinfo=timezone.utc)
    _mock_404_for_aggregate(requests_mock, "de.wikipedia")

    with pytest.raises(api_client.AmbiguousNoDataError):
        cli._fetch_site_monthly(session, "de.wikipedia", "2026-09", "2026-09", "user", today)


def test_fetch_redirect_monthly_or_zero_404_on_recent_range_propagates(requests_mock, session):
    today = datetime(2026, 9, 24, tzinfo=timezone.utc)
    _mock_404_for_article(requests_mock, "de.wikipedia", "RedirectAlt")

    with pytest.raises(api_client.AmbiguousNoDataError):
        cli._fetch_redirect_monthly_or_zero(session, "de.wikipedia", "RedirectAlt", "2026-09", "2026-09", "user", today)


def test_site_aggregate_404_on_older_range_does_not_abort_the_language_loop(requests_mock, session):
    # A real wiki's site total is essentially never actually zero, but this
    # only tests the "doesn't crash / other languages unaffected" behavior --
    # the exact numbers _fetch_site_monthly zero-fills to are metrics.py's
    # concern, not this test's.
    _mock_resolution(requests_mock, {"de": "ArticleDE", "pl": "ArticlePL"})
    _mock_pageviews(requests_mock)
    _mock_404_for_aggregate(requests_mock, "de.wikipedia")

    response = cli.run_analyze("Topic", ["de", "pl"], start="2024-01", end="2024-12", session=session)

    all_results = response["ranked"] + response["unranked"]
    by_lang = {r["lang"]: r for r in all_results}
    assert set(by_lang) == {"de", "pl"}
    # "de"'s site aggregate 404'd but its main article fetched fine -- a
    # real (non-"no_data") computation comes back, not an aborted language.
    assert by_lang["de"]["yoy_growth_flag"] != "no_data"
    assert by_lang["pl"]["yoy_growth_flag"] != "no_data"
    assert response["missing"] == []


# ---------------------------------------------------------------------------
# 16. _build_headline: zero-growth wording and all-non-positive subject choice
# ---------------------------------------------------------------------------


def _result(lang, yoy_growth, significant=True):
    return {
        "lang": lang,
        "title": lang,
        "yoy_growth": yoy_growth,
        "share_yoy_growth": yoy_growth,
        "momentum_6mo": yoy_growth,
        "spikes_removed": 0,
        "seasonal_peaks_kept": 0,
        "significant": significant,
        "trend_test": "seasonal_mann_kendall",
        "confidence": "medium",
        "reason": "reason",
    }


def test_build_headline_single_language_zero_growth_is_flat_not_declining():
    run_data = {"topic": "Topic", "results": [_result("de", 0.0)]}

    headline = cli._build_headline(run_data)

    assert "flat" in headline
    assert "declining" not in headline


def test_build_headline_all_non_positive_uses_most_negative_as_subject():
    # "de" is barely declining, "pl" is declining much harder -- the more
    # informative headline subject is "pl" (steepest decline), and the
    # sentence must not claim "de" (the least negative) is declining
    # FASTER than "pl" (the most negative), which would be backwards.
    run_data = {"topic": "Topic", "results": [_result("de", -0.05), _result("pl", -0.40)]}

    headline = cli._build_headline(run_data)

    assert headline.startswith("PL")
    assert "declining faster than DE" in headline


def test_build_headline_mixed_growth_still_uses_the_top_grower_as_subject():
    # Unchanged behavior for the normal (some positive) case: the best
    # grower is the subject, compared against the worst performer.
    run_data = {"topic": "Topic", "results": [_result("de", 0.30), _result("pl", -0.10)]}

    headline = cli._build_headline(run_data)

    assert headline.startswith("DE")
    assert "growing faster than PL" in headline


def test_build_headline_all_zero_growth_compares_without_faster_than():
    run_data = {"topic": "Topic", "results": [_result("de", 0.0), _result("pl", 0.0)]}

    headline = cli._build_headline(run_data)

    assert "flat" in headline
    assert "faster than" not in headline


def test_build_headline_preserves_significance_suffix_on_the_comparison_target():
    # All non-positive -> top/bottom swap (pl, the most negative, is the
    # subject; de, the least negative, is the comparison target) -- the
    # suffix must still follow whichever result ends up as "bottom" after
    # that swap, same as the original (pre-swap) behavior.
    run_data = {"topic": "Topic", "results": [_result("de", -0.05, significant=False), _result("pl", -0.40, significant=True)]}

    headline = cli._build_headline(run_data)

    assert headline.startswith("PL")
    assert "DE trend not significant" in headline


# ---------------------------------------------------------------------------
# 17. Saved results follow --rank-by order, not request/alphabetical order
# ---------------------------------------------------------------------------


def test_saved_results_are_rank_ordered_not_alphabetical(requests_mock, session):
    # "aa" is requested first and sorts first alphabetically, but "zz" is
    # the actual best grower -- both report.py's table/chart AND any
    # "top N" slicing take run_data["results"] as given, so the SAVED
    # order (not just the JSON response's "ranked" list) must reflect the
    # real ranking, or a naive positional "top 8" would silently show the
    # wrong languages.
    _mock_resolution(requests_mock, {"aa": "TopicAA", "mm": "TopicMM", "zz": "TopicZZ"})
    import re

    from wiki_interest.api_client import PAGEVIEWS_BASE

    requests_mock.get(re.compile(re.escape(PAGEVIEWS_BASE) + r"/per-article/aa\.wikipedia/.*"), json={"items": _broad_items(50)})
    requests_mock.get(re.compile(re.escape(PAGEVIEWS_BASE) + r"/per-article/mm\.wikipedia/.*"), json={"items": _broad_items(5000)})
    requests_mock.get(re.compile(re.escape(PAGEVIEWS_BASE) + r"/per-article/zz\.wikipedia/.*"), json={"items": _broad_items(500)})
    requests_mock.get(re.compile(re.escape(PAGEVIEWS_BASE) + r"/aggregate/.*"), json={"items": _broad_items(100000)})

    response = cli.run_analyze("Topic", ["aa", "mm", "zz"], start="2024-01", end="2024-12", session=session)

    saved = cache.load_run_data(response["run_id"])
    saved_lang_order = [r["lang"] for r in saved["results"]]
    # Uniform-views mocks all give yoy_growth == 0.0 (see this file's module
    # docstring), so rank_series's sort is stable -- it preserves whichever
    # order the results were originally built in (request order: aa, mm,
    # zz), which does NOT coincidentally equal alphabetical order here on
    # its own. The point of this test is structural: saved["results"] must
    # be EXACTLY the ranked+unranked concatenation the response itself
    # used, not a re-derived or different order -- assert that equality
    # directly, which is what actually catches a regression back to raw
    # insertion order if that concatenation is ever dropped.
    assert saved_lang_order == [r["lang"] for r in response["ranked"]] + [r["lang"] for r in response["unranked"]]
    assert saved.get("rank_by") == "yoy_growth"


def test_saved_results_rank_order_with_real_growth_differences(requests_mock, session):
    # Same as above, but with genuinely different growth rates so the sort
    # isn't just "stable on ties" -- "zz" (best grower) must end up FIRST
    # in saved results despite sorting last alphabetically and being
    # requested last.
    _mock_resolution(requests_mock, {"aa": "TopicAA", "zz": "TopicZZ"})
    import re

    from wiki_interest.api_client import PAGEVIEWS_BASE

    # A flat-then-doubled series gives a large positive yoy_growth; a
    # flat-then-halved series gives a large negative one.
    def _step_items(before, after):
        items = []
        year, month = 2015, 7
        while (year, month) < (2024, 1):
            items.append({"timestamp": f"{year:04d}{month:02d}0100", "views": before})
            month += 1
            if month > 12:
                month = 1
                year += 1
        while (year, month) <= (2024, 12):
            items.append({"timestamp": f"{year:04d}{month:02d}0100", "views": after})
            month += 1
            if month > 12:
                month = 1
                year += 1
        return items

    requests_mock.get(re.compile(re.escape(PAGEVIEWS_BASE) + r"/per-article/aa\.wikipedia/.*"), json={"items": _step_items(1000, 100)})
    requests_mock.get(re.compile(re.escape(PAGEVIEWS_BASE) + r"/per-article/zz\.wikipedia/.*"), json={"items": _step_items(100, 1000)})
    requests_mock.get(re.compile(re.escape(PAGEVIEWS_BASE) + r"/aggregate/.*"), json={"items": _broad_items(100000)})

    response = cli.run_analyze("Topic", ["aa", "zz"], start="2023-01", end="2024-12", session=session)

    saved = cache.load_run_data(response["run_id"])
    saved_lang_order = [r["lang"] for r in saved["results"]]

    assert saved_lang_order[0] == "zz"  # the real best grower, despite sorting last alphabetically
    assert saved_lang_order[1] == "aa"


# ---------------------------------------------------------------------------
# 16. `notes`: plain-language flags for anything the user must know, and a
#     shortened per-language `reason` (M6 fix round)
# ---------------------------------------------------------------------------


def test_shorten_reason_leaves_short_text_unchanged():
    assert cli._shorten_reason("short reason") == "short reason"


def test_shorten_reason_truncates_long_text_at_a_word_boundary():
    long_reason = "a " * 80 + "tail"  # 160+ chars, all single-char "words" plus spaces

    short = cli._shorten_reason(long_reason, max_chars=20)

    assert len(short) <= 21  # 20 chars + the ellipsis character
    assert short.endswith("…")
    assert not short[:-1].endswith(" ")  # cut at a word boundary, not mid-word or with trailing space+ellipsis


def test_response_reason_is_shortened_while_run_folder_keeps_the_full_reason(requests_mock, session):
    _mock_resolution(requests_mock, {"de": "Thema"})
    _mock_pageviews(requests_mock)  # default article_views=100 (< low-volume threshold) + a 12mo period -> a long, multi-clause base reason

    response = cli.run_analyze("Topic", ["de"], start="2024-01", end="2024-12", session=session)

    result = (response["ranked"] + response["unranked"])[0]
    saved_result = cache.load_run_data(response["run_id"])["results"][0]

    assert len(result["reason"]) <= cli.SHORT_REASON_MAX_CHARS + 1  # +1 for the ellipsis character
    assert len(saved_result["reason"]) > len(result["reason"])
    assert result["reason"] != saved_result["reason"]
    assert saved_result["reason"].startswith(result["reason"][: cli.SHORT_REASON_MAX_CHARS - 10])


def test_notes_flags_borrowed_yoy_baseline_for_a_short_period(requests_mock, session):
    _mock_resolution(requests_mock, {"de": "Thema", "nl": "Onderwerp"})
    _mock_pageviews(requests_mock)

    # 5 months -- under metrics.SHORT_PERIOD_MONTHS (24), so the YoY figure's
    # baseline is borrowed from extra history before the requested period.
    response = cli.run_analyze("Topic", ["de", "nl"], start="2026-04", end="2026-08", session=session, today=datetime(2026, 9, 24, tzinfo=timezone.utc))

    assert any("baseline" in note and "extra" in note for note in response["notes"])


def test_notes_omits_baseline_note_for_a_full_length_period(requests_mock, session):
    _mock_resolution(requests_mock, {"de": "Thema"})
    _mock_pageviews(requests_mock)

    response = cli.run_analyze("Topic", ["de"], start="2024-01", end="2025-12", session=session)  # 24 months, not "short"

    assert not any("baseline" in note for note in response["notes"])


def test_notes_flags_clamped_end_month(requests_mock, session):
    _mock_resolution(requests_mock, {"de": "Thema"})
    _mock_pageviews(requests_mock)
    today = datetime(2026, 9, 24, tzinfo=timezone.utc)

    response = cli.run_analyze("Topic", ["de"], start="2024-09", end="2026-09", session=session, today=today)

    assert response["period"]["end"] == "2026-08"
    assert any("clamp" in note.lower() for note in response["notes"])


def test_notes_flags_capped_redirects(requests_mock, session, monkeypatch):
    monkeypatch.setattr(cli, "MAX_REDIRECTS_PER_LANGUAGE", 3)
    _mock_resolution(requests_mock, {"de": "Thema"})
    redirect_titles = [f"Redirect{i}" for i in range(5)]
    requests_mock.get(
        _mediawiki_url("de.wikipedia"),
        json={"query": {"pages": {"1": {"title": "Thema", "redirects": [{"title": t} for t in redirect_titles]}}}},
    )
    _mock_pageviews(requests_mock)

    response = cli.run_analyze("Topic", ["de"], start="2024-01", end="2024-12", session=session)

    assert any("de" in note and "redirect" in note.lower() for note in response["notes"])
    # The full detail (exact counted-vs-total) still lives in the run folder,
    # unaffected by this note or by the response's shortened per-language reason.
    saved_result = cache.load_run_data(response["run_id"])["results"][0]
    assert "3 of 5" in saved_result["reason"]


def test_notes_flags_omitted_results_for_a_large_language_list(requests_mock, session):
    # 30 languages -- the scale M6 uses to actually exercise the omission
    # path against the new, tighter RESPONSE_BYTE_LIMIT (1800, down from 2048).
    langs = [f"l{i:02d}" for i in range(30)]
    _mock_resolution(requests_mock, {lang: f"Title {lang}" for lang in langs})
    _mock_pageviews(requests_mock)

    response = cli.run_analyze("Topic", langs, start="2024-01", end="2024-12", session=session)

    assert _json_bytes(response) <= cli.RESPONSE_BYTE_LIMIT
    assert response["omitted"] > 0
    kept = len(response["ranked"]) + len(response["unranked"])
    assert kept + response["omitted"] == len(langs)
    omission_notes = [note for note in response["notes"] if "omitted" in note.lower()]
    assert len(omission_notes) == 1
    assert str(response["omitted"]) in omission_notes[0]


def test_notes_is_empty_list_when_nothing_needs_flagging(requests_mock, session):
    _mock_resolution(requests_mock, {"de": "Thema"})
    _mock_pageviews(requests_mock)

    # A full 24-month period (not "short"), no clamping, no capped redirects,
    # not enough languages to trigger omission -- nothing here should need flagging.
    response = cli.run_analyze("Topic", ["de"], start="2024-01", end="2025-12", session=session)

    assert response["notes"] == []
