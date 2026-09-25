# M6: Blind Haiku eval results

Real (not simulated) runs of `evals.json` scenarios against a genuine Haiku-class
model (`model: haiku`), blind to the `expect`/`notes` fields, via the two-agent
harness described below. Each run's full tool-call transcript is under
`evals/transcripts/claude-code-haiku/<eval-name>.md`; grading is done separately,
against `expect` only, by a second model that never sees the runner's transcript
being produced live.

## Harness

- **Runner**: `.claude/agents/blind-runner.md` — `model: haiku`, tools `Bash`+`Read`
  only. Given nothing but the raw eval prompt; told to read only `SKILL.md`
  (never `evals/`, `tests/`, or source) and solve the request via the CLI.
- **Grader**: `.claude/agents/eval-grader.md` — `model: sonnet`, tool `Read` only.
  Given the runner's saved transcript plus that eval's `expect` list; marks each
  item PASS/FAIL with a quoted excerpt as evidence.
- **Registration caveat**: both files were added to `.claude/agents/` mid-session,
  and this session's agent-type registry doesn't hot-load new files — invoking
  `subagent_type: blind-runner` / `eval-grader` failed with "not found" both
  times it was tried. Every run below used `general-purpose` with an explicit
  `model` override and the target agent's exact instructions inlined as the
  prompt, which is behaviorally identical (same model, same system instructions,
  same inputs) — only the subagent-type label differs. The two `.md` files are
  still the real, reusable deliverable and will register normally in a fresh
  session.

## Results

### 2. astronomy-course-uk-trust — 5/6 PASS

Transcript: [astronomy-course-uk-trust.md](../transcripts/claude-code-haiku/astronomy-course-uk-trust.md)

1. PASS — analyze called with `--topic "Astronomy" --langs uk`.
2. **FAIL** — not exactly 2 `cli.py` calls. The model's first attempt ran
   `python cli.py analyze ...`, which failed (`command not found: python`,
   exit 127) on this machine, since only `python3` is on PATH. It correctly
   self-corrected and retried with `python3`, reaching the intended 2
   *successful* calls (analyze, report) — but 3 `cli.py`-directed Bash
   invocations were made in total.
3. PASS — states plainly that interest is declining (~61% YoY), not growing.
4. PASS — states confidence (medium) with a plain-language reason (modest
   audience size, ~964 views/month), not a raw field dump.
5. PASS — `significant: true` in the data, and the answer correctly reports
   the trend as real without overstating beyond what medium confidence
   supports.
6. PASS — full final answer in Ukrainian.

**Discrepancy from the design budget**: SKILL.md documents the basic pattern as
`python cli.py analyze ...` / `python cli.py report ...`, but this environment
only has `python3` on PATH. Haiku correctly recovered on its own (read the
error, swapped the binary name, retried) with no wasted back-and-forth beyond
the one failed call, so the failure was self-healing and cheap (49 bytes,
no retry loop) — but it does mean the real call count for this scenario was 3,
not the designed 2, and a differently-behaved model might not have recovered as
cleanly. **Fix candidate for a future SKILL.md revision** (out of scope for
this no-new-code milestone): show `python3 cli.py ...` in the examples, or note
that either `python` or `python3` may be needed depending on environment.

The model also made 2 additional non-`cli.py` tool calls beyond the "2 calls"
budget: `Read` on the generated `report.md` and `Read` on `chart.png`, to pull
richer detail into its final answer instead of relying solely on `report`'s
JSON `headline` field. This is consistent with the "2 `cli.py` calls" budget
as designed — that framing was always about CLI invocations, not total tool
calls — but is worth flagging as the actual number of tool-call round-trips
this scenario costs in practice (6 total: 1 Read SKILL.md + 3 Bash + 2 Read).

Both `cli.py` JSON responses stayed well under the 2KB cap: `analyze` = 761
bytes, `report` = 373 bytes.

### Fix and retest

