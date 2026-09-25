"""
One-page PDF + Markdown report assembly, built on charts.py.

`run_data` contract (what M4's `analyze` must save via cache.save_run_data,
and what `report` loads via cache.load_run_data and passes to build_report):

    {
        "run_id": str,
        "topic": str,
        "period": {
            "start": "YYYY-MM", "end": "YYYY-MM",
            "reference_months_before": int,
            "reference_reason": str,
            "actual_history_months": int,   # total history actually available (period + reference)
            "target_history_months": int,   # metrics.REFERENCE_TARGET_MONTHS (36)
        },
        "series": {lang: {"YYYY-MM": views, ...}},
            # RAW (not spike-free) monthly views, restricted to the requested
            # period only -- charts show what was actually observed (including
            # a real spike or seasonal peak), while the growth/significance
            # numbers in "results" are computed from the spike-free series
            # separately. Reference-window months must never appear here.
        "spike_months": {lang: [months...]},    # optional, from
        "seasonal_months": {lang: [months...]}, #   metrics.classify_spikes_and_seasonal,
            # restricted to the requested period -- marks those months on the trend chart.
        "summary": str,  # optional: a 2-3 sentence plain-language summary
            # (e.g. the agent's own --summary). If omitted, default_summary()
            # builds one from "results".
        "results": [
            {"lang": str, "title": str, "yoy_growth": float|None,
             "share_yoy_growth": float|None, "momentum_6mo": float|None,
             "spikes_removed": int, "seasonal_peaks_kept": int,
             "significant": bool, "confidence": "high"|"medium"|"low",
             "reason": str,
             "reference_only_yoy_growth": float|None (optional, for the context note)},
            ...
        ],
        "missing": [lang, ...],
    }

Every function takes this dict apart explicitly rather than assuming extra
keys, so a missing optional field (e.g. reference_only_yoy_growth) degrades
gracefully instead of raising.
"""

import os

from wiki_interest import charts

BOT_WORDING = "exclude known bots and automated traffic"
# More than a handful of overlapping lines on one small chart stops being
# readable -- cap the trend chart to the top languages by rank, same as the
# table/summary use the rank order results is already saved in.
MAX_CHART_LANGS = 5


def method_note(period: dict) -> str:
    """
    The method & limits paragraph -- reference-window disclosure, matching
    notes/plan.md's `report` description almost verbatim. Always states the
    target and, if the actual history fell short, discloses that explicitly
    (notes/plan.md's "may be shorter when data is unavailable" note).
    """
    target = period.get("target_history_months", 36)
    actual = period.get("actual_history_months")
    reference_months = period.get("reference_months_before", 0)

    shortfall = ""
    if actual is not None and actual < target:
        shortfall = f" Only {actual} months of history were actually available (short of the {target}-month target), so seasonal-vs-spike classification here is less reliable."

    note = (
        f"Charts and tables above cover only the requested period ({period['start']} to {period['end']}). "
        f"{reference_months} additional month(s) before that period were fetched only to classify peaks as "
        f"seasonal vs. one-off, targeting {target} months of total history.{shortfall} "
        f"Pageview counts {BOT_WORDING}."
    )
    if period.get("used_reference_baseline_for_yoy"):
        note += (
            " The requested period is under 24 months, so the year-over-year comparison above borrows its "
            "prior-year baseline from that earlier reference data (not shown in the charts)."
        )
    return note


def build_context_note(results: list) -> str:
    """
    An optional short note when a language's reference-only trend
    (reference_only_yoy_growth, if provided) points the opposite direction
    from its requested-period yoy_growth -- e.g. the requested period shows
    growth but the longer-term picture was actually declining before it.
    Separate from, and never overriding, the main conclusion -- this
    returns "" (no note) unless at least one language has both values and
    they disagree in sign.
    """
    flagged = []
    for r in results:
        period_growth = r.get("yoy_growth")
        reference_growth = r.get("reference_only_yoy_growth")
        if period_growth is None or reference_growth is None:
            continue
        if (period_growth > 0) != (reference_growth > 0):
            flagged.append(r["lang"])
    if not flagged:
        return ""
    return (
        f"Context: for {', '.join(sorted(flagged))}, the longer-term reference-period trend points in the "
        "opposite direction from the requested-period trend above. This doesn't change the main conclusion "
        "for the requested period, but is worth knowing before treating that trend as durable."
    )


