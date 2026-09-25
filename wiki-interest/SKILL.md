---
name: wiki-interest
description: Analyzes Wikipedia pageview trends across languages to help B2C product teams decide which topics to develop and which languages to launch in. Produces a one-page PDF, a PNG chart, and a Markdown report with growth rates, statistical significance, and trust notes. Use this skill whenever the user asks whether interest in a topic is growing, wants to compare a topic across Wikipedia language editions, is deciding which market or language to localize into next, or wants a data-backed short report they can share -- even if they phrase it as a business question ("is there demand for X", "should we launch in language Y", "which audience should we target next") rather than explicitly mentioning Wikipedia or pageviews.
---

# Wiki Interest

Cross-language Wikipedia pageview analysis: is interest in a topic growing,
in which languages, and how much should that be trusted. Every number comes
from real Wikimedia pageview data -- never estimate or fabricate one.

## Setup (once per environment)

Run from this skill's own directory (`cd` into it first, or use its full
path): `pip3 install -r requirements.txt` (if `pip3` is not found, try
`pip`). No other setup is required -- `WIKITREND_CONTACT` (a contact
URL/email for Wikimedia's User-Agent policy) is optional; the CLI already
sends a working default. Set it only if you want requests attributed to a
different contact than this project's own repo.

## A basic answer is 2 `cli.py` calls

```bash
python3 cli.py analyze --topic "<title>" --langs <lang1>,<lang2>,...
python3 cli.py report --run-id <run_id from analyze>
```

(If `python3` is not found, try `python`.)

`analyze` resolves each language's article, fetches pageviews, computes
growth/significance/confidence, and returns a compact JSON summary.
`report` reads what `analyze` already saved and renders the PDF/PNG/
Markdown -- it never re-fetches, so it's fast, and safe to call again later
(e.g. with `--summary "..."` to swap in your own headline). Don't call
`resolve` on the default path -- it's a manual pre-check for verifying a
match before committing to a full `analyze`; `analyze` already resolves
internally and reuses the same on-disk cache across calls, so adding a
language or re-running with new ones doesn't re-fetch what's already there.

You don't need to open the generated report files -- the `analyze` and
`report` JSON responses already contain everything for your answer (the
numbers from `analyze`, the file paths from `report`). Just give the user
the file paths `report` returns; opening the PDF/PNG/Markdown yourself only
costs extra calls without adding information you don't already have.

## Turning a request into an `analyze` call

- **`--topic`**: a real Wikipedia article title, in `--source-lang`
  (default English). If the user names a topic in another language, either
  translate it or pass `--source-lang` instead of guessing a translation.
- **`--langs`**: comma-separated codes (`pl`, `cs`, `uk`, ...). If the user
  hasn't actually named which languages, don't guess a default list --
  either ask, or state up front, before running anything, which languages
  you're about to use and why (never decide silently and only mention it
  afterward).
- **Period**: omit `--start`/`--end` for the default (last 24 complete
  months) -- e.g. "last two years" needs nothing extra. Always pass both or
  neither; one without the other is rejected. A period under 24 months
  borrows its year-over-year baseline from extra history outside the
  displayed range -- mention that if the user asked about a short window.

## Reading the response

`ranked`: languages with a real growth number, sorted by `--rank-by`
(default `yoy_growth`; also available: `share_yoy_growth`, `momentum_6mo`,
and `avg_monthly_views` for raw audience size instead of a growth rate --
each result also carries `avg_monthly_views`/`last12_avg_monthly_views`
regardless of which one you rank by). A low-volume topic has its
confidence capped even if the growth rate itself looks clean, since small
audiences make percentage swings noisier -- `reason` explains this the
same way it explains any other confidence factor. `unranked`: `yoy_growth` is `null` --
`yoy_growth_flag` says why (`new_or_dormant`/`flat`: no prior activity to
compare against, often the most interesting finding, not one to bury;
`no_data`: Wikimedia returned nothing for this range, possibly just not
loaded yet). `missing`: no matching article at all -- a content gap, not an
error; if surprising, it may exist under a different title -- try `resolve
--article "<title>" --langs <lang>` to check, or retry `analyze` with
`--article <lang>:"<correct title>"`. `omitted`: how many low-priority
results were cut to keep the response small -- point the user to the
Markdown report for the full table instead of listing everything yourself.

Every result carries `confidence` and `reason`, and a `significant` flag.
Never quote these fields' raw names or values back at the user -- translate
`reason` into plain language, and don't imply more certainty than
`significant: false` supports.

If a call fails, the output is `{"error": "...", "hint": "..."}` with a
non-zero exit code -- read `hint`, fix the arguments it points at, and
retry once; don't loop on the same call unchanged.

## Your final answer

Regardless of language, structure it as:
1. **Direct answer** to what was asked (growing/declining/flat, in which
   languages).
2. **Key numbers per language** (growth rate, plain language).
3. **Trust**: confidence level + the reason, in plain words.
4. **A concrete next step**: what to check to validate further (a longer
   window, another language, a manual `resolve`, etc.) -- not just "trust
   this."
5. **Report file paths** (PDF/PNG/Markdown), so the user can share them.

Answer in the same language the user asked in.

## What this skill will never do

Report a growth number without its confidence and reason, silently drop a
language that came back `missing` or `unranked`, or call anything other
than this CLI to get pageview data.
