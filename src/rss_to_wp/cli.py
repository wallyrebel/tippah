"""CLI interface for RSS to WordPress automation."""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Optional

import pendulum
import typer
from bs4 import BeautifulSoup
from dotenv import load_dotenv

from rss_to_wp import __version__
from rss_to_wp.config import (
    AppSettings,
    FeedConfig,
    WeeklyColumnConfig,
    get_app_settings,
    load_feeds_config,
)
from rss_to_wp.feeds import (
    generate_entry_key,
    get_entry_content,
    get_entry_link,
    get_entry_title,
    parse_feed,
    pick_entries,
)
from rss_to_wp.images import PexelsClient, download_image, find_fallback_image, find_rss_image
from rss_to_wp.rewriter import OpenAIRewriter
from rss_to_wp.storage import DedupeStore
from rss_to_wp.utils import (
    build_summary_email,
    get_logger,
    send_email_notification,
    setup_logging,
)
from rss_to_wp.wordpress import WordPressClient

# Load environment variables from .env file
load_dotenv()

app = typer.Typer(
    name="rss-to-wp",
    help="Automated RSS feed to WordPress publisher with AI rewriting.",
    add_completion=False,
)

WEEKDAY_TO_INDEX = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}

LOW_INFORMATION_MARKERS = [
    "content unavailable due to privacy settings",
    "content unavailable due to deletion",
    "content unavailable",
    "this content is unavailable",
    "this content isn't available right now",
    "page unavailable",
    "post unavailable",
    "you must be logged in",
    "sign in to continue",
]


def _strip_html_for_quality(content: str) -> str:
    """Convert HTML content to plain text for quality checks."""
    try:
        soup = BeautifulSoup(content, "html.parser")
        for element in soup(["script", "style", "nav", "footer", "header"]):
            element.decompose()
        text = soup.get_text(separator=" ")
    except Exception:
        text = re.sub(r"<[^>]+>", " ", content)

    text = re.sub(r"\s+", " ", text).strip()
    return text


def _has_sufficient_story_content(content: str) -> tuple[bool, str]:
    """Guardrail to skip entries with thin or placeholder text."""
    text = _strip_html_for_quality(content)
    if not text:
        return (False, "empty_content")

    lower = text.lower()
    if any(marker in lower for marker in LOW_INFORMATION_MARKERS):
        return (False, "placeholder_or_unavailable_content")

    words = [word for word in text.split() if word]
    if len(words) < 35:
        return (False, "too_few_words")

    meaningful_sentences = [
        sentence.strip()
        for sentence in re.split(r"[.!?]+", text)
        if len(sentence.strip().split()) >= 8
    ]
    if len(words) < 80 and len(meaningful_sentences) < 2:
        return (False, "too_few_meaningful_sentences")

    return (True, "ok")


def _collect_column_context(
    column_config: WeeklyColumnConfig,
    settings: AppSettings,
    logger,
) -> list[dict[str, str]]:
    """Collect recent context headlines for weekly columnist generation."""
    context_items: list[dict[str, str]] = []

    for context_feed_url in column_config.context_feeds:
        feed = parse_feed(context_feed_url)
        if not feed or not feed.entries:
            continue

        selected_entries = pick_entries(
            entries=feed.entries,
            max_count=column_config.max_context_entries,
            hours_window=column_config.context_hours,
            timezone=settings.timezone,
        )

        for entry in selected_entries:
            title = get_entry_title(entry)
            link = get_entry_link(entry) or ""
            if not title:
                continue

            context_items.append(
                {
                    "title": title,
                    "link": link,
                    "source": context_feed_url,
                }
            )

            if len(context_items) >= column_config.max_context_entries:
                break

        if len(context_items) >= column_config.max_context_entries:
            break

    logger.info(
        "weekly_column_context_collected",
        column=column_config.name,
        count=len(context_items),
    )
    return context_items


def _is_column_day(column_config: WeeklyColumnConfig, timezone: str) -> bool:
    """Check whether a weekly column should run today."""
    now = pendulum.now(timezone)
    target_day = WEEKDAY_TO_INDEX[column_config.day_of_week]
    return now.day_of_week == target_day


