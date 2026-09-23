# AI usage & verification log

How I used AI tools while building this project and how I checked their output.

## 2026-09-22

- **Requirements analysis (Claude Code):** misclassified 2 hard requirements
  as optional and listed a non-requirement as a requirement. I compared its
  checklist with the original task and corrected all three.
- **API research (api-researcher subagent):** claimed specific rate-limit
  numbers (10 vs 200 req/min) without a source, and overstated Wikidata
  sitelinks vs langlinks. I asked for evidence and had the claims corrected.
  - **Cross-checking agents:** the api-researcher subagent claimed 6 language
  editions differ between langlinks and Wikidata sitelinks. The main Claude
  Code session re-checked and found it was a naming artifact (be_x_old vs
  be-tarask, no vs nb): 120/120 identical. Decision: langlinks as primary,
  Wikidata as fallback. Lesson: one AI checking another catches real errors.
- **Rate-limit claim:** I opened the cited mediawiki.org source myself:
  <confirmed>.
- **Manual pageviews check:** API vs pageviews.wmcloud.org for uk
  "Астрономія", 2025, all-agents: <match>.