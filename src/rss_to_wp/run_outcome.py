"""Combine independent feed and official-source coverage without hiding outages."""
from __future__ import annotations

import json
import sys
from pathlib import Path


def combine(feed: dict, collector: dict) -> dict:
    feed_outcome = feed.get("feed_outcome", feed.get("outcome", "failed"))
    feed_complete = feed_outcome in ("healthy", "complete")
    collector_complete = collector.get("outcome") in ("complete", "deferred")
    if feed_complete and collector_complete:
        outcome = "complete"
    elif feed_outcome == "failed" and collector.get("outcome") == "failed":
        outcome = "failed"
    else:
        outcome = "partial"
    return {**feed, "feed_outcome": feed_outcome, "outcome": outcome,
            "official_sources": collector}


def read_report(path: Path) -> dict:
    try:
        value = json.loads(path.read_text())
        if not isinstance(value, dict):
            raise ValueError("Report must be an object")
        return value
    except (OSError, ValueError):
        return {"outcome": "failed", "errors": [{"error": f"Missing or invalid report: {path.name}"}]}


def main(data_dir: Path = Path("data"), summary: bool = False) -> int:
    path = data_dir / "run-report.json"
    report = combine(read_report(path), read_report(data_dir / "official-source-report.json"))
    if summary:
        print(f"- **Overall outcome**: {report['outcome']}")
        print(f"- **Feed outcome**: {report['feed_outcome']}; errors: {report.get('errors', 'unknown')}")
        collector = report["official_sources"]
        print(f"- **Official sources**: {collector.get('outcome')}; "
              f"checked: {collector.get('checked', 0)}/{collector.get('expected', 'unknown')}; "
              f"errors: {len(collector.get('errors', []))}")
    else:
        data_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, indent=2) + "\n")
    return 0 if report["outcome"] == "complete" else 1


if __name__ == "__main__":
    sys.exit(main(summary="--summary" in sys.argv))