def process_weekly_columns(
    columns: list[WeeklyColumnConfig],
    settings: AppSettings,
    dedupe_store: DedupeStore,
    rewriter: OpenAIRewriter,
    wp_client: Optional[WordPressClient],
    dry_run: bool,
    logger,
    published_articles: Optional[list[dict]] = None,
) -> tuple[int, int, int]:
    """Generate and publish configured weekly columnist posts."""
    processed = 0
    skipped = 0
    errors = 0

    if not columns:
        return (processed, skipped, errors)

    now = pendulum.now(settings.timezone)
    iso_year, iso_week, _ = now.isocalendar()

    for column_config in columns:
        if not _is_column_day(column_config, settings.timezone):
            logger.debug(
                "weekly_column_not_scheduled_today",
                column=column_config.name,
                scheduled_day=column_config.day_of_week,
            )
            continue

        column_key = f"column:{column_config.slug}:{iso_year}-W{iso_week:02d}"
        if dedupe_store.is_processed(column_key):
            logger.info(
                "weekly_column_already_processed",
                column=column_config.name,
                week=f"{iso_year}-W{iso_week:02d}",
            )
            skipped += 1
            continue

        logger.info("processing_weekly_column", name=column_config.name, type=column_config.column_type)

        context_items = _collect_column_context(column_config, settings, logger)
        rewritten = rewriter.write_weekly_column(
            column_name=column_config.name,
            column_type=column_config.column_type,
            current_date=now.format("MMMM D, YYYY"),
            context_items=context_items,
        )
        if not rewritten:
            logger.error("weekly_column_generation_failed", name=column_config.name)
            errors += 1
            continue

        image_result = None
        image_alt = rewritten["headline"][:120]
        featured_media_id = None

        # Weekly columns should use Pexels for stock imagery.
        if settings.pexels_api_key:
            try:
                pexels_query = rewritten.get("image_query") or rewritten["headline"]
                pexels_result = PexelsClient(settings.pexels_api_key).search(pexels_query)
                if pexels_result:
                    image_result = download_image(pexels_result["url"])
                    if image_result:
                        image_alt = pexels_result.get("alt_text", image_alt)
            except Exception as e:
                logger.warning(
                    "weekly_column_pexels_error",
                    column=column_config.name,
                    error=str(e),
                )

        if dry_run:
            logger.info(
                "dry_run_would_publish_weekly_column",
                column=column_config.name,
                headline=rewritten["headline"][:70],
                category=column_config.default_category,
            )
            processed += 1
            continue

        if not wp_client:
            errors += 1
            continue

        if image_result:
            image_bytes, filename, _ = image_result
            featured_media_id = wp_client.upload_media(
                image_bytes=image_bytes,
                filename=filename,
                alt_text=image_alt,
            )

        category_id = None
        if column_config.default_category:
            category_id = wp_client.get_or_create_category(column_config.default_category)

        tag_ids = []
        if column_config.default_tags:
            tag_ids = wp_client.get_or_create_tags(column_config.default_tags)

        post = wp_client.create_post(
            title=rewritten["headline"],
            content=rewritten["body"],
            excerpt=rewritten.get("excerpt", ""),
            category_id=category_id,
            tag_ids=tag_ids,
            featured_media_id=featured_media_id,
            source_url=None,
        )
        if not post:
            errors += 1
            continue

        dedupe_store.mark_processed(
            entry_key=column_key,
            feed_url=f"column:{column_config.slug}",
            entry_title=rewritten["headline"],
            entry_link=f"weekly-column://{column_config.slug}/{iso_year}-W{iso_week:02d}",
            category=column_config.default_category or None,
            wp_post_id=post.get("id"),
            wp_post_url=post.get("link"),
        )
        processed += 1

        if published_articles is not None and post.get("link"):
            published_articles.append(
                {
                    "title": post.get("title", {}).get("rendered", rewritten["headline"]),
                    "url": post.get("link"),
                    "feed_name": f"Weekly Column: {column_config.name}",
                }
            )

    return (processed, skipped, errors)


def version_callback(value: bool) -> None:
    """Print version and exit."""
    if value:
        typer.echo(f"rss-to-wp version {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: bool = typer.Option(
        False,
        "--version",
        "-v",
        callback=version_callback,
        is_eager=True,
        help="Show version and exit.",
    ),
) -> None:
    """RSS to WordPress automation CLI."""
    pass


