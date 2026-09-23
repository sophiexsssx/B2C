"""
Tests for wiki_interest.metrics.

Pure-Python statistics core, no I/O -- these are plain unit tests against
hand-built {"YYYY-MM": views} dicts, no mocking needed. See notes/plan.md
section 2 ("Metrics vs. traps") for the spec these implement.
"""

import random
from datetime import datetime, timezone

import pytest

from wiki_interest import metrics
from wiki_interest.metrics import (
    MANN_KENDALL_MIN_POINTS,
    classify_spikes_and_seasonal,
    compute_confidence,
    compute_momentum_6mo,
    compute_share_yoy_growth,
    compute_trend_significance,
    compute_yoy_growth,
    current_month,
    detect_elevated_months,
    is_current_month,
    last_complete_month,
    mann_kendall_test,
    rank_series,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _month_add(yyyymm: str, delta: int) -> str:
    """Same month-shift arithmetic as metrics._shift_month, kept independent here."""
    year, month = (int(part) for part in yyyymm.split("-"))
    total = year * 12 + (month - 1) + delta
    year, month = divmod(total, 12)
    return f"{year:04d}-{month + 1:02d}"


def _series(start: str, n: int, value):
    """n consecutive months from `start`, all set to `value` (or value(i) if callable)."""
    if callable(value):
        return {_month_add(start, i): value(i) for i in range(n)}
    return {_month_add(start, i): value for i in range(n)}


# ---------------------------------------------------------------------------
# 1. detect_elevated_months / classify_spikes_and_seasonal
# ---------------------------------------------------------------------------


def test_month_elevated_in_two_years_is_seasonal_not_spike():
    monthly = _series("2021-01", 36, 100)
    monthly["2022-09"] = 1000
    monthly["2023-09"] = 1000

    spikes, seasonal = classify_spikes_and_seasonal(monthly)

    assert spikes == set()
    assert seasonal == {"2022-09", "2023-09"}


def test_month_elevated_in_only_one_year_is_a_spike():
    monthly = _series("2021-01", 36, 100)
    monthly["2022-09"] = 1000  # no other year has an elevated September

    spikes, seasonal = classify_spikes_and_seasonal(monthly)

    assert spikes == {"2022-09"}
    assert seasonal == set()


def test_mad_zero_degenerate_case_detects_single_positive_spike_not_zero_months():
    # 24 months of 0 views, with exactly one positive one-off value -- the
    # MAD==0 fallback (views > baseline AND views > 0) must still catch it,
    # without flagging any of the zero-view months, even though "06" recurs
    # (at 0 views) in both 2022 and 2023.
    monthly = _series("2022-01", 24, 0)
    monthly["2022-06"] = 50

    elevated = detect_elevated_months(monthly)
    assert elevated == {"2022-06"}
    for month, views in monthly.items():
        if views == 0:
            assert month not in elevated

    spikes, seasonal = classify_spikes_and_seasonal(monthly)
    assert spikes == {"2022-06"}
    assert seasonal == set()


def test_elevated_threshold_boundary_below_and_at():
    # 7 hand-picked points: median=30, MAD=10 -> threshold = baseline + 3*MAD = 60.
    # 59 (just below) must NOT be elevated; 60 (at the threshold) MUST be.
    monthly = {
        "2023-01": 10,
        "2023-02": 20,
        "2023-03": 20,
        "2023-04": 30,
        "2023-05": 40,
        "2023-06": 59,
        "2023-07": 60,
    }

    elevated = detect_elevated_months(monthly)

    assert "2023-06" not in elevated
    assert "2023-07" in elevated


# ---------------------------------------------------------------------------
# 2. compute_yoy_growth
# ---------------------------------------------------------------------------


def test_yoy_growth_zero_baseline_positive_current_is_new_or_dormant():
    monthly = {}
    monthly.update(_series("2022-01", 12, 0))
    monthly.update(_series("2023-01", 12, 100))

    result = compute_yoy_growth(monthly, period_start="2023-01", period_end="2023-12")

    assert result["value"] is None
    assert result["flag"] == "new_or_dormant"


def test_yoy_growth_both_periods_zero_is_flat():
    monthly = _series("2022-01", 24, 0)

    result = compute_yoy_growth(monthly, period_start="2023-01", period_end="2023-12")

    assert result["value"] is None
    assert result["flag"] == "flat"


def test_yoy_growth_normal_case_exact_value():
    monthly = {}
    monthly.update(_series("2022-01", 12, 100))  # prior 12mo sum = 1200
    monthly.update(_series("2023-01", 12, 150))  # current 12mo sum = 1800

    result = compute_yoy_growth(monthly, period_start="2023-01", period_end="2023-12")

    assert result["value"] == pytest.approx(0.5)
    assert result["flag"] is None


def test_yoy_growth_24mo_plus_requested_period_does_not_use_reference_baseline():
    # 36 months requested, entirely covering both the last-12 and prior-12
    # windows -- no need to reach back into reference-only data.
    monthly = _series("2021-01", 36, 100)

    result = compute_yoy_growth(monthly, period_start="2021-01", period_end="2023-12")

    assert result["used_reference_baseline"] is False


def test_yoy_growth_short_requested_period_uses_reference_baseline():
    # Only "2023-06".."2023-12" was requested, but the prior-12mo window
    # needed for YoY reaches back into 2022 reference-only data.
    monthly = _series("2022-01", 24, 100)

    result = compute_yoy_growth(monthly, period_start="2023-06", period_end="2023-12")

    assert result["used_reference_baseline"] is True


def test_yoy_growth_insufficient_history_does_not_report_spurious_growth():
    # 18 months of GENUINELY FLAT traffic (100 views every month, no real
    # trend at all). last12 gets a full 12 months (sum 1200), but prev12
    # only has 6 real months before that (sum 600) -- comparing a full
    # window against a half window would report "+100% growth" purely from
    # the length mismatch, not a real trend. Must be flagged instead.
    monthly = _series("2022-01", 18, 100)

    result = compute_yoy_growth(monthly, period_start="2022-01", period_end="2023-06")

    assert result["value"] is None
    assert result["flag"] == "insufficient_history"


def test_yoy_growth_prev12_completely_empty_is_insufficient_history_not_new_or_dormant():
    # Only 3 months of history exist at all (e.g. a brand-new article) --
    # prev12 is completely empty. This must be "insufficient_history", not
    # silently reclassified as "new_or_dormant" (which would imply we know
    # there was zero prior activity, when really we just have no data).
    monthly = _series("2026-01", 3, 50)

    result = compute_yoy_growth(monthly, period_start="2026-01", period_end="2026-03")

    assert result["value"] is None
    assert result["flag"] == "insufficient_history"


def test_yoy_growth_both_windows_full_still_computes_normally():
    # Sanity check the fix doesn't over-trigger: 24 full months (both
    # windows have exactly 12) must still compute a real value, unaffected.
    monthly = {}
    monthly.update(_series("2022-01", 12, 100))
    monthly.update(_series("2023-01", 12, 150))

    result = compute_yoy_growth(monthly, period_start="2022-01", period_end="2023-12")

    assert result["value"] == pytest.approx(0.5)
    assert result["flag"] is None


def test_confidence_explains_insufficient_history_flag():
    level, reason = compute_confidence(
        yoy_growth_value=None,
        yoy_growth_flag="insufficient_history",
        share_yoy_growth_value=None,
        significant=False,
        history_months=18,
        match_confidence="high",
    )
    assert level == "low"
    assert "history" in reason.lower()


# ---------------------------------------------------------------------------
# 3. compute_share_yoy_growth
# ---------------------------------------------------------------------------


def test_share_yoy_growth_negative_despite_raw_views_growing():
    # Article views grow 50% YoY, but the site total grows 150% YoY -- so the
    # article's SHARE of total traffic actually shrinks, isolating the
    # topic-specific signal from the platform-wide trend.
    article_monthly = {}
    article_monthly.update(_series("2022-01", 12, 100))
    article_monthly.update(_series("2023-01", 12, 150))

    site_monthly = {}
    site_monthly.update(_series("2022-01", 12, 1000))
    site_monthly.update(_series("2023-01", 12, 2500))

    raw = compute_yoy_growth(article_monthly, period_start="2023-01", period_end="2023-12")
    assert raw["value"] == pytest.approx(0.5)  # sanity: raw views did grow

    share = compute_share_yoy_growth(article_monthly, site_monthly, period_end="2023-12")

    assert share["value"] == pytest.approx(-0.4)
    assert share["value"] < 0


def test_share_yoy_growth_insufficient_history_does_not_report_spurious_value():
    # Same unequal-window-length problem as compute_yoy_growth: 18 months
    # of flat article and site traffic (share is genuinely constant), but
    # prev12 only has 6 real months -- must flag rather than compute a
    # misleading share-growth figure.
    article_monthly = _series("2022-01", 18, 100)
    site_monthly = _series("2022-01", 18, 1000)

    result = compute_share_yoy_growth(article_monthly, site_monthly, period_end="2023-06")

    assert result["value"] is None
    assert result["flag"] == "insufficient_history"


# ---------------------------------------------------------------------------
# 4. compute_momentum_6mo
# ---------------------------------------------------------------------------


def test_momentum_6mo_known_value():
    monthly = {}
    monthly.update(_series("2022-07", 6, 100))  # same 6 calendar months last year
    monthly.update(_series("2023-07", 6, 200))  # last 6 months

    result = compute_momentum_6mo(monthly, period_end="2023-12")

    assert result["value"] == pytest.approx(1.0)  # (1200 - 600) / 600


# ---------------------------------------------------------------------------
# 5. mann_kendall_test
# ---------------------------------------------------------------------------


def test_mann_kendall_monotonic_increasing_is_significant():
    result = mann_kendall_test(list(range(1, 11)))
    assert result["significant"] is True
    assert result["insufficient_data"] is False


def test_mann_kendall_flat_series_is_not_significant():
    result = mann_kendall_test([5] * 10)
    assert result["significant"] is False
    assert result["insufficient_data"] is False


def test_mann_kendall_noisy_series_with_no_trend_is_not_significant():
    result = mann_kendall_test([5, 7, 4, 8, 3, 9, 2, 10, 1, 6])
    assert result["significant"] is False
    assert result["insufficient_data"] is False


def test_mann_kendall_too_few_points_is_insufficient_not_a_crash():
    values = list(range(MANN_KENDALL_MIN_POINTS - 1))
    result = mann_kendall_test(values)
    assert result["insufficient_data"] is True
    assert result["significant"] is False


# ---------------------------------------------------------------------------
# 6. compute_confidence
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "yoy_growth_flag,share_yoy_growth_value,significant,history_months,match_confidence",
    [
        ("new_or_dormant", None, False, 10, "low"),
        ("flat", 0.5, True, 60, "high"),
        ("new_or_dormant", -0.2, True, 40, "medium"),
    ],
)
def test_confidence_zero_baseline_is_always_low(
    yoy_growth_flag, share_yoy_growth_value, significant, history_months, match_confidence
):
    level, reason = compute_confidence(
        yoy_growth_value=None,
        yoy_growth_flag=yoy_growth_flag,
        share_yoy_growth_value=share_yoy_growth_value,
        significant=significant,
        history_months=history_months,
        match_confidence=match_confidence,
    )
    assert level == "low"
    assert reason


