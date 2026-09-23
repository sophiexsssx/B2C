"""
Statistical analysis of a monthly pageview series: growth metrics, spike vs.
seasonal classification, trend significance, and confidence scoring.

Every function works on plain {"YYYY-MM": views} dicts (already zero-filled
by api_client) -- a monthly series is small (tens to low hundreds of points),
so this stays pure Python/stdlib rather than adding numpy/scipy as a
dependency. See notes/plan.md section 2 for the metric-to-trap mapping this
implements.
"""

import math
from collections import Counter
from datetime import datetime, timezone

ELEVATED_MAD_MULTIPLIER = 3
# n=4 can never actually reach significance even for a perfectly monotonic
# run (max S=6, Var(S)=8.67 -> z~1.70, p~0.089); n=5 is the true minimum
# (S=10, Var(S)=16.67 -> z~2.20, p~0.028 < 0.05). Set to the real minimum
# rather than a value that would always fall through to "not significant"
# anyway, so the constant's name matches what it actually gates.
MANN_KENDALL_MIN_POINTS = 5
SIGNIFICANCE_ALPHA = 0.05
REFERENCE_TARGET_MONTHS = 36
SHORT_PERIOD_MONTHS = 24
ROLLING_WINDOW_MONTHS = 12

_CONFIDENCE_DOWNGRADE = {"high": "medium", "medium": "low", "low": "low"}


def to_series(monthly_rows: list) -> dict:
    """[{"month": "YYYY-MM", "views": int}, ...] (api_client's shape) -> {"YYYY-MM": views}."""
    return {row["month"]: row["views"] for row in monthly_rows}


def current_month(today=None) -> str:
    """"YYYY-MM" for `today` (UTC), or the real current month if `today` is omitted."""
    moment = today or datetime.now(timezone.utc)
    return f"{moment.year:04d}-{moment.month:02d}"


def is_current_month(yyyymm: str, today=None) -> bool:
    return yyyymm == current_month(today)


def last_complete_month(today=None) -> str:
    """
    The most recently fully-completed calendar month. Wikimedia's Pageviews
    API returns a real, non-zero count for an in-progress current month --
    that's correct API behavior, not a bug -- but it's a PARTIAL total, not
    final, and every growth/significance calculation in this module must
    exclude it: including it would understate current-period activity
    relative to a complete prior period and bias growth downward for no
    real reason.
    """
    return _shift_month(current_month(today), -1)


# ---------------------------------------------------------------------------
# Spike vs. seasonal classification
# ---------------------------------------------------------------------------


def _median(values):
    ordered = sorted(values)
    n = len(ordered)
    mid = n // 2
    if n % 2 == 1:
        return float(ordered[mid])
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def _mad(values, med):
    return _median([abs(v - med) for v in values])


def detect_elevated_months(monthly: dict) -> set:
    """
    A month is elevated if views >= baseline + 3xMAD, where baseline=median
    and MAD=median absolute deviation over every month in `monthly` (robust
    to the outliers being detected).

    MAD==0 fallback: the threshold would otherwise collapse to `baseline`
    (e.g. a sparse, mostly-zero-view series) -- if baseline is also 0, every
    non-negative view count would trivially satisfy ">= 0", wrongly flagging
    zero-view months. So when MAD==0, a month is elevated only if
    views > baseline AND views > 0.
    """
    values = list(monthly.values())
    if not values:
        return set()
    baseline = _median(values)
    spread = _mad(values, baseline)
    elevated = set()
    for month, views in monthly.items():
        if spread == 0:
            if views > baseline and views > 0:
                elevated.add(month)
        elif views >= baseline + ELEVATED_MAD_MULTIPLIER * spread:
            elevated.add(month)
    return elevated


def classify_spikes_and_seasonal(monthly: dict) -> tuple:
    """
    Split elevated months into (spike_months, seasonal_months). A month is
    seasonal (kept) if its calendar month (e.g. "09") is *also* elevated in
    at least one other year present in `monthly` (i.e. elevated in >=2
    distinct years total); otherwise it's a one-off spike (removed).
    """
    elevated = detect_elevated_months(monthly)
    spike_months, seasonal_months = set(), set()
    for month in elevated:
        calendar_month = month[5:7]
        years_elevated_for_this_calendar_month = {m[0:4] for m in elevated if m[5:7] == calendar_month}
        if len(years_elevated_for_this_calendar_month) >= 2:
            seasonal_months.add(month)
        else:
            spike_months.add(month)
    return spike_months, seasonal_months