def _fmt_pct(value) -> str:
    return "n/a" if value is None else f"{value * 100:+.1f}%"


def _fmt_views(value) -> str:
    return "n/a" if value is None else f"{value:,.0f}"


_MONTH_NAMES = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]


def _month_label(yyyymm: str) -> str:
    year, month = yyyymm.split("-")
    return f"{_MONTH_NAMES[int(month) - 1]} {year}"


def _detect_step_change(monthly: dict, period_start: str, period_end: str, min_fraction: float = 0.5):
    """
    If most of the overall change across the period happened in a single
    month-to-month step (rather than gradually), return that month and the
    fraction of the total change it accounts for; else None. Looks only at
    steps in the SAME direction as the overall change, since a step
    against the grain can't be "most of" a decline (or growth).
    """
    months = sorted(m for m in monthly if period_start <= m <= period_end)
    if len(months) < 3:
        return None
    values = [monthly[m] for m in months]
    total_change = values[-1] - values[0]
    if total_change == 0:
        return None

    biggest_step_month, biggest_step_value = None, 0
    for i in range(1, len(values)):
        step = values[i] - values[i - 1]
        if (step > 0) == (total_change > 0) and abs(step) > abs(biggest_step_value):
            biggest_step_month, biggest_step_value = months[i], step

    if biggest_step_month is None:
        return None
    fraction = abs(biggest_step_value) / abs(total_change)
    if fraction < min_fraction:
        return None
    return {"month": biggest_step_month, "fraction": fraction}


def _momentum_divergence_note(yoy_growth, momentum_6mo) -> str:
    """A sentence noting when the last 6 months tell a meaningfully different story than the full year."""
    if yoy_growth is None or momentum_6mo is None or yoy_growth == 0:
        return ""
    if (yoy_growth > 0) != (momentum_6mo > 0):
        return f" The most recent 6 months actually point the other way ({_fmt_pct(momentum_6mo)}), so this may be leveling off or reversing."
    ratio = abs(momentum_6mo) / abs(yoy_growth)
    if ratio < 0.5:
        return f" The most recent 6 months show a smaller change ({_fmt_pct(momentum_6mo)}) than the full year, suggesting this may be slowing down."
    if ratio > 1.8:
        return f" The most recent 6 months show a bigger change ({_fmt_pct(momentum_6mo)}) than the full year, suggesting this may be accelerating."
    return ""


