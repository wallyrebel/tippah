# Tippah News editorial workflow

Updated September 12, 2026.

## Categories

Use the existing WordPress archives; do not rename their slugs:

| Category | Existing ID (reference only) | Slug |
|---|---:|---|
| Tippah County News | 7 | tippah-county-news |
| Ripley News | 2107 | ripley-ms-news |
| Mississippi News | 8 | mississippi-news |

The publisher resolves these slugs through WordPress instead of hard-coding IDs.
Ripley stories receive **Ripley News and Tippah County News**. Other Tippah
stories receive **Tippah County News**. Relevant statewide stories receive
**Mississippi News**. Topic categories such as Sports and Education remain useful
additional categories. National stories retain National News.

Routing uses the original source text, not AI-generated keywords or tags. Set
`coverage_area` only for a verified source whose articles consistently concern
that area, such as a municipal notice feed or BMCU sports. A statewide newspaper
can carry national stories; its name alone does not establish local relevance.
Ambiguous geographic routing or statewide relevance requires a draft review.
The rules are conservative text checks, not a comprehensive geographic resolver.

## Article quality

The editor receives source title, text, name, URL and publication date. It must
preserve useful facts, identify the source, distinguish proposed actions from
approved decisions, and avoid invented local connections, quotes or advice.
There is no forced article length. Specific headlines and short excerpts should
help readers identify the subject without keyword stuffing.

Incomplete or refused model responses are not published. Missing editorial
approval, thin content, ambiguous timing and unclear geography go to WordPress
**Drafts**. Detected unsupported numbers/quotes and incomplete required fields
are rejected before uploading media. Promo-code and coupon entries are skipped.
Review reasons appear in the run log; they are not inserted into public articles.
These checks reduce common failures but are not independent fact-checking.

Source links display the source name rather than "Original Article". Existing
articles, permalinks, bylines, feeds and ad integrations are not bulk-modified.
An unresolved required category stops that article rather than publishing it to
an unintended archive. `post_status: draft` can be set per feed during onboarding.

## Official local sources

`official_sources.yaml` contains six verified public pages from the county board,
Chancery Clerk, TCDF, Town of Walnut and City of Ripley. The collector respects
robots.txt, requests only configured pages, and never creates WordPress posts.
First observation establishes a baseline. Subsequent changes enter
`data/official-source-review/` with source links and before/after information.
The scheduled workflow checks at most once every 12 hours after a successful scan
and exposes review leads as a GitHub Actions artifact retained for 14 days.
Failures retain the previous baseline and appear in the workflow log.

```sh
python -m rss_to_wp collect-official-updates --min-hours 12
```

The board page has meeting information; no public minutes feed was found.
The Chancery Clerk is the custodian of official board minutes. Seek dated agendas,
approved minutes, votes, contracts and budget documents before writing about
decisions. No request to an official has been sent automatically.

TCDF's development pages offer project leads, but a changed page is not proof
that a project or award is new. Verify the event date and actual funding status.
Ripley's official site links its City Calendar; calendar events require checking
the event date, location and cancellation status.

Walnut's verified RSS endpoint is `https://www.walnut.ms/news?format=rss`.
It is added to the normal ingest pipeline with **draft** status. Its newest item
at verification was April 8, 2025, so the normal 48-hour filter excludes the backlog.

## Reliability and review

The existing dedupe database is retained. An additive migration stores a hash of
the original source text. Identical source text and source URLs with tracking
parameters are caught even when a feed supplies a different GUID. Substantively
similar stories with different text still need editorial judgment; this is not
semantic event deduplication.

Feed dates are interpreted as UTC and future-dated entries are excluded beyond
a five-minute tolerance. Daily limits apply to the actual routed category, so a
statewide cap does not suppress a Tippah story in that feed. Drafts do not consume
publication caps or appear in published-article notification summaries.
The workflow serializes publishing runs and saves the dedupe database after
partial failures. Its existing database cache path is preserved.

```sh
python -m pytest -q
python -m rss_to_wp run --config feeds.yaml --dry-run --single-feed "Town of Walnut News"
```

A dry run does not publish or upload media, but normal article rewriting still
uses the configured OpenAI account. Tests use mocked WordPress and model clients.
Two isolated live model fixtures were also checked: a specific municipal notice
retained its supported facts, while a vague regional weather item was withheld.
Those fixtures were synthetic and were never published.

## Rollback

Revert the editorial change commit and redeploy. The new nullable `source_hash`
database column is backward compatible with the old application. Keep
`data/processed.db`; clearing it risks duplicate articles. The website menu and
category description changes are separately editable in WordPress and do not
require reverting the article code.

Official sources and implementation reference:

- https://www.co.tippah.ms.us/elected-officials/board-of-supervisors/
- https://www.co.tippah.ms.us/elected-officials/chancery-clerk/
- https://tippahcounty.org/about-tippah-county/county-development/
- https://tippahcounty.org/about-tippah-county/city-development/
- https://www.walnut.ms/news/
- https://www.ripley.ms.gov/
- https://developers.openai.com/api/docs/guides/structured-outputs