def spike_free_series(monthly: dict, spike_months: set) -> dict:
    """
    Winsorize spike months to the series baseline (median) instead of
    dropping them -- dropping would shrink 12-month sums unevenly depending
    on how many spikes happened to fall in a given window. Seasonal peaks
    are left untouched.
    """
    if not spike_months:
        return dict(monthly)
    baseline = round(_median(list(monthly.values())))
    return {month: (baseline if month in spike_months else views) for month, views in monthly.items()}


# ---------------------------------------------------------------------------
# Growth metrics
# ---------------------------------------------------------------------------


def _shift_month(yyyymm: str, delta: int) -> str:
    """Shift "YYYY-MM" by `delta` months (may be negative), handling year rollover."""
    year, month = (int(part) for part in yyyymm.split("-"))
    total = year * 12 + (month - 1) + delta
    year, month = divmod(total, 12)
    return f"{year:04d}-{month + 1:02d}"


def _last_n_months(monthly: dict, end_month: str, n: int) -> list:
    """The last `n` months at or before `end_month` that are present in `monthly`, oldest first."""
    months = sorted(m for m in monthly if m <= end_month)
    return months[-n:] if len(months) >= n else months


def _sum_months(monthly: dict, months: list) -> int:
    return sum(monthly[m] for m in months)


def _growth_with_zero_handling(current_sum, prior_sum):
    """
    (value, flag): division by a zero prior-period sum is undefined as a
    percentage, so instead of 0 or infinity this returns `None` with a flag
    -- "new_or_dormant" if the current period has activity, "flat" if both
    periods are zero (notes/plan.md YoY growth row).
    """
    if prior_sum == 0:
        return (None, "flat" if current_sum == 0 else "new_or_dormant")
    return ((current_sum - prior_sum) / prior_sum, None)


def _clamp_to_complete_month(period_end: str, today=None) -> tuple:
    """
    (effective_end, excluded_incomplete_month): if `period_end` is the
    current in-progress calendar month, clamp it back one month so growth
    math never mixes a partial month's count with complete ones.
    """
    complete = last_complete_month(today)
    if period_end > complete:
        return complete, True
    return period_end, False


def compute_yoy_growth(spike_free_monthly: dict, period_start: str, period_end: str, today=None) -> dict:
    """
    Last 12 months vs. the previous 12, both read from `spike_free_monthly`
    -- which must already include reference months if the requested period
    is <24 months (notes/plan.md's <24-month rule); this function doesn't
    fetch reference data itself, it just uses whatever months it's given.

    If `period_end` is the current in-progress month, it's excluded (see
    _clamp_to_complete_month) so a partial month never distorts the sum.

    Returns {"value": float|None, "flag": str|None,
             "used_reference_baseline": bool, "excluded_incomplete_month": bool}.
    """
    period_end, excluded_incomplete_month = _clamp_to_complete_month(period_end, today)
    last12 = _last_n_months(spike_free_monthly, period_end, 12)
    if not last12:
        return {"value": None, "flag": "flat", "used_reference_baseline": False, "excluded_incomplete_month": excluded_incomplete_month}
    prev12 = _last_n_months(spike_free_monthly, _shift_month(last12[0], -1), 12)
    used_reference_baseline = bool(prev12) and prev12[0] < period_start
    current_sum = _sum_months(spike_free_monthly, last12)
    prior_sum = _sum_months(spike_free_monthly, prev12) if prev12 else 0
    value, flag = _growth_with_zero_handling(current_sum, prior_sum)
    return {
        "value": value,
        "flag": flag,
        "used_reference_baseline": used_reference_baseline,
        "excluded_incomplete_month": excluded_incomplete_month,
    }


def compute_share_yoy_growth(spike_free_article_monthly: dict, spike_free_site_monthly: dict, period_end: str, today=None) -> dict:
    """
    Same YoY comparison as compute_yoy_growth, but on article_views /
    site_total_views instead of raw views -- isolates topic-specific change
    from platform-wide traffic trends. share_prev is zero exactly when
    article_prev is zero, since a real wiki's site total is never zero.
    Excludes an in-progress current month the same way compute_yoy_growth does.
    """
    period_end, excluded_incomplete_month = _clamp_to_complete_month(period_end, today)
    last12 = _last_n_months(spike_free_article_monthly, period_end, 12)
    if not last12:
        return {"value": None, "flag": "flat", "excluded_incomplete_month": excluded_incomplete_month}
    prev12 = _last_n_months(spike_free_article_monthly, _shift_month(last12[0], -1), 12)

    article_current = _sum_months(spike_free_article_monthly, last12)
    article_prior = _sum_months(spike_free_article_monthly, prev12) if prev12 else 0
    site_current = _sum_months(spike_free_site_monthly, last12)
    site_prior = _sum_months(spike_free_site_monthly, prev12) if prev12 else 0

    share_current = (article_current / site_current) if site_current else 0.0
    share_prior = (article_prior / site_prior) if site_prior else 0.0
    value, flag = _growth_with_zero_handling(share_current, share_prior)
    return {"value": value, "flag": flag, "excluded_incomplete_month": excluded_incomplete_month}


