#!/usr/bin/env python3
"""Pull new wallpapers from curated subreddits into the archive.

Auth: set REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET / REDDIT_USERNAME /
REDDIT_PASSWORD to use the OAuth API. Without them the script falls back to
the public .json endpoints, which work locally but are usually rate-limited
or blocked outright from cloud IPs such as GitHub Actions runners.

    python scripts/sync_reddit.py --limit 40
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import date
from urllib.parse import urlparse

import requests

from pipeline import DATA_DIR, MANIFEST, Item, ingest_image, load_items

UA = "web:okeyamy-wallpaper-archive:1.0 (by /u/okeyamy)"

FEEDS = [
    ("Animewallpaper", "top", "day"),
    ("Animewallpaper", "hot", None),
    ("AnimeWallpaperSFW", "top", "week"),
    ("Moescape", "top", "week"),
    ("awwnime", "top", "week"),
]

MIN_SCORE = 40
IMAGE_HOSTS = {"i.redd.it", "i.imgur.com", "preview.redd.it"}
MAX_DOWNLOAD_BYTES = 25 * 1024 * 1024


def token() -> str | None:
    cid, secret = os.getenv("REDDIT_CLIENT_ID"), os.getenv("REDDIT_CLIENT_SECRET")
    user, pw = os.getenv("REDDIT_USERNAME"), os.getenv("REDDIT_PASSWORD")
    if not all([cid, secret, user, pw]):
        return None
    r = requests.post(
        "https://www.reddit.com/api/v1/access_token",
        auth=(cid, secret),
        data={"grant_type": "password", "username": user, "password": pw},
        headers={"User-Agent": UA},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()["access_token"]


def fetch_listing(sub: str, sort: str, window: str | None, limit: int, tok: str | None) -> list[dict]:
    params = {"limit": min(limit, 100), "raw_json": 1}
    if window:
        params["t"] = window

    if tok:
        url = f"https://oauth.reddit.com/r/{sub}/{sort}"
        headers = {"Authorization": f"bearer {tok}", "User-Agent": UA}
    else:
        url = f"https://www.reddit.com/r/{sub}/{sort}.json"
        headers = {"User-Agent": UA}

    r = requests.get(url, params=params, headers=headers, timeout=30)
    if r.status_code != 200:
        print(f"  ! r/{sub}/{sort} -> HTTP {r.status_code}", file=sys.stderr)
        return []
    return [c["data"] for c in r.json().get("data", {}).get("children", [])]


def image_url(post: dict) -> str | None:
    """Resolve a post to a direct image URL, or None if it isn't a single image."""
    if post.get("is_video") or post.get("is_gallery") or post.get("over_18"):
        return None
    url = post.get("url_overridden_by_dest") or post.get("url") or ""
    host = urlparse(url).netloc.lower()
    if host in IMAGE_HOSTS and url.lower().split("?")[0].endswith((".jpg", ".jpeg", ".png", ".webp")):
        return url
    # imgur page link -> direct asset
    if host == "imgur.com" and "/a/" not in url and "/gallery/" not in url:
        return url.rstrip("/") + ".jpg"
    return None


def download(url: str) -> bytes | None:
    try:
        r = requests.get(url, headers={"User-Agent": UA}, timeout=60, stream=True)
        r.raise_for_status()
        if int(r.headers.get("content-length") or 0) > MAX_DOWNLOAD_BYTES:
            return None
        buf = bytearray()
        for chunk in r.iter_content(65536):
            buf.extend(chunk)
            if len(buf) > MAX_DOWNLOAD_BYTES:      # unknown length, stop mid-stream
                return None
        return bytes(buf)
    except requests.RequestException as e:
        print(f"  ! download failed: {e}", file=sys.stderr)
        return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=50, help="posts to consider per feed")
    ap.add_argument("--max-new", type=int, default=40, help="stop after N accepted images")
    ap.add_argument("--min-score", type=int, default=MIN_SCORE)
    args = ap.parse_args()

    tok = token()
    print(f"auth: {'oauth' if tok else 'anonymous (may be rate-limited)'}")

    existing = load_items()
    accepted: list[Item] = []
    today = date.today().isoformat()

    for sub, sort, window in FEEDS:
        if len(accepted) >= args.max_new:
            break
        print(f"── r/{sub}/{sort}{f'?t={window}' if window else ''}")
        for post in fetch_listing(sub, sort, window, args.limit, tok):
            if len(accepted) >= args.max_new:
                break
            if post.get("score", 0) < args.min_score:
                continue
            url = image_url(post)
            if not url:
                continue

            raw = download(url)
            if not raw:
                continue

            # duplicate check happens inside ingest_image, before upload —
            # rejecting after the fact would delete the live file when the
            # candidate's content-addressed key collides with the original's
            item = ingest_image(
                raw,
                source="reddit",
                added=today,
                title=post.get("title", ""),
                sub=post.get("subreddit", sub),
                author=post.get("author", ""),
                permalink="https://www.reddit.com" + post.get("permalink", ""),
                existing=existing + [a.to_dict() for a in accepted],
            )
            if item is None:
                continue

            accepted.append(item)
            print(f"  + {item.id}  {item.w}×{item.h}  {item.title[:48]}")
            time.sleep(0.6)          # stay well inside Reddit's rate limit

    if not accepted:
        print("no new wallpapers")
        return 0

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    payload = {"items": existing + [a.to_dict() for a in accepted]}
    MANIFEST.write_text(json.dumps(payload, indent=1))
    print(f"\n{len(accepted)} added / {len(payload['items'])} total")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