def default_summary(run_data: dict) -> str:
    """
    A plain-language summary (the answer, the key number, and how much to
    trust it) for when the caller doesn't supply run_data["summary"]
    directly (e.g. the agent's own --summary). The headline is
    with_growth[0] -- the actual TOP-RANKED result, not "whichever language
    happens to have the largest |yoy_growth|" -- because run_data["results"]
    is saved by cli.py in --rank-by order (ranked entries first, best
    first; see cli.py's run_analyze), so this is genuinely the #1 result a
    reader would find at the top of the table below, not a value picked
    independently of the ranking the rest of the report shows. Normally
    2-3 sentences; grows to explain itself further when the headline is
    large but not statistically significant, when recent momentum tells a
    different story than the full year, or (multi-language) to name the
    weakest performer and how many languages are growing vs. declining.
    """
    results = run_data.get("results", [])
    topic = run_data.get("topic", "this topic")
    if not results:
        return f'No pageview data was found for "{topic}" in any requested language.'

    with_growth = [r for r in results if r.get("yoy_growth") is not None]
    if not with_growth:
        return f'None of the languages checked for "{topic}" had enough prior activity to measure a growth trend yet.'

    headline = with_growth[0]
    direction = "grown" if headline["yoy_growth"] > 0 else "declined"
    pct = abs(headline["yoy_growth"]) * 100
    lang_note = f" in {headline['lang']}" if len(results) > 1 else ""
    confidence = headline.get("confidence", "unknown")

    sentences = [f'Interest in "{topic}"{lang_note} has {direction} by about {pct:.0f}% over the past year.']
    if headline.get("significant"):
        sentences.append(f"This is a statistically meaningful change, with {confidence} confidence overall.")
    else:
        explanation = f"This change isn't statistically strong enough to be confident about yet ({confidence} confidence overall)."
        # A large but not-significant change is often explained by a single
        # sharp step rather than a gradual trend -- worth naming if so. Skip
        # this extra detail once there are many languages in play: the table
        # already carries the per-language detail, and the summary has to
        # stay short enough to fit above it on a busy page.
        if abs(headline["yoy_growth"]) >= 0.2 and len(results) <= 6:
            period = run_data.get("period", {})
            monthly = run_data.get("series", {}).get(headline["lang"], {})
            step = _detect_step_change(monthly, period.get("start", ""), period.get("end", ""))
            if step:
                explanation += f" Most of the change happened in {_month_label(step['month'])}; since then it's been roughly stable."
        sentences.append(explanation)

    if len(results) <= 6:
        sentences[-1] += _momentum_divergence_note(headline.get("yoy_growth"), headline.get("momentum_6mo"))

    others = [r for r in with_growth if r is not headline]
    if others:
        # Named by actual VALUE, not by position in the list -- "weakest"
        # is whoever has the lowest signed yoy_growth (the worst decliner,
        # or the smallest grower), which is not necessarily whichever
        # language happened to be requested last.
        growing = sum(1 for r in with_growth if r["yoy_growth"] > 0)
        declining = sum(1 for r in with_growth if r["yoy_growth"] < 0)
        weakest = min(with_growth, key=lambda r: r["yoy_growth"])
        counts_sentence = f"Of the {len(with_growth)} languages with enough data to measure, {growing} are growing and {declining} are declining"
        if weakest is not headline:
            counts_sentence += f"; {weakest['lang'].upper()} shows the weakest trend, at {_fmt_pct(weakest['yoy_growth'])}"
        sentences.append(counts_sentence + ".")

    return " ".join(sentences)


def plain_trust_note(result: dict, period: dict = None) -> str:
    """
    A plain-language sentence explaining how much to trust `result`, with
    no code identifiers -- unlike result["reason"] (metrics.compute_confidence's
    internal, technical reason string, e.g. "share_yoy_growth sign disagrees
    with yoy_growth"), this is meant for a non-technical reader.
    """
    yoy = result.get("yoy_growth")
    share = result.get("share_yoy_growth")
    confidence = result.get("confidence", "unknown")

    if yoy is None:
        return "There isn't enough prior activity here to measure a reliable trend yet."

    parts = []
    if result.get("significant"):
        parts.append("this change is statistically meaningful")
    else:
        parts.append("this change is not statistically strong enough to be confident about")

    if share is not None:
        agrees = (share > 0) == (yoy > 0)
        if agrees:
            parts.append("and the pattern holds after accounting for overall Wikipedia traffic")
        else:
            parts.append("but the pattern looks different once overall Wikipedia traffic is accounted for, so treat it cautiously")

    if period is not None:
        actual = period.get("actual_history_months")
        target = period.get("target_history_months", 36)
        if actual is not None and actual < target:
            parts.append(f"and only {actual} months of history were available, so this is less certain than it would be with a full history")

    sentence = " ".join(parts)
    sentence = sentence[0].upper() + sentence[1:]
    return f"{sentence} (overall confidence: {confidence})."