def test_confidence_short_history_downgrades_level():
    kwargs = dict(
        yoy_growth_value=0.5,
        yoy_growth_flag=None,
        share_yoy_growth_value=0.3,
        significant=True,
        match_confidence="high",
    )
    high, high_reason = compute_confidence(history_months=40, **kwargs)
    assert high == "high"

    downgraded, downgraded_reason = compute_confidence(history_months=12, **kwargs)
    assert downgraded == "medium"
    assert "history" in downgraded_reason.lower() or "12mo" in downgraded_reason


def test_confidence_share_yoy_sign_disagreement_forces_low():
    level, reason = compute_confidence(
        yoy_growth_value=0.5,
        yoy_growth_flag=None,
        share_yoy_growth_value=-0.2,
        significant=True,
        history_months=40,
        match_confidence="high",
    )
    assert level == "low"
    assert "disagree" in reason.lower()


@pytest.mark.parametrize(
    "kwargs",
    [
        dict(
            yoy_growth_value=0.5,
            yoy_growth_flag=None,
            share_yoy_growth_value=0.3,
            significant=True,
            history_months=40,
            match_confidence="high",
        ),
        dict(
            yoy_growth_value=0.1,
            yoy_growth_flag=None,
            share_yoy_growth_value=None,
            significant=False,
            history_months=10,
            match_confidence="medium",
        ),
        dict(
            yoy_growth_value=None,
            yoy_growth_flag="flat",
            share_yoy_growth_value=None,
            significant=False,
            history_months=5,
            match_confidence="low",
        ),
        dict(
            yoy_growth_value=-0.3,
            yoy_growth_flag=None,
            share_yoy_growth_value=-0.4,
            significant=True,
            history_months=48,
            match_confidence="high",
        ),
    ],
)
def test_confidence_always_returns_a_nonempty_reason(kwargs):
    level, reason = compute_confidence(**kwargs)
    assert level in ("high", "medium", "low")
    assert isinstance(reason, str)
    assert reason.strip() != ""


