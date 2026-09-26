# AI usage & verification log

How I used AI tools while building this project and how I checked their output.

## 2026-09-22

- **Requirements analysis (Claude Code):** misclassified 2 hard requirements
  as optional and listed a non-requirement as a requirement. I compared its
  checklist with the original task and corrected all three.
- **API research (api-researcher subagent):** verified Question 1 manually:
  API returned 305 views for Астрономія (uk) in July 2025;
  pageviews.wmcloud.org shows 305. Result: match.
- **API research (api-researcher subagent):** claimed specific rate-limit
  numbers (10 vs 200 req/min) without a source, and overstated Wikidata
  sitelinks vs langlinks. I asked for evidence and had the claims corrected.

## 2026-09-26

- **Blind eval harness (Claude Code, blind-runner + eval-grader subagents):**
  ran the evals.json scenarios blind against a real Haiku model instead of
  trusting a self-reported pass. This found real gaps a design review alone
  wouldn't have: "uk" read as "United Kingdom" instead of Ukrainian in a
  live run, a `python`-vs-`python3` PATH mismatch, and a `notes` field that
  was generated correctly but never actually relayed to the user in the
  final answer. Fixed in cli.py/SKILL.md and reran to confirm.
- **Response-budget bug (Claude Code):** a code-review pass on my own
  earlier fix flagged that `_cap_response` trimmed `ranked`/`unranked`/
  `missing` but never checked whether `notes` itself could keep the
  response over budget. I reproduced it before accepting the finding (25
  missing languages -> 3888 bytes against an 1800-byte limit), then fixed
  and reverified at 25 and 100 missing languages.
- **Results-writeup arithmetic (Claude Code):** the same review pass caught
  an aggregate-score error in evals/results/haiku-blind.md -- summed with a
  denominator of 27 where the stated methodology (excluding one eval's
  inconclusive item) implied 26. I verified the correct total by recounting
  each eval's expect-list length directly rather than trusting the earlier
  tally.
- **Transcript accuracy (Claude Code):** two condensed eval transcripts
  under-reported what the model actually said (left out report-file paths
  and a growth-direction statement that were present in the raw run),
  which made an independent grading pass fail items that had actually
  passed. Caught by re-reading the raw transcripts myself instead of
  trusting the summaries, and corrected both files.