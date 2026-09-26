# Transcript: short-period-under-24-months (AFTER M6 fix round)

- **Eval id**: 6 — **Runner**: `blind-runner` (registered agent type, `model: haiku`)
- **Date**: 2026-09-26

## Tool calls

1. `Read` → `SKILL.md`
2. `Bash`: `python3 cli.py analyze --topic "Electric bicycle" --langs de,nl --start 2026-04 --end 2026-09` → 1516 bytes.
   `notes`: `["The year-over-year comparison's baseline for this period includes 31 extra month(s) of history before 2026-04, since the requested period is under 24 months."]` (plus the end-clamp note, since `--end 2026-09` isn't a complete month yet).
3. `Bash`: `python3 cli.py report --run-id r_20260926_fbb15d` → 414 bytes.

**Total `cli.py` calls: 2**, both successful on the first try -- the model went straight to `YYYY-MM` this time (didn't need the `YYYY-MM-DD` normalization fix in THIS run, though that fix still closes the gap for whichever future run guesses a full date, as the pilot did).

## Final answer (English, matches the prompt's language)

**"Key Notes" section explicitly states**: "Year-over-year baseline includes 31 extra months of history before April 2026, since the 5-month period is under the standard 24-month window" -- **this is the exact reference-baseline-borrowing disclosure that FAILED in the pilot** (which only said "insufficient years of data for significance," a different claim). The `notes` field surfaced it automatically and the model passed it on close to verbatim, exactly as SKILL.md's new "always pass every `notes` item on" instruction asks. Also correctly reports both languages' own significance (both not significant) and confidence with reasons.

## Metrics

| Metric | Before | After |
|---|---|---|
| `cli.py` calls | 3 (1 failed on date format) | **2** (0 failed) |
| Max response bytes | 1502 | 1516 |
| Reference-baseline-borrowing disclosed? | **No (FAIL)** | **Yes (fixed)** -- via the new `notes` field |
| Language | English | English |
