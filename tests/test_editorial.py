from types import SimpleNamespace
from unittest.mock import Mock

import pendulum
import pytest

from rss_to_wp.cli import process_entry, process_feed
from rss_to_wp.config import AppSettings, FeedConfig
from rss_to_wp.editorial import (
    canonical_source_url,
    clean_article_html,
    review_article,
    route_categories,
    source_fingerprint,
)
from rss_to_wp.feeds.filter import is_within_window, parse_entry_date
from rss_to_wp.rewriter.openai_client import OpenAIRewriter
from rss_to_wp.storage import DedupeStore
from rss_to_wp.wordpress import WordPressClient

SOURCE = (
    "The City of Ripley, Mississippi, announced a water service interruption on Oak Street "
    "on September 14, 2026, from 8 a.m. to noon while crews replace a valve. "
    "The city said the work affects the block between Main Street and Pine Street. "
    "Residents on that block should store water before work begins. "
    "The city said an update will be issued when service is restored."
)


def article(**overrides):
    return dict(
        {
            "headline": "Ripley announces Oak Street water interruption",
            "excerpt": "The City of Ripley announced work on Oak Street.",
            "body": f"<p>{SOURCE}</p>",
            "ready_for_publication": True,
            "review_reasons": [],
        },
        **overrides,
    )


@pytest.mark.parametrize(
    "title,content,area,default,expected,review",
    [
        (
            "Water notice",
            SOURCE,
            "unknown",
            "Local News",
            ["Ripley News", "Tippah County News"],
            False,
        ),
        (
            "Ripley, MS schools",
            "School meeting",
            "unknown",
            "Education",
            ["Ripley News", "Tippah County News", "Education"],
            False,
        ),
        (
            "County bridge work",
            "Tippah County approved a bridge repair.",
            "unknown",
            "Local News",
            ["Tippah County News"],
            False,
        ),
        (
            "Sportsplex",
            "Town of Walnut project",
            "tippah",
            "Local News",
            ["Tippah County News"],
            False,
        ),
        (
            "Grant",
            "Walnut Grove, Mississippi",
            "mississippi",
            "Government",
            ["Mississippi News", "Government"],
            False,
        ),
        (
            "Budget",
            "Mississippi lawmakers voted.",
            "mississippi",
            "Government",
            ["Mississippi News", "Government"],
            False,
        ),
        (
            "Ripley, Tennessee storm",
            "Rain near Mississippi",
            "unknown",
            "National News",
            ["Mississippi News"],
            False,
        ),
        ("Storms in the area", "Heavy rain across the region.", "unknown", "Local News", [], True),
        (
            "Congress votes",
            "A national bill",
            "national",
            "National News",
            ["National News"],
            False,
        ),
        (
            "BMCU scores",
            "Team wins away game",
            "tippah",
            "Sports",
            ["Tippah County News", "Sports"],
            False,
        ),
    ],
)
def test_geographic_routing(title, content, area, default, expected, review):
    categories, reasons = route_categories(title, content, default, area)
    assert categories == expected
    assert bool(reasons) is review


def test_rewrite_checks_facts_and_relative_time():
    assert review_article(article(), SOURCE, "Water notice") == []
    assert "numbers_not_in_source" in review_article(
        article(body=f"<p>{SOURCE} Cost: $900.</p>"), SOURCE, "Water notice"
    )
    assert "quote_not_in_source" in review_article(
        article(body=f"<p>{SOURCE} “We guarantee safe water,” a worker said.</p>"),
        SOURCE,
        "Water notice",
    )
    assert "relative_time_needs_review" in review_article(
        article(headline="Water off tomorrow"), SOURCE, "Water notice"
    )


def test_safe_html():
    result = clean_article_html(
        '<p onclick="bad()">News <a href="javascript:bad()">link</a></p><script>x</script><svg><title>x</title></svg>'
    )
    assert result == "<p>News link</p>"


def test_url_preserves_identifiers():
    assert (
        canonical_source_url(
            "https://www.facebook.com/story.php?story_fbid=123&id=456&utm_source=x#top"
        )
        == "https://www.facebook.com/story.php?id=456&story_fbid=123"
    )
    assert canonical_source_url("javascript:alert(1)") == ""


