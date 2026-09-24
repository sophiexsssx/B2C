"""
Tests for wiki_interest.charts.

Pure matplotlib figure-building, no network I/O -- these build small
hand-crafted {lang: {"YYYY-MM": views}} series and results lists and inspect
the resulting Figure/Axes objects and saved PNG files. No mocking needed.
"""

import warnings

import matplotlib.pyplot as plt
import pytest

from wiki_interest import charts
from wiki_interest.charts import (
    build_growth_comparison_chart,
    build_trend_chart,
    save_chart_png,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _month_add(yyyymm: str, delta: int) -> str:
    year, month = (int(part) for part in yyyymm.split("-"))
    total = year * 12 + (month - 1) + delta
    year, month = divmod(total, 12)
    return f"{year:04d}-{month + 1:02d}"


def _series(start: str, n: int, value):
    return {_month_add(start, i): value for i in range(n)}


# ---------------------------------------------------------------------------
# 1. build_trend_chart: basic figure + save_chart_png + period restriction
# ---------------------------------------------------------------------------


def test_build_trend_chart_produces_a_figure_with_two_languages():
    series_by_lang = {
        "en": _series("2023-01", 12, 100),
        "de": _series("2023-01", 12, 50),
    }

    fig = build_trend_chart(series_by_lang, "2023-01", "2023-12")

    assert isinstance(fig, plt.Figure)
    ax = fig.axes[0]
    assert len(ax.get_lines()) == 2


def test_save_chart_png_writes_a_nonempty_file(tmp_path):
    series_by_lang = {"en": _series("2023-01", 12, 100)}
    fig = build_trend_chart(series_by_lang, "2023-01", "2023-12")

    output_path = str(tmp_path / "chart.png")
    result_path = save_chart_png(fig, output_path)

    assert result_path == output_path
    from pathlib import Path

    saved = Path(output_path)
    assert saved.exists()
    assert saved.stat().st_size > 0


def test_build_trend_chart_restricts_to_period_range():
    # 24 months of data, 2021-01..2022-12, but only 2022 is requested --
    # the plotted line's x-data must exclude the 2021 months entirely.
    series_by_lang = {"en": _series("2021-01", 24, 100)}

    fig = build_trend_chart(series_by_lang, "2022-01", "2022-12")

    ax = fig.axes[0]
    xdata = list(ax.get_lines()[0].get_xdata())
    assert all(m >= "2022-01" for m in xdata)
    assert all(m <= "2022-12" for m in xdata)
    assert "2021-12" not in xdata
    assert "2022-01" in xdata
    assert "2022-12" in xdata


# ---------------------------------------------------------------------------
# 2. build_trend_chart: ax= parameter draws onto the given Axes
# ---------------------------------------------------------------------------


def test_build_trend_chart_with_existing_ax_draws_onto_it_not_a_new_figure():
    outer_fig, outer_ax = plt.subplots()
    series_by_lang = {"en": _series("2023-01", 12, 100)}

    returned_fig = build_trend_chart(series_by_lang, "2023-01", "2023-12", ax=outer_ax)

    assert returned_fig is outer_ax.figure
    assert returned_fig is outer_fig
    assert len(outer_ax.get_lines()) == 1
    plt.close(outer_fig)


# ---------------------------------------------------------------------------
# 3. build_trend_chart: empty data must not raise or warn (no-legend bug)
# ---------------------------------------------------------------------------


def test_build_trend_chart_empty_series_by_lang_does_not_raise_or_warn():
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        fig = build_trend_chart({}, "2023-01", "2023-12")

    assert isinstance(fig, plt.Figure)
    user_warnings = [w for w in caught if issubclass(w.category, UserWarning)]
    assert user_warnings == [], f"unexpected UserWarning(s): {[str(w.message) for w in user_warnings]}"


def test_build_trend_chart_period_with_no_matching_months_does_not_raise_or_warn():
    # Data exists, but entirely outside the requested period -- same
    # "nothing to plot" situation as an empty dict, from build_trend_chart's
    # point of view (plotted_any stays False).
    series_by_lang = {"en": _series("2020-01", 12, 100)}

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        fig = build_trend_chart(series_by_lang, "2023-01", "2023-12")

    assert isinstance(fig, plt.Figure)
    user_warnings = [w for w in caught if issubclass(w.category, UserWarning)]
    assert user_warnings == []


# ---------------------------------------------------------------------------
# 4. build_growth_comparison_chart: None metric values excluded from bars
# ---------------------------------------------------------------------------


def test_build_growth_comparison_chart_excludes_none_metric_entries():
    results = [
        {"lang": "en", "yoy_growth": 0.2},
        {"lang": "de", "yoy_growth": None},
        {"lang": "fr", "yoy_growth": -0.1},
        {"lang": "pl", "yoy_growth": None},
    ]

    fig = build_growth_comparison_chart(results)

    ax = fig.axes[0]
    assert len(ax.patches) == 2  # only en and fr are plottable


def test_build_growth_comparison_chart_uses_custom_metric_key():
    results = [
        {"lang": "en", "share_yoy_growth": 0.3, "yoy_growth": None},
        {"lang": "de", "share_yoy_growth": None, "yoy_growth": 0.9},
    ]

    fig = build_growth_comparison_chart(results, metric_key="share_yoy_growth")

    ax = fig.axes[0]
    assert len(ax.patches) == 1  # only "en" has a share_yoy_growth value


# ---------------------------------------------------------------------------
# 5. build_growth_comparison_chart: zero plottable entries -- no crash
# ---------------------------------------------------------------------------


def test_build_growth_comparison_chart_all_none_does_not_raise():
    results = [
        {"lang": "en", "yoy_growth": None},
        {"lang": "de", "yoy_growth": None},
    ]

    fig = build_growth_comparison_chart(results)

    assert isinstance(fig, plt.Figure)
    ax = fig.axes[0]
    assert len(ax.patches) == 0


def test_build_growth_comparison_chart_empty_results_list_does_not_raise():
    fig = build_growth_comparison_chart([])

    assert isinstance(fig, plt.Figure)
    ax = fig.axes[0]
    assert len(ax.patches) == 0


# ---------------------------------------------------------------------------
# 6. save_chart_png: returns given path, writes a valid PNG
# ---------------------------------------------------------------------------


def test_save_chart_png_returns_path_and_writes_valid_png_magic_bytes(tmp_path):
    fig = build_growth_comparison_chart([{"lang": "en", "yoy_growth": 0.1}])
    output_path = str(tmp_path / "growth.png")

    result_path = save_chart_png(fig, output_path)

    assert result_path == output_path
    with open(output_path, "rb") as f:
        header = f.read(8)
    assert header.startswith(b"\x89PNG"), f"unexpected file header: {header!r}"


def test_save_chart_png_closes_the_figure(tmp_path):
    fig = build_trend_chart({"en": _series("2023-01", 3, 10)}, "2023-01", "2023-03")
    fig_num = fig.number
    save_chart_png(fig, str(tmp_path / "chart.png"))

    assert fig_num not in plt.get_fignums()


# ---------------------------------------------------------------------------
# 7. build_trend_chart: legend_ax draws the legend on a separate axes
# ---------------------------------------------------------------------------


def test_build_trend_chart_without_legend_ax_draws_legend_on_the_chart_axes():
    fig, ax = plt.subplots()
    series_by_lang = {"en": _series("2023-01", 12, 100), "de": _series("2023-01", 12, 50)}

    build_trend_chart(series_by_lang, "2023-01", "2023-12", ax=ax)

    assert ax.get_legend() is not None
    plt.close(fig)


def test_build_trend_chart_with_legend_ax_keeps_chart_axes_free_of_a_legend():
    fig, (ax, legend_ax) = plt.subplots(2, 1)
    series_by_lang = {"en": _series("2023-01", 12, 100), "de": _series("2023-01", 12, 50)}

    build_trend_chart(series_by_lang, "2023-01", "2023-12", ax=ax, legend_ax=legend_ax)

    # The whole point of legend_ax: it can never collide with the chart's
    # own data, because it isn't drawn on the chart's axes at all.
    assert ax.get_legend() is None
    assert legend_ax.get_legend() is not None
    labels = [t.get_text() for t in legend_ax.get_legend().get_texts()]
    assert "en" in labels and "de" in labels
    plt.close(fig)


# ---------------------------------------------------------------------------
# 8. build_trend_chart: spike/seasonal month markers
# ---------------------------------------------------------------------------


def test_build_trend_chart_marks_spike_and_seasonal_months():
    series_by_lang = {"en": _series("2023-01", 12, 100)}

    fig = build_trend_chart(
        series_by_lang,
        "2023-01",
        "2023-12",
        spikes_by_lang={"en": ["2023-03"]},
        seasonal_by_lang={"en": ["2023-09"]},
    )

    ax = fig.axes[0]
    # One line plus two scatter marker collections (spike x, seasonal o).
    assert len(ax.collections) == 2
    labels = [t.get_text() for t in ax.get_legend().get_texts()]
    assert any("spike" in label for label in labels)
    assert any("seasonal" in label for label in labels)


def test_build_trend_chart_no_spike_or_seasonal_months_adds_no_markers():
    series_by_lang = {"en": _series("2023-01", 12, 100)}

    fig = build_trend_chart(series_by_lang, "2023-01", "2023-12")

    ax = fig.axes[0]
    assert len(ax.collections) == 0
