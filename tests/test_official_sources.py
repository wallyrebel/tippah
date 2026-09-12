import json
from unittest.mock import Mock

from rss_to_wp.official_sources import collect_updates, extract_page


def page(text):
    return (
        "<html><title>City notice</title><nav>Menu</nav><main><p>"
        + text
        + '</p><a href="/agenda.pdf">Meeting agenda</a></main><footer>Copyright</footer></html>'
    )


def response(body, url="https://city.example/news", content_type="text/html"):
    result = Mock(status_code=200, text=body, url=url, headers={"Content-Type": content_type})
    result.raise_for_status.return_value = None
    return result


def test_first_run_baselines_then_queues_changes(tmp_path):
    config = tmp_path / "sources.yaml"
    config.write_text(
        "sources:\n  - name: City\n    url: https://city.example/news\n", encoding="utf-8"
    )
    data = tmp_path / "data"
    session = Mock()
    original = page("Original information for residents about a public meeting. " * 5)
    changed = page("Updated information for residents about a public meeting. " * 5)
    session.get.side_effect = [response("User-agent: *\nAllow: /"), response(original)]
    assert collect_updates(config, data, session)["baselined"] == 1
    assert not (data / "official-source-review").exists()
    call_count = session.get.call_count
    assert (
        collect_updates(config, data, session, min_hours=12)["deferred"]
        == "sources_checked_recently"
    )
    assert session.get.call_count == call_count
    session.get.side_effect = [response("User-agent: *\nAllow: /"), response(original)]
    assert collect_updates(config, data, session)["unchanged"] == 1
    session.get.side_effect = [response("User-agent: *\nAllow: /"), response(changed)]
    assert collect_updates(config, data, session)["changed"] == 1
    leads = list((data / "official-source-review").glob("*.json"))
    assert len(leads) == 1
    lead = json.loads(leads[0].read_text())
    assert lead["status"] == "needs_editor_review"
    assert lead["current"]["documents"] == ["https://city.example/agenda.pdf"]
    session.post.assert_not_called()


def test_robots_denial_does_not_fetch_page(tmp_path):
    config = tmp_path / "sources.yaml"
    config.write_text(
        "sources:\n  - name: City\n    url: https://city.example/news\n", encoding="utf-8"
    )
    session = Mock()
    session.get.return_value = response("User-agent: *\nDisallow: /")
    result = collect_updates(config, tmp_path / "data", session)
    assert len(result["errors"]) == 1
    assert session.get.call_count == 1


def test_page_excludes_navigation_and_footer():
    result = extract_page(page("Useful public information. " * 10), "https://city.example/news")
    assert "Menu" not in result["text"]
    assert "Copyright" not in result["text"]
