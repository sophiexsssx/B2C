"""
Tests for wiki_interest.report (one-page PDF + Markdown report assembly,
built on charts.py). See report.py's module docstring for the exact
`run_data` dict contract every function here expects.

No network I/O -- these hand-build run_data dicts and inspect the generated
files. matplotlib's default PDF backend compresses its content stream with
FlateDecode, so raw byte/string search on the .pdf file will NOT find
embedded text -- extraction must go through
`pypdf.PdfReader(path).pages[i].extract_text()`.

`textwrap.wrap()` (used for some PDF paragraphs) can legitimately break a
sentence across a line boundary, turning a space into a newline. That's
correct wrapping, not a bug -- so wording checks against extracted PDF text
normalize whitespace first via `re.sub(r"\\s+", " ", text)`. The Markdown
report has no wrapping, so plain substring checks are fine there.
"""

import re

import pypdf
import pytest

from wiki_interest import report
from wiki_interest.report import (
    build_context_note,
    build_report,
    default_summary,
    method_note,
    render_markdown,
    render_pdf,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _normalize(text: str) -> str:
    """Collapse all whitespace runs (including line-wrap newlines) to a single space."""
    return re.sub(r"\s+", " ", text)


def _pdf_text(path: str) -> str:
    reader = pypdf.PdfReader(path)
    return "\n".join(page.extract_text() or "" for page in reader.pages)


def _basic_period(**overrides) -> dict:
    period = {
        "start": "2023-01",
        "end": "2023-12",
        "reference_months_before": 12,
        "reference_reason": "classify peaks as seasonal vs. one-off",
        "actual_history_months": 24,
        "target_history_months": 36,
    }
    period.update(overrides)
    return period


def _month_add(yyyymm: str, delta: int) -> str:
    year, month = (int(part) for part in yyyymm.split("-"))
    total = year * 12 + (month - 1) + delta
    year, month = divmod(total, 12)
    return f"{year:04d}-{month + 1:02d}"


def _series(start: str, n: int, value):
    return {_month_add(start, i): value for i in range(n)}


def _realistic_run_data() -> dict:
    """
    2 languages: one significant/medium-confidence, one not-significant/
    low-confidence, plus one missing language -- matches the coverage the
    task asks for in render_markdown/render_pdf/build_report tests.
    """
    return {
        "run_id": "test-run-1",
        "topic": "Solar eclipse",
        "period": _basic_period(),
        "series": {
            "en": _series("2023-01", 12, 1000),
            "de": _series("2023-01", 12, 500),
        },
        "results": [
            {
                "lang": "en",
                "title": "Solar eclipse",
                "yoy_growth": 0.25,
                "share_yoy_growth": 0.10,
                "momentum_6mo": 0.15,
                "avg_monthly_views": 12345,
                "last12_avg_monthly_views": 13000,
                "spikes_removed": 1,
                "seasonal_peaks_kept": 1,
                "significant": True,
                "confidence": "medium",
                "reason": "Trend is statistically significant with medium confidence.",
                "reference_only_yoy_growth": 0.05,
            },
            {
                "lang": "de",
                "title": "Sonnenfinsternis",
                "yoy_growth": -0.05,
                "share_yoy_growth": None,
                "momentum_6mo": None,
                "spikes_removed": 0,
                "seasonal_peaks_kept": 0,
                "significant": False,
                "confidence": "low",
                "reason": "Not enough history to compute a reliable trend.",
            },
        ],
        "missing": ["ja"],
    }


# ---------------------------------------------------------------------------
# 1. method_note
# ---------------------------------------------------------------------------


def test_method_note_states_period_and_reference_and_target():
    period = _basic_period()

    note = method_note(period)
    normalized = _normalize(note)

    assert "2023-01" in note and "2023-12" in note
    assert "12" in note  # reference_months_before
    assert "36" in note  # target_history_months
    assert "only" in normalized.lower()
    assert "seasonal" in normalized.lower()


def test_method_note_discloses_shortfall_when_actual_below_target():
    period = _basic_period(actual_history_months=18, target_history_months=36)

    note = method_note(period)

    assert "18" in note
    assert "short of" in note.lower()
    assert "36" in note


def test_method_note_no_shortfall_language_when_actual_meets_target():
    period = _basic_period(actual_history_months=36, target_history_months=36)

    note = method_note(period)

    assert "short of" not in note.lower()


def test_method_note_no_shortfall_language_when_actual_history_months_absent():
    period = _basic_period()
    del period["actual_history_months"]

    note = method_note(period)

    assert "short of" not in note.lower()


def test_method_note_mentions_reference_baseline_when_flag_true():
    period = _basic_period(used_reference_baseline_for_yoy=True)

    note = method_note(period)

    assert "24 months" in note
    assert "baseline" in note.lower()


@pytest.mark.parametrize("period_overrides", [{}, {"used_reference_baseline_for_yoy": False}])
def test_method_note_omits_reference_baseline_sentence_when_absent_or_false(period_overrides):
    period = _basic_period(**period_overrides)

    note = method_note(period)

    assert "borrows its" not in note
    assert "24 months, so the yoy" not in note.lower()


def test_method_note_contains_bot_wording_and_excludes_forbidden_phrases():
    period = _basic_period()

    note = method_note(period)
    lower = note.lower()

    assert "exclude known bots and automated traffic" in note
    assert "humans only" not in lower
    assert "human readers" not in lower
    assert "regular readers" not in lower
    assert "real users" not in lower
    assert "real people" not in lower


# ---------------------------------------------------------------------------
# 2. build_context_note
# ---------------------------------------------------------------------------


def test_build_context_note_empty_when_no_pair_present():
    results = [
        {"lang": "en", "yoy_growth": 0.2},
        {"lang": "de", "yoy_growth": None},
    ]

    assert build_context_note(results) == ""


def test_build_context_note_empty_when_signs_agree():
    results = [
        {"lang": "en", "yoy_growth": 0.2, "reference_only_yoy_growth": 0.1},
        {"lang": "de", "yoy_growth": -0.2, "reference_only_yoy_growth": -0.3},
    ]

    assert build_context_note(results) == ""


def test_build_context_note_flags_language_with_opposite_signs():
    results = [
        {"lang": "en", "yoy_growth": 0.2, "reference_only_yoy_growth": -0.1},
        {"lang": "de", "yoy_growth": -0.2, "reference_only_yoy_growth": -0.3},
    ]

    note = build_context_note(results)

    assert note != ""
    assert "en" in note
    assert "de" not in note


def test_build_context_note_skips_result_missing_key_entirely():
    # "de" has no reference_only_yoy_growth key at all (not just None) --
    # must be skipped via .get() semantics, not raise a KeyError.
    results = [
        {"lang": "en", "yoy_growth": 0.2, "reference_only_yoy_growth": -0.1},
        {"lang": "de", "yoy_growth": 0.3},
    ]

    note = build_context_note(results)

    assert note != ""
    assert "en" in note
    assert "de" not in note


# ---------------------------------------------------------------------------
# 3. render_markdown
# ---------------------------------------------------------------------------


def test_render_markdown_realistic_run_data_contains_expected_sections():
    run_data = _realistic_run_data()

    md = render_markdown(run_data)

    assert "Solar eclipse" in md
    assert "2023-01" in md and "2023-12" in md
    # table rows for both languages
    assert "| en |" in md
    assert "| de |" in md
    # trust notes section: plain-language notes (not the raw technical
    # `reason` string) for both languages
    assert "## Trust notes" in md
    for r in run_data["results"]:
        assert report.plain_trust_note(r, run_data["period"]) in md
    # method & limits section
    assert "## Method & limits" in md
    # missing language noted
    assert "ja" in md
    assert "Not found" in md or "not found" in md.lower()


def test_render_markdown_wording_checks():
    run_data = _realistic_run_data()

    md = render_markdown(run_data)
    lower = md.lower()

    assert "humans only" not in lower
    assert "human readers" not in lower
    assert "regular readers" not in lower
    assert "real users" not in lower
    assert "real people" not in lower
    assert "exclude known bots and automated traffic" in md


# ---------------------------------------------------------------------------
# 4. render_pdf
# ---------------------------------------------------------------------------


def test_render_pdf_creates_one_page_pdf_with_expected_content(tmp_path):
    run_data = _realistic_run_data()
    output_path = str(tmp_path / "report.pdf")

    result_path = render_pdf(run_data, output_path)

    assert result_path == output_path
    from pathlib import Path

    saved = Path(output_path)
    assert saved.exists()
    assert saved.stat().st_size > 1024

    reader = pypdf.PdfReader(output_path)
    assert len(reader.pages) == 1

    normalized = _normalize(_pdf_text(output_path))
    assert "Solar eclipse" in normalized
    assert "en" in normalized
    assert "de" in normalized
    assert "Method & limits" in normalized
    assert "exclude known bots and automated traffic" in normalized


def test_render_pdf_wording_checks(tmp_path):
    run_data = _realistic_run_data()
    output_path = str(tmp_path / "report.pdf")

    render_pdf(run_data, output_path)

    normalized_lower = _normalize(_pdf_text(output_path)).lower()

    assert "humans only" not in normalized_lower
    assert "human readers" not in normalized_lower
    assert "regular readers" not in normalized_lower
    assert "real users" not in normalized_lower
    assert "real people" not in normalized_lower


# ---------------------------------------------------------------------------
# 5. build_report
# ---------------------------------------------------------------------------


def test_build_report_creates_all_three_artifacts_in_output_dir(tmp_path):
    run_data = _realistic_run_data()
    output_dir = tmp_path / "out"

    paths = build_report(run_data, str(output_dir))

    assert set(paths.keys()) == {"pdf", "png", "md"}
    for key, path_str in paths.items():
        from pathlib import Path

        path = Path(path_str)
        assert path.exists(), f"{key} file missing: {path}"
        assert path.stat().st_size > 0, f"{key} file is empty: {path}"
        assert str(output_dir) in str(path), f"{key} path {path} not inside {output_dir}"


def test_build_report_with_no_results_and_no_series_does_not_raise(tmp_path):
    run_data = {
        "run_id": "empty-run",
        "topic": "Nonexistent topic",
        "period": _basic_period(),
        "series": {},
        "results": [],
        "missing": ["en", "de"],
    }
    output_dir = tmp_path / "empty_out"

    paths = build_report(run_data, str(output_dir))

    from pathlib import Path

    for path_str in paths.values():
        path = Path(path_str)
        assert path.exists()
        assert path.stat().st_size > 0


# ---------------------------------------------------------------------------
# 6. Full wording check across all three generated artifacts
# ---------------------------------------------------------------------------


def test_wording_check_across_all_report_outputs(tmp_path):
    run_data = _realistic_run_data()
    output_dir = tmp_path / "wording_out"

    paths = build_report(run_data, str(output_dir))

    # PNG: just confirm it's a valid PNG via magic bytes (no text to check).
    with open(paths["png"], "rb") as f:
        header = f.read(8)
    assert header.startswith(b"\x89PNG")

    # Markdown: plain substring checks, no wrapping applied.
    with open(paths["md"], encoding="utf-8") as f:
        md_text = f.read()
    md_lower = md_text.lower()
    assert "humans only" not in md_lower
    assert "human readers" not in md_lower
    assert "regular readers" not in md_lower
    assert "real users" not in md_lower
    assert "real people" not in md_lower
    assert "exclude known bots and automated traffic" in md_text

    # PDF: whitespace-normalized text, since textwrap.wrap() may break the
    # required phrase across a line boundary.
    pdf_normalized = _normalize(_pdf_text(paths["pdf"]))
    pdf_normalized_lower = pdf_normalized.lower()
    assert "humans only" not in pdf_normalized_lower
    assert "human readers" not in pdf_normalized_lower
    assert "regular readers" not in pdf_normalized_lower
    assert "real users" not in pdf_normalized_lower
    assert "real people" not in pdf_normalized_lower
    assert "exclude known bots and automated traffic" in pdf_normalized

# ---------------------------------------------------------------------------
# 7. One-page guarantee under load (many-language worst case)
# ---------------------------------------------------------------------------


def test_render_pdf_stays_one_page_with_many_language_series(tmp_path):
    """
    No MAX_SERIES cap exists in charts.py/report.py -- _LINE_COLORS just
    cycles via modulo once the language count exceeds its 6 colors. This
    test guards the actual invariant that matters: no matter how many
    languages are plotted, the PDF must still render as exactly one page.
    12 languages is a plausible worst case for a multi-language comparison.
    """
    langs = [f"lang{i:02d}" for i in range(12)]
    run_data = {
        "run_id": "many-langs-run",
        "topic": "Many languages topic",
        "period": _basic_period(),
        "series": {lang: _series("2023-01", 12, 100 * (i + 1)) for i, lang in enumerate(langs)},
        "results": [
            {
                "lang": lang,
                "title": f"Title {lang}",
                "yoy_growth": 0.1 * (i + 1),
                "share_yoy_growth": 0.05,
                "momentum_6mo": 0.02,
                "spikes_removed": 0,
                "seasonal_peaks_kept": 0,
                "significant": i % 2 == 0,
                "confidence": "medium",
                "reason": f"Reason text for {lang}.",
            }
            for i, lang in enumerate(langs)
        ],
        "missing": [],
    }
    output_path = str(tmp_path / "report_many_langs.pdf")

    render_pdf(run_data, output_path)

    reader = pypdf.PdfReader(output_path)
    assert len(reader.pages) == 1


# ---------------------------------------------------------------------------
# 8. Key numbers (yoy_growth, confidence, reason) appear in both PDF and MD
# ---------------------------------------------------------------------------


def test_key_numbers_appear_in_both_pdf_and_markdown(tmp_path):
    run_data = _realistic_run_data()
    output_path = str(tmp_path / "report_key_numbers.pdf")

    render_pdf(run_data, output_path)
    md = render_markdown(run_data)

    pdf_normalized = _normalize(_pdf_text(output_path))

    assert len(run_data["results"]) >= 2  # covers multiple languages, not just one

    for r in run_data["results"]:
        expected_pct = report._fmt_pct(r["yoy_growth"])
        expected_confidence = r["confidence"]
        # Trust notes display plain_trust_note()'s output, not the raw
        # technical `reason` string (task: no variable names in the PDF/MD).
        expected_trust_note = report.plain_trust_note(r, run_data["period"])

        assert expected_pct in pdf_normalized, f"{expected_pct!r} missing from PDF for {r['lang']}"
        assert expected_pct in md, f"{expected_pct!r} missing from Markdown for {r['lang']}"

        assert expected_confidence in pdf_normalized, f"{expected_confidence!r} missing from PDF for {r['lang']}"
        assert expected_confidence in md, f"{expected_confidence!r} missing from Markdown for {r['lang']}"

        assert expected_trust_note in pdf_normalized, f"{expected_trust_note!r} missing from PDF for {r['lang']}"
        assert expected_trust_note in md, f"{expected_trust_note!r} missing from Markdown for {r['lang']}"


# ---------------------------------------------------------------------------
# 9. Cyrillic support
# ---------------------------------------------------------------------------


def test_render_pdf_supports_cyrillic_topic_and_title(tmp_path):
    """
    matplotlib's default font (DejaVu Sans) covers Cyrillic, and pypdf
    extracts it correctly with no special font configuration needed.
    """
    run_data = {
        "run_id": "cyrillic-run",
        "topic": "Астрономія",
        "period": _basic_period(),
        "series": {"uk": _series("2023-01", 12, 800)},
        "results": [
            {
                "lang": "uk",
                "title": "Астрономія",
                "yoy_growth": 0.12,
                "share_yoy_growth": 0.03,
                "momentum_6mo": 0.01,
                "spikes_removed": 0,
                "seasonal_peaks_kept": 0,
                "significant": True,
                "confidence": "high",
                "reason": "Trend is statistically significant with high confidence.",
            }
        ],
        "missing": [],
    }
    output_path = str(tmp_path / "report_cyrillic.pdf")

    render_pdf(run_data, output_path)

    normalized = _normalize(_pdf_text(output_path))
    assert "Астрономія" in normalized




# ---------------------------------------------------------------------------
# 10. Empty run: no results/series, only missing languages
# ---------------------------------------------------------------------------


def test_empty_run_lists_missing_languages_in_markdown(tmp_path):
    """
    A fully-empty run (no results, no series, only a missing-languages
    list) must clearly say no data was found and list every missing
    language, not just render an empty table with no explanation.
    """
    run_data = {
        "run_id": "empty-run-missing-only",
        "topic": "Obscure topic",
        "period": _basic_period(),
        "series": {},
        "results": [],
        "missing": ["de", "fr", "pl"],
    }

    md = render_markdown(run_data)

    assert "no data found" in md.lower()
    assert "Not found" in md
    # Word-boundary match: plain substring checks would false-positive on
    # "de" inside the "Confidence" table-header column, which is present
    # even when results is empty.
    for lang in run_data["missing"]:
        assert re.search(rf"\b{re.escape(lang)}\b", md) is not None


def test_empty_run_pdf_shows_no_results_and_lists_missing_languages(tmp_path):
    """
    render_pdf must clearly state no data was found and list every missing
    language -- fixed after an earlier version of this test caught render_pdf
    silently dropping run_data["missing"] entirely (it only read that key
    inside render_markdown, never render_pdf).
    """
    run_data = {
        "run_id": "empty-run-missing-only",
        "topic": "Obscure topic",
        "period": _basic_period(),
        "series": {},
        "results": [],
        "missing": ["de", "fr", "pl"],
    }
    output_path = str(tmp_path / "report_empty.pdf")

    render_pdf(run_data, output_path)

    pdf_normalized = _normalize(_pdf_text(output_path))

    assert "no data found" in pdf_normalized.lower()
    assert "no results" in pdf_normalized.lower()

    # Word-boundary match: plain substring checks false-positive on "de"
    # inside "Confidence".
    for lang in run_data["missing"]:
        assert re.search(rf"\b{re.escape(lang)}\b", pdf_normalized) is not None, f"{lang!r} not found in PDF text"


# ---------------------------------------------------------------------------
# 11. default_summary / _detect_step_change / _momentum_divergence_note
# ---------------------------------------------------------------------------


def _run_data_with_headline(yoy_growth, significant, momentum_6mo=None, monthly=None, n_results=1, confidence="medium"):
    period = _basic_period(start="2024-09", end="2026-08", actual_history_months=36)
    headline = {
        "lang": "uk",
        "title": "Topic",
        "yoy_growth": yoy_growth,
        "share_yoy_growth": yoy_growth,
        "momentum_6mo": momentum_6mo,
        "spikes_removed": 0,
        "seasonal_peaks_kept": 0,
        "significant": significant,
        "confidence": confidence,
        "reason": "reason",
    }
    results = [headline]
    for i in range(1, n_results):
        results.append(
            {
                "lang": f"lang{i:02d}",
                "title": "Other",
                "yoy_growth": yoy_growth * 0.1,
                "share_yoy_growth": yoy_growth * 0.1,
                "momentum_6mo": None,
                "spikes_removed": 0,
                "seasonal_peaks_kept": 0,
                "significant": False,
                "confidence": "low",
                "reason": "reason",
            }
        )
    return {
        "run_id": "summary-test",
        "topic": "Topic",
        "period": period,
        "series": {"uk": monthly} if monthly else {},
        "results": results,
        "missing": [],
    }


def test_default_summary_no_results_says_no_data_found():
    run_data = {"run_id": "x", "topic": "Ghost topic", "period": _basic_period(), "series": {}, "results": [], "missing": []}

    assert "Ghost topic" in default_summary(run_data)
    assert "no" in default_summary(run_data).lower()


def test_default_summary_significant_change_states_statistically_meaningful():
    run_data = _run_data_with_headline(yoy_growth=0.3, significant=True)

    summary = default_summary(run_data)

    assert "statistically meaningful" in summary
    assert "30%" in summary


def test_default_summary_large_nonsignificant_change_explains_dominant_step():
    # 36 flat months at 1000, a single sharp drop to 400 at index 25, then flat.
    monthly = {}
    y, m = 2023, 9
    for i in range(36):
        monthly[f"{y:04d}-{m:02d}"] = 1000 if i < 25 else 400
        m += 1
        if m > 12:
            m = 1
            y += 1
    run_data = _run_data_with_headline(yoy_growth=-0.5, significant=False, momentum_6mo=-0.02, monthly=monthly)

    summary = default_summary(run_data)

    assert "Most of the change happened in" in summary
    assert "2025" in summary  # the step month falls in the period


def test_default_summary_small_nonsignificant_change_has_no_step_explanation():
    run_data = _run_data_with_headline(yoy_growth=0.1, significant=False)  # below the 20% threshold

    summary = default_summary(run_data)

    assert "Most of the change happened in" not in summary


def test_default_summary_momentum_divergence_appended_for_diverging_momentum():
    run_data = _run_data_with_headline(yoy_growth=0.4, significant=True, momentum_6mo=-0.1)

    summary = default_summary(run_data)

    assert "leveling off or reversing" in summary


def test_default_summary_gates_explanatory_notes_for_many_languages():
    # Same headline as the step-change and momentum-divergence cases above,
    # but with more than 6 results -- the summary must stay short since the
    # table below it is already claiming most of the page for many languages.
    monthly = {}
    y, m = 2023, 9
    for i in range(36):
        monthly[f"{y:04d}-{m:02d}"] = 1000 if i < 25 else 400
        m += 1
        if m > 12:
            m = 1
            y += 1
    run_data = _run_data_with_headline(yoy_growth=-0.5, significant=False, momentum_6mo=-0.02, monthly=monthly, n_results=7)

    summary = default_summary(run_data)

    assert "Most of the change happened in" not in summary
    assert "leveling off" not in summary and "slowing down" not in summary and "accelerating" not in summary


def test_detect_step_change_finds_the_dominant_step():
    monthly = {"2023-01": 100, "2023-02": 100, "2023-03": 20, "2023-04": 20, "2023-05": 18}

    step = report._detect_step_change(monthly, "2023-01", "2023-05")

    assert step is not None
    assert step["month"] == "2023-03"
    assert step["fraction"] > 0.9


def test_detect_step_change_returns_none_for_a_gradual_change():
    monthly = {"2023-01": 100, "2023-02": 90, "2023-03": 80, "2023-04": 70, "2023-05": 60}

    assert report._detect_step_change(monthly, "2023-01", "2023-05") is None


def test_detect_step_change_returns_none_for_flat_series():
    monthly = {"2023-01": 100, "2023-02": 100, "2023-03": 100}

    assert report._detect_step_change(monthly, "2023-01", "2023-03") is None


@pytest.mark.parametrize(
    "yoy,momentum,expected_phrase",
    [
        (0.4, -0.1, "leveling off or reversing"),
        (0.4, 0.1, "slowing down"),
        (0.2, 0.5, "accelerating"),
        (0.4, 0.38, ""),  # ratio ~0.95, not meaningfully different
    ],
)
def test_momentum_divergence_note_cases(yoy, momentum, expected_phrase):
    note = report._momentum_divergence_note(yoy, momentum)

    if expected_phrase:
        assert expected_phrase in note
    else:
        assert note == ""


def test_momentum_divergence_note_missing_values_returns_empty():
    assert report._momentum_divergence_note(None, 0.1) == ""
    assert report._momentum_divergence_note(0.1, None) == ""
    assert report._momentum_divergence_note(0.0, 0.1) == ""


# ---------------------------------------------------------------------------
# 12. Founder-friendly table headers: PDF drops Title, Markdown keeps it
# ---------------------------------------------------------------------------


def test_render_pdf_table_headers_are_founder_friendly_and_drop_title(tmp_path):
    run_data = _realistic_run_data()
    output_path = str(tmp_path / "report.pdf")

    render_pdf(run_data, output_path)

    normalized = _normalize(_pdf_text(output_path))
    for header in ["Language", "Avg. views/mo", "Year-over-year", "vs. all Wikipedia traffic", "Last 6 months", "Steady trend?", "Confidence"]:
        assert header in normalized
    assert "Title" not in normalized
    # The Title column is dropped from the PDF table specifically (still in
    # Markdown, see below) to make room on a one-page layout. The topic name
    # ("Solar eclipse") legitimately appears elsewhere (header, summary), so
    # check for the per-language title text instead, which only ever shows
    # up in that dropped table column.
    assert "Sonnenfinsternis" not in normalized


def test_render_markdown_table_headers_keep_title():
    run_data = _realistic_run_data()

    md = render_markdown(run_data)

    assert "| Language | Title | Avg. monthly views | Year-over-year | vs. all Wikipedia traffic | Last 6 months | Steady trend? | Confidence |" in md
    assert "| en | Solar eclipse |" in md


# ---------------------------------------------------------------------------
# 13. avg_monthly_views column
# ---------------------------------------------------------------------------


def test_render_markdown_shows_avg_monthly_views_and_n_a_for_missing():
    run_data = _realistic_run_data()

    md = render_markdown(run_data)

    assert "12,345" in md  # en's avg_monthly_views
    # de has no avg_monthly_views key at all -- must degrade to "n/a", not KeyError.
    assert "| de | Sonnenfinsternis | n/a |" in md


def test_render_pdf_shows_avg_monthly_views(tmp_path):
    run_data = _realistic_run_data()
    output_path = str(tmp_path / "report.pdf")

    render_pdf(run_data, output_path)

    normalized = _normalize(_pdf_text(output_path))
    assert "12,345" in normalized
    assert "n/a" in normalized  # de's missing avg_monthly_views


def test_render_pdf_table_never_overflows_its_axes_width(tmp_path):
    """
    Regression test for a real bug: matplotlib's Table.auto_set_column_width
    has no awareness of the containing axes' actual width, so adding a
    column (avg_monthly_views) pushed the outer columns off both edges of
    the page -- only caught by visually rendering the PDF. A later fix that
    manually corrected the table's width also silently got undone by
    matplotlib re-running auto_set_column_width's own sizing on the next
    draw (Table._update_positions), unless that tracking is cleared too.
    Assert the invariant directly: after rendering, the table's measured
    bounding box must fit within its axes' bounding box.
    """
    import matplotlib.figure
    import matplotlib.table

    run_data = _realistic_run_data()
    output_path = str(tmp_path / "report.pdf")

    captured = {}
    original_savefig = matplotlib.figure.Figure.savefig

    def spy_savefig(self, *args, **kwargs):
        renderer = self.canvas.get_renderer()
        for ax in self.axes:
            for child in ax.get_children():
                if isinstance(child, matplotlib.table.Table):
                    captured["table_bbox"] = child.get_window_extent(renderer)
                    captured["axes_bbox"] = ax.get_window_extent()
        return original_savefig(self, *args, **kwargs)

    matplotlib.figure.Figure.savefig = spy_savefig
    try:
        render_pdf(run_data, output_path)
    finally:
        matplotlib.figure.Figure.savefig = original_savefig

    assert "table_bbox" in captured, "no matplotlib Table found in the rendered figure"
    # A small tolerance for floating-point rounding in the width correction.
    assert captured["table_bbox"].width <= captured["axes_bbox"].width + 1.0
    assert captured["table_bbox"].x0 >= captured["axes_bbox"].x0 - 1.0
    assert captured["table_bbox"].x1 <= captured["axes_bbox"].x1 + 1.0


# ---------------------------------------------------------------------------
# 13. No code formatting (backticks / variable names) in PDF or Markdown
# ---------------------------------------------------------------------------


def test_render_pdf_and_markdown_have_no_backticks_or_code_jargon(tmp_path):
    run_data = _realistic_run_data()
    output_path = str(tmp_path / "report.pdf")

    render_pdf(run_data, output_path)
    md = render_markdown(run_data)
    pdf_text = _pdf_text(output_path)

    for text in (md, pdf_text):
        assert "`" not in text
        assert "agent=user" not in text


# ---------------------------------------------------------------------------
# 14. Many-language cap: PDF caps table rows and trust notes; Markdown doesn't
# ---------------------------------------------------------------------------


def _many_lang_run_data(n=12):
    langs = [f"lang{i:02d}" for i in range(n)]
    return {
        "run_id": "many-langs-run",
        "topic": "Many languages topic",
        "period": _basic_period(),
        "series": {lang: _series("2023-01", 12, 100 * (i + 1)) for i, lang in enumerate(langs)},
        "results": [
            {
                "lang": lang,
                "title": f"Title {lang}",
                "yoy_growth": 0.1 * (i + 1),
                "share_yoy_growth": 0.05,
                "momentum_6mo": 0.02,
                "spikes_removed": 0,
                "seasonal_peaks_kept": 0,
                "significant": i % 2 == 0,
                "confidence": "medium",
                "reason": f"Reason text for {lang}.",
            }
            for i, lang in enumerate(langs)
        ],
        "missing": [],
    }


def test_render_pdf_caps_table_rows_for_many_languages(tmp_path):
    run_data = _many_lang_run_data(12)
    output_path = str(tmp_path / "report_many.pdf")

    render_pdf(run_data, output_path)

    normalized = _normalize(_pdf_text(output_path))
    # Exact count depends on how many rows fit at the enforced minimum
    # readable font size (see report.py's MIN_TABLE_FONTSIZE) -- assert the
    # capping behavior itself (some rows omitted, pointed to the Markdown
    # report), not a specific number that would need updating every time
    # the layout budget shifts slightly.
    assert re.search(r"and \d+ more language\(s\)", normalized)
    assert "Markdown report" in normalized


def test_render_pdf_caps_trust_notes_for_many_languages(tmp_path):
    run_data = _many_lang_run_data(12)
    output_path = str(tmp_path / "report_many.pdf")

    render_pdf(run_data, output_path)

    normalized = _normalize(_pdf_text(output_path))
    assert "more language(s) -- see the Markdown report for full detail" in normalized


def test_render_markdown_does_not_cap_table_rows_or_trust_notes_for_many_languages():
    run_data = _many_lang_run_data(12)

    md = render_markdown(run_data)

    for r in run_data["results"]:
        assert f"| {r['lang']} |" in md
        assert report.plain_trust_note(r, run_data["period"]) in md


# ---------------------------------------------------------------------------
# 15. default_summary (multi-language): names top-ranked, weakest, and counts
# ---------------------------------------------------------------------------


def test_default_summary_names_top_ranked_weakest_and_growth_counts():
    # results is deliberately or NOT in any obvious value order -- "ja" (the
    # real top-ranked entry, i.e. results[0], matching how cli.py now saves
    # results in --rank-by order) is neither alphabetically first nor last,
    # and "hi" (the actual weakest/most-declining) is placed in the MIDDLE
    # of the list, not first or last by position -- so a naive
    # "first"/"last-in-list" implementation would name the wrong languages.
    run_data = {
        "topic": "Yoga",
        "results": [
            {"lang": "ja", "yoy_growth": -0.151, "significant": True},
            {"lang": "de", "yoy_growth": -0.183, "significant": True},
            {"lang": "hi", "yoy_growth": -0.601, "significant": True},
            {"lang": "pl", "yoy_growth": -0.185, "significant": True},
        ],
    }

    summary = default_summary(run_data)

    assert summary.startswith('Interest in "Yoga" in ja')  # top-ranked = results[0]
    assert "HI" in summary  # the weakest (most negative) performer, by value not position
    assert "-60.1%" in summary
    assert "4 languages" in summary
    assert "0 are growing and 4 are declining" in summary


def test_default_summary_omits_weakest_when_it_is_the_headline_itself():
    # If the top-ranked (headline) result is ALSO the weakest one (e.g. a
    # single sharp decliner with everyone else flat-ish), naming it twice
    # would be redundant -- the weakest clause should only appear when it's
    # a DIFFERENT language from the headline.
    run_data = {
        "topic": "Topic",
        "results": [
            {"lang": "aa", "yoy_growth": -0.9, "significant": True},
            {"lang": "bb", "yoy_growth": -0.1, "significant": True},
            {"lang": "cc", "yoy_growth": -0.05, "significant": True},
        ],
    }

    summary = default_summary(run_data)

    assert summary.startswith('Interest in "Topic" in aa')
    assert "shows the weakest trend" not in summary
    assert "0 are growing and 3 are declining" in summary


def test_default_summary_single_language_has_no_counts_sentence():
    run_data = {"topic": "Topic", "results": [{"lang": "en", "yoy_growth": 0.3, "significant": True}]}

    summary = default_summary(run_data)

    assert "are growing and" not in summary
    assert "weakest trend" not in summary


# ---------------------------------------------------------------------------
# 16. No overlapping text bounding boxes (header, Trust notes, Method & limits)
# ---------------------------------------------------------------------------


def _rendered_text_bboxes(run_data: dict, tmp_path) -> dict:
    """
    Render `run_data` and return {text_content: bbox} for every Text
    artist matplotlib actually placed, in figure-pixel coordinates,
    captured via a savefig spy (render_pdf saves-and-closes internally, so
    this is the only point the Figure is reachable before it's gone).
    """
    import matplotlib.figure

    captured = {}
    original_savefig = matplotlib.figure.Figure.savefig

    def spy_savefig(self, *args, **kwargs):
        renderer = self.canvas.get_renderer()
        for ax in self.axes:
            for text_artist in ax.texts:
                content = text_artist.get_text()
                if content.strip():
                    captured[content] = text_artist.get_window_extent(renderer)
        return original_savefig(self, *args, **kwargs)

    matplotlib.figure.Figure.savefig = spy_savefig
    try:
        render_pdf(run_data, str(tmp_path / "report.pdf"))
    finally:
        matplotlib.figure.Figure.savefig = original_savefig
    return captured


def _find_bbox(bboxes: dict, prefix: str):
    for content, bbox in bboxes.items():
        if content.startswith(prefix):
            return content, bbox
    raise AssertionError(f"no rendered text starts with {prefix!r} -- have: {list(bboxes)}")


def _assert_no_vertical_overlap(bboxes: dict, prefix_a: str, prefix_b: str):
    """
    Two text blocks overlap if the lower one's TOP edge is above the
    higher one's BOTTOM edge -- bbox.y0/y1 are in figure-pixel coordinates
    with y increasing UPWARD (matplotlib convention), so "A above B, not
    overlapping" means A's bottom (y0) is at or above B's top (y1).
    """
    content_a, bbox_a = _find_bbox(bboxes, prefix_a)
    content_b, bbox_b = _find_bbox(bboxes, prefix_b)
    top, bottom = (bbox_a, bbox_b) if bbox_a.y0 >= bbox_b.y0 else (bbox_b, bbox_a)
    assert top.y0 >= bottom.y1, f"{content_a[:40]!r} and {content_b[:40]!r} vertically overlap: {bbox_a} vs {bbox_b}"


def _series_n(start: str, n: int, value) -> dict:
    year, month = (int(p) for p in start.split("-"))
    out = {}
    for _ in range(n):
        out[f"{year:04d}-{month:02d}"] = value
        month += 1
        if month > 12:
            month = 1
            year += 1
    return out


def _overlap_check_run_data(n_langs: int) -> dict:
    langs = [f"lang{i:02d}" for i in range(n_langs)]
    return {
        "run_id": "overlap-check",
        "topic": "Overlap Check Topic",
        "period": _basic_period(start="2024-09", end="2026-08", reference_months_before=12, actual_history_months=36, target_history_months=36),
        "series": {lang: _series_n("2024-09", 24, 100 * (i + 1)) for i, lang in enumerate(langs)},
        "results": [
            {
                "lang": lang,
                "title": f"Title {lang}",
                "yoy_growth": 0.1 * (i + 1) * (-1 if i % 2 else 1),
                "share_yoy_growth": 0.05,
                "momentum_6mo": 0.02,
                "avg_monthly_views": 100 * (i + 1),
                "last12_avg_monthly_views": 90 * (i + 1),
                "spikes_removed": 0,
                "seasonal_peaks_kept": 0,
                "significant": i % 2 == 0,
                "confidence": "medium",
                "reason": f"Reason text for {lang}.",
            }
            for i, lang in enumerate(langs)
        ],
        "missing": [],
    }


@pytest.mark.parametrize("n_langs", [1, 2, 12])
def test_no_overlapping_headings_or_body_text_across_layouts(tmp_path, n_langs):
    run_data = _overlap_check_run_data(n_langs)

    bboxes = _rendered_text_bboxes(run_data, tmp_path)

    # Header: title vs. the "Period: ..." line under it.
    _assert_no_vertical_overlap(bboxes, "Wiki interest report:", "Period:")
    # Trust notes: the bold heading vs. its own body paragraph.
    _assert_no_vertical_overlap(bboxes, "Trust notes", "lang00:")
    # Method & limits: the bold heading vs. its own body paragraph.
    _assert_no_vertical_overlap(bboxes, "Method & limits", "Charts and tables")
    # Cross-section: the trust-notes body must not bleed down into the
    # Method & limits heading below it.
    _, trust_heading_bbox = _find_bbox(bboxes, "Trust notes")
    _, trust_body_bbox = _find_bbox(bboxes, "lang00:")
    assert trust_body_bbox.y1 <= trust_heading_bbox.y0, "trust-notes body starts at or above its own heading"
    _assert_no_vertical_overlap(bboxes, "lang00:", "Method & limits")
