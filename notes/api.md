# Wikimedia Pageviews API and MediaWiki API Research

## Question 1: Monthly pageviews for one article

**Article:** "Астрономія" (Cyrillic) on uk.wikipedia for year 2025

**Endpoint URL Template:**
```
https://wikimedia.org/api/rest_v1/metrics/pageviews/per-article/{project}/{access}/{agent}/{article}/{granularity}/{start}/{end}
```

**Params:**
- `project`: uk.wikipedia
- `access`: all-access
- `agent`: all-agents
- `article`: %D0%90%D1%81%D1%82%D1%80%D0%BE%D0%BD%D0%BE%D0%BC%D1%96%D1%8F (URL-encoded UTF-8)
- `granularity`: monthly
- `start`: 2025010100
- `end`: 2025123100

**Curl Command:**
```bash
curl -s "https://wikimedia.org/api/rest_v1/metrics/pageviews/per-article/uk.wikipedia/all-access/all-agents/%D0%90%D1%81%D1%82%D1%80%D0%BE%D0%BD%D0%BE%D0%BC%D1%96%D1%8F/monthly/2025010100/2025123100" \
  -H "User-Agent: WikiInterestBot/1.0 (https://example.org/wiki-interest; sofia@example.org)"
```

**Trimmed Response:**
```json
{
  "items": [
    {"project":"uk.wikipedia","article":"Астрономія","granularity":"monthly","timestamp":"2025010100","views":1906},
    {"project":"uk.wikipedia","article":"Астрономія","granularity":"monthly","timestamp":"2025020100","views":1807},
    {"project":"uk.wikipedia","article":"Астрономія","granularity":"monthly","timestamp":"2025090100","views":2286}
  ]
}
```

**Takeaway:** Cyrillic article titles must be UTF-8 URL-encoded; monthly timestamps are YYYYMMDDHH format, using the first day at hour 00 (e.g. YYYYMM0100); always get data as an items array.

---

## Question 2: Total monthly pageviews for entire language edition

**Language Edition:** uk.wikipedia for year 2025

**Endpoint URL Template:**
```
https://wikimedia.org/api/rest_v1/metrics/pageviews/aggregate/{project}/{access}/{agent}/{granularity}/{start}/{end}
```

**Params:**
- `project`: uk.wikipedia
- `access`: all-access
- `agent`: all-agents
- `granularity`: monthly
- `start`: 2025010100
- `end`: 2025123100

**Curl Command:**
```bash
curl -s "https://wikimedia.org/api/rest_v1/metrics/pageviews/aggregate/uk.wikipedia/all-access/all-agents/monthly/2025010100/2025123100" \
  -H "User-Agent: WikiInterestBot/1.0 (https://example.org/wiki-interest; sofia@example.org)"
```

**Trimmed Response:**
```json
{
  "items": [
    {"project":"uk.wikipedia","access":"all-access","agent":"all-agents","granularity":"monthly","timestamp":"2025010100","views":150156688},
    {"project":"uk.wikipedia","access":"all-access","agent":"all-agents","granularity":"monthly","timestamp":"2025020100","views":134249124}
  ]
}
```

**Takeaway:** Site-level pageviews use the `aggregate` endpoint (no article name); sum of all article views for the entire wiki per month.

---

## Question 3: Finding the same article across language editions

**Test Case:** "Python (programming language)" on en.wikipedia → find titles on other Wikipedias

**Finding:** No real divergence found for this test article, once naming conventions are normalized.

For "Python (programming language)":
- **MediaWiki langlinks**: Returns 120 Wikipedia editions
- **Wikidata sitelinks (Q28865)**: Returns 164 total, but that count includes non-Wikipedia projects (`commons`, `meta`, `species`, `mediawiki`, `wikifunctions`, `abstract`, sister projects like wikivoyage/wikiquote/etc.) and the source article's own `enwiki` entry. Filtering to Wikipedia-only editions gives 124 raw keys.
- The apparent "extra" or "missing" entries (`be_x_old` vs `be-tarask`, `zh_classical` vs `lzh`, `zh_yue` vs `yue`, `zh_min_nan` vs `nan`, `no` vs `nb`, `als` vs `gsw`) are **not real divergences** — they're the same Wikipedia editions, just keyed differently: Wikidata uses legacy internal site-key strings (underscored), MediaWiki langlinks uses the standard ISO-ish language code. After mapping known aliases, the two sets are identical: 120 Wikipedia editions on both sides, zero unmatched.
- A first-pass raw string diff (which an earlier version of this doc did) makes it *look* like the two sources disagree by ~6 editions each way — that's a naming-convention artifact, not a data source disagreement. Don't compare raw keys without normalizing first.