# ---------------------------------------------------------------------------
# 7. rank_series
# ---------------------------------------------------------------------------


def test_rank_series_excludes_none_values_and_sorts_descending():
    results = [
        {"lang": "en", "yoy_growth": 0.5},
        {"lang": "de", "yoy_growth": None},
        {"lang": "fr", "yoy_growth": 1.2},
        {"lang": "pl", "yoy_growth": -0.1},
    ]

    ranked, unranked = rank_series(results, rank_by="yoy_growth")

    assert ranked == [
        {"lang": "fr", "yoy_growth": 1.2},
        {"lang": "en", "yoy_growth": 0.5},
        {"lang": "pl", "yoy_growth": -0.1},
    ]
    assert unranked == [{"lang": "de", "yoy_growth": None}]


# ---------------------------------------------------------------------------
# 8. current_month / is_current_month / last_complete_month
# ---------------------------------------------------------------------------

# Fixed reference instants -- never rely on real wall-clock time in a test.
_TODAY = datetime(2026, 9, 23, tzinfo=timezone.utc)
_TODAY_JAN = datetime(2026, 1, 15, tzinfo=timezone.utc)


def test_current_month_from_fixed_today():
    assert current_month(_TODAY) == "2026-09"


def test_is_current_month_true_and_false_cases():
    assert is_current_month("2026-09", _TODAY) is True
    assert is_current_month("2026-08", _TODAY) is False


