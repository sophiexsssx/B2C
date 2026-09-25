# Transcript: many-languages-output-cap

- **Eval id**: 7
- **Runner**: `blind-runner` subagent (`model: haiku`)
- **Date**: 2026-09-26
- **Prompt** (verbatim):

> We're a language-learning app. Compare interest in "Yoga" across as many Wikipedia language editions as you reasonably can -- at least 15 -- and tell us the top 3 fastest-growing markets.

## Tool calls

1. `Read` → `SKILL.md` (6420 bytes)
2. `Bash`: `python3 cli.py analyze --topic "Yoga" --langs en,es,fr,de,it,pt,ru,pl,ja,zh,ko,ar,hi,nl,sv,tr,el` (17 languages) → **1960 bytes** -- very close to the 2KB cap (95.7% of the 2048-byte budget).
3. `Bash`: `python3 cli.py report --run-id r_20260926_eceeff` → 388 bytes.

Clean 2-call run.

## Final answer (English)

All 17 languages came back declining -- none growing. Rather than fabricating
a "top 3 fastest-growing" answer that doesn't exist in the data, the model
correctly reframed to "top 3 most resilient / least-declining markets" (Japan
-15.1%, Germany -18.3%, Sweden -18.4%) and stated plainly "this is not a
selective finding -- the entire landscape is declining. Even the
'fastest-growing' regions are contracting." Points to the Markdown report for
the full table rather than listing all 17 in the chat reply.

## Metrics

| Metric | Value |
|---|---|
| `cli.py` calls | 2 |
| Max single response | **1960 bytes** -- close to, but under, the 2KB cap |
| Language of answer | English (matches the prompt) |

## Notes

This is the tightest real-world margin against the 2KB budget observed across
all 8 evals -- a `--langs` list even modestly larger than 17, or with longer
resolved article titles, could plausibly push a response over 2KB and trigger
the CLI's own truncation/`omitted` path. Worth keeping an eye on if the skill
is ever used with 20+ languages routinely.
