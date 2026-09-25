"""
CLI entrypoint for the wiki-interest skill: `analyze`, `report`, `resolve`.

Target caller is an agent (Claude Haiku 4.5, notes/requirements.md), not a
human typing commands -- every subcommand prints one compact JSON object to
stdout and nothing else. A basic question is exactly 2 calls: `analyze`
(fetches, computes metrics, caches full results, returns a <=2KB summary)
then `report` (reads the cached run, renders PDF/PNG/Markdown, returns file
paths + a one-line headline). `resolve` is optional -- a manual pre-check,
never required on the default path.

`--topic` is resolved as a title on `--source-lang`'s Wikipedia (default
"en"), matching notes/plan.md's own analyze example, which passes an
English topic string against non-English --langs. `--article lang:"Title"`
overrides resolution for specific languages (the title is taken as given,
not cross-checked against Wikidata) -- useful when auto cross-language
matching gets a language wrong, or --topic isn't a --source-lang title.

Per notes/requirements.md's decision, "a topic's interest" is the main
article PLUS its redirects, each fetched separately and summed by month --
so a redirect that draws real traffic under a different title isn't
silently under-counted.
"""

import argparse
import json
import os
import re
import sys

import requests

# cli.py is the top-level entry-point script (notes/plan.md's file layout:
# it sits next to requirements.txt and SKILL.md, not inside src/), so unlike
# the package modules it imports, Python won't put src/ on sys.path for it
# automatically -- add it ourselves, the same way tests/conftest.py does.
_SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from wiki_interest import api_client, cache, metrics, resolve as resolve_mod  # noqa: E402
from wiki_interest import report as report_mod  # noqa: E402

DEFAULT_SOURCE_LANG = "en"
RESPONSE_BYTE_LIMIT = 2048
# Only a range that reaches the current in-progress month needs a short
# cache TTL (metrics.is_current_month) -- see cache.get_cached_json's
# docstring for why a finalized month caches forever.
CURRENT_MONTH_MAX_AGE_SECONDS = 86400
OVERRIDE_MATCH_CONFIDENCE = "medium"
OVERRIDE_MATCH_REASON = "manually specified via --article, not cross-checked against Wikidata"
# A pathological article can have hundreds of redirects (notes/api.md: "United
# States" has 301) -- most carry negligible traffic, and fetching each one is
# its own HTTP call, so cap how many count toward a language's topic total.
MAX_REDIRECTS_PER_LANGUAGE = 50
RANK_BY_CHOICES = ["yoy_growth", "share_yoy_growth", "momentum_6mo"]


# ---------------------------------------------------------------------------
# Month arithmetic (small and local -- metrics._shift_month is private to
# that module, and reference-window math belongs to the CLI orchestration
# layer, not the stats layer)
# ---------------------------------------------------------------------------


def _shift_month(yyyymm: str, delta: int) -> str:
    year, month = (int(part) for part in yyyymm.split("-"))
    total = year * 12 + (month - 1) + delta
    year, month = divmod(total, 12)
    return f"{year:04d}-{month + 1:02d}"


def _month_count(start: str, end: str) -> int:
    year1, month1 = (int(part) for part in start.split("-"))
    year2, month2 = (int(part) for part in end.split("-"))
    return (year2 * 12 + month2) - (year1 * 12 + month1) + 1


def _month_range(start: str, end: str) -> list:
    months = []
    month = start
    while month <= end:
        months.append(month)
        month = _shift_month(month, 1)
    return months


_YYYYMM_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


def _validate_yyyymm(label: str, value: str) -> None:
    """
    Raise ValueError unless `value` is a canonical "YYYY-MM" string (4-digit
    year, zero-padded 2-digit month 01-12). Every ordering comparison in
    this module (start > end, the start <= m <= end filters used throughout
    run_analyze) is a plain string comparison that only agrees with
    chronological order for this exact canonical form -- e.g. the
    non-canonical "2024-9" sorts AFTER "2024-10" lexicographically ('9' >
    '1'), which would silently corrupt every downstream comparison without
    this check catching it first.
    """
    if not _YYYYMM_RE.match(value):
        raise ValueError(f'{label} ({value!r}) must be a canonical "YYYY-MM" value (4-digit year, 2-digit month 01-12)')