@app.command()
def run(
    config: Path = typer.Option(
        Path("feeds.yaml"),
        "--config",
        "-c",
        help="Path to feeds configuration file.",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        "-n",
        help="Process feeds without publishing to WordPress.",
    ),
    single_feed: Optional[str] = typer.Option(
        None,
        "--single-feed",
        "-f",
        help="Process only a specific feed by name.",
    ),
    hours: int = typer.Option(
        48,
        "--hours",
        "-h",
        help="Time window in hours for entries (strictly enforced).",
    ),
) -> None:
    """Run the RSS to WordPress automation.

    Fetches RSS feeds, rewrites content, and publishes to WordPress.
    """
    # Load settings
    try:
        settings = get_app_settings()
    except Exception as e:
        typer.echo(f"Error loading settings: {e}", err=True)
        typer.echo("Make sure you have a .env file with required variables.", err=True)
        raise typer.Exit(1)

    # Setup logging
    logger = setup_logging(
        level=settings.log_level,
        log_file=settings.log_file,
    )

    logger.info(
        "starting_rss_to_wp",
        version=__version__,
        dry_run=dry_run,
        config=str(config),
    )

    # Load feeds config
    try:
        feeds_config = load_feeds_config(config)
    except FileNotFoundError:
        logger.error("config_not_found", path=str(config))
        raise typer.Exit(1)
    except Exception as e:
        logger.error("config_load_error", error=str(e))
        raise typer.Exit(1)

    feeds = feeds_config.feeds
    weekly_columns = feeds_config.weekly_columns

    # Filter to single feed if specified
    if single_feed:
        feeds = [f for f in feeds if f.name.lower() == single_feed.lower()]
        if not feeds:
            logger.error("feed_not_found", name=single_feed)
            raise typer.Exit(1)

    logger.info(
        "feeds_loaded",
        count=len(feeds),
        weekly_columns=len(weekly_columns),
    )

    # Initialize components
    dedupe_store = DedupeStore()

    category_limits = {
        "Mississippi News": max(0, settings.max_daily_mississippi_posts),
        "National News": max(0, settings.max_daily_national_posts),
    }
    now_local = pendulum.now(settings.timezone)
    day_start_utc = now_local.start_of("day").in_timezone("UTC").naive().isoformat()
    day_end_utc = now_local.start_of("day").add(days=1).in_timezone("UTC").naive().isoformat()
    category_counts = {
        category: dedupe_store.get_published_count_for_category_between(
            category=category,
            start_utc_iso=day_start_utc,
            end_utc_iso=day_end_utc,
        )
        for category in category_limits
    }
    logger.info(
        "daily_category_limits_loaded",
        limits=category_limits,
        current_counts=category_counts,
        day_start_utc=day_start_utc,
        day_end_utc=day_end_utc,
    )

    rewriter = OpenAIRewriter(
        api_key=settings.openai_api_key,
        model=settings.openai_model,
        fallback_model=settings.openai_fallback_model,
    )

    wp_client = None
    if not dry_run:
        wp_client = WordPressClient(
            base_url=settings.wordpress_base_url,
            username=settings.wordpress_username,
            password=settings.wordpress_app_password,
            default_status=settings.wordpress_post_status,
        )

    # Process each feed
    total_processed = 0
    total_skipped = 0
    total_errors = 0
    published_articles: list[dict] = []  # Track for email notification

    for feed_config in feeds:
        try:
            processed, skipped, errors = process_feed(
                feed_config=feed_config,
                settings=settings,
                dedupe_store=dedupe_store,
                rewriter=rewriter,
                wp_client=wp_client,
                dry_run=dry_run,
                hours=hours,
                logger=logger,
                category_limits=category_limits,
                category_counts=category_counts,
                published_articles=published_articles,  # Pass for tracking
            )
            total_processed += processed
            total_skipped += skipped
            total_errors += errors

            # Rate limit between feeds
            time.sleep(1)

        except Exception as e:
            logger.error(
                "feed_processing_error",
                feed=feed_config.name,
                error=str(e),
            )
            total_errors += 1
            continue

    # Process configured weekly columns after RSS feed ingest.
    if single_feed:
        logger.info("weekly_columns_skipped_for_single_feed_run", feed=single_feed)
    else:
        try:
            column_processed, column_skipped, column_errors = process_weekly_columns(
                columns=weekly_columns,
                settings=settings,
                dedupe_store=dedupe_store,
                rewriter=rewriter,
                wp_client=wp_client,
                dry_run=dry_run,
                logger=logger,
                published_articles=published_articles,
            )
            total_processed += column_processed
            total_skipped += column_skipped
            total_errors += column_errors
        except Exception as e:
            logger.error("weekly_columns_processing_error", error=str(e))
            total_errors += 1

    # Summary
    logger.info(
        "run_complete",
        total_processed=total_processed,
        total_skipped=total_skipped,
        total_errors=total_errors,
    )

    # Send email notification ONLY if new articles were published
    if (not dry_run 
        and published_articles  # Only if there are new articles
        and settings.smtp_email 
        and settings.smtp_password 
        and settings.notification_email):
        try:
            subject, html_body = build_summary_email(
                processed_articles=published_articles,
                skipped_count=total_skipped,
                error_count=total_errors,
                site_name="TippahNews",
            )
            send_email_notification(
                smtp_email=settings.smtp_email,
                smtp_password=settings.smtp_password,
                to_email=settings.notification_email,
                subject=subject,
                html_body=html_body,
            )
        except Exception as e:
            logger.error("email_notification_error", error=str(e))

    # Only fail if nothing was processed AND nothing was skipped (complete failure)
    # Partial failures (some articles succeed, some fail) should not cause the run to fail
    if total_errors > 0 and total_processed == 0 and total_skipped == 0:
        raise typer.Exit(1)


