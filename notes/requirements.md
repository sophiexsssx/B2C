# Requirements

## My decisions

- A topic's interest = main article + its redirects, each fetched separately (the per-article endpoint takes one title per call), then summed by matching timestamp across all of them into one topic series
- Default `agent=user` (traffic not classified as spider or automated — a heuristic bucket, not verified-human)
- 404 handling: check the date range first (Pageviews API data starts July 2015). An in-range 404 is ambiguous — the API's own error message says it means either zero views for that period or data "not loaded yet" — so don't treat it as proof the article has no data; treat it as retryable for recent/current periods (data may land later) and as likely-zero for older, fully-in-range periods. The API also omits zero-view periods from a range's results rather than returning explicit `views: 0` entries, so callers must materialize those missing periods as zero themselves when building a continuous series.
