# Transcript: short-period-under-24-months

- **Eval id**: 6
- **Runner**: `blind-runner` subagent (`model: haiku`)
- **Date**: 2026-09-26
- **Prompt** (verbatim):

> Compare interest in "Electric bicycle" between German and Dutch Wikipedia specifically for the last 6 months.

## Tool calls

1. `Read` → `SKILL.md` (6420 bytes)
2. `Bash`: `python3 cli.py analyze --topic "Electric bicycle" --langs de,nl --start 2026-04-01 --end 2026-09-30` → 193 bytes, **failed** (exit 1):
   `{"error":"--start ('2026-04-01') must be a canonical \"YYYY-MM\" value...", "hint":"check the argument that was rejected and correct it, then retry"}`
   The model used a full `YYYY-MM-DD` date instead of the CLI's required `YYYY-MM`.
3. `Bash`: `python3 cli.py analyze --topic "Electric bicycle" --langs de,nl --start 2026-04 --end 2026-09` → 1502 bytes (success).
   Both `de` and `nl` came back `significant: false` (too few years of same-calendar-month data for the seasonal test), with `reference_months_before: 31` (a large reference window borrowed to compute YoY over a 5-month requested period).
4. `Bash`: `python3 cli.py report --run-id r_20260926_3d68a7` → 414 bytes.

**Total `cli.py`-directed calls: 3** (1 failed on date format, 2 successful), reading `hint` and self-correcting on one retry as SKILL.md instructs.

## Final answer (English)

States both languages are declining (-22.45% de, -28.07% nl) but explicitly flags
**neither trend as statistically significant** -- "we cannot confidently conclude
the decline is real rather than normal fluctuation." Attributes the low
confidence to small audience size / insufficient years of data for the
significance test. Does **not** separately mention that the YoY figure's
reference baseline was borrowed from ~31 months of extra history outside the
requested 6-month window -- it conflates "not enough data to test significance"
with the distinct reference-baseline-borrowing concept from SKILL.md's `period`
field guidance, without naming the latter.

## Metrics

| Metric | Value |
|---|---|
| `cli.py` calls | 3 (1 failed on date format) |
| Max single response | 1502 bytes (under 2KB) |
| Language of answer | English (matches the prompt) |

## Notes

Real discrepancy, same class as the earlier `python`/`python3` issue: the CLI
requires `--start`/`--end` in `YYYY-MM` format, but nothing in the prompt or
SKILL.md's wording stopped the model from reasonably guessing `YYYY-MM-DD` first
("I'll use the period 2026-04-01 to 2026-09-30"). It self-corrected cleanly from
the `hint`, costing one wasted call.