def compute_momentum_6mo(spike_free_monthly: dict, period_end: str, today=None) -> dict:
    """
    Last 6 months vs. the same 6 calendar months a year earlier -- a
    seasonality control. Excludes an in-progress current month the same way
    compute_yoy_growth does.
    """
    period_end, excluded_incomplete_month = _clamp_to_complete_month(period_end, today)
    last6 = _last_n_months(spike_free_monthly, period_end, 6)
    if not last6:
        return {"value": None, "flag": "flat", "excluded_incomplete_month": excluded_incomplete_month}
    same6_last_year = [_shift_month(m, -12) for m in last6]
    current_sum = _sum_months(spike_free_monthly, last6)
    prior_sum = sum(spike_free_monthly.get(m, 0) for m in same6_last_year)
    value, flag = _growth_with_zero_handling(current_sum, prior_sum)
    return {"value": value, "flag": flag, "excluded_incomplete_month": excluded_incomplete_month}


# ---------------------------------------------------------------------------
# Trend significance
# ---------------------------------------------------------------------------


def _normal_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2)))


def mann_kendall_test(values: list) -> dict:
    """
    Standard Mann-Kendall trend test (tie-corrected normal approximation),
    chosen over a Poisson/binomial test because pageviews are overdispersed
    -- those would flag tiny fluctuations as significant (notes/api.md).

    Needs >= MANN_KENDALL_MIN_POINTS values for the normal approximation to
    be meaningful; with fewer, returns significant=False and
    insufficient_data=True rather than a possibly-nonsensical statistic.
    """
    n = len(values)
    if n < MANN_KENDALL_MIN_POINTS:
        return {"significant": False, "z": 0.0, "p_value": 1.0, "insufficient_data": True}

    s = 0
    for i in range(n - 1):
        for j in range(i + 1, n):
            diff = values[j] - values[i]
            s += (diff > 0) - (diff < 0)

    tie_counts = Counter(values)
    tie_correction = sum(t * (t - 1) * (2 * t + 5) for t in tie_counts.values() if t > 1)
    var_s = (n * (n - 1) * (2 * n + 5) - tie_correction) / 18.0

    if var_s <= 0:
        z = 0.0
    elif s > 0:
        z = (s - 1) / math.sqrt(var_s)
    elif s < 0:
        z = (s + 1) / math.sqrt(var_s)
    else:
        z = 0.0

    p_value = 2 * (1 - _normal_cdf(abs(z)))
    return {"significant": p_value < SIGNIFICANCE_ALPHA, "z": z, "p_value": p_value, "insufficient_data": False}


def _rolling_12mo_ratio_series(article_monthly: dict, site_monthly: dict, months: list) -> list:
    """
    Trailing-12-month article/site view-share ratio for each window ending
    at each month in `months` from the 12th one onward (chronological order).

    This transform does two things at once, both required for the
    significance test to match notes/plan.md's "normalized, spike-free
    monthly series": every window sums a full calendar year, so within-year
    seasonality cancels by construction (a strong summer dip / September
    peak no longer looks like a trend); and article/site is a *ratio*, so
    it isolates topic-specific change from Wikipedia's overall traffic
    trend, same as compute_share_yoy_growth.
    """
    ordered = sorted(months)
    ratios = []
    for i in range(ROLLING_WINDOW_MONTHS - 1, len(ordered)):
        window = ordered[i - (ROLLING_WINDOW_MONTHS - 1) : i + 1]
        article_sum = sum(article_monthly.get(m, 0) for m in window)
        site_sum = sum(site_monthly.get(m, 0) for m in window)
        if site_sum > 0:
            ratios.append(article_sum / site_sum)
    return ratios


