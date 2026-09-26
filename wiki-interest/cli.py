"""
CLI entrypoint for the wiki-interest skill: `analyze`, `report`, `resolve`.

Target caller is an agent (Claude Haiku 4.5, notes/requirements.md), not a
human typing commands -- every subcommand prints one compact JSON object to
stdout and nothing else. A basic question is exactly 2 calls: `analyze`
(fetches, computes metrics, caches full results, returns a compact,
size-capped JSON summary)
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
from datetime import datetime, timezone

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
RESPONSE_BYTE_LIMIT = 1800
# Per-language `reason` strings can run long once compute_confidence joins
# several factors with "; " -- the full text is always saved to the run
# folder (report.py reads it from there), only the JSON response shortens
# it, so a many-language request doesn't burn most of its byte budget on
# reason prose instead of actual per-language numbers.
SHORT_REASON_MAX_CHARS = 100
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
RANK_BY_CHOICES = ["yoy_growth", "share_yoy_growth", "momentum_6mo", "avg_monthly_views"]


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


_YYYYMM_RE = re.compile(r"\A[0-9]{4}-(0[1-9]|1[0-2])\Z")
_YYYYMMDD_RE = re.compile(r"\A([0-9]{4}-(?:0[1-9]|1[0-2]))-(?:0[1-9]|[12][0-9]|3[01])\Z")


def _normalize_month_input(value: str) -> str:
    """
    "2026-04-01" -> "2026-04"; "2026-04" -> "2026-04" (unchanged).

    --start/--end only ever mean a MONTH, but an agent reasoning about "the
    last 6 months" naturally thinks in calendar dates and may pass a full
    YYYY-MM-DD (real observed Haiku behavior, M6 eval 6: it guessed
    "2026-04-01", got the CLI's own rejection, and had to retry) -- the day
    component carries no information either --start or --end ever use, so
    silently dropping it here is strictly more useful than forcing that
    retry. Anything that isn't a full YYYY-MM-DD is passed through
    unchanged for _validate_yyyymm to accept or reject as usual -- this
    never widens what a plain "YYYY-MM" input already accepts.
    """
    match = _YYYYMMDD_RE.match(value)
    return match.group(1) if match else value


def _validate_yyyymm(label: str, value: str) -> None:
    """
    Raise ValueError unless `value` is a canonical "YYYY-MM" string (4-digit
    year, zero-padded 2-digit month 01-12). Every ordering comparison in
    this module (start > end, the start <= m <= end filters used throughout
    run_analyze) is a plain string comparison that only agrees with
    chronological order for this exact canonical form -- e.g. the
    non-canonical "2024-9" sorts AFTER "2024-10" lexicographically ('9' >
    '1'), which would silently corrupt every downstream comparison without
    this check catching it first. Anchored with \\A/\\Z (not ^/$, which in
    Python matches just before a trailing newline too -- "2024-01\\n" would
    otherwise slip through) and [0-9] (not \\d, which also accepts non-ASCII
    decimal digits) to actually enforce "exactly this and nothing else".
    """
    if not _YYYYMM_RE.match(value):
        raise ValueError(f'{label} ({value!r}) must be a canonical "YYYY-MM" value (4-digit year, 2-digit month 01-12)')


def _validate_and_normalize_period(start: str, end: str, today=None) -> tuple:
    """
    (start, end, note): raises ValueError for a non-canonical start/end or
    for start > end. `start`/`end` are first passed through
    _normalize_month_input, so a full "YYYY-MM-DD" is accepted and reduced
    to its month. If `end` is the current or a future month, clamps it
    to the last complete month instead of silently mixing a partial
    in-progress count into every growth calculation -- `note` explains the
    clamp so the agent isn't surprised the returned period differs from
    what it asked for; None if untouched.
    """
    start = _normalize_month_input(start)
    end = _normalize_month_input(end)
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


# Wikimedia's monthly pageview aggregates for a month that just ended
# aren't necessarily fully loaded on day 1 of the next month -- real-world
# pipeline lag means a short window past month-end can still plausibly
# 404 for "not loaded yet" rather than genuinely zero.
POST_MONTH_END_GRACE_DAYS = 7


def _is_recent_range(end: str, today=None) -> bool:
    """
    "Recent" per api_client.AmbiguousNoDataError's documented policy (an
    in-range 404 should stay retryable for a recent period, since Wikimedia
    may simply not have finished loading it yet, vs. treated as likely-zero
    for an older, fully-in-range one). Covers the current (in-progress)
    month AND the month that just completed, for a short grace window into
    the following month -- by the time this is reached through run_analyze,
    `end` is always already clamped to a complete month
    (_validate_and_normalize_period), so checking only is_current_month
    (as this used to) could never actually be true there, making the
    "stays retryable" branch unreachable in practice.
    """
    moment = today or datetime.now(timezone.utc)
    if metrics.is_current_month(end, moment):
        return True
    return end == metrics.last_complete_month(moment) and moment.day <= POST_MONTH_END_GRACE_DAYS


def _fetch_article_monthly(session, project, article, start, end, agent, today=None) -> dict:
    key = cache.cache_key(fn="per_article", project=project, article=article, start=start, end=end, agent=agent)
    rows = cache.cached_call(
        key,
        lambda: api_client.get_pageviews_per_article(session, project, article, start, end, agent=agent),
        max_age_seconds=_max_age_for_range(end, today),
    )
    return metrics.to_series(rows)


def _fetch_site_monthly(session, project, start, end, agent, today=None) -> dict:
    """
    Site-wide totals, used to normalize an article's share of traffic. An
    in-range 404 (AmbiguousNoDataError) follows the shared date-aware
    policy (_is_recent_range): for a recent range it's left to propagate --
    the caller (run_analyze) marks that language "no_data" rather than this
    function fabricating a site total that may simply be pending, not
    genuinely zero (a real wiki's site-wide total is essentially never
    actually zero, unlike a specific article or redirect). For an older,
    fully-in-range range it's treated as zero, same as
    _fetch_redirect_monthly_or_zero.
    """
    key = cache.cache_key(fn="aggregate", project=project, start=start, end=end, agent=agent)
    try:
        rows = cache.cached_call(
            key,
            lambda: api_client.get_pageviews_aggregate(session, project, start, end, agent=agent),
            max_age_seconds=_max_age_for_range(end, today),
        )
    except api_client.AmbiguousNoDataError:
        if _is_recent_range(end, today):
            raise
        return {month: 0 for month in _month_range(start, end)}
    return metrics.to_series(rows)


def _resolve_cached(session, source_project: str, topic: str, target_langs: list) -> dict:
    key = cache.cache_key(fn="resolve_topic", project=source_project, title=topic, target_langs=sorted(target_langs))
    return cache.cached_call(key, lambda: resolve_mod.resolve_topic(session, source_project, topic, target_langs))


def _fetch_redirect_monthly_or_zero(session, project, article, start, end, agent, today=None) -> dict:
    """
    Like _fetch_article_monthly, but an in-range 404 (AmbiguousNoDataError)
    follows the shared date-aware policy (_is_recent_range) instead of
    always raising: for an older, fully-in-range range it's treated as zero
    for every month -- one obscure alternate title having no recorded
    traffic isn't reason to lose the rest of the topic's real traffic
    counted under its main title. For a RECENT range it's left to
    propagate instead (the caller marks the whole language "no_data"),
    since Wikimedia's own 404 there is ambiguous -- it may mean genuinely
    zero views, or just that the data isn't loaded yet -- and silently
    reporting zero would misrepresent traffic that simply hasn't landed.
    """
    try:
        return _fetch_article_monthly(session, project, article, start, end, agent, today)
    except api_client.AmbiguousNoDataError:
        if _is_recent_range(end, today):
            raise
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


def _shorten_reason(reason: str, max_chars: int = SHORT_REASON_MAX_CHARS) -> str:
    """
    The full `reason` (unchanged, saved to the run folder via run_data) cut
    to a plain-language summary for the JSON response -- at the last whole
    word within `max_chars`, not mid-word, so a many-language response
    doesn't burn its byte budget on reason prose, and doesn't produce a
    garbled partial word either.
    """
    if len(reason) <= max_chars:
        return reason
    truncated = reason[:max_chars].rsplit(" ", 1)[0]
    return f"{truncated}…"


def _for_response(entries: list, base_reason_by_lang: dict = None) -> list:
    """
    Shallow copies of `entries` with a shortened `reason` -- never mutates
    the originals (still referenced by run_data, already saved). Shortens
    only the BASE confidence reason (`base_reason_by_lang[lang]`, the value
    metrics.compute_confidence returned before run_analyze appended any
    match-confidence/redirect-cap addendum) and keeps whatever was appended
    after it intact -- so a long base reason never crowds out an addendum
    that matters (e.g. "manually specified via --article") just because it
    happened to come last. Falls back to shortening the whole `reason`
    verbatim when there's no tracked base for that language (e.g. the
    no_data early-return case, which never has an addendum anyway).
    """
    base_reason_by_lang = base_reason_by_lang or {}
    shortened = []
    for entry in entries:
        if "reason" not in entry:
            shortened.append(entry)
            continue
        base = base_reason_by_lang.get(entry.get("lang"))
        if base is not None and entry["reason"].startswith(base):
            addendum = entry["reason"][len(base) :]
            reason = _shorten_reason(base) + addendum
        else:
            reason = _shorten_reason(entry["reason"])
        shortened.append(dict(entry, reason=reason))
    return shortened


def _omitted_note_text(omitted: int) -> str:
    return f"{omitted} result(s) were omitted from this response to stay under the size limit -- see the saved run folder, or narrow --langs, for full detail."


def _trim_notes_to_fit(response: dict, byte_limit: int, protect_last: bool = False) -> dict:
    """
    Last-resort safety net, called after result entries have already been
    trimmed: `notes` itself has no size cap of its own -- e.g. dozens of
    individually-`missing` languages each contribute their own `--article`
    retry sentence (notes/plan.md never anticipated a list that scales with
    --langs) -- so ranked/unranked/missing all being empty doesn't
    guarantee the response fits. Drops notes from the low-priority (end)
    side until it does, or until only the protected one remains.
    `protect_last=True` (used once the omission note itself has been
    appended) keeps THAT note in place and trims the ones before it
    instead -- in a degenerate case this bad, knowing data was cut at all
    matters more than any one specific retry hint.
    """
    if _json_bytes(response) <= byte_limit:
        return response
    response = dict(response)
    notes = list(response.get("notes", []))
    protected = notes[-1:] if protect_last and notes else []
    trimmable = notes[:-1] if protect_last and notes else notes
    while trimmable and _json_bytes({**response, "notes": trimmable + protected}) > byte_limit:
        trimmable.pop()
    response["notes"] = trimmable + protected
    return response


def _add_omitted_note(response: dict, byte_limit: int = RESPONSE_BYTE_LIMIT) -> dict:
    """
    Adds a plain-language note about `_cap_response`'s own omitted count --
    but the note itself costs bytes that weren't accounted for when that
    count was computed, so if adding it pushes the response back over
    budget, trim one more entry (same low-priority order as _cap_response:
    ranked, then unranked, then missing) and keep the note's count in sync,
    repeating until it fits again. A no-op when nothing was omitted. Falls
    back to _trim_notes_to_fit (protecting this note) if ranked/unranked/
    missing are already empty and the response is still over budget.
    """
    if response.get("omitted", 0) <= 0:
        return response
    response = dict(response)
    response["ranked"] = list(response["ranked"])
    response["unranked"] = list(response["unranked"])
    response["missing"] = list(response.get("missing", []))
    response["notes"] = list(response.get("notes", []))
    response["notes"].append(_omitted_note_text(response["omitted"]))

    def _fits() -> bool:
        return _json_bytes(response) <= byte_limit

    while not _fits() and response["ranked"]:
        response["ranked"].pop()
        response["omitted"] += 1
        response["notes"][-1] = _omitted_note_text(response["omitted"])
    while not _fits() and response["unranked"]:
        response["unranked"].pop()
        response["omitted"] += 1
        response["notes"][-1] = _omitted_note_text(response["omitted"])
    while not _fits() and response["missing"]:
        response["missing"].pop()
        response["omitted"] += 1
        response["notes"][-1] = _omitted_note_text(response["omitted"])
    return _trim_notes_to_fit(response, byte_limit, protect_last=True)


def _cap_response(response: dict, byte_limit: int = RESPONSE_BYTE_LIMIT) -> dict:
    """
    Trim `ranked`, then (only if that alone still isn't enough) `unranked`,
    then (only if THAT still isn't enough -- e.g. a huge `missing` list on
    its own pushes the response over budget) `missing`, from each one's
    low-priority end until the response fits `byte_limit` -- notes/plan.md:
    report the top N plus how many were omitted, never silently truncate
    JSON mid-object or return something over budget.

    Trimming those three lists isn't actually sufficient on its own: `notes`
    can independently grow past `byte_limit` all by itself (e.g. many
    individually-`missing` languages each carry their own `--article`
    retry sentence), in which case ranked/unranked/missing all end up
    empty and the response is STILL over budget -- so the final step here
    is always _trim_notes_to_fit as a guaranteed catch-all, not just a
    fallback for an unlikely edge case.
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
    return _trim_notes_to_fit(response, byte_limit)


def run_analyze(topic, langs, start=None, end=None, agent="user", rank_by="yoy_growth", article_overrides=None, source_lang=DEFAULT_SOURCE_LANG, session=None, today=None) -> dict:
    """
    Resolve every language internally (cached), fetch+compute metrics,
    generate a server-side run_id, save the full result to that run's cache
    folder, and return a compact, size-capped JSON summary (RESPONSE_BYTE_LIMIT) --
    `notes` on the response flags anything the caller needs to relay to the
    user that isn't obvious from the numbers alone: a borrowed YoY baseline,
    a clamped end month, capped redirects, low-volume or not-statistically-
    significant languages, a missing article's `--article lang:"Title"`
    retry, and an omitted-results count. Each per-language `reason` is a
    shortened summary of the full reason saved to the run folder. See the
    module docstring for `--topic`/`--source-lang` and the redirect-summing
    rule.
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
    redirect_capped_langs = []
    base_reason_by_lang = {}
    low_volume_langs = []
    not_significant_langs = []
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
            site_monthly = _fetch_site_monthly(session, project, reference_start, end, agent, today)
        except api_client.AmbiguousNoDataError as exc:
            # Either the main article's own 404 (see _topic_monthly -- a
            # redirect's 404 doesn't reach here, it's absorbed as zero
            # inside _topic_monthly) or the site aggregate's, for a RECENT
            # range only (see _fetch_site_monthly -- an older range is
            # already handled as zero in there and never raises). Either
            # way: mark only this language "no_data" and move on, rather
            # than aborting every other requested language.
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
                    "reason": f"no pageview data returned in the requested range (ambiguous 404 -- may mean zero views or data not yet loaded, not proof there is none): {exc}",
                }
            )
            continue

        spike_months_all, seasonal_months_all = metrics.classify_spikes_and_seasonal(article_monthly)
        spike_free = metrics.spike_free_series(article_monthly, spike_months_all)

        yoy = metrics.compute_yoy_growth(spike_free, start, end, today)
        share_yoy = metrics.compute_share_yoy_growth(spike_free, site_monthly, end, today)
        momentum = metrics.compute_momentum_6mo(spike_free, end, today)
        trend = metrics.compute_trend_significance(spike_free, site_monthly, start, end, today)
        avg_views = metrics.compute_average_monthly_views(spike_free, start, end, today)

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
            avg_monthly_views=avg_views["period_avg"],
        )
        # Tracked separately from `reason` below, which goes on to accumulate
        # the match-confidence/redirect-cap addenda -- _for_response shortens
        # THIS base reason for the JSON response, not the combined one, so a
        # long base reason never crowds out an addendum that matters (e.g. a
        # manually-overridden match) just because it happened to come last.
        base_reason_by_lang[lang] = reason
        if lang_match_confidence != "high":
            reason = f"{reason}; {lang_match_reason}"
        if redirects_capped:
            reason = f"{reason}; only the first {MAX_REDIRECTS_PER_LANGUAGE} of {len(all_redirects)} redirects were counted"
            redirect_capped_langs.append(lang)
        # Tracked for top-level `notes` -- these two confidence factors are
        # easy to miss buried inside a per-language `reason` string
        # (especially once _for_response shortens it), so they also get a
        # blunt, hard-to-miss top-level sentence naming every affected
        # language, on top of (not instead of) the per-language reason.
        if avg_views["period_avg"] is not None and avg_views["period_avg"] < metrics.AVG_VIEWS_LOW_THRESHOLD:
            low_volume_langs.append(lang)
        if not trend["significant"]:
            not_significant_langs.append(lang)

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
                "avg_monthly_views": round(avg_views["period_avg"]) if avg_views["period_avg"] is not None else None,
                "last12_avg_monthly_views": round(avg_views["last12_avg"]) if avg_views["last12_avg"] is not None else None,
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

    # rank_series's order (best-ranked first, within "ranked"; "unranked"
    # after) is what the agent already sees in the JSON response below --
    # save results in that SAME order, not raw request-iteration order, so
    # report.py's table/chart rows (which just take results as given) show
    # the actual ranking too, not an arbitrary one that happens to match
    # whatever order --langs was typed in.
    ranked, unranked = metrics.rank_series(results, rank_by)
    period_for_run = {k: v for k, v in period.items() if k != "reference_start"}
    requested_run_id = cache.generate_run_id()
    run_data = {
        "run_id": requested_run_id,
        "topic": topic,
        "period": period_for_run,
        "series": series,
        "spike_months": spike_months_out,
        "seasonal_months": seasonal_months_out,
        "results": ranked + unranked,
        "rank_by": rank_by,
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

    response_period = {"start": start, "end": end, "reference_months_before": period["reference_months_before"], "reference_reason": period["reference_reason"]}
    if period_note:
        response_period["note"] = period_note

    notes = []
    if period_note:
        notes.append(period_note)
    if period["used_reference_baseline_for_yoy"]:
        notes.append(
            f"The year-over-year comparison's baseline for this period includes "
            f"{period['reference_months_before']} extra month(s) of history before "
            f"{start}, since the requested period is under {metrics.SHORT_PERIOD_MONTHS} months."
        )
    if redirect_capped_langs:
        notes.append(f"Only the first {MAX_REDIRECTS_PER_LANGUAGE} redirects were counted for: {', '.join(sorted(redirect_capped_langs))}.")
    if low_volume_langs:
        notes.append(f"Average interest is low for: {', '.join(sorted(low_volume_langs))} -- their growth percentages are noisier and less reliable than a higher-traffic result's.")
    if not_significant_langs:
        notes.append(f"The trend is not statistically significant for: {', '.join(sorted(not_significant_langs))} -- treat their growth direction as provisional, not a confirmed trend.")
    for lang in missing:
        notes.append(f'No article found for this topic on {lang} Wikipedia -- it may exist under another title; retry with --article {lang}:"<correct title>".')

    response = {
        "run_id": run_id,
        "period": response_period,
        "ranked": _for_response(ranked, base_reason_by_lang),
        "unranked": _for_response(unranked, base_reason_by_lang),
        "missing": missing,
        "omitted": 0,
        "notes": notes,
    }
    response = _cap_response(response)
    return _add_omitted_note(response)


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
    # When even the best performer is flat or declining, the more useful
    # headline subject is the one declining THE MOST (ranked[-1], the most
    # negative) -- ranked[0] there is only the LEAST negative, and calling
    # it the one "declining faster than" the true worst performer would
    # have the comparison backwards.
    if ranked[0]["yoy_growth"] > 0:
        top, bottom = ranked[0], ranked[-1]
    else:
        top, bottom = ranked[-1], ranked[0]
    if top["yoy_growth"] == 0:
        top_direction = "flat"
    else:
        top_direction = "growing" if top["yoy_growth"] > 0 else "declining"
    if len(ranked) == 1:
        suffix = "" if top.get("significant") else " (trend not significant)"
        return f"{top['lang'].upper()} interest {top_direction}{suffix}."
    suffix = "" if bottom.get("significant") else f"; {bottom['lang'].upper()} trend not significant"
    comparison = "flat, compared to" if top_direction == "flat" else f"{top_direction} faster than"
    return f"{top['lang'].upper()} interest {comparison} {bottom['lang'].upper()}{suffix}."


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
    analyze.add_argument("--start", default=None, help="YYYY-MM (a full YYYY-MM-DD is also accepted; the day is dropped). Omit with --end to default to the last 24 complete months.")
    analyze.add_argument("--end", default=None, help="YYYY-MM (a full YYYY-MM-DD is also accepted; the day is dropped). Omit with --start to default to the last 24 complete months. The current/a future month is clamped to the last complete one.")
    # Limited to the two values notes/api.md actually verified against the
    # live API ("user" is this skill's own default; "all-agents" is the
    # only other value exercised there) -- not the wider Wikimedia enum
    # (spider/automated/etc.) that was never curl-verified for this project.
    analyze.add_argument("--agent", default="user", choices=["user", "all-agents"], help='Traffic filter (default "user": excludes known bots and automated traffic).')
    analyze.add_argument("--rank-by", default="yoy_growth", dest="rank_by", choices=RANK_BY_CHOICES, help="Result field to sort by (default: yoy_growth, already spike-free; avg_monthly_views ranks by raw audience size instead of a growth rate).")
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