def render_markdown(run_data: dict) -> str:
    """Build the full Markdown report text."""
    period = run_data["period"]
    results = run_data["results"]
    missing = run_data.get("missing", [])

    lines = [f"# Wiki interest report: {run_data.get('topic', '(topic)')}", ""]
    lines.append(f"Period: {period['start']} to {period['end']}")
    lines.append("")
    lines.append(run_data.get("summary") or default_summary(run_data))
    lines.append("")

    if not results:
        lines.append("**No data found for this topic.**" if missing else "**No data found for this topic** (no languages were requested).")
        lines.append("")

    lines.append("## Key numbers")
    lines.append("")
    if results:
        lines.append("| Language | Title | Avg. monthly views | Year-over-year | vs. all Wikipedia traffic | Last 6 months | Steady trend? | Confidence |")
        lines.append("|---|---|---|---|---|---|---|---|")
        for r in results:
            lines.append(
                f"| {r['lang']} | {r.get('title', '')} | {_fmt_views(r.get('avg_monthly_views'))} | {_fmt_pct(r.get('yoy_growth'))} | "
                f"{_fmt_pct(r.get('share_yoy_growth'))} | {_fmt_pct(r.get('momentum_6mo'))} | "
                f"{'yes' if r.get('significant') else 'no'} | {r.get('confidence', 'n/a')} |"
            )
    else:
        lines.append("(no results)")
    lines.append("")

    if missing:
        lines.append(f"**Not found:** {', '.join(sorted(missing))} (possible content gap, not an error)")
        lines.append("")

    if results:
        lines.append("## Trust notes")
        lines.append("")
        for r in results:
            lines.append(f"- **{r['lang']}**: {plain_trust_note(r, period)}")
        lines.append("")

    context = build_context_note(results)
    if context:
        lines.append("## Context")
        lines.append("")
        lines.append(context)
        lines.append("")

    lines.append("## Method & limits")
    lines.append("")
    lines.append(method_note(period))
    lines.append("")

    return "\n".join(lines)


def _fit_block(lines: list, row_height_ratio: float, total_height_ratio: float, page_height_inches: float = 11.0, base_fontsize: float = 9.0, min_fontsize: float = 6.0, wrap_width: int = 100) -> tuple:
    """
    Wrap `lines` and pick a fontsize that fits the wrapped text within the
    vertical space a gridspec row of `row_height_ratio` actually gets --
    shrinking (never growing) from `base_fontsize` down to `min_fontsize`
    if there'd be more wrapped lines than comfortably fit. Content length
    here is inherently variable (more languages = more trust-note lines),
    so a fixed fontsize risks overlapping the next section for reports
    with several languages. Returns (fontsize, wrapped_text).
    """
    wrapped = _wrap_lines(lines, width=wrap_width) if lines else ""
    n_lines = max(1, wrapped.count("\n") + 1)
    # Empirically calibrated (not a precise geometric model): matplotlib's
    # actual rendered line spacing runs noticeably taller than a naive
    # fontsize/72*1.35 estimate once the section header, hspace, and the
    # block's own top offset are accounted for -- a lighter safety factor
    # here visibly overflowed into the next section during manual testing.
    available_inches = page_height_inches * (row_height_ratio / total_height_ratio) * 0.42
    max_lines_at_base = available_inches / (base_fontsize / 72 * 1.35)
    fontsize = base_fontsize if n_lines <= max_lines_at_base else max(min_fontsize, base_fontsize * max_lines_at_base / n_lines)
    return fontsize, wrapped


def _body_start_y(row_height_ratio: float, total_height_ratio: float, page_height_inches: float, heading_fontsize: float, padding_pt: float = 3.0) -> float:
    """
    The y (axes-fraction, 1.0 = top) where a section's body text can start
    without colliding with its own bold heading directly above it. A fixed
    offset (e.g. always y=0.82) looks fine for a row with generous height,
    but the trust-notes/method rows can shrink a lot when many languages
    push extra height into the table above them (see render_pdf's
    extra_table_share) -- the same 18%-of-the-row gap that comfortably
    clears a 12pt heading in a tall row can be smaller than the heading's
    own rendered height in a squeezed one, so the body's first line ends
    up drawn right on top of the heading. Uses the same row-height-to-
    inches conversion (the "0.42" fudge factor) _fit_block already relies
    on, so it stays consistent with how much of this row's nominal height
    actually renders.
    """
    row_height_inches = page_height_inches * (row_height_ratio / total_height_ratio) * 0.42
    heading_height_inches = (heading_fontsize / 72) * 1.35 + (padding_pt / 72)
    if row_height_inches <= 0:
        return 0.75
    return max(0.45, 1.0 - heading_height_inches / row_height_inches)


