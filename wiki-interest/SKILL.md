---
name: wiki-interest
description: Analyzes Wikipedia pageview trends across languages to help B2C product teams decide which topics to develop and which languages to launch in. Produces a one-page PDF, a PNG chart, and a Markdown report with growth rates, statistical significance, and trust notes. Use this skill whenever the user asks whether interest in a topic is growing, wants to compare a topic across Wikipedia language editions, is deciding which market or language to localize into next, or wants a data-backed short report they can share -- even if they phrase it as a business question ("is there demand for X", "should we launch in language Y", "which audience should we target next") rather than explicitly mentioning Wikipedia or pageviews.
---

# Wiki Interest

**Always reply in the language the user wrote in.**

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

## Language codes

`--langs` and `--source-lang` take Wikipedia's ISO 639-1 edition codes, not
country names or domain suffixes -- a request phrased around a country
needs translating to the right edition first:

- `uk` = **Ukrainian Wikipedia**. **There is no "United Kingdom" Wikipedia
  -- English (`en`) is Wikipedia's global edition, and that's what covers
  the UK.** This is a real, easy mix-up in English text ("uk and pl" reads
  naturally as "the United Kingdom and Poland" even though `uk` is
  Ukrainian's code) -- if a request could plausibly mean the country,
  **say so explicitly in your answer** (e.g. "I've used `en`, since
  Wikipedia doesn't have a UK-specific edition -- let me know if you meant
  Ukrainian") rather than silently picking one reading. Never use `uk` for
  "the UK".
- Other common codes: `en` English, `de` German, `fr` French, `es` Spanish,
  `pt` Portuguese, `ru` Russian, `pl` Polish, `cs` Czech, `nl` Dutch, `sv`
  Swedish, `tr` Turkish, `it` Italian, `el` Greek, `ja` Japanese, `zh`
  Chinese, `ko` Korean, `ar` Arabic, `hi` Hindi, `vi` Vietnamese, `ro`
  Romanian.
- Anything not listed above: use its standard ISO 639-1 code -- don't guess
  a country's domain suffix (e.g. Brazil's Wikipedia is `pt`, not `br`).

## Turning a request into an `analyze` call

- **`--topic`**: a real Wikipedia article title, in `--source-lang`
  (default English). If the user names a topic in another language, either
  translate it or pass `--source-lang` instead of guessing a translation.
- **`--langs`**: comma-separated codes -- see "Language codes" above. If
  the user hasn't actually named which languages, don't stop to ask --
  proceed with a sensible default set (e.g. a handful of major and/or
  clearly relevant editions for the request), then state up front, before
  showing any numbers, which languages you picked and why, and offer to
  rerun with different ones. Never decide silently and only reveal the
  choice after the fact -- the "state it up front" part is what makes a
  default acceptable instead of a guess.
- **Period**: omit `--start`/`--end` for the default (last 24 complete
  months) -- e.g. "last two years" needs nothing extra. Always pass both or
  neither; one without the other is rejected. `YYYY-MM` is the format
  (`YYYY-MM-DD` also works -- the day is dropped). A period under 24 months
  borrows its year-over-year baseline from extra history outside the
  displayed range; `analyze`'s response flags this itself in `notes` when it
  applies, so you don't have to remember the 24-month threshold yourself --
  just pass every `notes` item on (see "Reading the response" below).

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
error. **Say so explicitly in your final answer** (don't just silently skip
that language) -- `notes` already gives you the exact retry to offer (the
next paragraph), so you don't need to construct the `--article` syntax
yourself; just relay it. `omitted`: how many low-priority results were cut
to keep the response small -- point the user to the Markdown report for the
full table instead of listing everything yourself.

`notes`: a list of plain-language sentences (possibly empty) flagging
anything you need to know that isn't obvious from the numbers alone -- a
borrowed year-over-year baseline for a short period, an end date that got
clamped to the last complete month, capped redirects, low-volume or
not-statistically-significant languages, a ready-to-use `--article
<lang>:"<correct title>"` retry for each language in `missing`, or results
omitted to stay under the size limit. **Always pass every item in `notes`
on to the user, in your own words** -- these are facts they need, not
internal/optional detail you can decide to skip, and for `missing` in
particular, relaying the `notes` entry verbatim (as the concrete retry
syntax) is more useful to the user than paraphrasing it away.

Every result carries `confidence` and `reason`, and a `significant` flag.
Never quote these fields' raw names or values back at the user -- translate
`reason` into plain language, and don't imply more certainty than
`significant: false` supports. `reason` in the JSON response is a shortened
summary (the full version is saved to the run folder) -- it's still enough
to state the confidence factor in your own words, so this never blocks
answering.

If a call fails, the output is `{"error": "...", "hint": "..."}` with a
non-zero exit code -- read `hint`, fix the arguments it points at, and
retry once; don't loop on the same call unchanged.

## Your final answer

Structure it as:
1. **Answer in the language the user wrote in.** This is the first rule,
   before anything about content -- it applies to every message you send
   them, including a stated-default-languages note or an offer to adjust,
   not just a polished final summary.
2. **Direct answer** to what was asked (growing/declining/flat, in which
   languages).
3. **For EACH language** in `ranked` and `unranked` alike (not just the
   headline one): its growth direction/rate, whether the trend is
   statistically significant, and its confidence level with the
   plain-language reason. Don't blend multiple languages into one summary
   statement -- a reader comparing languages needs each one's own numbers.
4. **Every item in `notes`**, passed on in your own words (or verbatim for
   a `--article` retry, see "Reading the response" above) -- never
   skipped.
5. **A concrete next step**: what to check to validate further (a longer
   window, another language, a manual `resolve`, etc.) -- not just "trust
   this."
6. **Report file paths** (PDF/PNG/Markdown), so the user can share them.

## What this skill will never do

Report a growth number without its confidence and reason, silently drop a
language that came back `missing` or `unranked`, or call anything other
than this CLI to get pageview data.
