"""RSS feed parsing."""

from __future__ import annotations

from typing import Any, Optional
import time

import feedparser
import requests

from rss_to_wp.utils import get_logger

logger = get_logger("feeds.parser")

RETRY_STATUSES = {429, 500, 502, 503, 504}
MAX_ATTEMPTS = 3


def fetch_feed(url: str) -> bytes:
    """Identified GETs only, with bounded timeouts and at most two retries.

    Never retry access denials. A long Retry-After ends this attempt rather
    than retrying sooner than the source allows or blocking the whole run.
    """
    with requests.Session() as session:
        session.headers.update(
            {
                "User-Agent": "RSS-to-WP-Bot/1.0 (https://github.com/wallyrebel/tippah)",
                "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*",
            }
        )
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                response = session.get(url, timeout=(10, 30))
            except (requests.ConnectionError, requests.Timeout) as exc:
                if attempt == MAX_ATTEMPTS:
                    raise
                logger.warning("feed_fetch_retry", attempt=attempt, error_type=type(exc).__name__)
                time.sleep(2 ** (attempt - 1))
                continue
            if response.status_code in RETRY_STATUSES and attempt < MAX_ATTEMPTS:
                delay = 2 ** (attempt - 1)
                retry_after = response.headers.get("Retry-After")
                if retry_after:
                    # HTTP-date values are interpreted below, without guessing.
                    from email.utils import parsedate_to_datetime
                    from datetime import datetime, timezone

                    try:
                        delay = max(delay, float(retry_after))
                    except ValueError:
                        try:
                            delay = max(
                                delay,
                                (
                                    parsedate_to_datetime(retry_after) - datetime.now(timezone.utc)
                                ).total_seconds(),
                            )
                        except (TypeError, ValueError, OverflowError):
                            response.raise_for_status()
                    if delay > 10:
                        response.raise_for_status()
                logger.warning(
                    "feed_fetch_retry",
                    attempt=attempt,
                    http_status=response.status_code,
                    backoff_seconds=delay,
                )
                response.close()
                time.sleep(delay)
                continue
            response.raise_for_status()
            if response.status_code != 200:
                raise ValueError("unexpected_http_status")
            return response.content
    raise RuntimeError("feed_attempts_exhausted")


def parse_feed(url: str) -> Optional[dict[str, Any]]:
    """Parse an RSS/Atom feed from URL.

    Args:
        url: URL of the RSS feed.

    Returns:
        Parsed feed dictionary or None if parsing failed.
    """
    logger.info("parsing_feed", url=url)

    try:
        # feedparser's URL fetch hides HTTP errors inside an empty parsed feed.
        # Check the response first and parse bytes, never an error page as RSS.
        feed = feedparser.parse(fetch_feed(url))

        if not feed.get("version") or (feed.bozo and not feed.entries):
            logger.error(
                "feed_invalid_document",
                error_type=type(feed.get("bozo_exception")).__name__,
            )
            return None

        # Check for parsing errors
        if feed.bozo and feed.bozo_exception:
            logger.warning(
                "feed_parse_warning",
                url=url,
                error_type=type(feed.bozo_exception).__name__,
            )
            # Continue anyway - feedparser often recovers

        if not feed.entries:
            logger.info("feed_empty", url=url)
            return feed

        logger.info(
            "feed_parsed",
            url=url,
            entry_count=len(feed.entries),
            feed_title=feed.feed.get("title", "Unknown"),
        )

        return feed

    except Exception as e:
        status = getattr(getattr(e, "response", None), "status_code", None)
        logger.error("feed_parse_error", error_type=type(e).__name__, http_status=status)
        return None


def get_entry_content(entry: dict[str, Any]) -> str:
    """Extract the best available content from an RSS entry.

    Prefers full content over summary.

    Args:
        entry: RSS entry dictionary.

    Returns:
        Content string (may be HTML).
    """
    # Try content first (usually full article)
    if "content" in entry and entry["content"]:
        # content is usually a list
        contents = entry["content"]
        if isinstance(contents, list) and len(contents) > 0:
            return contents[0].get("value", "")

    # Fall back to summary
    if "summary" in entry:
        return entry.get("summary", "")

    # Last resort: description
    return entry.get("description", "")


def get_entry_link(entry: dict[str, Any]) -> Optional[str]:
    """Get the link URL from an RSS entry.

    Args:
        entry: RSS entry dictionary.

    Returns:
        Link URL or None.
    """
    # Direct link attribute
    if "link" in entry and entry["link"]:
        return entry["link"]

    # Links list
    if "links" in entry and entry["links"]:
        for link in entry["links"]:
            if link.get("rel") == "alternate" or link.get("type") == "text/html":
                return link.get("href")
        # Return first link as fallback
        return entry["links"][0].get("href")

    return None


def get_entry_title(entry: dict[str, Any]) -> str:
    """Get the title from an RSS entry.

    Args:
        entry: RSS entry dictionary.

    Returns:
        Title string.
    """
    return entry.get("title", "Untitled")
