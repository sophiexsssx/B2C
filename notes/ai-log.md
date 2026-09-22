# AI usage & verification log

How I used AI tools while building this project and how I checked their output.

## 2026-09-22

- **Requirements analysis (Claude Code):** misclassified 2 hard requirements
  as optional and listed a non-requirement as a requirement. I compared its
  checklist with the original task and corrected all three.
- **API research (api-researcher subagent):** verified Question 1 manually:
  API returned <NUMBER> views for Астрономія (uk) in <MONTH> 2025;
  pageviews.wmcloud.org shows <NUMBER>. Result: <match / mismatch>.
- **API research (api-researcher subagent):** claimed specific rate-limit
  numbers (10 vs 200 req/min) without a source, and overstated Wikidata
  sitelinks vs langlinks. I asked for evidence and had the claims corrected.