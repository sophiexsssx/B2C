# Plan: wiki-interest skill

Source of truth for scope decisions: [notes/requirements.md](requirements.md) ("My decisions" + "Hard requirements"). This plan operationalizes those decisions into a CLI, metrics, file layout, and milestones.

## 1. CLI commands

Basic question = **`analyze` + `report` = 2 calls**. `resolve` is optional — a manual pre-check (e.g. to verify article match before committing to a full run), not on the default path.

Every JSON response returned *to the agent* stays summary-only (≤2KB); the full per-month series is written to a disk-cached run folder that `report` reads from, so multi-language comparisons and follow-ups don't need to round-trip raw data through the LLM context.

### `analyze`

```
analyze --topic "<topic>" --langs <lang1>,<lang2>,... --start <YYYY-MM> --end <YYYY-MM> \
        [--agent user] [--rank-by yoy_growth_spike_free] [--article lang:"Title",...]
```

- Resolves each language's canonical title + redirects internally (cached; langlinks primary, Wikidata sitelinks fallback, QID match check). `--article lang:"Title"` overrides resolution for specific languages.
- Fetches the requested window, plus as many months before it as needed so total history (requested + reference) reaches **≥36 months**, bounded by actual data availability (Pageviews API starts July 2015). This extra window is reference-only: used to classify a peak as seasonal vs. one-off, and is never included in reported metrics, charts, or tables.
  - **Seasonal rule:** a peak month is seasonal (kept, not removed) if the same calendar month is elevated in **≥2 of the available years** — requested period and reference combined, not restricted to reference years. Elevated in only 1 year = one-off spike (removed).
  - If total available history is still **<36 months** (article too new, or near the July 2015 data start), seasonal-vs-spike classification is marked unreliable and confidence is lowered, with that stated explicitly.
  - **If the requested period is <24 months**, YoY growth borrows its "same calendar months one year earlier" baseline from the reference data (computed, not charted/tabled) — the report states this explicitly. If the requested period is ≥24 months, the YoY comparison is computed entirely within it.
- Computes per-language metrics (see §2), ranks series by `--rank-by` (default: spike-free YoY growth). Languages with zero-baseline ("new_or_dormant"/"flat") growth are excluded from the sort and returned separately under `unranked` — **both `ranked` and `unranked` must be surfaced in the response**, since an emerging topic with no prior-period baseline is often the most interesting finding, not one to silently drop.
- `missing`: languages where the topic wasn't found at all (a possible content gap, not an error).
- Generates its own `run_id` (the agent never supplies one) and writes full series + reference data to that run's cache folder.
- **Output cap:** if serializing all ranked series would exceed 2KB, returns the top N by `--rank-by` and reports how many were omitted; the full set remains in the run folder for `report`.
- **Incomplete current month:** if the requested (or reference) window reaches the current in-progress calendar month, that month's partial count is excluded from every growth/momentum/significance calculation (it's a real but not-yet-final number from the API, not zero) — flagged in the response as `excluded_incomplete_month`. Caching a fetch whose range includes the current month uses a short TTL rather than caching forever, since the same range's true total changes once the month is finalized.

Example (24-month request):
```bash
analyze --topic "Intermittent fasting" --langs pl,cs --start 2023-10 --end 2025-09 --agent user
```
```json
{"run_id":"r_20250922_ab12",
 "period":{"start":"2023-10","end":"2025-09","reference_months_before":12,
           "reference_reason":"36mo total-history target − 24mo requested"},
 "ranked":[
   {"lang":"pl","title":"Przerywany post",
    "yoy_growth":0.34,"share_yoy_growth":0.31,"momentum_6mo":0.28,
    "spikes_removed":1,"seasonal_peaks_kept":1,
    "significant":true,"trend_test":"mann_kendall",
    "confidence":"medium","reason":"36mo+ history; share_yoy_growth agrees in sign with yoy_growth"},
   {"lang":"cs","title":"Přerušovaný půst",
    "yoy_growth":0.11,"share_yoy_growth":-0.02,"momentum_6mo":0.09,
    "spikes_removed":0,"seasonal_peaks_kept":0,
    "significant":false,"trend_test":"mann_kendall",
    "confidence":"low","reason":"share_yoy_growth sign disagrees with yoy_growth"}],
 "unranked":[
   {"lang":"sk","title":"Prerušovaný pôst",
    "yoy_growth":null,"yoy_growth_flag":"new_or_dormant","share_yoy_growth":null,
    "confidence":"low","reason":"zero baseline for YoY comparison (no prior-period activity to compare against) -- not evidence of a reliable trend"}],
 "missing":[],
 "omitted":0}
```
With many languages: `"ranked":[...top N...],"omitted":7`.

### `report`

```
report --run-id <id from analyze> --format pdf,png,md
```

Reads cached series from the run folder, no refetch. Method note states: charts/tables cover only the requested period; N reference months before it — targeting ≥36 months of total history, though shorter when that much data isn't available (article too new, or near the July 2015 data start), in which case the note discloses the actual history length achieved — were used only to classify peaks as seasonal, and — when the requested period is <24 months — to supply the prior-year YoY baseline. If the reference year shows a materially different long-term picture than the requested-period trend, adds a short "context" note, separate from and not overriding the main conclusion.