def test_last_complete_month_is_the_month_before_today():
    assert last_complete_month(_TODAY) == "2026-08"


def test_last_complete_month_year_boundary_rolls_back_to_prior_december():
    # today is in January -> the last complete month is December of the
    # PREVIOUS year, not "month 0" or the current year.
    assert current_month(_TODAY_JAN) == "2026-01"
    assert last_complete_month(_TODAY_JAN) == "2025-12"


# ---------------------------------------------------------------------------
# 9. Incomplete-current-month exclusion (compute_yoy_growth,
#    compute_share_yoy_growth, compute_momentum_6mo)
# ---------------------------------------------------------------------------


def test_yoy_growth_excludes_incomplete_current_month():
    # With today=_TODAY, "2026-09" is the in-progress current month --
    # last_complete_month is "2026-08". The tiny partial count at "2026-09"
    # must be excluded from the last-12mo sum; if it were wrongly included,
    # the window would shift forward by one month (dropping "2025-09" and
    # picking up the tiny "2026-09" value instead), producing a materially
    # different growth value.
    monthly = {}
    monthly.update(_series("2024-09", 12, 100))  # prior 12mo (complete): sum 1200
    monthly.update(_series("2025-09", 12, 200))  # last 12mo (complete): sum 2400
    monthly["2026-09"] = 5  # tiny partial count for the in-progress month

    result = compute_yoy_growth(monthly, period_start="2024-09", period_end="2026-09", today=_TODAY)

    # Matches the complete-months-only computation: (2400 - 1200) / 1200 = 1.0.
    # Wrongly including "2026-09" (dropping "2025-09" instead) would give
    # (200*11 + 5 - (100*11 + 200)) / (100*11 + 200) ~= 0.696, not 1.0.
    assert result["value"] == pytest.approx(1.0)
    assert result["excluded_incomplete_month"] is True


def test_yoy_growth_does_not_exclude_an_already_complete_period_end():
    # period_end ("2026-08") is already the last complete month -- nothing
    # to clamp, and the presence of a later partial-month entry in the
    # series must not affect the result.
    monthly = {}
    monthly.update(_series("2024-09", 12, 100))
    monthly.update(_series("2025-09", 12, 200))
    monthly["2026-09"] = 5  # irrelevant: after period_end, never included

    result = compute_yoy_growth(monthly, period_start="2024-09", period_end="2026-08", today=_TODAY)

    assert result["value"] == pytest.approx(1.0)
    assert result["excluded_incomplete_month"] is False