SKILL.md was updated (see [SKILL.md](../../SKILL.md)) to: use `python3` in
every command example with a "try `python`" fallback note, add "you don't
need to open the generated report files" guidance, and switch the setup line
to `pip3` (this environment has neither `python` nor `pip` on PATH, only
`python3`/`pip3` — confirmed by running `which python python3 pip pip3`
directly and by a `pip3 install -r requirements.txt` dry run, which resolved
cleanly).

Retest transcript: [astronomy-course-uk-trust-retest.md](../transcripts/claude-code-haiku/astronomy-course-uk-trust-retest.md)

- **`python3` fix: confirmed working.** The retest made exactly 2 `cli.py`
  calls, both successful on the first try — zero failed `python` attempts,
  vs. 1 in the pilot.
- **"Don't open report files" guidance: added, but not confirmed effective.**
  The retest still read `report.md` and `chart.png` after the successful
  `report` call, reasoning it wanted "a complete picture." This doesn't add
  extra `cli.py` calls or break the 2KB response budget, but the total
  tool-call count (6) didn't drop as intended. One retest isn't enough to
  call the guidance ineffective, but it isn't confirmed fixed either —
  noting this plainly rather than overstating the fix.

---

## Remaining scenarios (ids 1, 3–8)

All run blind via `blind-runner` (`model: haiku`, now registers correctly),
graded by `eval-grader` (`model: sonnet`) against each eval's `expect` list.
Full transcripts under `evals/transcripts/claude-code-haiku/`.

**Methodology note**: unlike the pilot/retest transcripts (fully verbatim),
these 7 transcripts are condensed summaries of each real tool call and its
result rather than a byte-for-byte log -- accurate to the underlying data
(sizes, commands, flags, response contents are all real and copied from the
actual run), but the grader for id 4 flagged this explicitly: grading relies
on the transcript author's characterization of things like "final answer
doesn't fabricate a number" rather than the grader independently re-deriving
that from raw text. Treat PASS/FAIL calls here as reliable for anything
tied to a concrete quoted number, flag, or command, and slightly softer for
characterizations of tone/completeness.

### 1. intermittent-fasting-pl-cs — 5/8 PASS

- FAIL — 5 `cli.py`-directed calls (1 analyze, 3 resolve -- 1 failed on a
  missing `--langs` flag, 1 report), not the "exactly 2" basic-question
  budget. The extra calls were the model double-checking a genuinely missing
  `pl` article rather than accepting `missing` at face value -- reasonable
  diligence, but real budget overrun.
- FAIL — final answer doesn't offer the specific `--article pl:"..."` retry
  next step (it had already run that style of check as a tool call, but
  never surfaced it as a suggested next step in the answer itself).
- FAIL — final answer states cs's direction, confidence, and reason, but
  never states whether the trend is statistically significant.
- 5 items PASS: correct topic/langs resolution, no `--start`/`--end` passed,
  explicitly flags `pl` as a genuine content gap rather than staying silent,
  mentions report files, and is written entirely in Ukrainian.

### 3. english-learning-cross-language-recommendation — 3/5 PASS

- PASS — correctly recognized `--langs` was never specified and asked a
  clarifying question *before* running anything (0 `cli.py` calls), exactly
  the behavior this deliberately-ambiguous prompt is designed to test for.
- FAIL — the clarifying question itself was written in **English**, despite
  the prompt being in Ukrainian.
- FAIL — since it stopped to ask rather than proceeding, it never produced
  the data-driven "which audience to investigate next" recommendation the
  task ultimately wants -- correct judgment call on *whether* to proceed,
  but means this scenario never reaches its main point.

### 4. missing-article-content-gap — 4/4 PASS

Clean 2-call run (analyze, report), both `de`/`ja` correctly came back
`missing`, no fabricated growth number, "Very High confidence" framing for
the content-gap conclusion itself. No discrepancies.

### 5. low-volume-emerging-topic — 4/4 PASS

Correctly handled the `no_data` flag for the exact requested topic
("Ferrofluid art") with a plain-language explanation and no fabrication.
**Notable behavior beyond the expect list**: the model then substituted a
different, broader topic ("Ferrofluid," dropping "art") on its own
initiative and reported real data for *that* instead -- disclosed
transparently in the final answer (clearly labeled as the broader topic,
not hidden), but a real, unprompted scope change worth knowing about. Used
4 `cli.py` calls (analyze x2, resolve, report), not 2.

