"""
PNG chart generation for wiki-interest reports.

Every function here builds a matplotlib Figure and returns it -- it's
report.py's job to save one standalone (chart.png) and/or embed figures
into the PDF page. Uses the "Agg" backend since this runs headless (no
display), and no numpy-heavy plotting beyond what matplotlib needs itself.
"""

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt  # noqa: E402

# A brand-neutral, colorblind-friendlyish palette, cycled across languages.
_LINE_COLORS = ["#2563eb", "#dc2626", "#16a34a", "#9333ea", "#ea580c", "#0891b2"]


def build_trend_chart(
    series_by_lang: dict,
    period_start: str,
    period_end: str,
    title: str = "Monthly interest",
    ax=None,
    spikes_by_lang: dict = None,
    seasonal_by_lang: dict = None,
    legend_ax=None,
):
    """
    Line chart of monthly views per language, restricted to
    [period_start, period_end] -- reference-window months must never be
    passed in here (charts cover only the requested period, notes/plan.md).

    `series_by_lang`: {lang: {"month": views, ...}} (api_client/metrics'
    zero-filled monthly series shape, already spike-free if desired).

    `spikes_by_lang`/`seasonal_by_lang` (optional): {lang: set/list of
    "YYYY-MM"} from metrics.classify_spikes_and_seasonal -- if given, those
    months are marked on the line (x = one-off spike, o = recurring
    seasonal peak) so a reader can see which bumps were excluded from the
    growth numbers and which were kept as real seasonal pattern.

    Draws onto `ax` if given (e.g. a subplot in a larger PDF page), else
    creates its own standalone Figure. Either way, returns the Figure.

    `legend_ax` (optional): draw the legend on a SEPARATE axes instead of
    below this chart's own axes. For a standalone chart (no `legend_ax`),
    the legend sits just below the plot via bbox_to_anchor, which
    save_chart_png's bbox_inches="tight" happily grows the image to fit.
    But embedded in a larger fixed-size page (the PDF), that same
    below-axes offset is fragile -- its exact footprint depends on how
    many legend rows matplotlib wraps to, which varies with entry count
    and page width, and guessing a fixed offset that always clears the
    chart's own (rotated) x-tick labels without colliding with whatever
    comes after was not reliable in practice. A dedicated gridspec row
    for the legend sidesteps that: it can only ever occupy its own
    allocated space, never anyone else's.
    """
    standalone = ax is None
    if standalone:
        fig, ax = plt.subplots(figsize=(8, 3.2))
    else:
        fig = ax.figure
    plotted_any = False
    marker_handles = {}
    for i, (lang, monthly) in enumerate(sorted(series_by_lang.items())):
        months = sorted(m for m in monthly if period_start <= m <= period_end)
        if not months:
            continue
        values = [monthly[m] for m in months]
        color = _LINE_COLORS[i % len(_LINE_COLORS)]
        ax.plot(months, values, label=lang, color=color, linewidth=1.8)
        plotted_any = True

        spike_months = set((spikes_by_lang or {}).get(lang, ()))
        seasonal_months = set((seasonal_by_lang or {}).get(lang, ()))
        spike_x = [m for m in months if m in spike_months]
        if spike_x:
            ax.scatter(spike_x, [monthly[m] for m in spike_x], marker="x", color=color, s=45, zorder=5, linewidths=1.6)
            marker_handles["one-off spike (excluded)"] = ("x", "#333333")
        seasonal_x = [m for m in months if m in seasonal_months]
        if seasonal_x:
            ax.scatter(seasonal_x, [monthly[m] for m in seasonal_x], marker="o", facecolors="none", edgecolors=color, s=55, zorder=5, linewidths=1.4)
            marker_handles["recurring seasonal peak (kept)"] = ("o", "#333333")

    ax.set_title(title, fontsize=11, fontweight="bold")
    ax.set_ylabel("monthly views")
    if plotted_any:
        handles, labels = ax.get_legend_handles_labels()
        # Cap per-language legend entries so a many-language chart doesn't
        # get its whole plot area swallowed by the legend; the lines
        # themselves are always drawn regardless of the cap.
        max_language_entries = 8
        if len(labels) > max_language_entries:
            omitted = len(labels) - max_language_entries
            handles = handles[:max_language_entries] + [plt.Line2D([0], [0], color="none")]
            labels = labels[:max_language_entries] + [f"(+{omitted} more not shown in legend)"]
        for marker_label, (marker, color) in marker_handles.items():
            handles.append(plt.Line2D([0], [0], marker=marker, color="none", markeredgecolor=color, markerfacecolor="none" if marker == "o" else color, markersize=6))
            labels.append(marker_label)
        legend_fontsize = 7.5 if len(labels) <= 10 else 6.0
        ncol = min(len(labels), 4) if len(labels) > 3 else len(labels)
        if legend_ax is not None:
            # A dedicated row -- centered within it, can't collide with anything else.
            legend_ax.axis("off")
            legend_ax.legend(handles, labels, loc="center", fontsize=legend_fontsize, frameon=False, ncol=ncol)
        else:
            # Standalone chart: below the plot, not inside it -- "upper left"
            # (or any inside-axes placement) can sit directly on top of a
            # data line, e.g. when a series starts high in the top-left
            # corner. Below the x-axis is never occupied by data.
            ax.legend(handles, labels, loc="upper center", bbox_to_anchor=(0.5, -0.27), fontsize=legend_fontsize, frameon=False, ncol=ncol)
    else:
        ax.text(0.5, 0.5, "No data for this period", ha="center", va="center", fontsize=9, transform=ax.transAxes)
    _sparse_xticks(ax, series_by_lang, period_start, period_end)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    if standalone:
        fig.tight_layout()
    return fig


