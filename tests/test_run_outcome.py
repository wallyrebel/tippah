import json

import pytest
import requests

from rss_to_wp.official_sources import collect_updates
from rss_to_wp.run_outcome import combine, main
from test_official_sources import page, response
from unittest.mock import Mock


def test_failed_robots_shared_origin_is_one_request_and_keeps_baseline(tmp_path):
    config = tmp_path / "sources.yaml"
    config.write_text("sources:\n  - name: A\n    url: https://county.example/a\n  - name: B\n    url: https://county.example/b\n")
    state = {"https://county.example/a": {"hash": "old", "page": {"text": "old"}}}
    (tmp_path / "official-source-state.json").write_text(json.dumps(state))
    session = Mock()
    session.get.side_effect = requests.HTTPError("503 robots unavailable")
    report = collect_updates(config, tmp_path, session)
    assert report["outcome"] == "failed" and len(report["errors"]) == 2
    assert session.get.call_count == 1
    assert json.loads((tmp_path / "official-source-state.json").read_text()) == state
    assert json.loads((tmp_path / "official-source-report.json").read_text())["outcome"] == "failed"


def test_partial_collector_and_complete_then_deferred(tmp_path):
    config = tmp_path / "sources.yaml"
    config.write_text("sources:\n  - name: A\n    url: https://a.example/news\n  - name: B\n    url: https://b.example/news\n")
    session = Mock()
    session.get.side_effect = [response("User-agent: *\nAllow: /"), response(page("Verified public notice. " * 10)), requests.HTTPError("503")]
    report = collect_updates(config, tmp_path, session)
    assert report["outcome"] == "partial" and report["checked"] == 1
    state = json.loads((tmp_path / "official-source-state.json").read_text())
    assert "_checked_at" not in state
    session.get.side_effect = [response("User-agent: *\nAllow: /"), response(page("Verified public notice. " * 10))] * 2
    assert collect_updates(config, tmp_path, session)["outcome"] == "complete"
    count = session.get.call_count
    assert collect_updates(config, tmp_path, session, min_hours=12)["outcome"] == "deferred"
    assert session.get.call_count == count


@pytest.mark.parametrize("feed,collector,expected", [
    ("healthy", "complete", "complete"), ("healthy", "deferred", "complete"),
    ("healthy", "partial", "partial"), ("healthy", "failed", "partial"),
    ("failed", "failed", "failed"), ("partial", "complete", "partial"),
])
def test_aggregate_never_hides_collector_failure(feed, collector, expected):
    assert combine({"outcome": feed}, {"outcome": collector})["outcome"] == expected


def test_missing_collector_report_cannot_be_healthy(tmp_path):
    (tmp_path / "run-report.json").write_text('{"outcome":"healthy","errors":0}')
    assert main(tmp_path) == 1
    report = json.loads((tmp_path / "run-report.json").read_text())
    assert report["outcome"] == "partial"
    assert report["feed_outcome"] == "healthy"
    assert report["official_sources"]["outcome"] == "failed"
