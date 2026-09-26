# Transcript: two-turn-follow-up-add-language-and-rerank

- **Eval id**: 8
- **Runner**: `blind-runner` subagent (`model: haiku`), continued across 2 turns via
  the same agent (`SendMessage` to its agentId), so turn 2 has full context of
  turn 1 -- not a fresh spawn.
- **Date**: 2026-09-26

## Turn 1

**Prompt** (verbatim): "Compare interest in astronomy in uk and pl"

Tool calls:
1. `Read` → `SKILL.md`
2. `Bash`: `python3 cli.py analyze --topic "Astronomy" --langs en,pl` → 1137 bytes
3. `Bash`: `python3 cli.py report --run-id r_20260926_132687` → 388 bytes

**Real discrepancy (the headline finding of this eval): the model read "uk" as
the country "United Kingdom" and used `--langs en,pl`, not `--langs uk,pl`
(Ukrainian) as `evals.json` intends.** The prompt "Compare interest in
astronomy in uk and pl" is genuinely ambiguous in English -- "uk" is both a
valid ISO language code (Ukrainian) and the common abbreviation for the United
Kingdom -- and the model picked the country reading. SKILL.md's own examples
list `uk` in its `--langs` code list (`pl`, `cs`, `uk`, ...), so the correct
reading was available to it, but nothing in the prompt or the skill forces
disambiguation toward the language-code reading over the more common
everyday-English reading of "uk."

Turn-1 answer (English) reports UK (English Wikipedia) at -20.7% YoY, high
confidence, and Poland at -32.9% YoY, medium confidence, with both `cli.py`
calls only 2, matching the basic-question budget.

## Turn 2

**Prompt** (verbatim): "Now add German and rank by audience size instead"

Tool calls:
4. `Bash`: `python3 cli.py analyze --topic "Astronomy" --langs en,pl,de --rank-by avg_monthly_views` → 1557 bytes
5. `Bash`: `python3 cli.py report --run-id r_20260926_e4ee7d` → 388 bytes

Correctly treated as an incremental update: kept `en` (carrying forward turn 1's
language-code misreading) and `pl`, added `de`, and used `--rank-by
avg_monthly_views` (the CLI's own ranking option) rather than hand-rolling any
sort/filter logic itself. Re-ran a fresh `analyze` (new `run_id`) rather than
trying to patch turn 1's run in place -- this is normal for `analyze` (SKILL.md
documents it as re-fetching only what's new via the on-disk cache), and the
per-language JSON output is unaffected by whether the underlying HTTP fetch
was a cache hit; **whether `en`/`pl` were actually re-downloaded from Wikimedia
vs. served from cache is not observable from the tool-call transcript alone**
(would need to inspect `cache/` on disk or network-level logs) and is not
verified here.

Turn-2 answer (English) ranks by audience size: UK 28,516 views/month, Germany
4,156, Poland 1,425 -- correctly re-ranked, correctly incorporates the new
language, in the same single incremental pass (2 more `cli.py` calls).

## Metrics

| Metric | Turn 1 | Turn 2 |
|---|---|---|
| `cli.py` calls | 2 | 2 |
| Max single response | 1137 bytes | 1557 bytes |
| Both ≤2KB? | Yes | Yes |
| Language of answer | English | English |

Across both turns: 4 `cli.py` calls total (2 per turn, matching the per-turn
budget), no hand-rolled sorting/filtering, no re-fetch of the full language set
from scratch (added only `de` to the existing `en,pl`).