def test_share_yoy_growth_excludes_incomplete_current_month():
    article_monthly = {}
    article_monthly.update(_series("2024-09", 12, 100))  # prior share = 1200/12000 = 0.1
    article_monthly.update(_series("2025-09", 12, 200))  # current share = 2400/12000 = 0.2
    article_monthly["2026-09"] = 1_000_000  # anomalous partial-month count

    site_monthly = _series("2024-09", 25, 1000)  # flat site total, incl. "2026-09"

    result = compute_share_yoy_growth(article_monthly, site_monthly, period_end="2026-09", today=_TODAY)

    # (0.2 - 0.1) / 0.1 = 1.0. If the anomalous "2026-09" article count were
    # wrongly included, share_current would spike to roughly 83 (1,002,200 /
    # 12,000), an enormous, obviously-wrong distortion.
    assert result["value"] == pytest.approx(1.0)
    assert result["excluded_incomplete_month"] is True


def test_share_yoy_growth_does_not_exclude_an_already_complete_period_end():
    article_monthly = {}
    article_monthly.update(_series("2024-09", 12, 100))
    article_monthly.update(_series("2025-09", 12, 200))
    article_monthly["2026-09"] = 1_000_000  # irrelevant: after period_end

    site_monthly = _series("2024-09", 25, 1000)

    result = compute_share_yoy_growth(article_monthly, site_monthly, period_end="2026-08", today=_TODAY)

    assert result["value"] == pytest.approx(1.0)
    assert result["excluded_incomplete_month"] is False


def test_momentum_6mo_excludes_incomplete_current_month():
    monthly = {}
    monthly.update(_series("2025-03", 6, 100))  # same 6 calendar months last year: sum 600
    monthly.update(_series("2026-03", 6, 300))  # last 6 complete months: sum 1800
    monthly["2026-09"] = 999_999  # anomalous partial-month count

    result = compute_momentum_6mo(monthly, period_end="2026-09", today=_TODAY)

    # (1800 - 600) / 600 = 2.0. Wrongly including "2026-09" (dropping
    # "2026-03" instead, and comparing against mostly-missing 2025-09 data)
    # would produce a growth value in the thousands.
    assert result["value"] == pytest.approx(2.0)
    assert result["excluded_incomplete_month"] is True


def test_momentum_6mo_does_not_exclude_an_already_complete_period_end():
    monthly = {}
    monthly.update(_series("2025-03", 6, 100))
    monthly.update(_series("2026-03", 6, 300))
    monthly["2026-09"] = 999_999  # irrelevant: after period_end

    result = compute_momentum_6mo(monthly, period_end="2026-08", today=_TODAY)

    assert result["value"] == pytest.approx(2.0)
    assert result["excluded_incomplete_month"] is False


# ---------------------------------------------------------------------------
# 10. compute_trend_significance
# ---------------------------------------------------------------------------


def _flat_seasonal_series(years, baseline=100, spike=1000, spike_month=9):
    """Same repeating pattern every year: a big spike_month spike, flat baseline
    otherwise -- no real underlying year-over-year growth."""
    monthly = {}
    for year in years:
        for month in range(1, 13):
            monthly[f"{year:04d}-{month:02d}"] = spike if month == spike_month else baseline
    return monthly


def test_trend_significance_flat_but_seasonal_series_is_not_significant():
    # Every rolling 12-month window contains exactly one September spike, so
    # the rolling-12mo article/site ratio series is perfectly constant --
    # correctly not significant. Plain Mann-Kendall run directly on the raw
    # monthly series (not through this rolling-window transform) can be
    # misled by the seasonal cycle itself; this is the behavior the fix
    # guards against.
    years = [2023, 2024, 2025]
    article_monthly = _flat_seasonal_series(years)
    site_monthly = {f"{year:04d}-{month:02d}": 10000 for year in years for month in range(1, 13)}

    result = compute_trend_significance(
        article_monthly, site_monthly, period_start="2023-01", period_end="2025-12", today=_TODAY
    )

    assert result["significant"] is False
    assert "series_length" in result
    assert "excluded_incomplete_month" in result
    assert result["excluded_incomplete_month"] is False