**Recommendation:**
Use **MediaWiki langlinks as the primary approach** — single API call, no normalization needed, and (for this test case) no loss of coverage vs. Wikidata. Keep **Wikidata sitelinks as a fallback**, useful when:
- You need coverage of non-Wikipedia Wikimedia projects (Commons, etc.)
- You want a second source to sanity-check langlinks for a specific article (langlinks can occasionally be stale/incomplete on the source wiki, independent of naming)
- You already have the Wikidata QID for other reasons and want to avoid a second round-trip

No case was found where Wikidata sitelinks had *substantively* better/different Wikipedia-edition coverage than langlinks for the one article tested — this should be re-checked on a handful of other articles before treating it as general, since one example doesn't prove there's never a real divergence.

**Endpoint URLs:**
1. MediaWiki langlinks: `https://en.wikipedia.org/w/api.php?action=query&titles=ARTICLE&prop=langlinks&format=json`
2. Wikidata sitelinks: `https://www.wikidata.org/w/api.php?action=wbgetentities&ids=Q#&props=sitelinks&format=json`

**Curl Commands:**
```bash
# MediaWiki langlinks approach (single call)
curl -s "https://en.wikipedia.org/w/api.php?action=query&titles=Python_(programming_language)&prop=langlinks&lllimit=500&format=json" \
  -H "User-Agent: WikiInterestBot/1.0 (https://example.org/wiki-interest; sofia@example.org)"

# Wikidata sitelinks approach (two calls)
# Step 1: Get Wikidata ID from Wikipedia
curl -s "https://en.wikipedia.org/w/api.php?action=query&titles=Python_(programming_language)&prop=pageprops&ppprop=wikibase_item&format=json" \
  -H "User-Agent: WikiInterestBot/1.0 (https://example.org/wiki-interest; sofia@example.org)"

# Step 2: Query Wikidata for sitelinks
curl -s "https://www.wikidata.org/w/api.php?action=wbgetentities&ids=Q28865&props=sitelinks&format=json" \
  -H "User-Agent: WikiInterestBot/1.0 (https://example.org/wiki-interest; sofia@example.org)"
```

**Trimmed Response (langlinks):**
```json
{
  "query": {
    "pages": {
      "23033":{
        "title":"Python (programming language)",
        "langlinks":[
          {"lang":"cs","*":"Python (programovací jazyk)"},
          {"lang":"de","*":"Python (Programmiersprache)"},
          {"lang":"fr","*":"Python (langage)"},
          {"lang":"pl","*":"Python (język programowania)"}
        ]
      }
    }
  }
}
```

**Takeaway:** MediaWiki langlinks are the simpler choice for finding articles across Wikipedias; Wikidata sitelinks offer broader coverage but require two API calls and include non-Wikipedia projects—both can diverge, so validate critical results against both sources.


---

## Question 4: How redirects affect pageview counts

**Test Case:** "Astronomical" (redirect to "Astronomy") on en.wikipedia vs the target "Astronomy"

**Endpoints:**
1. Find redirects: `https://en.wikipedia.org/w/api.php?action=query&titles=Astronomy&prop=redirects`
2. Get pageviews: `https://wikimedia.org/api/rest_v1/metrics/pageviews/per-article/en.wikipedia/all-access/all-agents/{article}/monthly/2025010100/2025013100`
3. List backlinks (reverse): `https://en.wikipedia.org/w/api.php?action=query&bltitle=Astronomy&list=backlinks&blfilterredir=redirects`

**Params:**
- `titles`: Astronomy
- `prop`: redirects
- `rdlimit`: max
- `list`: backlinks
- `blfilterredir`: redirects

**Curl Commands:**
```bash
# Find redirects TO an article — use rdlimit=max, not the 10-result default
# (rdlimit=max is 500 for anonymous requests; verified this returns all 7
# redirects for "Astronomy" and all 301 for "United States" in one call)
curl -s "https://en.wikipedia.org/w/api.php?action=query&titles=Astronomy&prop=redirects&rdlimit=max&format=json" \
  -H "User-Agent: WikiInterestBot/1.0 (https://example.org/wiki-interest; sofia@example.org)"

# If the response includes a "continue": {"rdcontinue": "..."} block, the
# article has more redirects than the rdlimit cap — repeat the call with
# &rdcontinue=<value from the response> and keep appending pages' redirects
# until no "continue" block remains. Verified this happens for "United
# States" at rdlimit=20 (301 total redirects, would need ~15 pages at that
# limit — rdlimit=max avoids it for all but the most-redirected articles).
curl -s "https://en.wikipedia.org/w/api.php?action=query&titles=Astronomy&prop=redirects&rdlimit=max&rdcontinue=RDCONTINUE_VALUE&format=json" \
  -H "User-Agent: WikiInterestBot/1.0 (https://example.org/wiki-interest; sofia@example.org)"

# Get pageviews for redirect
curl -s "https://wikimedia.org/api/rest_v1/metrics/pageviews/per-article/en.wikipedia/all-access/all-agents/Astronomical/monthly/2025010100/2025013100" \
  -H "User-Agent: WikiInterestBot/1.0 (https://example.org/wiki-interest; sofia@example.org)"

# Get pageviews for target
curl -s "https://wikimedia.org/api/rest_v1/metrics/pageviews/per-article/en.wikipedia/all-access/all-agents/Astronomy/monthly/2025010100/2025013100" \
  -H "User-Agent: WikiInterestBot/1.0 (https://example.org/wiki-interest; sofia@example.org)"
```