def render_pdf(run_data: dict, output_path: str) -> str:
    """
    One-page PDF: header, plain-language summary, key numbers table, trend
    chart (marked with spike/seasonal months if given), a growth-comparison
    chart only when there are >=2 series to compare, plain-language trust
    notes, method & limits. Returns output_path.
    """
    import matplotlib.pyplot as plt

    period = run_data["period"]
    results = run_data["results"]
    series = run_data.get("series", {})
    missing = run_data.get("missing", [])
    spike_months = run_data.get("spike_months", {})
    seasonal_months = run_data.get("seasonal_months", {})

    show_growth_chart = len(results) >= 2
    summary_row_index = 1
    # A dedicated row for the trend chart's legend (index 4) -- not
    # attached below the chart's own axes via a bbox_to_anchor offset.
    # That was tried first and was fragile: its exact footprint depends on
    # how many rows matplotlib wraps the legend to, which varies with
    # entry count, and a fixed offset that cleared the chart's own
    # (rotated) x-tick labels kept either colliding with whatever came
    # after it or, pulled closer, colliding with those tick labels
    # instead. A real gridspec row can only ever occupy its own space.
    if show_growth_chart:
        n_rows, height_ratios = 8, [0.6, 1.05, 1.2, 2.85, 0.4, 1.25, 1.55, 1.3]
    else:
        n_rows, height_ratios = 7, [0.6, 1.05, 1.2, 3.45, 0.4, 1.65, 1.4]
    # More languages need a taller table row; split the extra space between
    # the trend chart (which has generous headroom to begin with) and trust
    # notes (which already shrinks its own font for long content -- see
    # _fit_block -- rather than needing a fixed row height itself).
    table_row_index = 2
    trend_row_index = 3
    trust_row_index_estimate = n_rows - 2
    extra_table_share = min(1.4, max(0.0, (len(results) - 4)) * 0.4)
    height_ratios[table_row_index] += extra_table_share
    from_trend = min(extra_table_share * 0.5, height_ratios[trend_row_index] - 1.5)
    height_ratios[trend_row_index] -= from_trend
    height_ratios[trust_row_index_estimate] = max(1.0, height_ratios[trust_row_index_estimate] - (extra_table_share - from_trend))
    total_ratio = sum(height_ratios)
    page_height_inches = 11.0

    fig = plt.figure(figsize=(8.5, page_height_inches))
    # left/right narrower than matplotlib's ~12.5%/10% defaults -- the page
    # was leaving real usable width on the table unused. top/bottom left at
    # their defaults deliberately: _fit_block's vertical-fit calibration
    # (the "0.42" fudge factor) was measured against the default vertical
    # margins, and this fix is specifically about horizontal space.
    grid = fig.add_gridspec(n_rows, 1, height_ratios=height_ratios, hspace=1.1, left=0.08, right=0.97)
    row = iter(range(n_rows))

    # -- header --
    ax_header = fig.add_subplot(grid[next(row)])
    ax_header.axis("off")
    topic = run_data.get("topic", "(topic)")
    ax_header.text(0, 1.0, f"Wiki interest report: {topic}", fontsize=16, fontweight="bold", va="top")
    ax_header.text(0, 0.0, f"Period: {period['start']} to {period['end']}", fontsize=9, va="top", color="#444444")
    if not results:
        ax_header.text(1.0, 0.0, "No data found for this topic.", fontsize=10, va="top", ha="right", color="#b91c1c", fontweight="bold")

    # -- plain-language summary (dynamically sized: the summary can now grow
    # to explain a step-change or momentum divergence, see default_summary) --
    ax_summary = fig.add_subplot(grid[next(row)])
    ax_summary.axis("off")
    summary_text = run_data.get("summary") or default_summary(run_data)
    summary_fontsize, summary_wrapped = _fit_block([summary_text], height_ratios[summary_row_index], total_ratio, page_height_inches, base_fontsize=10.5, min_fontsize=8.0, wrap_width=88)
    ax_summary.text(0, 1.0, summary_wrapped, fontsize=summary_fontsize, va="top")

    # -- key numbers table --
    ax_table = fig.add_subplot(grid[next(row)])
    ax_table.axis("off")
    col_labels = ["Language", "Avg. views/mo", "Year-over-year", "vs. all Wikipedia traffic", "Last 6 months", "Steady trend?", "Confidence"]
    all_rows = [
        [
            r["lang"],
            _fmt_views(r.get("avg_monthly_views")),
            _fmt_pct(r.get("yoy_growth")),
            _fmt_pct(r.get("share_yoy_growth")),
            _fmt_pct(r.get("momentum_6mo")),
            "yes" if r.get("significant") else "no",
            r.get("confidence", "n/a"),
        ]
        for r in results
    ]
    # Font never shrinks below MIN_TABLE_FONTSIZE -- a printed table
    # smaller than that stops being reliably readable, so past that point
    # the only lever left is showing fewer rows (same pattern as the trust
    # notes cap below), not smaller and smaller text. Start from a generous
    # cap and let the measured height-correction loop below trim it down to
    # what actually fits -- an UPFRONT closed-form prediction of "how many
    # rows fit at 8pt" was tried and measured badly short (the "0.42"
    # fudge factor is calibrated for _fit_block's text blocks, not a
    # matplotlib Table, and the two don't transfer 1:1), so real
    # measurement, not a formula, decides the row count here.
    MIN_TABLE_FONTSIZE = 8.0
    max_table_rows = 8
    rows = all_rows[:max_table_rows]
    omitted_rows = len(all_rows) - len(rows)
    empty_row = [["(no results)"] + [""] * (len(col_labels) - 1)]
    table = ax_table.table(cellText=rows or empty_row, colLabels=col_labels, loc="center", cellLoc="center")
    table.auto_set_font_size(False)
    n_total_rows = len(rows) + 1  # + header row
    fontsize = 10.5 if n_total_rows <= 5 else MIN_TABLE_FONTSIZE
    scale_y = 1.7 if n_total_rows <= 5 else 1.0
    table.set_fontsize(fontsize)
    table.scale(1, scale_y)
    table.auto_set_column_width(list(range(len(col_labels))))
    # A matplotlib table's row height is a fixed size driven by fontsize
    # and scale, NOT by how tall its containing axes actually is -- and
    # this GridSpec's hspace eats a large, non-obvious share of each row's
    # nominal height, so a closed-form fontsize/scale formula calibrated
    # against one config drifted badly when reused at another (verified:
    # it either overflowed for many languages or over-shrank for few).
    # Measuring the table's actual rendered size and correcting once is
    # exact regardless of fontsize, since scale multiplies row height
    # linearly (confirmed empirically).
    fig.canvas.draw()
    renderer = fig.canvas.get_renderer()
    table_bbox = table.get_window_extent(renderer)
    axes_bbox = ax_table.get_window_extent()
    # Unlike row height, auto_set_column_width has no notion of the axes'
    # actual width either -- it sizes each column from its widest cell's
    # rendered text, and the columns' total can simply run off the page
    # edges if the content is wide (verified: adding the volume column
    # pushed "Language" and "Confidence" off both sides at the previous
    # fontsize). Same measured correction, applied horizontally -- but
    # unlike row height, matplotlib's Table re-runs auto_set_column_width's
    # own sizing on every subsequent draw (Table._update_positions() calls
    # _auto_set_column_width() for every column index it remembers being
    # auto-set), which would silently undo this scale() the next time the
    # figure is drawn (e.g. at savefig()) -- clearing that memory makes the
    # correction actually stick.
    if table_bbox.width > axes_bbox.width:
        correction_x = axes_bbox.width / table_bbox.width
        table.scale(correction_x, 1)
        table._autoColumns = []
        # The cell boxes are now narrower, but the text inside them is
        # still sized for the wider boxes auto_set_column_width originally
        # fit it to -- shrink the font by the same factor so header/cell
        # text doesn't overflow into its neighbor. Floored at
        # MIN_TABLE_FONTSIZE like everywhere else -- the wider margins
        # below make this correction small in practice (verified), so
        # hitting the floor here would mean genuinely too much column
        # content for the page, not a routine case.
        fontsize = max(MIN_TABLE_FONTSIZE, fontsize * correction_x)
        table.set_fontsize(fontsize)
    # Leave a clear strip below the table for the "...and N more" note.
    target_frac = 0.82 if omitted_rows else 0.95
    if fontsize > MIN_TABLE_FONTSIZE + 0.01:
        # Comfortable headroom above the font floor (the common few-
        # language case, base fontsize 10.5) -- a small scale compress is
        # harmless here, same as this always worked before the floor rule
        # existed at all.
        if table_bbox.height > axes_bbox.height * target_frac:
            correction = (axes_bbox.height * target_frac) / table_bbox.height
            table.scale(1, correction)
    else:
        # Already at MIN_TABLE_FONTSIZE -- scale() shrinks a row's box
        # without shrinking the text inside it, so compressing further
        # risks the same row-into-row text overlap the font floor exists
        # to prevent, just vertically instead of horizontally. Drop rows
        # and rebuild instead (same pattern as the trust-notes cap).
        while table_bbox.height > axes_bbox.height * target_frac and len(rows) > 1:
            rows = rows[:-1]
            omitted_rows += 1
            target_frac = 0.82
            table.remove()
            table = ax_table.table(cellText=rows, colLabels=col_labels, loc="center", cellLoc="center")
            table.auto_set_font_size(False)
            table.set_fontsize(fontsize)
            table.scale(1, scale_y)
            table.auto_set_column_width(list(range(len(col_labels))))
            fig.canvas.draw()
            renderer = fig.canvas.get_renderer()
            table_bbox = table.get_window_extent(renderer)
            axes_bbox = ax_table.get_window_extent()
            if table_bbox.width > axes_bbox.width:
                correction_x = axes_bbox.width / table_bbox.width
                table.scale(correction_x, 1)
                table._autoColumns = []
                fontsize = max(MIN_TABLE_FONTSIZE, fontsize * correction_x)
                table.set_fontsize(fontsize)
    if omitted_rows:
        ax_table.text(0.5, 0.02, f"...and {omitted_rows} more language(s) -- see the Markdown report for the full table.", fontsize=7, ha="center", va="top", transform=ax_table.transAxes, style="italic", color="#444444")

    # -- trend chart (drawn directly onto this page's subplot, not copied
    # from a standalone figure -- see charts.py's ax= parameter), with its
    # legend on its own dedicated row right after it (see build_trend_chart's
    # legend_ax docstring for why) -- capped to the top MAX_CHART_LANGS by
    # rank, since more than a handful of overlapping lines stops being
    # readable; results is already in rank order (see cli.py's run_analyze),
    # so results[:MAX_CHART_LANGS] is genuinely the top N, not an arbitrary slice.
    chart_langs = [r["lang"] for r in results[:MAX_CHART_LANGS]]
    chart_series = {lang: monthly for lang, monthly in series.items() if lang in chart_langs}
    omitted_chart_langs = max(0, len(results) - MAX_CHART_LANGS)
    ax_trend = fig.add_subplot(grid[next(row)])
    ax_trend_legend = fig.add_subplot(grid[next(row)])
    charts.build_trend_chart(
        chart_series,
        period["start"],
        period["end"],
        ax=ax_trend,
        spikes_by_lang=spike_months,
        seasonal_by_lang=seasonal_months,
        legend_ax=ax_trend_legend,
        omitted_lang_count=omitted_chart_langs,
    )

    # -- growth comparison chart: only meaningful with >=2 series to compare --
    if show_growth_chart:
        ax_growth = fig.add_subplot(grid[next(row)])
        charts.build_growth_comparison_chart(results, ax=ax_growth)

    # -- trust notes (plain language, no code identifiers) --
    trust_row_index = next(row)
    ax_trust = fig.add_subplot(grid[trust_row_index])
    ax_trust.axis("off")
    ax_trust.text(0, 1.0, "Trust notes", fontsize=12, fontweight="bold", va="top")
    trust_lines = []
    if missing:
        trust_lines.append(f"Not found: {', '.join(sorted(missing))} (possible content gap, not an error)")
    per_lang_notes = [f"{r['lang']}: {plain_trust_note(r, period)}" for r in results]
    # A per-language note for every result doesn't fit a one-page PDF once
    # there are many languages (font-shrinking alone bottoms out, since the
    # table above is also claiming extra height at that point); cap it here
    # and point to the Markdown report, which has no such limit. Tighter
    # once the table itself is eating into this row's space.
    max_trust_notes = 4 if len(results) <= 8 else 2
    if len(per_lang_notes) > max_trust_notes:
        omitted = len(per_lang_notes) - max_trust_notes
        trust_lines.extend(per_lang_notes[:max_trust_notes])
        trust_lines.append(f"...and {omitted} more language(s) -- see the Markdown report for full detail.")
    else:
        trust_lines.extend(per_lang_notes)
    context = build_context_note(results)
    if context:
        trust_lines.append(context)
    trust_heading_fontsize = 12
    trust_body_y = _body_start_y(height_ratios[trust_row_index], total_ratio, page_height_inches, trust_heading_fontsize)
    trust_fontsize, trust_wrapped = _fit_block(trust_lines, height_ratios[trust_row_index] * trust_body_y, total_ratio, page_height_inches, base_fontsize=9.0, wrap_width=100)
    ax_trust.text(0, trust_body_y, trust_wrapped, fontsize=trust_fontsize, va="top")

    # -- method & limits --
    method_row_index = next(row)
    ax_method = fig.add_subplot(grid[method_row_index])
    ax_method.axis("off")
    ax_method.text(0, 1.0, "Method & limits", fontsize=12, fontweight="bold", va="top")
    method_heading_fontsize = 12
    method_body_y = _body_start_y(height_ratios[method_row_index], total_ratio, page_height_inches, method_heading_fontsize)
    method_fontsize, method_wrapped = _fit_block([method_note(period)], height_ratios[method_row_index] * method_body_y, total_ratio, page_height_inches, base_fontsize=9.0, wrap_width=105)
    ax_method.text(0, method_body_y, method_wrapped, fontsize=method_fontsize, va="top")

    fig.savefig(output_path, format="pdf")
    plt.close(fig)
    return output_path


