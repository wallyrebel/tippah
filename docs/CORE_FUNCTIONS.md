# Core Functions Reference

This document describes the runtime flow for `rss-to-wp` and where each core behavior lives.

## 1. Entry Point

- Command: `python -m rss_to_wp run --config feeds.yaml`
- File: `src/rss_to_wp/cli.py`
- Function: `run()`

`run()` does the following:
- Loads environment settings from `AppSettings` (`src/rss_to_wp/config.py`).
- Loads feed and weekly column config from `feeds.yaml`.
- Initializes:
  - `DedupeStore` for SQLite tracking.
  - `OpenAIRewriter` for rewrite/generation.
  - `WordPressClient` for publishing (unless `--dry-run`).
- Applies daily category caps for:
  - `Mississippi News` via `MAX_DAILY_MISSISSIPPI_POSTS` (default `8`)
  - `National News` via `MAX_DAILY_NATIONAL_POSTS` (default `8`)
- Processes normal RSS feeds first.
- Processes configured weekly columns second.
- Sends summary email only when new posts were published.

## 2. RSS Feed Processing

- File: `src/rss_to_wp/cli.py`
- Function: `process_feed()`

Per-feed behavior:
- Parse feed URL with `parse_feed()` (`src/rss_to_wp/feeds/parser.py`).
- Filter entries by time window with `pick_entries()` (`src/rss_to_wp/feeds/filter.py`).
- Generate dedupe key with `generate_entry_key()`.
- Skip if already in SQLite dedupe table.
- Process each entry via `process_entry()`.

## 3. Entry Processing (Core Publishing Path)

- File: `src/rss_to_wp/cli.py`
- Function: `process_entry()`

Per-entry behavior:
- Extract title/content/link from RSS.
- Run content-quality guard:
  - skips placeholder/unavailable text
  - skips entries with too little meaningful content
- Rewrite to AP-style JSON via OpenAI.
- Image workflow:
  - try RSS image first
  - fallback to stock providers (`Pexels` first, then `Unsplash`)
- Resolve WordPress category/tags.
- Publish post via WordPress REST API.

Status handling:
- Published entries are stored in dedupe DB.
- Low-information entries are marked as skipped and stored in dedupe DB so they do not loop.

## 4. OpenAI Rewrite + Fallback Logic

- File: `src/rss_to_wp/rewriter/openai_client.py`
- Class: `OpenAIRewriter`

Model behavior:
- Primary model: `OPENAI_MODEL` (default `gpt-5-mini`)
- Fallback model: `OPENAI_FALLBACK_MODEL` (default `gpt-4.1-nano`)

Compatibility behavior:
- Uses model-compatible token parameters.
- Avoids unsupported temperature overrides on `gpt-5*` models.
- Tries fallback model when the primary request fails or returns unusable JSON.

## 5. Weekly Column Automation

- File: `src/rss_to_wp/cli.py`
- Function: `process_weekly_columns()`
- Config section: `weekly_columns` in `feeds.yaml`

Weekly column behavior:
- Runs only on the configured `day_of_week`.
- Uses ISO-week dedupe keys (`column:{slug}:{year}-W{week}`) to guarantee one post per week.
- Builds context from configured RSS `context_feeds`.
- Generates column body via `OpenAIRewriter.write_weekly_column()`.
- Uses Pexels image search for columnist artwork.
- Publishes to WordPress with configured category/tags.

Supported `column_type` values:
- `christian`
- `human_interest`
- `sports`

## 6. WordPress Publish Layer

- File: `src/rss_to_wp/wordpress/client.py`
- Class: `WordPressClient`

Core behaviors:
- Authenticated REST calls to categories, tags, media, and posts.
- Duplicate source URL checks before creating a post.
- Optional featured media upload (`src/rss_to_wp/wordpress/media.py`).

## 7. Deduplication Store

- File: `src/rss_to_wp/storage/dedupe.py`
- Class: `DedupeStore`

SQLite table tracks:
- entry key
- source feed URL
- source title/link
- published WordPress ID/URL
- processed timestamp

This prevents duplicate processing across scheduled runs.

## 8. Config Sources

- Environment settings: `.env` / `.env.example`
- Feed + columnist config: `feeds.yaml`
- Scheduler: `.github/workflows/rss_to_wp.yml`

## 9. Operational Notes

- Existing RSS feeds remain active unless explicitly removed from `feeds.yaml`.
- New statewide and national feeds can be appended as additional `feeds` entries.
- `--single-feed` runs only one feed and skips weekly columnist processing.