def _validate_and_normalize_period(start: str, end: str, today=None) -> tuple:
    """
    (start, end, note): raises ValueError for a non-canonical start/end or
    for start > end. If `end` is the current or a future month, clamps it
    to the last complete month instead of silently mixing a partial
    in-progress count into every growth calculation -- `note` explains the
    clamp so the agent isn't surprised the returned period differs from
    what it asked for; None if untouched.
    """
    _validate_yyyymm("--start", start)
    _validate_yyyymm("--end", end)
    if start > end:
        raise ValueError(f"--start ({start}) must not be after --end ({end})")
    last_complete = metrics.last_complete_month(today)
    if end <= last_complete:
        return start, end, None
    note = f"--end ({end}) is the current or a future month; clamped to the last complete month ({last_complete})"
    end = last_complete
    if start > end:
        raise ValueError(f"--start ({start}) is after the last complete month ({end}) once --end was clamped")
    return start, end, note


def _compute_period(start: str, end: str) -> dict:
    """
    The (start, end, reference window) block saved to the run and shown in
    the report's method note -- notes/plan.md: fetch enough months before
    `start` to reach >=36 months of total history, bounded by how far back
    the Pageviews API actually goes (api_client.DATA_START_YYYYMM).
    """
    requested_months = _month_count(start, end)
    target = metrics.REFERENCE_TARGET_MONTHS
    reference_months_before = max(0, target - requested_months)
    reference_start = _shift_month(start, -reference_months_before)
    if reference_start < api_client.DATA_START_YYYYMM:
        reference_start = api_client.DATA_START_YYYYMM
        reference_months_before = max(0, _month_count(reference_start, start) - 1)
    actual_history_months = _month_count(reference_start, end)
    return {
        "start": start,
        "end": end,
        "reference_start": reference_start,
        "reference_months_before": reference_months_before,
        "reference_reason": f"{target}mo total-history target - {requested_months}mo requested period",
        "actual_history_months": actual_history_months,
        "target_history_months": target,
        "used_reference_baseline_for_yoy": requested_months < metrics.SHORT_PERIOD_MONTHS,
    }


# ---------------------------------------------------------------------------
# Cached fetches
# ---------------------------------------------------------------------------


def _max_age_for_range(end: str, today=None):
    return CURRENT_MONTH_MAX_AGE_SECONDS if metrics.is_current_month(end, today) else None


def _fetch_article_monthly(session, project, article, start, end, agent, today=None) -> dict:
    key = cache.cache_key(fn="per_article", project=project, article=article, start=start, end=end, agent=agent)
    rows = cache.cached_call(
        key,
        lambda: api_client.get_pageviews_per_article(session, project, article, start, end, agent=agent),
        max_age_seconds=_max_age_for_range(end, today),
    )
    return metrics.to_series(rows)


def _fetch_site_monthly(session, project, start, end, agent, today=None) -> dict:
    key = cache.cache_key(fn="aggregate", project=project, start=start, end=end, agent=agent)
    rows = cache.cached_call(
        key,
        lambda: api_client.get_pageviews_aggregate(session, project, start, end, agent=agent),
        max_age_seconds=_max_age_for_range(end, today),
    )
    return metrics.to_series(rows)


def _resolve_cached(session, source_project: str, topic: str, target_langs: list) -> dict:
    key = cache.cache_key(fn="resolve_topic", project=source_project, title=topic, target_langs=sorted(target_langs))
    return cache.cached_call(key, lambda: resolve_mod.resolve_topic(session, source_project, topic, target_langs))


