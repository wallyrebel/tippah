"""Authorized single-sentence date correction, locked to one existing post."""

import hashlib
import json
import os
from pathlib import Path

import requests

POST_ID = 26759
BASE = "https://tippahnews.com"
LINK = (
    BASE
    + "/tippah-county-news/tippah-county-news-today-october-3-2026-weather-dixie-theatre-walnut/"
)
OLD = "</a> Thursday at Tyler Gym, 25-19, 19-25, 25-15, 25-20."
NEW = "</a> Friday, Oct. 2, at Tyler Gym, 25-19, 19-25, 25-15, 25-20."


def replacement(content):
    if content.count(OLD) == 0 and content.count(NEW) == 1:
        return content
    if content.count(OLD) != 1 or NEW in content:
        raise ValueError("Exact old sentence must occur once")
    return content.replace(OLD, NEW, 1)


def correct(base, session, public_get=requests.get):
    if base.rstrip("/") != BASE:
        raise ValueError("Unexpected WordPress host")
    endpoint = BASE + f"/wp-json/wp/v2/posts/{POST_ID}"
    before_response = session.get(endpoint, params={"context": "edit"}, timeout=(10, 30))
    before_response.raise_for_status()
    before = before_response.json()
    if before["id"] != POST_ID or before["link"] != LINK or before["status"] != "publish":
        raise ValueError("Unexpected post identity or status")
    old = before["content"]["raw"]
    new = replacement(old)
    changed = new != old
    if changed:
        # Never retry the write. An uncertain outcome requires read-back first.
        result = session.post(endpoint, json={"content": new}, timeout=(10, 30))
        result.raise_for_status()
    after_response = session.get(endpoint, params={"context": "edit"}, timeout=(10, 30))
    after_response.raise_for_status()
    after = after_response.json()
    if after["content"]["raw"] != new:
        raise ValueError("Raw content verification failed")
    for field in (
        "id",
        "status",
        "link",
        "title",
        "excerpt",
        "date",
        "slug",
        "categories",
        "tags",
        "featured_media",
        "author",
    ):
        if after.get(field) != before.get(field):
            raise ValueError("Other post fields changed")
    public_response = public_get(
        endpoint, params={"_fields": "id,status,link,content"}, timeout=(10, 30)
    )
    public_response.raise_for_status()
    public = public_response.json()
    if public["id"] != POST_ID or public["status"] != "publish" or public["link"] != LINK:
        raise ValueError("Public post identity verification failed")
    rendered = public["content"]["rendered"]
    if rendered.count(NEW) != 1 or OLD in rendered:
        raise ValueError("Public correction verification failed")
    return {
        "post_id": POST_ID,
        "url": LINK,
        "changed": changed,
        "verified": True,
        "before_sha256": hashlib.sha256(old.encode()).hexdigest(),
        "after_sha256": hashlib.sha256(new.encode()).hexdigest(),
    }


def main():
    report_path = Path("data/post26759-correction.json")
    report_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with requests.Session() as session:
            session.auth = (
                os.environ["WORDPRESS_USERNAME"],
                os.environ["WORDPRESS_APP_PASSWORD"],
            )
            report = correct(os.environ["WORDPRESS_BASE_URL"], session)
    except Exception as exc:
        report = {
            "post_id": POST_ID,
            "verified": False,
            "error_type": type(exc).__name__,
        }
    report_path.write_text(json.dumps(report, indent=2))
    print(json.dumps(report))
    return 0 if report["verified"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