def compute_trend_significance(spike_free_article_monthly: dict, spike_free_site_monthly: dict, period_start: str, period_end: str, today=None) -> dict:
    """
    The intended entry point for trend significance -- NOT plain
    mann_kendall_test on a raw monthly series. Runs Mann-Kendall on a
    rolling trailing-12-month article/site share series (see
    _rolling_12mo_ratio_series) restricted to the requested period only
    (reference months are for spike/seasonal classification and the
    <24-month YoY baseline, never a reported metric input beyond that).

    Plain MK directly on a spike-free-but-still-seasonal monthly series
    would treat a real, strong seasonal cycle (e.g. a reliable September
    peak) as trend-like structure and can produce false "significant"
    results on a topic with no real long-term trend at all -- the rolling
    12-month window cancels that by construction.
    """
    period_end, excluded_incomplete_month = _clamp_to_complete_month(period_end, today)
    months = [m for m in spike_free_article_monthly if period_start <= m <= period_end]
    ratios = _rolling_12mo_ratio_series(spike_free_article_monthly, spike_free_site_monthly, months)
    result = mann_kendall_test(ratios)
    result["series_length"] = len(ratios)
    result["excluded_incomplete_month"] = excluded_incomplete_month
    return result


# ---------------------------------------------------------------------------
# Confidence
# ---------------------------------------------------------------------------


def _sign(x: float) -> int:
    return (x > 0) - (x < 0)


def compute_confidence(
    *,
    yoy_growth_value,
    yoy_growth_flag,
    share_yoy_growth_value,
    significant,
    history_months,
    match_confidence,
) -> tuple:
    """
    Always returns (level, reason) -- the assignment's hard requirement that
    every conclusion ships with a confidence level and a reason for it.

    `match_confidence` is required, not defaulted: the `--article
    lang:"Title"` manual-override path (notes/plan.md) bypasses
    resolve_topic's QID cross-check entirely, so there is no automatic way
    to know a title is correct for that path. A default of "high" here
    would silently give an unverified manual override the *most* favorable
    rating; callers must pass an explicit value (e.g. "medium" with a
    "manually specified, not cross-checked" reason for the override case).
    """
    if yoy_growth_value is None:
        why = "no prior-period activity to compare against" if yoy_growth_flag == "new_or_dormant" else "no activity in either period"
        return "low", f"zero baseline for YoY comparison ({why}) -- not evidence of a reliable trend"

    level = "high"
    reasons = []

    if history_months < REFERENCE_TARGET_MONTHS:
        level = _CONFIDENCE_DOWNGRADE[level]
        reasons.append(f"only {history_months}mo of history (<{REFERENCE_TARGET_MONTHS}mo target) -- seasonal-vs-spike classification is less reliable")

    if not significant:
        level = _CONFIDENCE_DOWNGRADE[level]
        reasons.append("trend not statistically significant (Mann-Kendall)")

    if share_yoy_growth_value is not None and _sign(share_yoy_growth_value) != _sign(yoy_growth_value):
        level = "low"
        reasons.append("share_yoy_growth sign disagrees with yoy_growth")

    if match_confidence == "low":
        level = "low"
        reasons.append("cross-language article match confidence is low")
    elif match_confidence == "medium":
        level = _CONFIDENCE_DOWNGRADE[level]
        reasons.append("cross-language article match confidence is medium")

    if not reasons:
        reasons.append(f"{REFERENCE_TARGET_MONTHS}mo+ history; trend significant; share_yoy_growth agrees in sign")

    return level, "; ".join(reasons)


# ---------------------------------------------------------------------------
# Ranking
# ---------------------------------------------------------------------------


def rank_series(results: list, rank_by: str = "yoy_growth") -> tuple:
    """
    (ranked, unranked): entries whose `rank_by` value is None (zero-baseline
    growth -- often the most interesting finding, e.g. a brand-new emerging
    topic) are excluded from the sort and returned separately instead of
    being coerced into it (notes/plan.md YoY growth row).

    CALLERS MUST SURFACE BOTH VALUES. Every entry in `results` ends up in
    exactly one of the two returned lists -- nothing is dropped by this
    function -- but a caller that only serializes `ranked` (e.g.
    `ranked, _ = rank_series(...)`) would silently drop every zero-baseline
    series from the final output.
    """
    ranked = [r for r in results if r.get(rank_by) is not None]
    unranked = [r for r in results if r.get(rank_by) is None]
    ranked.sort(key=lambda r: r[rank_by], reverse=True)
    return ranked, unranked