def _fetch_redirect_monthly_or_zero(session, project, article, start, end, agent, today=None) -> dict:
    """
    Like _fetch_article_monthly, but an in-range 404 (AmbiguousNoDataError)
    is treated as zero views for every month instead of raising -- one
    obscure alternate title having no recorded traffic isn't reason to lose
    the rest of the topic's real traffic counted under its main title, and
    the API's own 404 is ambiguous (zero views vs. not-yet-loaded) anyway,
    so "probably close to zero" is a reasonable reading for a redirect
    specifically (see api_client.AmbiguousNoDataError's docstring).
    """
    try:
        return _fetch_article_monthly(session, project, article, start, end, agent, today)
    except api_client.AmbiguousNoDataError:
        return {month: 0 for month in _month_range(start, end)}


def _topic_monthly(session, lang, title, start, end, agent, redirect_titles, today=None) -> dict:
    """
    Main article + up to MAX_REDIRECTS_PER_LANGUAGE known redirects, summed
    by month (see module docstring). The main article's own 404 is NOT
    caught here -- it propagates to the caller, which marks the whole
    language "no_data" rather than silently reporting zero interest for a
    possibly-just-not-loaded-yet article.
    """
    project = f"{lang}.wikipedia"
    combined = dict(_fetch_article_monthly(session, project, title, start, end, agent, today))
    for redirect_title in redirect_titles[:MAX_REDIRECTS_PER_LANGUAGE]:
        redirect_monthly = _fetch_redirect_monthly_or_zero(session, project, redirect_title, start, end, agent, today)
        for month, views in redirect_monthly.items():
            combined[month] = combined.get(month, 0) + views
    return combined


# ---------------------------------------------------------------------------
# analyze
# ---------------------------------------------------------------------------


def _strip_matching_outer_quotes(text: str) -> str:
    """
    'Przerywany post' -> unchanged; '"Przerywany post"' -> 'Przerywany post'.
    The --article help text shows the lang:"Title" form with literal
    quotes -- a caller building argv programmatically (this CLI's target
    user is an agent, not someone typing at a shell that would normally
    strip those quotes itself) can end up passing them through as literal
    characters. Only a genuine matching OUTER pair is removed; a lone or
    unmatched quote, or quotes elsewhere in the title, are left as-is.
    """
    if len(text) >= 2 and text[0] == '"' and text[-1] == '"':
        return text[1:-1]
    return text


def _parse_article_overrides(article_args) -> dict:
    """["pl:Przerywany post", 'pl:"Przerywany post"', ...] -> {"pl": "Przerywany post", ...}."""
    overrides = {}
    for raw in article_args or []:
        if ":" not in raw:
            raise ValueError(f"--article must be lang:\"Title\", got {raw!r}")
        lang, title = raw.split(":", 1)
        overrides[lang.strip()] = _strip_matching_outer_quotes(title.strip())
    return overrides


def _json_bytes(obj) -> int:
    return len(json.dumps(obj, separators=(",", ":")).encode("utf-8"))


def _cap_response(response: dict, byte_limit: int = RESPONSE_BYTE_LIMIT) -> dict:
    """
    Trim `ranked`, then (only if that alone still isn't enough) `unranked`,
    then (only if THAT still isn't enough -- e.g. a huge `missing` list on
    its own pushes the response over budget) `missing`, from each one's
    low-priority end until the response fits `byte_limit` -- notes/plan.md:
    report the top N plus how many were omitted, never silently truncate
    JSON mid-object or return something over budget.
    """
    if _json_bytes(response) <= byte_limit:
        return response
    response = dict(response)
    ranked = list(response["ranked"])
    unranked = list(response["unranked"])
    missing = list(response.get("missing", []))
    omitted = 0

    def _still_over_budget() -> bool:
        return _json_bytes({**response, "ranked": ranked, "unranked": unranked, "missing": missing, "omitted": omitted}) > byte_limit

    while _still_over_budget() and ranked:
        ranked.pop()
        omitted += 1
    while _still_over_budget() and unranked:
        unranked.pop()
        omitted += 1
    while _still_over_budget() and missing:
        missing.pop()
        omitted += 1
    response["ranked"] = ranked
    response["unranked"] = unranked
    response["missing"] = missing
    response["omitted"] = omitted
    return response