def test_dates_are_utc_and_future_is_rejected():
    import time

    parsed = parse_entry_date(
        {"published_parsed": time.struct_time((2026, 9, 12, 15, 0, 0, 5, 255, 0))}
    )
    assert parsed.hour == 15
    assert not is_within_window(pendulum.now("UTC").add(days=1))
    assert not is_within_window(pendulum.now("UTC").subtract(days=3))


def test_dedupe_migrates_and_catches_new_guid(tmp_path):
    import sqlite3

    path = tmp_path / "db.sqlite"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE processed_entries (id INTEGER PRIMARY KEY, entry_key TEXT UNIQUE, feed_url TEXT, entry_title TEXT, entry_link TEXT, wp_post_id INTEGER, wp_post_url TEXT, processed_at TEXT)"
        )
    store = DedupeStore(path)
    store.mark_processed(
        "one",
        "feed",
        "water",
        "https://example.com/story?utm_source=one",
        wp_post_id=12,
        source_hash=source_fingerprint(SOURCE),
    )
    assert store.is_duplicate_source("https://example.com/story?utm_source=two", "")
    assert store.is_duplicate_source("https://another.example/story", source_fingerprint(SOURCE))
    assert not store.is_duplicate_source("https://example.com/new-story", "")


@pytest.fixture
def settings():
    return AppSettings(
        _env_file=None,
        openai_api_key="test",
        wordpress_base_url="https://example.com",
        wordpress_username="test",
        wordpress_app_password="test",
    )


def test_dry_run_does_not_upload_or_publish(settings):
    wp = Mock()
    rewriter = Mock()
    rewriter.rewrite.return_value = article()
    result = process_entry(
        {
            "title": "Water notice",
            "summary": SOURCE,
            "link": "https://example.com/water",
            "published": "2026-09-12T15:00:00Z",
        },
        FeedConfig(name="City", url="https://example.com/feed"),
        settings,
        rewriter,
        wp,
        True,
        Mock(),
    )
    assert result["categories"] == ["Ripley News", "Tippah County News"]
    assert result["_status"] == "published"
    assert not wp.mock_calls
    assert rewriter.rewrite.call_args.kwargs["published_at"].startswith("2026-09-12")


def test_ambiguous_story_is_draft(settings):
    rewriter = Mock()
    rewriter.rewrite.return_value = article()
    result = process_entry(
        {
            "title": "Weather",
            "summary": "Weather conditions remain unsettled across the area and additional rain is expected. "
            * 10,
            "link": "https://example.com/weather",
        },
        FeedConfig(name="Local", url="https://example.com/feed", default_category="Local News"),
        settings,
        rewriter,
        None,
        True,
        Mock(),
    )
    assert result["_status"] == "draft"
    assert "geography_needs_review" in result["review_reasons"]


def test_missing_category_prevents_post(settings):
    wp = Mock()
    wp.get_or_create_category.return_value = None
    rewriter = Mock()
    rewriter.rewrite.return_value = article()
    assert (
        process_entry(
            {"title": "Water", "summary": SOURCE, "link": "https://example.com/water"},
            FeedConfig(name="City", url="https://example.com/feed"),
            settings,
            rewriter,
            wp,
            False,
            Mock(),
        )
        is None
    )
    wp.create_post.assert_not_called()
    wp.upload_media.assert_not_called()


def test_existing_ripley_slug_and_multiple_categories():
    wp = WordPressClient("https://example.com", "test", "test")
    wp._rate_limit = Mock()
    wp.session = Mock()
    wp.session.get.return_value.json.return_value = [{"id": 2107}]
    assert wp.get_or_create_category("Ripley News") == 2107
    assert wp.session.get.call_args.kwargs["params"] == {"slug": "ripley-ms-news"}
    wp.check_duplicate_by_source_url = Mock(return_value=False)
    wp.session.post.return_value.json.return_value = {"id": 99}
    wp.create_post(
        "Headline",
        "<p>Story</p>",
        category_ids=[2107, 7, 7],
        source_url="https://example.com/?a=1&b=2",
        source_name="City & County",
    )
    payload = wp.session.post.call_args.kwargs["json"]
    assert payload["categories"] == [2107, 7]
    assert "City &amp; County" in payload["content"]
    assert "a=1&amp;b=2" in payload["content"]


