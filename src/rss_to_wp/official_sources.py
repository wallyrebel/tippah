"""Collect public official-page changes as local editorial leads, never posts."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import requests
import yaml
from bs4 import BeautifulSoup

USER_AGENT = "TippahNewsSourceMonitor/1.0 (+https://tippahnews.com/contact-us/)"


def extract_page(html: str, url: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    title = soup.title.get_text(" ", strip=True) if soup.title else url
    for element in soup(["script", "style", "nav", "footer", "header", "form"]):
        element.decompose()
    main = soup.select_one("main, article, [role='main'], .entry-content") or soup.body or soup
    text = " ".join(main.get_text(" ", strip=True).split())
    if len(text) < 100:
        raise ValueError("Insufficient readable page content")
    documents = sorted(
        {
            urljoin(url, a["href"])
            for a in main.select("a[href]")
            if any(
                word in a.get_text(" ").lower() + a["href"].lower()
                for word in ("minute", "agenda", "notice", ".pdf", "calendar")
            )
        }
    )
    return {"title": title, "text": text, "documents": documents}


def collect_updates(config: Path, data_dir: Path, session=None, min_hours: int = 0) -> dict:
    """First observation establishes a baseline; later changes enter a review queue.

    One bounded request per page, no crawling, login or CAPTCHA bypass. Failures
    leave the previous baseline intact and return a visible error summary.
    """
    session = session or requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    sources = yaml.safe_load(config.read_text(encoding="utf-8"))["sources"]
    data_dir.mkdir(parents=True, exist_ok=True)
    state_path = data_dir / "official-source-state.json"
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
    summary = {"baselined": 0, "unchanged": 0, "changed": 0, "errors": []}
    checked_at = state.get("_checked_at")
    if checked_at and min_hours:
        elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(checked_at)).total_seconds()
        if elapsed < min_hours * 3600:
            return {**summary, "deferred": "sources_checked_recently"}
    robots_cache = {}
    for source in sources:
        url = source["url"]
        try:
            parts = urlsplit(url)
            if parts.scheme != "https" or not parts.hostname or parts.username:
                raise ValueError("Source must be a public HTTPS URL")
            origin = f"{parts.scheme}://{parts.netloc}"
            if origin not in robots_cache:
                response = session.get(origin + "/robots.txt", timeout=(10, 30))
                robot = RobotFileParser()
                if response.status_code == 404:
                    robot.parse([])
                else:
                    response.raise_for_status()
                    robot.parse(response.text.splitlines())
                robots_cache[origin] = robot
            if not robots_cache[origin].can_fetch(USER_AGENT, url):
                raise ValueError("Source robots.txt does not allow this page")
            response = session.get(url, timeout=(10, 30))
            response.raise_for_status()
            if "text/html" not in response.headers.get("Content-Type", ""):
                raise ValueError("Expected an HTML page")
            page = extract_page(response.text, response.url)
            digest = hashlib.sha256(json.dumps(page, sort_keys=True).encode()).hexdigest()
            previous = state.get(url)
            if previous is None:
                summary["baselined"] += 1
            elif previous["hash"] == digest:
                summary["unchanged"] += 1
                continue
            else:
                summary["changed"] += 1
                lead = {
                    "source": source,
                    "observed_at": datetime.now(timezone.utc).isoformat(),
                    "status": "needs_editor_review",
                    "previous": previous["page"],
                    "current": page,
                    "note": "Page change is not an event date or proof of a new decision.",
                }
                queue = data_dir / "official-source-review"
                queue.mkdir(exist_ok=True)
                identity = hashlib.sha256(url.encode()).hexdigest()[:12]
                (queue / f"{identity}-{digest[:12]}.json").write_text(
                    json.dumps(lead, ensure_ascii=False, indent=2), encoding="utf-8"
                )
            state[url] = {"hash": digest, "page": page}
        except (requests.RequestException, ValueError, KeyError) as exc:
            summary["errors"].append({"source": source["name"], "error": str(exc)})
    if not summary["errors"]:
        state["_checked_at"] = datetime.now(timezone.utc).isoformat()
    temporary = state_path.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(state_path)
    return summary