def run_analyze(topic, langs, start=None, end=None, agent="user", rank_by="yoy_growth", article_overrides=None, source_lang=DEFAULT_SOURCE_LANG, session=None, today=None) -> dict:
    """
    Resolve every language internally (cached), fetch+compute metrics,
    generate a server-side run_id, save the full result to that run's cache
    folder, and return a <=2KB summary. See the module docstring for
    `--topic`/`--source-lang` and the redirect-summing rule.
    """
    session = session or api_client.build_session()
    source_project = f"{source_lang}.wikipedia"
    if start is None and end is None:
        start, end = metrics.default_period(today)
    elif start is None or end is None:
        # A partial pair is ambiguous, not a request for the default range --
        # silently replacing BOTH with the default would discard whichever
        # one the caller DID specify without any indication that happened.
        raise ValueError("--start and --end must both be given, or both omitted (to use the default 24-month period)")
    start, end, period_note = _validate_and_normalize_period(start, end, today)
    overrides = article_overrides or {}
    period = _compute_period(start, end)
    reference_start = period["reference_start"]

    auto_langs = [lang for lang in langs if lang not in overrides]
    editions, redirects_by_lang, missing = dict(overrides), {}, []
    match_confidence, match_reason = "high", "no automatic resolution needed (all languages manually specified)"
    if auto_langs:
        resolved = _resolve_cached(session, source_project, topic, auto_langs)
        editions.update(resolved["editions"])
        redirects_by_lang.update(resolved["redirects"])
        missing.extend(resolved["missing"])
        match_confidence, match_reason = resolved["match_confidence"], resolved["reason"]
    for lang, title in overrides.items():
        project = f"{lang}.wikipedia"
        key = cache.cache_key(fn="redirects", project=project, title=title)
        redirects_by_lang[lang] = cache.cached_call(key, lambda p=project, t=title: api_client.get_redirects(session, p, t))

    results, series, spike_months_out, seasonal_months_out = [], {}, {}, {}
    for lang in langs:
        if lang not in editions:
            if lang not in missing:
                missing.append(lang)
            continue
        title = editions[lang]
        project = f"{lang}.wikipedia"
        all_redirects = redirects_by_lang.get(lang, [])
        redirects_capped = len(all_redirects) > MAX_REDIRECTS_PER_LANGUAGE
        try:
            article_monthly = _topic_monthly(session, lang, title, reference_start, end, agent, all_redirects, today)
        except api_client.AmbiguousNoDataError:
            # The MAIN article's own 404 (not a redirect's -- see
            # _topic_monthly) -- mark only this language "no_data" and move
            # on, rather than aborting every other requested language.
            results.append(
                {
                    "lang": lang,
                    "title": title,
                    "yoy_growth": None,
                    "yoy_growth_flag": "no_data",
                    "share_yoy_growth": None,
                    "momentum_6mo": None,
                    "spikes_removed": 0,
                    "seasonal_peaks_kept": 0,
                    "significant": False,
                    "trend_test": None,
                    "confidence": "low",
                    "reason": "no pageview data returned for the main article in the requested range (ambiguous 404 -- may mean zero views or data not yet loaded, not proof there is none)",
                }
            )
            continue
        site_monthly = _fetch_site_monthly(session, project, reference_start, end, agent, today)

        spike_months_all, seasonal_months_all = metrics.classify_spikes_and_seasonal(article_monthly)
        spike_free = metrics.spike_free_series(article_monthly, spike_months_all)

        yoy = metrics.compute_yoy_growth(spike_free, start, end, today)
        share_yoy = metrics.compute_share_yoy_growth(spike_free, site_monthly, end, today)
        momentum = metrics.compute_momentum_6mo(spike_free, end, today)
        trend = metrics.compute_trend_significance(spike_free, site_monthly, start, end, today)

        lang_match_confidence = OVERRIDE_MATCH_CONFIDENCE if lang in overrides else match_confidence
        lang_match_reason = OVERRIDE_MATCH_REASON if lang in overrides else match_reason
        confidence, reason = metrics.compute_confidence(
            yoy_growth_value=yoy["value"],
            yoy_growth_flag=yoy["flag"],
            share_yoy_growth_value=share_yoy["value"],
            significant=trend["significant"],
            history_months=period["actual_history_months"],
            match_confidence=lang_match_confidence,
            significance_insufficient_data=trend["insufficient_data"],
        )
        if lang_match_confidence != "high":
            reason = f"{reason}; {lang_match_reason}"
        if redirects_capped:
            reason = f"{reason}; only the first {MAX_REDIRECTS_PER_LANGUAGE} of {len(all_redirects)} redirects were counted"

        period_spikes = sorted(m for m in spike_months_all if start <= m <= end)
        period_seasonal = sorted(m for m in seasonal_months_all if start <= m <= end)

        results.append(
            {
                "lang": lang,
                "title": title,
                "yoy_growth": yoy["value"],
                "yoy_growth_flag": yoy["flag"],
                "share_yoy_growth": share_yoy["value"],
                "momentum_6mo": momentum["value"],
                "spikes_removed": len(period_spikes),
                "seasonal_peaks_kept": len(period_seasonal),
                "significant": trend["significant"],
                "trend_test": "seasonal_mann_kendall",
                "confidence": confidence,
                "reason": reason,
            }
        )
        series[lang] = {m: v for m, v in article_monthly.items() if start <= m <= end}
        spike_months_out[lang] = period_spikes
        seasonal_months_out[lang] = period_seasonal

    period_for_run = {k: v for k, v in period.items() if k != "reference_start"}
    requested_run_id = cache.generate_run_id()
    run_data = {
        "run_id": requested_run_id,
        "topic": topic,
        "period": period_for_run,
        "series": series,
        "spike_months": spike_months_out,
        "seasonal_months": seasonal_months_out,
        "results": results,
        "missing": missing,
    }
    run_id = cache.save_run_data(requested_run_id, run_data)
    if run_id != requested_run_id:
        # save_run_data regenerated the id after a collision (astronomically
        # rare, but its 24-bit suffix is refreshed only daily -- see its
        # docstring) -- the data we just wrote still has the stale id baked
        # into its own "run_id" field, so re-save under the id that was
        # actually reserved. That id's directory is freshly created by the
        # retry above, so this second write can't itself collide again.
        run_data["run_id"] = run_id
        cache.save_run_data(run_id, run_data)

    ranked, unranked = metrics.rank_series(results, rank_by)
    response_period = {"start": start, "end": end, "reference_months_before": period["reference_months_before"], "reference_reason": period["reference_reason"]}
    if period_note:
        response_period["note"] = period_note
    response = {
        "run_id": run_id,
        "period": response_period,
        "ranked": ranked,
        "unranked": unranked,
        "missing": missing,
        "omitted": 0,
    }
    return _cap_response(response)


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------


