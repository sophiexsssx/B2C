"""
Higher-level article resolution: cross-language matching + redirects +
match confidence, built on top of api_client's primitives.

Unlike api_client.resolve_cross_language (which only queries Wikidata for
languages langlinks missed, to save a call), this module always fetches
both sources in full for every requested language and distinguishes, per
language, whether a title was corroborated by both or came from only one --
because its purpose is specifically to judge how much to trust the match,
not just to resolve a title as cheaply as possible. See notes/plan.md's
`resolve` CLI example and the "Wrong article match" trap in its metrics
table.
"""

from wiki_interest import api_client


def resolve_topic(session, project, title, target_langs):
    """
    Resolve `title` (on `project`, e.g. "en.wikipedia") into each of
    `target_langs`, including each edition's redirects and a QID-based
    match-confidence rating.

    Returns:
        {
            "qid": str | None,
            "editions": {lang: title},
            "redirects": {lang: [title, ...]},
            "missing": [lang, ...],
            "match_confidence": "high" | "medium" | "low",
            "reason": str,
        }
    """
    langlinks = api_client.get_langlinks(session, project, title)
    qid = api_client.get_wikidata_qid(session, project, title)
    sitelinks = api_client.get_wikidata_sitelinks(session, project, title) if qid else {}

    editions = {}
    disagreements = []
    single_source = []
    for lang in target_langs:
        from_langlinks = langlinks.get(lang)
        from_sitelinks = sitelinks.get(lang)
        if from_langlinks and from_sitelinks:
            if from_langlinks != from_sitelinks:
                disagreements.append(lang)
            editions[lang] = from_langlinks
        elif from_langlinks or from_sitelinks:
            single_source.append(lang)
            editions[lang] = from_langlinks or from_sitelinks

    missing = [lang for lang in target_langs if lang not in editions]
    match_confidence, reason = _match_confidence(qid, editions, disagreements, single_source)

    redirects = {}
    for lang, edition_title in editions.items():
        redirects[lang] = api_client.get_redirects(session, f"{lang}.wikipedia", edition_title)

    return {
        "qid": qid,
        "editions": editions,
        "redirects": redirects,
        "missing": missing,
        "match_confidence": match_confidence,
        "reason": reason,
    }


def _match_confidence(qid, editions, disagreements, single_source):
    """
    "high": a Wikidata item exists and every requested language that was
      found is corroborated by BOTH langlinks and sitelinks, agreeing on
      the title.
    "medium": a Wikidata item exists, but at least one found language came
      from only ONE of the two sources -- unverified (not necessarily
      wrong), but never actually cross-checked.
    "low": either no Wikidata item was found at all (nothing to cross-check
      langlinks against), or at least one language has a direct title
      disagreement between the two sources -- positive evidence something
      is wrong, which is worse than merely unverified.
    """
    if qid is None:
        return "low", "no Wikidata item found for this article -- nothing to cross-check langlinks against"
    if disagreements:
        return "low", f"langlinks and Wikidata sitelinks disagree on the title for: {', '.join(sorted(disagreements))}"
    if single_source:
        return "medium", f"only one source (langlinks or sitelinks) covers: {', '.join(sorted(single_source))} -- unverified, not cross-checked"
    if not editions:
        return "medium", "Wikidata item found, but neither langlinks nor sitelinks cover any requested language"
    return "high", "QID agrees across langlinks and sitelinks for every requested language"
