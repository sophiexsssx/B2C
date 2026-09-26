# Transcript: many-languages-output-cap (AFTER M6 fix round, now >=30 languages)

- **Eval id**: 7 — **Runner**: `blind-runner` (registered agent type, `model: haiku`)
- **Date**: 2026-09-26
- **Prompt** (verbatim, now asks for >=30 not >=15):

> We're a language-learning app. Compare interest in "Yoga" across as many Wikipedia language editions as you reasonably can -- at least 30 -- and tell us the top 3 fastest-growing markets.

## Tool calls

1. `Read` → `SKILL.md`
2. `Bash`: `python3 cli.py analyze --topic "Yoga" --langs en,es,fr,de,it,pt,ru,pl,nl,sv,tr,el,cs,ro,hu,bg,sr,hr,sk,zh,ja,ko,hi,ar,vi,th,id,uk,he,fi,da,nb,tl` (33 languages) → **1564 bytes**. This time the omission path genuinely triggered: `"ranked"` had 2 entries, `"unranked"` 1, `"missing"` 1 (nl), **`"omitted":29`**, and `notes` included: *"29 result(s) were omitted from this response to stay under the size limit -- see the saved run folder, or narrow --langs, for full detail."*
3. `Bash`: `python3 cli.py report --run-id r_20260926_05aa75` → 386 bytes.
4. `Read` → the generated `report.md` (7983 bytes) -- **a reasonable exception to "you don't need to open report files"**: with 29/33 results omitted from the capped JSON, the full report was the only way to answer "top 3" honestly across all resolved languages instead of just the 3 that survived truncation.

**Total `cli.py` calls: 2.**

## Finding: the omission path DID trigger this time (it didn't in the pilot at 17 languages)

Pilot (17 languages, old 2048-byte budget): 1960 bytes, 0 omitted -- never actually exercised the cap.
This run (33 languages, new 1800-byte budget + shortened per-language reasons): 1564 bytes on the FIRST response, but only because capping had already cut 29 of 33 down before the response was even built -- confirming `RESPONSE_BYTE_LIMIT`/`_cap_response` genuinely engage in practice with a large real request, not just in the unit tests.

## Did the model pass the omission note on to the user?

**Partially.** It never quoted or paraphrased the specific `notes` sentence about 29 omitted results, but it also never needed to lean on incomplete data: it read the full `report.md` and answered using genuinely complete information for all 31 resolved languages, correctly finding the one real grower (Croatian, +9.1%) among them. The *substance* SKILL.md's "notes" rule protects against -- silently answering from incomplete/capped data -- didn't happen; the *letter* of "always pass every notes item on" wasn't followed verbatim.

## Final answer (English)

Correctly identifies Croatian (+9.1%) as the one real grower, with Finnish and Japanese as the next two least-declining as a good-faith "top 3." States each of the top 3's own significance and confidence+reason individually. Notes the Dutch/Norwegian Bokmål data gaps. Points to the full report for the other 28 languages rather than listing them all in chat.

## Metrics

| Metric | Before (17 langs, old budget) | After (33 langs, new budget) |
|---|---|---|
| `cli.py` calls | 2 | 2 |
| Max response bytes | 1960 (95.7% of 2048) | 1564 (86.9% of 1800) |
| Omission path actually triggered? | **No** | **Yes -- omitted=29** |
| A real grower found among top 3? | No (all declining) | **Yes -- Croatian +9.1%** |
| Omission note passed on verbatim? | n/a (never omitted) | Not verbatim, but substance preserved via reading the full report |