def _build_headline(run_data: dict) -> str:
    results = run_data.get("results", [])
    with_growth = [r for r in results if r.get("yoy_growth") is not None]
    topic = run_data.get("topic", "this topic")
    if not with_growth:
        return f'No measurable growth trend for "{topic}" in any requested language.'
    ranked = sorted(with_growth, key=lambda r: r["yoy_growth"], reverse=True)
    top = ranked[0]
    top_direction = "growing" if top["yoy_growth"] > 0 else "declining"
    if len(ranked) == 1:
        suffix = "" if top.get("significant") else " (trend not significant)"
        return f"{top['lang'].upper()} interest {top_direction}{suffix}."
    bottom = ranked[-1]
    suffix = "" if bottom.get("significant") else f"; {bottom['lang'].upper()} trend not significant"
    return f"{top['lang'].upper()} interest {top_direction} faster than {bottom['lang'].upper()}{suffix}."


def run_report(run_id: str, formats=None, summary=None) -> dict:
    """
    Read the cached run (no refetch) and render the requested report
    formats. `summary`, if given, overrides report.py's auto-generated
    plain-language summary for just this render -- a shallow copy, so it's
    never written back to the cached run.json (a later `report` call
    without --summary reverts to the default).
    """
    run_data = cache.load_run_data(run_id)
    if summary:
        run_data = {**run_data, "summary": summary}
    output_dir = cache.run_dir(run_id)
    all_paths = report_mod.build_report(run_data, output_dir)
    wanted = formats or list(all_paths.keys())
    files = {fmt: all_paths[fmt] for fmt in wanted if fmt in all_paths}
    return {"run_id": run_id, "files": files, "headline": _build_headline(run_data)}