def _wrap_text(text: str, width: int) -> str:
    import textwrap

    return "\n".join(textwrap.wrap(text, width=width))


def _wrap_lines(lines: list, width: int) -> str:
    return "\n".join(_wrap_text(line, width) for line in lines)


def build_report(run_data: dict, output_dir: str) -> dict:
    """
    Generate all three report artifacts into output_dir: report.pdf,
    chart.png (the standalone trend chart), report.md. Returns
    {"pdf": path, "png": path, "md": path}.
    """
    os.makedirs(output_dir, exist_ok=True)

    period = run_data["period"]
    series = run_data.get("series", {})
    results = run_data.get("results", [])

    # Same top-MAX_CHART_LANGS-by-rank cap as render_pdf's embedded chart --
    # this standalone chart.png should show the same picture, not a
    # different, uncapped one.
    chart_langs = [r["lang"] for r in results[:MAX_CHART_LANGS]]
    chart_series = {lang: monthly for lang, monthly in series.items() if lang in chart_langs}
    omitted_chart_langs = max(0, len(results) - MAX_CHART_LANGS)

    trend_fig = charts.build_trend_chart(
        chart_series,
        period["start"],
        period["end"],
        title=f"Monthly interest: {run_data.get('topic', '')}",
        spikes_by_lang=run_data.get("spike_months", {}),
        seasonal_by_lang=run_data.get("seasonal_months", {}),
        omitted_lang_count=omitted_chart_langs,
    )
    png_path = charts.save_chart_png(trend_fig, os.path.join(output_dir, "chart.png"))

    pdf_path = render_pdf(run_data, os.path.join(output_dir, "report.pdf"))

    md_path = os.path.join(output_dir, "report.md")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(render_markdown(run_data))

    return {"pdf": pdf_path, "png": png_path, "md": md_path}
