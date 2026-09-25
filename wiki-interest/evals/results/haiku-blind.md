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

*Remaining scenarios (ids 1, 3–8) not yet run — this file covers the pilot
(plus its retest) only, per instruction to start with astronomy-course-uk-trust
before continuing.*
