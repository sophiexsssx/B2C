# wiki-interest

Cross-language Wikipedia pageview analysis: is interest in a topic growing,
in which languages, and how much should that be trusted. Built as an Agent
Skill (see [`SKILL.md`](SKILL.md) for the instructions an agent reads), but
the underlying `cli.py` is a normal command-line tool you can run yourself.

Every number comes from the real Wikimedia Pageviews API, MediaWiki
langlinks, and Wikidata sitelinks -- nothing here is estimated or
fabricated. See [`../notes/plan.md`](../notes/plan.md) and
[`../notes/requirements.md`](../notes/requirements.md) for the full design
rationale; this file covers how to install and run it, plus where it's
headed next.

## Install

```bash
cd wiki-interest
pip3 install -r requirements.txt
```

(If `pip3` isn't on your PATH, try `pip`.) No other setup is required.
`WIKITREND_CONTACT` is an optional environment variable -- a contact
URL/email Wikimedia's User-Agent policy asks API clients to identify
themselves with. The CLI already sends a working default (this repo's own
URL); set `WIKITREND_CONTACT` only if you want requests attributed to a
different contact instead.

## Usage

The tool has three subcommands. A basic question is just two calls:
`analyze` then `report`.

### `analyze` -- fetch, compute, and cache

```bash
python3 cli.py analyze --topic "Intermittent fasting" --langs pl,cs
```

Resolves each requested language's article (cross-language matching via
MediaWiki langlinks, with Wikidata sitelinks as a fallback check), fetches
its pageviews plus enough history to reach 36 months total, computes
growth/significance/confidence per language, and prints a compact JSON
summary to stdout. The full result (every month, every language, all
detail) is saved to a run folder under `cache/runs/<run_id>/` -- `report`
reads from there, so nothing needs refetching.

Common flags:

| Flag | Meaning |
|---|---|
| `--topic "<title>"` | Required. A Wikipedia article title in `--source-lang` (default `en`). |
| `--langs <a>,<b>,...` | Required. Comma-separated language codes (ISO 639-1 edition codes, e.g. `pl,cs,uk` -- not country codes; see `SKILL.md`'s "Language codes" section for the common mix-ups). |
| `--start`/`--end` | `YYYY-MM` (a full `YYYY-MM-DD` also works; the day is dropped). Omit both together for the default last 24 complete months -- you can't pass just one. |
| `--source-lang` | Language `--topic` itself is written in (default `en`). |
| `--rank-by` | `yoy_growth` (default), `share_yoy_growth`, `momentum_6mo`, or `avg_monthly_views` (raw audience size instead of a growth rate). |
| `--article lang:"Title"` | Override automatic resolution for one language, e.g. `--article pl:"Post przerywany"`. Repeatable. Skips the langlinks/Wikidata cross-check for that language. |
| `--agent` | `user` (default, excludes known bots and automated traffic) or `all-agents`. |

The response's `ranked`/`unranked` entries carry `yoy_growth`,
`share_yoy_growth`, `momentum_6mo`, `avg_monthly_views`, `significant`,
`confidence`, and a plain-language `reason`. `missing` lists languages
with no matching article at all. `notes` flags anything else worth
knowing -- a borrowed year-over-year baseline for a short period, a
clamped end date, capped redirects, low-volume or not-statistically-
significant results, a ready-to-use `--article` retry for a missing
language, or an omitted-results count if the response would otherwise
exceed its size budget. If the response was capped and you need the full
per-language table anyway, it's always in the generated Markdown report
(see `report`, below) -- nothing is ever dropped from the run folder
itself, only from the summary JSON.

### `report` -- render PDF/PNG/Markdown from a cached run

```bash
python3 cli.py report --run-id r_20250922_ab12
```

Reads the run folder `analyze` already wrote (no refetch) and renders a
one-page PDF (summary, key-numbers table, trend + growth-comparison charts,
trust notes, method & limits), a standalone PNG chart, and a Markdown
version of the same report. Safe to call again later, including after
`analyze` was re-run with an expanded `--langs` list for the same topic
(the on-disk cache means only the new languages get fetched).

| Flag | Meaning |
|---|---|
| `--run-id <id>` | Required. The `run_id` `analyze` returned. Never supplied by the caller to `analyze` itself -- it's always server-generated. |
| `--format pdf,png,md` | Comma-separated subset of the three formats (default: all three). |
| `--summary "..."` | Override the auto-generated plain-language headline for this render only; not persisted to the cached run. |

### `resolve` -- optional manual pre-check

```bash
python3 cli.py resolve --article "Astronomy" --langs uk,pl,de
```

Cross-language + redirect discovery and a Wikidata QID match-confidence
check, without fetching any pageviews. Useful for verifying a title
resolves the way you expect before committing to a full `analyze` run, or
for tracking down the right title for a language that came back `missing`.
Not on the default path -- `analyze` already does this resolution
internally.

## Where the output goes

`cache/` (gitignored) holds the disk cache for downloaded API responses
plus one folder per `run_id` under `cache/runs/`, each with the full
per-language series, computed metrics, and (once `report` has been called)
`report.pdf`, `chart.png`, and `report.md`.

## Testing

```bash
python3 -m pytest tests -q
```

All HTTP calls are mocked (`requests_mock`) -- no real network access is
needed to run the test suite. Real, blind runs against a live Haiku-class
model via a background subagent, with full transcripts, live under
`evals/` (`evals/evals.json` for the scenarios, `evals/transcripts/` for
the runs, `evals/results/haiku-blind.md` for the write-up).

## Roadmap

This version covers the core case well: one topic, a handful of named
languages, a default or short custom date range, run interactively. Three
directions it would need to grow in for more ambitious use:

**More complex research.** A request today is one topic against one
language list. A real question is often a *basket* of related topics
("intermittent fasting," "ketogenic diet," "time-restricted eating")
compared together, or user-defined success criteria ("only markets above
some audience threshold *and* statistically significant") instead of a
fixed top-N. Both want a small query layer above `analyze` that fans out to
several runs (reusing the shared disk cache) and filters/combines against
caller-supplied criteria, rather than an agent hand-rolling that in
context. Pageviews also only show what people *looked up*, not what they
*bought* -- a real launch decision should cross-check against at least one
independent signal (app-store search volume, Google Trends) before trusting
a Wikipedia trend alone.

**Larger data.** One API call per article/redirect/site-total, kept in
per-run JSON, doesn't scale to every language edition at once or
multi-decade history. Wikimedia's bulk pageview dumps, ingested into a
local DuckDB or SQLite store, would turn most of that into one-time bulk
loads plus local SQL -- `analyze` becomes a query against the store for
anything already ingested, only hitting the live API for what isn't.
Whatever still needs a live fetch could go in parallel across languages,
behind a shared rate limiter (today's client is deliberately sequential
with backoff, out of respect for Wikimedia's API etiquette).

**Iteration.** `evals/evals.json` plus the blind-runner/eval-grader pair is
already a real, manual eval loop: run blind, grade against `expect`, fix,
rerun (see `evals/results/haiku-blind.md`). Scaling it up means running it
on a schedule or on every `SKILL.md`/`cli.py` change instead of by hand,
growing the scenario set as real usage turns up new edge cases (the way
evals 6-9 here each targeted one observed failure), and tracking pass rate
over time so a regression shows up as a trend, not a one-off surprise.