def test_truncated_completion_is_never_parsed():
    rewriter = OpenAIRewriter("test", fallback_model=None)
    rewriter.client = Mock()
    rewriter.client.chat.completions.create.return_value = SimpleNamespace(
        choices=[
            SimpleNamespace(
                finish_reason="length",
                message=SimpleNamespace(
                    content='{"headline":"Incomplete","body":"text"}', refusal=None
                ),
            )
        ]
    )
    assert rewriter._request_json_completion("rewrite", []) is None


def test_missing_editorial_flag_requires_review():
    rewriter = OpenAIRewriter("test")
    result = rewriter._normalize_response({"headline": "Title", "body": "<p>Body</p>"})
    assert result["ready_for_publication"] is False


def test_statewide_publisher_does_not_prove_story_relevance():
    categories, reasons = route_categories(
        "National poll", "A poll of American adults.", "Mississippi News"
    )
    assert categories == ["Mississippi News"]
    assert reasons == ["statewide_relevance_needs_review"]


def test_promotions_are_skipped_before_model_call(settings):
    rewriter = Mock()
    result = process_entry(
        {
            "title": "Betr Promo Code FOXNEWS",
            "summary": SOURCE,
            "link": "https://example.com/promo",
        },
        FeedConfig(name="National", url="https://example.com/feed"),
        settings,
        rewriter,
        None,
        True,
        Mock(),
    )
    assert result["_skip_reason"] == "commercial_promotion"
    rewriter.rewrite.assert_not_called()


def test_process_entry_publishes_both_local_categories(settings, monkeypatch):
    from rss_to_wp import cli

    monkeypatch.setattr(cli, "find_rss_image", lambda *a, **kw: None)
    monkeypatch.setattr(cli, "find_fallback_image", lambda *a, **kw: None)
    wp = Mock()
    wp.get_or_create_category.side_effect = [2107, 7]
    wp.create_post.return_value = {"id": 12, "link": "https://example.com/new-post"}
    rewriter = Mock()
    rewriter.rewrite.return_value = article()
    result = process_entry(
        {"title": "Water", "summary": SOURCE, "link": "https://example.com/water"},
        FeedConfig(name="City of Ripley", url="https://example.com/feed"),
        settings,
        rewriter,
        wp,
        False,
        Mock(),
    )
    assert result["_status"] == "published"
    assert wp.create_post.call_args.kwargs["category_ids"] == [2107, 7]
    assert wp.create_post.call_args.kwargs["source_name"] == "City of Ripley"
    assert wp.create_post.call_args.kwargs["status"] == "publish"


def test_rejected_model_does_not_upload(settings):
    rewriter = Mock()
    rewriter.rewrite.return_value = article(
        ready_for_publication=False, review_reasons=["numbers_not_in_source"]
    )
    wp = Mock()
    result = process_entry(
        {"title": "Water", "summary": SOURCE, "link": "https://example.com/water"},
        FeedConfig(name="City", url="https://example.com/feed"),
        settings,
        rewriter,
        wp,
        False,
        Mock(),
    )
    assert result["_status"] == "skipped"
    assert not wp.mock_calls


def test_local_story_survives_statewide_cap(settings, tmp_path, monkeypatch):
    from rss_to_wp import cli

    now = pendulum.now("UTC").to_iso8601_string()
    entries = [
        {
            "id": "state",
            "title": "Mississippi budget",
            "summary": "State budget",
            "link": "https://example.com/state",
            "published": now,
        },
        {
            "id": "local",
            "title": "Tippah bridge",
            "summary": "Tippah County bridge",
            "link": "https://example.com/local",
            "published": now,
        },
    ]
    monkeypatch.setattr(
        cli, "parse_feed", lambda url: SimpleNamespace(feed={"title": "News"}, entries=entries)
    )
    handler = Mock(return_value={"_status": "published", "id": 0})
    monkeypatch.setattr(cli, "process_entry", handler)
    monkeypatch.setattr(cli.time, "sleep", lambda _: None)
    result = process_feed(
        FeedConfig(
            name="State", url="https://example.com/feed", default_category="Mississippi News"
        ),
        settings,
        DedupeStore(tmp_path / "db"),
        Mock(),
        None,
        True,
        48,
        Mock(),
        {"Mississippi News": 8},
        {"Mississippi News": 8},
    )
    assert result == (1, 1, 0)
    assert handler.call_args.kwargs["entry"]["id"] == "local"