def test_trend_significance_seasonal_plus_real_growth_is_significant():
    # Same seasonal shape as above, but each year's baseline (and its
    # September spike) is meaningfully higher than the last -- a real
    # underlying YoY growth trend. Confirms the seasonal-Kendall approach
    # doesn't just suppress all significance; it still detects genuine trends.
    article_monthly = {}
    for year, baseline, spike in [(2023, 100, 1000), (2024, 200, 1100), (2025, 300, 1200)]:
        for month in range(1, 13):
            article_monthly[f"{year:04d}-{month:02d}"] = spike if month == 9 else baseline
    site_monthly = {f"{year:04d}-{month:02d}": 10000 for year in (2023, 2024, 2025) for month in range(1, 13)}

    result = compute_trend_significance(
        article_monthly, site_monthly, period_start="2023-01", period_end="2025-12", today=_TODAY
    )

    assert result["significant"] is True
    assert "series_length" in result
    assert "excluded_incomplete_month" in result


def test_trend_significance_false_positive_rate_near_alpha_on_flat_noisy_seasonal_data():
    # The regression this guards against: an earlier implementation ran
    # plain Mann-Kendall on a ROLLING 12-month-summed ratio series to cancel
    # seasonality. That works for a perfectly deterministic seasonal pattern
    # (see the test above), but overlapping rolling sums are strongly
    # autocorrelated with their neighbors, and plain MK's variance formula
    # assumes independent observations -- on flat-but-NOISY seasonal data,
    # that measured false-positive rate empirically at ~61% against a 5%
    # nominal alpha. The seasonal (within-calendar-month) approach compares
    # only independent same-month-across-years observations, so it should
    # stay near the nominal alpha instead.
    rng = random.Random(20260101)  # fixed seed: deterministic, reproducible
    simulations = 500
    false_positives = 0

    for _ in range(simulations):
        article_monthly = {}
        site_monthly = {}
        for year in (2023, 2024, 2025):
            for month in range(1, 13):
                baseline = 1000 if month == 9 else 500  # flat seasonal pattern, NO real trend
                noisy_views = max(0, round(baseline + rng.gauss(0, 40)))
                article_monthly[f"{year:04d}-{month:02d}"] = noisy_views
                site_monthly[f"{year:04d}-{month:02d}"] = 100_000

        result = compute_trend_significance(article_monthly, site_monthly, period_start="2023-01", period_end="2025-12")
        if result["significant"]:
            false_positives += 1

    false_positive_rate = false_positives / simulations
    # Generous band around SIGNIFICANCE_ALPHA (0.05) to avoid test flakiness
    # from a single seed's sampling noise, while still failing hard if the
    # rate is anywhere near the ~0.61 the old rolling-window bug produced.
    assert false_positive_rate <= 3 * metrics.SIGNIFICANCE_ALPHA, (
        f"false positive rate {false_positive_rate:.3f} is too high for alpha={metrics.SIGNIFICANCE_ALPHA} "
        "-- looks like the autocorrelation-inflated-significance bug"
    )


# ---------------------------------------------------------------------------
# 11. MANN_KENDALL_MIN_POINTS == 5
# ---------------------------------------------------------------------------


def test_mann_kendall_four_points_is_insufficient_data():
    assert MANN_KENDALL_MIN_POINTS == 5
    result = mann_kendall_test([1, 2, 3, 4])
    assert result["insufficient_data"] is True
    assert result["significant"] is False


def test_mann_kendall_five_monotonic_points_can_be_significant():
    # Confirms 5 is a genuinely usable minimum, not just also insufficient.
    result = mann_kendall_test([1, 2, 3, 4, 5])
    assert result["insufficient_data"] is False
    assert result["significant"] is True


# ---------------------------------------------------------------------------
# 12. compute_confidence requires match_confidence (no silent default)
# ---------------------------------------------------------------------------


def test_compute_confidence_requires_match_confidence_kwarg():
    with pytest.raises(TypeError):
        compute_confidence(
            yoy_growth_value=0.5,
            yoy_growth_flag=None,
            share_yoy_growth_value=0.3,
            significant=True,
            history_months=40,
        )