**Trimmed Response (pageviews):**
```json
Redirect "Astronomical": {"views":1142}
Target "Astronomy": {"views":45699}
```

**Takeaway:** Redirect pageviews are counted SEPARATELY under the redirect's own title, not attributed to the target (Astronomical gets 1142, Astronomy gets 45699); use `prop=redirects` to list redirects TO an article, or `backlinks&blfilterredir=redirects` to find all redirects pointing to a page. Always pass `rdlimit=max` and follow the `rdcontinue` token until no `continue` block remains — the default/low limits silently truncate the list for heavily-redirected articles (verified: "United States" has 301 redirects, well past a 20-result cap).

---

## Question 5: agent=user vs agent=all-agents parameter

**Test Article:** "Astronomy" on en.wikipedia for January 2025

**Endpoint URL Template:**
```
https://wikimedia.org/api/rest_v1/metrics/pageviews/per-article/{project}/{access}/{agent}/{article}/{granularity}/{start}/{end}
```

**Params:**
- `agent=user`: Excludes identified bots/spiders
- `agent=all-agents`: Includes all traffic (human + bots)

**Curl Commands:**
```bash
curl -s "https://wikimedia.org/api/rest_v1/metrics/pageviews/per-article/en.wikipedia/all-access/user/Astronomy/monthly/2025010100/2025013100" \
  -H "User-Agent: WikiInterestBot/1.0 (https://example.org/wiki-interest; sofia@example.org)"

curl -s "https://wikimedia.org/api/rest_v1/metrics/pageviews/per-article/en.wikipedia/all-access/all-agents/Astronomy/monthly/2025010100/2025013100" \
  -H "User-Agent: WikiInterestBot/1.0 (https://example.org/wiki-interest; sofia@example.org)"
```

**Trimmed Response & Comparison:**
```
agent=user:        35,324 views (77.3% of total)
agent=all-agents:  45,699 views
bot/spider:        10,375 views (22.7% of total)
```

**Note:** This measurement is specific to "Astronomy" in January 2025; bot/spider ratios vary significantly by article and should not be treated as universal.