### 6. short-period-under-24-months — 3/4 PASS

- FAIL — the final answer explains low confidence as "insufficient years of
  data for the significance test," but never surfaces the *distinct*
  reference-baseline-borrowing concept (`reference_months_before: 31` in the
  raw response) -- i.e., that the YoY figure's baseline came from ~31 months
  of history outside the requested 6-month window. These are two different
  claims and only the first was made.
- One `cli.py` call failed first: the model guessed `--start 2026-04-01`
  (full `YYYY-MM-DD`) before the CLI's `hint` corrected it to the required
  `YYYY-MM` format -- self-corrected cleanly on retry, but a real wasted
  call, the same class of issue as the earlier `python`/`python3` fix (an
  input-format assumption not stated clearly enough for the model to get
  right on the first try). **Not fixed in this milestone** (out of the
  explicitly-scoped `python3` fix) -- flagging as a candidate for a future
  SKILL.md revision, e.g. showing the `YYYY-MM` format explicitly in the
  Period bullet's prose, not just implied by the example.
- 3 items PASS: explicit `--start`/`--end` pair given together, report called
  after analyze, and the 6-month window wasn't dodged/fabricated to avoid the
  reference-baseline case.

### 7. many-languages-output-cap — 4/5 PASS

- FAIL/untested — no nonzero `omitted` count actually occurred in this run
  (17 languages fit in 1960 bytes), so "handles a nonzero omitted count
  gracefully" was never actually exercised. Not a behavioral failure, just
  an expect item this run didn't get a chance to test.
- 4 items PASS, including a good-faith handling of a real mismatch: **none**
  of the 17 languages were actually growing, so instead of fabricating a
  "top 3 fastest-growing" the model reframed to "top 3 most resilient /
  least-declining" and said so explicitly ("this is not a selective finding
  -- the entire landscape is declining").
- **Near-miss on the 2KB budget**: the `analyze` response for 17 languages
  was **1960 bytes -- 95.7% of the 2048-byte cap**. This is the tightest
  margin observed across all 8 evals; a modestly larger `--langs` list, or
  languages with longer resolved article titles, could plausibly cross 2KB
  in practice.

### 8. two-turn-follow-up-add-language-and-rerank — 1/4 PASS

**Headline finding of this eval, and arguably of the whole milestone**: the
prompt "Compare interest in astronomy in **uk** and pl" is genuinely
ambiguous in English -- "uk" is both Ukrainian's ISO language code (what
`evals.json` intends, consistent with the rest of the eval set) and the
everyday abbreviation for the United Kingdom. The model read it as the
country and called `analyze --langs en,pl`, not `--langs uk,pl`. SKILL.md
does list `uk` among its own `--langs` examples, so the correct reading was
available, but nothing forces that reading over the far more common
everyday-English one.

- FAIL (turn 1) — `--langs en,pl` used instead of `--langs uk,pl`, for the
  reason above. The 2-call mechanics (analyze then report) were otherwise
  correct.
- FAIL (turn 2) — correctly added `de` and used `--rank-by
  avg_monthly_views` (not hand-rolled logic) rather than fetching German in
  isolation or ignoring the re-rank -- but because turn 1's language error
  carried forward unchanged, the resulting set is still `en,pl,de`, never
  `uk,pl,de`.
- PASS — turn 2 ranks via the CLI's own `--rank-by`, no custom sort/filter
  script.
- Inconclusive — whether turn 2 re-fetched `en`/`pl` from Wikimedia instead
  of reusing turn 1's cache isn't observable from the tool-call transcript
  alone (would need cache-file timestamps or network logs); response sizes
  and call counts are consistent with the cache-backed design SKILL.md
  documents, but this isn't independently confirmed here.

Across both turns: 4 `cli.py` calls total (2 per turn, matching the
per-turn budget), max single response 1557 bytes (under 2KB).

## Summary table