def build_growth_comparison_chart(results: list, metric_key: str = "yoy_growth", title: str = "YoY growth by language", ax=None):
    """
    Bar chart comparing `metric_key` (default: YoY growth) across languages.
    Entries whose metric_key value is None (zero-baseline/emerging topics,
    see metrics.rank_series) are skipped -- there's no bar to draw for a
    growth rate that doesn't exist, not zero.

    Draws onto `ax` if given, else creates its own standalone Figure.
    """
    standalone = ax is None
    if standalone:
        fig, ax = plt.subplots(figsize=(8, 2.6))
    else:
        fig = ax.figure

    plotted = [(r["lang"], r[metric_key]) for r in results if r.get(metric_key) is not None]
    plotted.sort(key=lambda pair: pair[1], reverse=True)

    if not plotted:
        ax.text(0.5, 0.5, "No languages with a comparable growth figure", ha="center", va="center", fontsize=9, transform=ax.transAxes)
        ax.axis("off")
        return fig

    langs = [lang for lang, _ in plotted]
    values = [value * 100 for _, value in plotted]
    colors = ["#16a34a" if v >= 0 else "#dc2626" for v in values]
    ax.bar(langs, values, color=colors)
    ax.axhline(0, color="#333333", linewidth=0.8)
    ax.set_title(title, fontsize=11, fontweight="bold")
    ax.set_ylabel("% change")
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    if standalone:
        fig.tight_layout()
    return fig


def save_chart_png(fig, output_path: str) -> str:
    """Save a matplotlib Figure as PNG and close it (frees memory). Returns output_path."""
    fig.savefig(output_path, format="png", dpi=150, bbox_inches="tight")
    plt.close(fig)
    return output_path


def _sparse_xticks(ax, series_by_lang, period_start, period_end):
    """Thin out x-axis month labels so they don't overlap on a long period."""
    all_months = sorted({m for monthly in series_by_lang.values() for m in monthly if period_start <= m <= period_end})
    if not all_months:
        return
    step = max(1, len(all_months) // 8)
    ax.set_xticks(all_months[::step])
    ax.tick_params(axis="x", labelrotation=45, labelsize=7)