# ---------------------------------------------------------------------------
# resolve
# ---------------------------------------------------------------------------


def run_resolve(article: str, langs: list, source_lang=DEFAULT_SOURCE_LANG, session=None) -> dict:
    session = session or api_client.build_session()
    return resolve_mod.resolve_topic(session, f"{source_lang}.wikipedia", article, langs)


# ---------------------------------------------------------------------------
# argv wiring
# ---------------------------------------------------------------------------


def _split_langs(value: str) -> list:
    return [lang.strip() for lang in value.split(",") if lang.strip()]


class _RaisingArgumentParser(argparse.ArgumentParser):
    """
    argparse's default error() prints a usage message to stderr and calls
    sys.exit(2) -- SystemExit, which doesn't inherit from Exception, so it
    passes straight through main()'s `except Exception` untouched instead of
    becoming the single {"error", "hint"} JSON line on stdout every other
    failure mode (network, validation, ...) produces. A bad --rank-by or a
    missing required --topic should get that same treatment, since this
    CLI's caller is an agent parsing JSON, not a human reading a usage
    string. Only error() is overridden -- -h/--help goes through exit(),
    which is untouched, so --help still prints help text and exits 0
    normally.
    """

    def error(self, message):
        raise ValueError(message)


def build_parser() -> argparse.ArgumentParser:
    # add_subparsers() defaults each subparser to type(self), so analyze/
    # report/resolve automatically raise the same way as the top-level parser.
    parser = _RaisingArgumentParser(prog="wiki-interest", description="Wikipedia cross-language interest analysis.")
    sub = parser.add_subparsers(dest="command", required=True)

    analyze = sub.add_parser("analyze", help="Resolve, fetch, and compute metrics for a topic across languages.")
    analyze.add_argument("--topic", required=True, help='A --source-lang Wikipedia article title, e.g. "Intermittent fasting".')
    analyze.add_argument("--langs", required=True, type=_split_langs, help="Comma-separated language codes, e.g. pl,cs.")
    analyze.add_argument("--source-lang", default=DEFAULT_SOURCE_LANG, dest="source_lang", help='Language --topic itself is written in (default "en").')
    analyze.add_argument("--start", default=None, help="YYYY-MM. Omit with --end to default to the last 24 complete months.")
    analyze.add_argument("--end", default=None, help="YYYY-MM. Omit with --start to default to the last 24 complete months. The current/a future month is clamped to the last complete one.")
    # Limited to the two values notes/api.md actually verified against the
    # live API ("user" is this skill's own default; "all-agents" is the
    # only other value exercised there) -- not the wider Wikimedia enum
    # (spider/automated/etc.) that was never curl-verified for this project.
    analyze.add_argument("--agent", default="user", choices=["user", "all-agents"], help='Traffic filter (default "user": excludes known bots and automated traffic).')
    analyze.add_argument("--rank-by", default="yoy_growth", dest="rank_by", choices=RANK_BY_CHOICES, help="Result field to sort by (default: yoy_growth, already spike-free).")
    analyze.add_argument("--article", action="append", default=[], metavar='lang:"Title"', help='Override resolution for one language, e.g. pl:"Przerywany post". Repeatable.')
    # No --run-id here, deliberately: run_id is always server-generated (notes/plan.md) --
    # a caller-supplied one on `analyze` isn't a recognized argument at all.

    report = sub.add_parser("report", help="Render PDF/PNG/Markdown for a previously analyzed run.")
    report.add_argument("--run-id", required=True, dest="run_id")
    report.add_argument("--format", default=None, type=_split_langs, help="Comma-separated subset of pdf,png,md (default: all three).")
    report.add_argument("--summary", default=None, help="Plain-language summary text to use instead of the auto-generated one, for this render only.")

    resolve = sub.add_parser("resolve", help="Manual cross-language + redirect + match-confidence check (optional, not on the default path).")
    resolve.add_argument("--article", required=True, help='Title on --source-lang Wikipedia, e.g. "Astronomy".')
    resolve.add_argument("--langs", required=True, type=_split_langs)
    resolve.add_argument("--source-lang", default=DEFAULT_SOURCE_LANG, dest="source_lang", help='Language --article itself is written in (default "en").')

    return parser