**Takeaway:** `agent=all-agents` includes bot/crawler traffic (measured ~22.7% for "Astronomy" January 2025, but varies by article); use `agent=user` for traffic not classified as spider or automated — this is a heuristic classification (`agent_type: spider|user|automated` per Wikimedia's pageview definition), not a verified-human guarantee.

---

## Question 6: Rate limits and User-Agent header requirements

**Wikimedia API Rate Limits** ([official documentation](https://www.mediawiki.org/wiki/Wikimedia_APIs/Rate_limits)):

| Client Type | Limit | Details |
|-------------|-------|---------|
| Unidentified (no User-Agent) | 10 req/min | By IP only |
| Unauthenticated with User-Agent | 200 req/min | Recommended minimum |
| Authenticated/editors | 200-2000 req/min | Depends on account status |
| Bots with flags | Exempt from these per-minute limits | Must be approved; still subject to operational rate limits and the Robot policy |

Source: [Wikimedia APIs/Rate limits - MediaWiki](https://www.mediawiki.org/wiki/Wikimedia_APIs/Rate_limits)

**Curl Commands & Results:**

```bash
# WITHOUT a custom User-Agent header (curl still sends its own default,
# e.g. "curl/8.x.x" — that counts as non-descriptive, not "no header")
curl -s -w "\nHTTP %{http_code}\n" \
  "https://wikimedia.org/api/rest_v1/metrics/pageviews/per-article/en.wikipedia/all-access/all-agents/Astronomy/monthly/2025010100/2025013100"
# Result this run: HTTP 200 — but a 200 here is NOT proof this is policy-compliant.
# Per https://foundation.wikimedia.org/wiki/Policy:User-Agent_policy, Wikimedia
# has required a descriptive User-Agent on every request since Feb 2010, and
# non-descriptive/default strings (curl's default included) "may be blocked
# without notice" even if a given request happens to succeed. Treat this as
# always required, not as optional-but-rate-limited.

# WITH a meaningful, contact-bearing User-Agent header (required)
curl -s -w "\nHTTP %{http_code}\n" \
  "https://wikimedia.org/api/rest_v1/metrics/pageviews/per-article/en.wikipedia/all-access/all-agents/Astronomy/monthly/2025010100/2025013100" \
  -H "User-Agent: WikiInterestBot/1.0 (https://example.org/wiki-interest; sofia@example.org)"
# Result: HTTP 200 (also gets the 200 req/min rate-limit tier per the table above)
```

**User-Agent Format Requirements:**
- Required (not optional) on every request, per official policy — see source link below
- Must be "meaningful"/descriptive with contact information, not a bare tool default
- Recommended: `BotName/version (URL; contact@email.com) library/version`
- Example: `WikiInterestBot/1.0 (https://example.org/wiki-interest; sofia@example.org)`
- Source: [Wikimedia Foundation User-Agent policy](https://foundation.wikimedia.org/wiki/Policy:User-Agent_policy)

**Exceeding Limits:** Returns HTTP 429 (Too Many Requests) with message: `"You are making too many requests to the API"`

**Takeaway:** A meaningful User-Agent with contact information is required by policy on every request — a request without one may still return HTTP 200 in a given test, but that is not evidence of compliance; non-descriptive/absent User-Agent strings "may be blocked without notice" regardless of status code. Separately, the rate-limit table above still applies: identified requests get 200 req/min vs. 10 req/min for unidentified ones, and exceeding the limit returns HTTP 429. Always send the header — never treat a passing test as license to omit it.

---

## Question 7: Missing/nonexistent articles and months with no data

**Test Cases:**
1. Non-existent article: "XyzAbcDef12345"
2. Date before Pageviews API data (before July 2015)
3. Date within API data range: July 2015

**Curl Commands:**

```bash
# Non-existent article
curl -s -w "\nHTTP %{http_code}\n" \
  "https://wikimedia.org/api/rest_v1/metrics/pageviews/per-article/en.wikipedia/all-access/all-agents/XyzAbcDef12345/monthly/2025010100/2025013100"

# Before API data (January 2015)
curl -s -w "\nHTTP %{http_code}\n" \
  "https://wikimedia.org/api/rest_v1/metrics/pageviews/per-article/en.wikipedia/all-access/all-agents/Astronomy/monthly/2015010100/2015013100"

# Early API data (July 2015)
curl -s -w "\nHTTP %{http_code}\n" \
  "https://wikimedia.org/api/rest_v1/metrics/pageviews/per-article/en.wikipedia/all-access/all-agents/Astronomy/monthly/2015070100/2015073100"
```

**Response Bodies:**

```json
// Non-existent article (HTTP 404)
{
  "detail":"The date(s) you used are valid, but we either do not have data for those date(s), or the project you asked for is not loaded yet.",
  "status":404,
  "title":"Not Found"
}

// Before API data (HTTP 404)
{
  "detail":"The date(s) you used are valid, but we either do not have data for those date(s), or the project you asked for is not loaded yet.",
  "status":404,
  "title":"Not Found"
}

// Valid early data (HTTP 200)
{
  "items":[{"project":"en.wikipedia","article":"Astronomy","timestamp":"2015070100","views":58191}]
}
```

**Takeaway:** Both missing articles and dates before July 2015 return HTTP 404 with identical error messages; check for empty items array or 404 status; Pageviews API data availability starts July 2015.

---

## Key Gotchas for wiki-interest Skill Implementation

1. **Rate Limiting:** Implement User-Agent header to get 200 req/min instead of 10 req/min; handle HTTP 429 with exponential backoff
2. **Redirect Handling:** Pageviews are NOT aggregated with target articles—redirects show separate counts; retrieve redirects via `prop=redirects`, fetch pageviews for each one separately (main article + every redirect), then aggregate by matching time period (e.g. sum same-month values) across all of them into one series — don't just report them independently
3. **UTF-8 Encoding:** Cyrillic and other non-ASCII article titles must be URL-encoded; Python's `urllib.parse.quote()` handles this correctly
4. **Missing Data:** Don't assume 200 status means data exists; check items array is non-empty; dates before July 2015 return 404
5. **Bot vs User Traffic:** Use `agent=user` for human readers only; `agent=all-agents` includes bot traffic which varies by article (measured ~23% for "Astronomy" but not universal); choose based on use case and validate for your articles
6. **Cross-Wiki Discovery:** Use MediaWiki langlinks as primary approach (simpler, single call); use Wikidata sitelinks as fallback. For the one article tested, the two sources matched exactly once site-key naming was normalized (e.g. `be_x_old`↔`be-tarask`, `no`↔`nb`) — an earlier draft of this doc misread that naming difference as a real data divergence. Re-verify on more articles before assuming they always agree.
7. **Site-Level vs Per-Article:** Remember `/aggregate/` endpoint for wiki-wide totals vs `/per-article/` for individual articles; they're different endpoints
