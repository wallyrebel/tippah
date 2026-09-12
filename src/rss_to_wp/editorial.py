"""Source-grounded editing and geographic routing for Tippah News."""

from __future__ import annotations

import hashlib
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from bs4 import BeautifulSoup

TARGET_SLUGS = {
    "Tippah County News": "tippah-county-news",
    "Ripley News": "ripley-ms-news",
    "Mississippi News": "mississippi-news",
}
GEOGRAPHIC_CATEGORIES = set(TARGET_SLUGS) | {"Local News", "National News"}


def plain_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for element in soup(["script", "style", "nav", "footer", "header"]):
        element.decompose()
    return " ".join(soup.get_text(" ").split())


def canonical_source_url(url: str) -> str:
    parts = urlsplit(url.strip())
    if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username:
        return ""
    # Preserve identifying query parameters (especially Facebook story IDs).
    query = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not k.lower().startswith("utm_") and k.lower() not in {"fbclid", "gclid"}
    ]
    return urlunsplit(
        (parts.scheme.lower(), parts.netloc.lower(), parts.path, urlencode(sorted(query)), "")
    )


def source_fingerprint(content: str) -> str:
    text = plain_text(content).casefold()
    return hashlib.sha256(text.encode()).hexdigest() if len(text.split()) >= 35 else ""


def route_categories(
    title: str, content: str, default_category: str | None, coverage_area: str = "unknown"
) -> tuple[list[str], list[str]]:
    """Use source text and verified feed coverage, never generated keywords or tags.

    Ambiguous city names require Mississippi/Tippah context. County-wide feed
    ownership alone does not make every story a Ripley story.
    """
    text = plain_text(f"{title} {content}").casefold()
    ms_context = coverage_area in {"ripley", "tippah", "mississippi"} or bool(
        re.search(
            r"\b(?:tippah|mississippi)\b|\b(?:ripley|walnut|falkner|dumas|blue mountain),?\s+ms\b",
            text,
        )
    )
    ripley = coverage_area == "ripley" or bool(
        ms_context
        and re.search(
            r"\bripley\b(?!\s*(?:,\s*|in\s+)(?:tennessee|west virginia|ohio|new york|tn|wv)\b)",
            text,
        )
    )
    tippah = ripley or coverage_area == "tippah" or bool(re.search(r"\btippah\b", text))
    if ms_context and re.search(
        r"\b(?:falkner|tiplersville|chalybeate|blue mountain christian(?: university)?|bmcu)\b",
        text,
    ):
        tippah = True
    if ms_context and re.search(r"\b(?:walnut|dumas|blue mountain)\b", text):
        # Do not conflate Walnut Grove or another state's town with Tippah County.
        tippah = tippah or bool(re.search(r"\b(?:walnut(?!\s+grove)|dumas|blue mountain)\b", text))
    categories = []
    reasons = []
    if ripley:
        categories = ["Ripley News", "Tippah County News"]
    elif tippah:
        categories = ["Tippah County News"]
    elif ms_context or default_category == "Mississippi News":
        categories = ["Mississippi News"]
        if not ms_context:
            reasons.append("statewide_relevance_needs_review")
    elif coverage_area == "national" or default_category == "National News":
        categories = ["National News"]
    else:
        reasons.append("geography_needs_review")
    if default_category and default_category not in GEOGRAPHIC_CATEGORIES:
        categories.append(default_category)
    # Unverified generic local feeds must not silently fill targeted categories.
    return list(dict.fromkeys(categories)), reasons


def clean_article_html(body: str) -> str:
    """Allow article formatting only; source links are appended by the publisher."""
    soup = BeautifulSoup(body, "html.parser")
    for node in list(soup.find_all(True)):
        if node.parent is None or not node.name:
            continue
        if node.name in {"script", "style", "iframe", "object", "form", "svg"}:
            node.decompose()
        elif node.name not in {"p", "h2", "ul", "ol", "li", "strong", "em", "blockquote"}:
            node.unwrap()
        else:
            node.attrs = {}
    return str(soup).strip()


def review_article(article: dict, source: str, title: str) -> list[str]:
    """Conservative checks supplement model assessment; this is not fact verification."""
    source_text = plain_text(f"{title} {source}")
    body = plain_text(article.get("body", ""))
    output = plain_text(" ".join(article.get(k, "") for k in ("headline", "excerpt", "body")))
    reasons = []
    if len(body.split()) < 55:
        reasons.append("thin_rewrite")
    if len(body.split()) > max(120, int(len(source_text.split()) * 1.5)):
        reasons.append("rewrite_expands_beyond_source")

    def numbers(text):
        return set(re.findall(r"(?<!\w)\d+(?:[.,]\d+)*(?!\w)", text))

    if numbers(output) - numbers(source_text):
        reasons.append("numbers_not_in_source")
    for straight, curly in re.findall(r'"([^"\n]*)"|“([^”\n]*)”', output):
        quote = straight or curly
        if len(quote) < 12:
            continue
        if " ".join(quote.casefold().split()) not in " ".join(source_text.casefold().split()):
            reasons.append("quote_not_in_source")
            break
    if not article.get("headline", "").strip() or not article.get("excerpt", "").strip():
        reasons.append("missing_headline_or_excerpt")
    if re.search(r"\b(?:today|tonight|tomorrow|this morning|this evening)\b", output, re.I):
        reasons.append("relative_time_needs_review")
    if article.get("ready_for_publication") is not True:
        reasons.append("editorial_review_required")
    reasons.extend(article.get("review_reasons", []))
    return list(dict.fromkeys(reasons))