# Exception type -> a short, actionable hint for the agent calling this CLI,
# since a raw exception message alone often doesn't say what to DO about it.
_ERROR_HINTS = (
    (api_client.ContactNotConfiguredError, "set the WIKITREND_CONTACT environment variable to a URL/email Wikimedia can use to reach you, then retry"),
    (api_client.OutOfRangeError, "use a --start on or after 2015-07 (the Pageviews API has no earlier data)"),
    (api_client.AmbiguousNoDataError, "the article may genuinely have no data yet, or Wikimedia hasn't loaded it -- retry later, or check the title with `resolve`"),
    (ValueError, "check the argument that was rejected and correct it, then retry"),
    (FileNotFoundError, "the run-id wasn't found in the cache -- run `analyze` first, or double-check the run-id"),
    (requests.exceptions.Timeout, "the request to Wikimedia timed out -- retry, possibly after a short wait"),
    (requests.exceptions.ConnectionError, "could not reach Wikimedia -- check network connectivity and retry"),
    (requests.exceptions.RequestException, "a network request to Wikimedia failed -- retry, possibly after a short wait"),
)


def _error_hint(exc: Exception) -> str:
    for exc_type, hint in _ERROR_HINTS:
        if isinstance(exc, exc_type):
            return hint
    return "retry; if this keeps happening, check the CLI arguments and the run-id/cache state"


def main(argv=None) -> int:
    parser = build_parser()

    try:
        args = parser.parse_args(argv)
        if args.command == "analyze":
            overrides = _parse_article_overrides(args.article)
            result = run_analyze(
                args.topic,
                args.langs,
                start=args.start,
                end=args.end,
                agent=args.agent,
                rank_by=args.rank_by,
                article_overrides=overrides,
                source_lang=args.source_lang,
            )
        elif args.command == "report":
            result = run_report(args.run_id, formats=args.format, summary=args.summary)
        elif args.command == "resolve":
            result = run_resolve(args.article, args.langs, source_lang=args.source_lang)
        else:  # pragma: no cover -- argparse's `required=True` on the subparsers already excludes this
            parser.error("unknown command")
            return 2
    except Exception as exc:
        # Every failure mode -- network, validation, a bug we didn't
        # anticipate -- must come back as one short JSON line the calling
        # agent can parse, never a Python traceback on stdout.
        print(json.dumps({"error": str(exc) or exc.__class__.__name__, "hint": _error_hint(exc)}, separators=(",", ":")))
        return 1

    print(json.dumps(result, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
