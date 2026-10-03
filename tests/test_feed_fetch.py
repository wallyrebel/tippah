from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import requests

from rss_to_wp.feeds import parser
from rss_to_wp.cli import process_feed

RSS = b'<rss version="2.0"><channel><title>News</title></channel></rss>'


def response(status=200, content=RSS, headers=None):
    r = requests.Response()
    r.status_code = status
    r._content = content
    r._content_consumed = True
    r.headers.update(headers or {})
    r.url = "https://example.org/feed"
    return r


def session(monkeypatch, responses):
    client = Mock()
    client.__enter__ = Mock(return_value=client)
    client.__exit__ = Mock(return_value=False)
    client.get.side_effect = responses
    monkeypatch.setattr(parser.requests, "Session", Mock(return_value=client))
    sleep = Mock()
    monkeypatch.setattr(parser.time, "sleep", sleep)
    return client, sleep


def test_transient_http_retries_then_parses_bytes(monkeypatch):
    client, sleep = session(monkeypatch, [response(503), response(429), response()])
    feed = parser.parse_feed("https://example.org/feed")
    assert feed is not None and feed.entries == [] and feed.version == "rss20"
    assert client.get.call_count == 3
    assert [c.args[0] for c in sleep.call_args_list] == [1, 2]
    assert client.get.call_args.kwargs["timeout"] == (10, 30)


@pytest.mark.parametrize("status", [401, 403, 404])
def test_access_denials_not_retried(monkeypatch, status):
    client, sleep = session(monkeypatch, [response(status)])
    assert parser.parse_feed("https://example.org/feed") is None
    assert client.get.call_count == 1 and not sleep.called


def test_retry_after_respected_and_long_delay_stops(monkeypatch):
    client, sleep = session(monkeypatch, [response(429, headers={"Retry-After": "4"}), response()])
    assert parser.parse_feed("https://example.org/feed") is not None
    sleep.assert_called_once_with(4)
    client, sleep = session(monkeypatch, [response(429, headers={"Retry-After": "3600"})])
    assert parser.parse_feed("https://example.org/feed") is None
    assert client.get.call_count == 1 and not sleep.called


def test_exhausted_transport_is_failure(monkeypatch):
    client, sleep = session(monkeypatch, [requests.Timeout("secret")] * 3)
    assert parser.parse_feed("https://example.org/feed") is None
    assert client.get.call_count == 3 and sleep.call_count == 2


@pytest.mark.parametrize(
    "body",
    [
        b"<html><body>Access denied</body></html>",
        b"not XML",
        b'<rss version="2.0"><channel>',
    ],
)
def test_nonfeed_or_broken_document_is_not_empty_success(monkeypatch, body):
    client, sleep = session(monkeypatch, [response(content=body)])
    assert parser.parse_feed("https://example.org/feed") is None
    assert client.get.call_count == 1 and not sleep.called


@pytest.mark.parametrize(
    "feed,expected", [(SimpleNamespace(entries=[]), (0, 0, 0)), (None, (0, 0, 1))]
)
def test_valid_empty_feed_distinguished_from_failed_feed(monkeypatch, feed, expected):
    monkeypatch.setattr("rss_to_wp.cli.parse_feed", lambda url: feed)
    rewriter, wp = Mock(), Mock()
    result = process_feed(
        SimpleNamespace(name="News", url="https://example.org/feed"),
        Mock(),
        Mock(),
        rewriter,
        wp,
        True,
        48,
        Mock(),
        {},
        {},
    )
    assert result == expected
    assert not rewriter.rewrite.called and not wp.create_post.called