def process_feed(
    feed_config: FeedConfig,
    settings: AppSettings,
    dedupe_store: DedupeStore,
    rewriter: OpenAIRewriter,
    wp_client: Optional[WordPressClient],
    dry_run: bool,
    hours: int,
    logger,
    category_limits: dict[str, int],
    category_counts: dict[str, int],
    published_articles: Optional[list[dict]] = None,
) -> tuple[int, int, int]:
    """Process a single feed.

    Returns:
        Tuple of (processed_count, skipped_count, error_count)
    """
    logger.info("processing_feed", name=feed_config.name, url=feed_config.url)

    processed = 0
    skipped = 0
    errors = 0

    # Parse feed
    feed = parse_feed(feed_config.url)
    if not feed or not feed.entries:
        logger.warning("feed_empty_or_failed", name=feed_config.name)
        return (0, 0, 1)

    # Filter entries
    entries = pick_entries(
        entries=feed.entries,
        max_count=feed_config.max_per_run,
        hours_window=hours,
        timezone=settings.timezone,
    )

    if not entries:
        logger.info("no_valid_entries", name=feed_config.name)
        return (0, 0, 0)

    logger.info("entries_to_process", name=feed_config.name, count=len(entries))

    feed_category = feed_config.default_category or ""

    for entry_idx, entry in enumerate(entries):
        if not dry_run and feed_category in category_limits:
            category_limit = category_limits[feed_category]
            current_count = category_counts.get(feed_category, 0)
            if current_count >= category_limit:
                logger.info(
                    "daily_category_limit_reached",
                    category=feed_category,
                    current_count=current_count,
                    limit=category_limit,
                    feed=feed_config.name,
                )
                skipped += len(entries) - entry_idx
                break

        try:
            # Generate unique key
            entry_key = generate_entry_key(entry, feed_config.url)

            # Check if already processed
            if dedupe_store.is_processed(entry_key):
                logger.info(
                    "entry_skipped_duplicate",
                    key=entry_key,
                    title=get_entry_title(entry)[:50],
                )
                skipped += 1
                continue

            # Process entry
            result = process_entry(
                entry=entry,
                feed_config=feed_config,
                settings=settings,
                rewriter=rewriter,
                wp_client=wp_client,
                dry_run=dry_run,
                logger=logger,
            )

            if not result:
                errors += 1
                continue

            status = result.get("_status", "published")
            if status == "skipped":
                if not dry_run:
                    dedupe_store.mark_processed(
                        entry_key=entry_key,
                        feed_url=feed_config.url,
                        entry_title=get_entry_title(entry),
                        entry_link=get_entry_link(entry) or "",
                        category=None,
                        wp_post_id=None,
                        wp_post_url=None,
                    )
                skipped += 1
                continue

            if status != "published":
                errors += 1
                continue

            if not dry_run:
                # Mark as processed
                dedupe_store.mark_processed(
                    entry_key=entry_key,
                    feed_url=feed_config.url,
                    entry_title=get_entry_title(entry),
                    entry_link=get_entry_link(entry) or "",
                    category=feed_category or None,
                    wp_post_id=result.get("id"),
                    wp_post_url=result.get("link"),
                )
                if feed_category in category_limits:
                    category_counts[feed_category] = category_counts.get(feed_category, 0) + 1

            processed += 1

            # Track for email notification
            if not dry_run and published_articles is not None and result.get("link"):
                published_articles.append({
                    "title": result.get("title", {}).get("rendered", get_entry_title(entry)),
                    "url": result.get("link"),
                    "feed_name": feed_config.name,
                })

            # Rate limit between entries
            time.sleep(1)

        except Exception as e:
            logger.error(
                "entry_processing_error",
                title=get_entry_title(entry)[:50],
                error=str(e),
            )
            errors += 1
            continue

    return (processed, skipped, errors)