```json
{"run_id":"r_20250922_ab12","files":{"pdf":"cache/runs/r_20250922_ab12/report.pdf",
  "png":"cache/runs/r_20250922_ab12/chart.png","md":"cache/runs/r_20250922_ab12/report.md"},
 "headline":"PL interest growing faster than CS; CS trend not significant"}
```

### `resolve` (optional)

```
resolve --article "<title>" --langs <lang1>,<lang2>,...
```
Manual cross-language + redirect discovery + QID match-confidence check, for when the agent (or user) wants to verify article matching before running a full `analyze`.
```json
{"qid":"Q911219","editions":{"pl":"Przerywany post","cs":"Přerušovaný půst"},
 "redirects":{"pl":["Post przerywany"],"cs":[]},
 "match_confidence":"high","reason":"QID agrees across langlinks+sitelinks"}
```

## 2. Metrics vs. traps

| Metric | Formula (brief) | Trap it guards against |
|---|---|---|
| YoY growth (raw) | (views_last_12mo − views_prev_12mo) / views_prev_12mo, spike-free. If requested period <24 months, "prev 12mo" baseline is pulled from reference data (not charted); report states this explicitly. **If views_prev_12mo == 0:** growth is not computed as a percentage (division by zero) — reported as `null` with a `"new_or_dormant"` flag instead of 0 or infinity (`null` + `"flat"` if views_last_12mo is also 0). Excluded from numeric `--rank-by` ordering and listed separately (e.g. an "emerging/no baseline" group) instead of being coerced into the sort; confidence is capped at "low" — a zero baseline isn't evidence of a reliable trend. | Baseline trend signal |
| share_yoy_growth | same YoY formula, computed on article_views/site_total_views instead of raw views. If its sign disagrees with raw `yoy_growth`, confidence is lowered. **Zero-baseline case:** share_prev_12mo is zero exactly when views_prev_12mo is zero (site totals are never zero), so it gets the same treatment as raw yoy_growth — `null`/`"new_or_dormant"` (or `"flat"`), excluded from ranking, confidence capped at "low"; the sign-disagreement check against raw `yoy_growth` is skipped (not applicable) when either value is `null`. | **Overall Wikipedia traffic trend** confounding the raw signal |
| 6-mo momentum | same 6 calendar months vs. same 6 months last year | **Seasonality** |
| Spike removal | a month is **elevated** if its views ≥ baseline + 3×MAD, where baseline = median and MAD = median absolute deviation, both computed over all months in the combined requested+reference series (robust to the outliers being detected; MAD chosen over stdev since pageviews are overdispersed/skewed — same rationale as the Mann-Kendall choice above). **MAD == 0 fallback** (e.g. a sparse series where most months are zero-view, so there's no spread to measure): the threshold otherwise collapses to `baseline` — if `baseline` is also 0, every non-negative view count would trivially satisfy "≥ 0", wrongly flagging zero-view months as elevated. So when MAD == 0, a month is elevated only if `views > baseline AND views > 0` — zero-view months are never elevated regardless of baseline. Classify each elevated month using the reference window (enough to reach ≥36 months total history, reference-only) — seasonal (kept) if the same calendar month is elevated by this rule in ≥2 of the available years (requested + reference combined); one-off (removed) if elevated in only 1. A genuine nonzero recurring peak is unaffected by the fallback and is still kept as seasonal when it qualifies. | **One-off spikes**, explicitly including "recurring yearly peak ≠ spike" |
| Trend significance | **Seasonal Mann-Kendall** (Hirsch-Slack-Smith 1982) — not Poisson/binomial, since pageviews are overdispersed and those would flag tiny fluctuations as significant — run on the monthly article/site share ratio, not raw views. Splits the series into 12 within-calendar-month subseries (all Januaries, all Februaries, ...), computes each one's S/Var(S) independently, then pools them: every comparison is strictly same-calendar-month-across-years, so a repeating seasonal cycle (e.g. a back-to-school September peak) can never look like a trend, and each subseries' observations are genuinely independent so the classical MK variance formula validly applies. Earlier draft used plain MK on a *rolling* 12-month-summed ratio series instead — that also canceled seasonality, but overlapping rolling sums are strongly autocorrelated with their neighbors, which plain MK's independence-assuming variance formula doesn't account for; measured empirically at a ~61% false-positive rate against a 5% nominal alpha on flat, noisy seasonal data. The seasonal (within-calendar-month) approach avoids inducing that correlation in the first place. | **Low-volume noise**, and avoids seasonality being misread as trend |
| Match confidence | QID agreement (langlinks vs. Wikidata sitelinks) + redirect sanity, at `resolve`/`analyze`-internal-resolve time | **Wrong article match** |
| Confidence + reason (final) | combines: significance result, history length (≥36mo?), `share_yoy_growth` sign agreement, match confidence | Assignment's hard requirement — every conclusion ships with a level and a reason |

Cross-language matching: MediaWiki langlinks primary, Wikidata sitelinks fallback (verified no real divergence on the one article tested — [notes/api.md](api.md) Q3).

Wording rule (all outputs — SKILL.md, CLI help, report/PDF/MD boilerplate, JSON field docs): the `agent=user` filter is described as **"excludes known bots and automated traffic,"** never "humans only."

## 3. File structure

```
wiki-interest/
  SKILL.md
  requirements.txt
  cli.py                     # resolve / analyze / report subcommands
  src/wiki_interest/
    api_client.py             # pageviews + MediaWiki + Wikidata, UA header, 429 backoff, rdcontinue paging, 404-ambiguity handling
    resolve.py                 # cross-language + redirect resolution, QID match check
    cache.py                    # disk cache + run-folder management
    metrics.py                   # YoY, share_yoy_growth, momentum, spike removal, Mann-Kendall significance, confidence
    charts.py                     # PNG chart generation
    report.py                      # PDF one-pager + Markdown assembly
  tests/
    test_api_client.py, test_resolve.py, test_metrics.py, test_report.py
    fixtures/                       # recorded API responses for mocking
  evals/
    evals.json
    transcripts/                      # saved Haiku/OpenRouter eval runs
  cache/                                # gitignored: runtime disk cache + run folders
  README.md                             # usage + roadmap section
```

## 4. Milestones (branch → PR → tests, per CLAUDE.md workflow)

| # | Branch | Scope | Test gate |
|---|---|---|---|
| M1 | `feat/wiki-api-client` | `api_client.py`: pageviews per-article/aggregate, MediaWiki redirects (`rdlimit=max` + `rdcontinue` paging), Wikidata sitelinks, langlinks, contact-bearing UA header, 429 backoff, 404-ambiguity handling (in-range 404 ≠ proof of no data) | `test_api_client.py`, all network mocked; covers empty data, 404, rate-limit backoff |
| M2 | `feat/wiki-analysis` | `resolve.py`, `metrics.py`, `cache.py`: variable-length reference-window fetch to ≥36mo total history, corrected seasonal rule (≥2 of available years, requested+reference combined), Mann-Kendall/bootstrap significance, `share_yoy_growth` + sign-disagreement confidence rule, <24-month-request YoY-baseline-from-reference case, ranking, `missing`-language detection, server-side `run_id` generation | tests cover: <36mo history lowers confidence; a peak recurring in ≥2 available years (incl. within a long requested period) is kept; a peak in only 1 year is removed; `share_yoy_growth` sign disagreement lowers confidence; <24mo request pulls YoY baseline from reference and reports say so; zero-baseline (views_prev_12mo == 0) yields `null`/`"new_or_dormant"` (or `"flat"` if both periods are zero) for both `yoy_growth` and `share_yoy_growth`, not a division error or infinity, is excluded from `--rank-by` ordering, and gets confidence capped at "low"; a month just below baseline+3×MAD is not flagged elevated, a month at or above it is; a combined series with median==0 and MAD==0 (mostly zero-view months, one positive one-off spike) asserts all three: the positive spike month IS classified as elevated (the fallback still catches a real spike, not just suppresses everything), the zero-view months are NOT classified as elevated, and the spike is NOT misclassified as seasonal just because unrelated zero-view months land in the same calendar-month slot in other years |
| M3 | `feat/wiki-charts-report` | `charts.py`, `report.py` (PDF one-pager + PNG + Markdown), method note incl. reference-window disclosure and optional "context" note | tests assert output files exist, parse, stay reasonably sized, and the method note text is present |
| M4 | `feat/wiki-cli` | `cli.py`: `analyze` takes `--topic`/`--langs` and resolves internally (cached); `resolve` is a separate optional subcommand; JSON I/O contracts | tests assert: basic-question path is exactly 2 calls (`analyze`+`report`); each response ≤2KB; output-cap/omission behavior with many languages; `run_id` is server-generated, never accepted as input |
| M5 | `docs/wiki-skill` | `SKILL.md`, `evals/evals.json` from the assignment's example prompts | eval-runner subagent dry run against `SKILL.md` only, not source code |
| M6 | `test/wiki-haiku-openrouter` | No new prod code — real runs via OpenRouter against Claude Haiku 4.5, transcripts saved | manual/eval-runner verification of the 2-call, ≤2KB budgets in practice; transcripts saved under `evals/` |
| M7 | `docs/wiki-readme-roadmap` | `README.md` usage + half-page roadmap (topic baskets, user-defined criteria, cross-checks with other sources; bulk dumps + local DuckDB/SQLite + parallel fetching with rate limits; eval-driven iteration loop) | none (docs only) |

Each milestone: branch per the `<type>/<short-name>` convention in CLAUDE.md, small commits, `gh pr create --fill`, then stop for review — no self-merge; subagents don't run git.

## 5. Wording rule

See §2 — applies globally to SKILL.md, CLI help text, report/PDF/Markdown boilerplate, and JSON field docs.