| Eval | PASS / total | `cli.py` calls | Max response bytes | Failures (one line each) |
|---|---|---|---|---|
| 1. intermittent-fasting-pl-cs | 5/8 | 5 (1 failed) | 731 | 5 calls not 2; no `--article pl:"..."` retry offer; no significance statement for cs |
| 2. astronomy-course-uk-trust (pilot) | 5/6 | 3 (1 failed) | 761 | not exactly 2 calls -- `python` failed, retried `python3` (fixed + retested, see above) |
| 3. english-learning-cross-language-recommendation | 3/5 | 0 | n/a | answered in English, not Ukrainian; never reached the data-driven recommendation (stopped to ask) |
| 4. missing-article-content-gap | 4/4 | 2 | 452 | none |
| 5. low-volume-emerging-topic | 4/4 | 4 | 781 | none against `expect`; unprompted topic substitution (disclosed) not covered by `expect` |
| 6. short-period-under-24-months | 3/4 | 3 (1 failed) | 1502 | no reference-baseline-borrowing disclosure; 1 call failed on date format (`YYYY-MM-DD` vs `YYYY-MM`) |
| 7. many-languages-output-cap | 4/5 | 2 | 1960 | omitted-count handling untested (no capping occurred this run); response size 95.7% of 2KB cap |
| 8. two-turn-follow-up-add-language-and-rerank | 1/4 | 4 | 1557 | "uk" read as United Kingdom, not Ukrainian, in both turns; caching reuse unverified |

**Totals**: 8/8 scenarios run for real against a genuine blind `model: haiku`.
29/38 individual `expect` items PASS across all scenarios (not counting the
untested/inconclusive items above as either PASS or FAIL).

## Discrepancies between the design budget and actual model behavior

1. **`python` vs `python3` (fixed and retested, confirmed working)** --
   SKILL.md's examples used bare `python`, which isn't on this machine's
   PATH. Fixed to `python3` with a fallback note; retest on eval 2 confirmed
   0 failed attempts afterward, vs. 1 in the pilot.
2. **`--start`/`--end` date format (not fixed this milestone)** -- the CLI
   requires `YYYY-MM`, but a model reasonably guesses `YYYY-MM-DD` first
   (eval 6). Self-corrects cleanly from the `hint`, but costs a call every
   time. Candidate for a future SKILL.md wording tweak.
3. **"uk" language-code ambiguity (not fixed this milestone)** -- the single
   biggest real discrepancy found. `evals.json`'s own eval 8 uses "uk" to
   mean Ukrainian, but a model reading the prompt in isolation has no strong
   signal to prefer that over "United Kingdom." This is a property of
   natural-language input, not something a CLI fix can resolve by itself;
   worth flagging to the assignment/prompt design rather than the skill.
4. **"Don't open report files" guidance added but not confirmed effective**
   -- added to SKILL.md alongside the `python3` fix; the one retest run
   still read `report.md`/`chart.png` afterward anyway. Doesn't cost extra
   `cli.py` calls or blow the 2KB budget, just more total tool calls than
   hoped.
5. **2KB response budget: real, but with a visible ceiling** -- held in
   every one of the 8 scenarios (max observed: 1960/2048 bytes on the
   17-language run), but that near-miss shows the margin isn't large.
6. **The "basic answer is 2 `cli.py` calls" framing holds only for the
   clean-data case** -- of the 8 scenarios, only 3 (missing-article,
   many-languages, and the astronomy retest) actually landed on exactly 2
   calls end-to-end. The other 5 needed 3-5 calls, driven by: a failed
   command (wrong binary or date format, self-corrected), the model
   double-checking a genuinely missing article via `resolve`, or an
   unprompted topic substitution. None of these are bugs in the CLI itself
   -- they're realistic behavior when the input data doesn't match the
   textbook case SKILL.md's "2 calls" example describes.
7. **Language-matching the user's prompt is inconsistent** -- eval 1 (a
   full analysis) correctly answered in Ukrainian; eval 3 (a clarifying
   question, no analysis) answered in English despite a Ukrainian prompt.
   SKILL.md says "answer in the same language the user asked in" but this
   wasn't tested for the "ask a clarifying question first" branch
   specifically, and it's the one place it broke.