def process_entry(
    entry,
    feed_config: FeedConfig,
    settings: AppSettings,
    rewriter: OpenAIRewriter,
    wp_client: Optional[WordPressClient],
    dry_run: bool,
    logger,
) -> Optional[dict]:
    """Process a single RSS entry.

    Returns:
        WordPress post data if successful, None otherwise.
    """
    title = get_entry_title(entry)
    content = get_entry_content(entry)
    link = get_entry_link(entry)

    logger.info("processing_entry", title=title[:50])

    quality_ok, quality_reason = _has_sufficient_story_content(content)
    if not quality_ok:
        logger.warning(
            "entry_skipped_low_information",
            title=title[:50],
            reason=quality_reason,
            link=link,
        )
        return {"_status": "skipped", "_skip_reason": quality_reason}

    # Rewrite with OpenAI
    rewritten = rewriter.rewrite(
        content=content,
        original_title=title,
        use_original_title=feed_config.use_original_title,
    )

    if not rewritten:
        logger.error("rewrite_failed", title=title[:50])
        return None

    # Find image
    featured_media_id = None
    image_result = None

    # Try RSS image first
    image_url = find_rss_image(entry, base_url=link or "")
    image_alt = ""

    if image_url:
        logger.info("using_rss_image", url=image_url)
        image_result = download_image(image_url)
        if image_result:
            image_bytes, filename, _ = image_result
            image_alt = title[:100]  # Use title as alt for RSS images
        else:
            image_url = None

    # Fallback to stock photos
    if not image_url:
        fallback = find_fallback_image(
            title=title,
            feed_name=feed_config.name,
            pexels_key=settings.pexels_api_key,
            unsplash_key=settings.unsplash_access_key,
        )
        if fallback:
            logger.info("using_fallback_image", source=fallback["source"])
            image_result = download_image(fallback["url"])
            if image_result:
                image_bytes, filename, _ = image_result
                image_alt = fallback["alt_text"]
            else:
                fallback = None

        if not fallback:
            logger.warning("no_image_available", title=title[:50])

    # Upload image to WordPress
    if not dry_run and wp_client and image_result:
        featured_media_id = wp_client.upload_media(
            image_bytes=image_bytes,
            filename=filename,
            alt_text=image_alt,
        )

    # Get/create category
    category_id = None
    if not dry_run and wp_client and feed_config.default_category:
        category_id = wp_client.get_or_create_category(feed_config.default_category)

    # Get/create tags
    tag_ids = []
    if not dry_run and wp_client and feed_config.default_tags:
        tag_ids = wp_client.get_or_create_tags(feed_config.default_tags)

    # Create post
    if dry_run:
        logger.info(
            "dry_run_would_publish",
            headline=rewritten["headline"][:50],
            body_length=len(rewritten["body"]),
            has_image=featured_media_id is not None or image_result is not None,
            category=feed_config.default_category,
            tags=feed_config.default_tags,
        )
        return {
            "_status": "published",
            "id": 0,
            "link": "dry-run://not-published",
        }

    if not wp_client:
        return None

    post = wp_client.create_post(
        title=rewritten["headline"],
        content=rewritten["body"],
        excerpt=rewritten.get("excerpt", ""),
        category_id=category_id,
        tag_ids=tag_ids,
        featured_media_id=featured_media_id,
        source_url=link,
    )

    if not post:
        return None

    post["_status"] = "published"
    return post


@app.command()
def status() -> None:
    """Show status of processed entries."""
    logger = setup_logging()
    dedupe_store = DedupeStore()

    count = dedupe_store.get_processed_count()
    logger.info("processed_entries_count", count=count)

    recent = dedupe_store.get_recent_entries(limit=10)
    if recent:
        typer.echo("\nRecent entries:")
        for entry in recent:
            typer.echo(f"  - {entry['entry_title'][:60]}...")
            typer.echo(f"    Processed: {entry['processed_at']}")
            if entry.get("wp_post_url"):
                typer.echo(f"    URL: {entry['wp_post_url']}")


@app.command()
def clear_db(
    confirm: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Confirm database clear without prompting.",
    ),
) -> None:
    """Clear the processed entries database."""
    if not confirm:
        confirm = typer.confirm("Are you sure you want to clear all processed entries?")

    if confirm:
        dedupe_store = DedupeStore()
        count = dedupe_store.clear_all()
        typer.echo(f"Cleared {count} entries from database.")
    else:
        typer.echo("Cancelled.")


if __name__ == "__main__":
    app()
