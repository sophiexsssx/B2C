# Requirements

## Hard requirements

- [ ] Deliverable is a skill (not a one-off script) — must contain `SKILL.md`
- [ ] Must include own code that does the actual data work (fetching/analyzing/charting) — instructions alone that make the agent write the code each time are not sufficient
- [ ] No compiled binaries in the deliverable
- [ ] Dependencies and environment setup are reproducible
- [ ] All own materials and code live inside the skill's own directory
- [ ] Usable efficiently and conveniently by a fast/cheap tool-calling model (Claude Haiku 4.5 is the reference example)
- [ ] The full scenario is actually tested on such a model
- [ ] The skill helps the agent evaluate results, verify conclusions, and handle repeated/related queries efficiently
- [ ] Recommendations and reports are based on data, with clear assumptions and limitations
- [ ] AI tools were used during development, and I'm ready to explain how I verified their output
- [ ] Explain how the skill would be iterated further (harder research, bigger data volumes)

## My decisions

- A topic's interest = main article + its redirects, each fetched separately (the per-article endpoint takes one title per call), then summed by matching timestamp across all of them into one topic series
- Default `agent=user` (traffic not classified as spider or automated — a heuristic bucket, not verified-human)
- 404 handling: check the date range first (Pageviews API data starts July 2015). An in-range 404 is ambiguous — the API's own error message says it means either zero views for that period or data "not loaded yet" — so don't treat it as proof the article has no data; treat it as retryable for recent/current periods (data may land later) and as likely-zero for older, fully-in-range periods. The API also omits zero-view periods from a range's results rather than returning explicit `views: 0` entries, so callers must materialize those missing periods as zero themselves when building a continuous series.
- Report format: one-page PDF (summary, key numbers table, 2 charts, trust notes, method & limits) + a PNG chart + a Markdown version of the report
- Efficient for Haiku: a basic question takes ≤3 tool calls; each tool output is ≤2 KB of JSON
- Repeated/related queries: disk cache for downloaded data + saved run folders, so reports can be rebuilt without refetching
- Statistics: YoY growth (last 12 months vs. the previous 12) as the main metric; 6-month momentum (last 6 months vs. the same 6 months a year earlier); spike removal that does NOT remove recurring yearly peaks; normalization by total wiki traffic; a significance test; confidence level reported with reasons
- Cross-language matching: MediaWiki langlinks as primary, Wikidata sitelinks as fallback
- Wording: say "excludes known bots and automated traffic," never "humans only"
- Scaling roadmap: half-page "Roadmap" section in the README — topic baskets, user-defined criteria, cross-checks with other sources; bulk dumps + local DuckDB/SQLite, parallel fetching with rate limits; an eval-driven iteration loop
- Testing: real runs on Claude Haiku 4.5 via OpenRouter, with transcripts saved under evals/
