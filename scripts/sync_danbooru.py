#!/usr/bin/env python3
"""Pull new wallpapers from Danbooru's public API — no account, no key.

Reddit's script-app OAuth needs a verified account and breaks with 2FA on,
which blocked setup entirely. Danbooru's read API needs neither: it is
public, unauthenticated, purpose-built for anime art, and returns real
resolution/tag/source metadata per post. This replaces sync_reddit.py as the
primary feed; that script is left in place in case Reddit access is sorted
out later.

    python scripts/sync_danbooru.py --max-new 40
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date

import requests

from pipeline import DATA_DIR, MANIFEST, Item, discard, ingest_image, is_duplicate, load_items

UA = "web:okeyamy-wallpaper-archive:1.0 (by anonymous)"
API = "https://danbooru.donmai.us/posts.json"

# rating:general only — this is a public archive, keep it safe-for-work.
# order:score surfaces well-received art rather than a raw firehose.
#
# Anonymous (unauthenticated) requests are capped at 2 real tags — `order:`
# is a search modifier and doesn't count against that, but a literal
# "wallpaper" tag does not exist on Danbooru and silently returns zero
# results, so each entry here is rating:general + exactly one real tag.
QUERIES = [
    "rating:general highres order:score",
    "rating:general absurdres order:score",
    "rating:general scenery order:score",
    "rating:general landscape order:score",
]

MIN_SCORE = 15
MIN_PAGES = 3          # pages fetched per query per run


def fetch(tags: str, page: int, limit: int) -> list[dict]:
    r = requests.get(
        API,
        params={"tags": tags, "page": page, "limit": limit},
        headers={"User-Agent": UA},
        timeout=30,
    )
    if r.status_code != 200:
        print(f"  ! {tags!r} page {page} -> HTTP {r.status_code}", file=sys.stderr)
        return []
    return r.json()


def image_url(post: dict) -> str | None:
    if post.get("rating") != "g":                 # double-check even though the query already filters
        return None
    return post.get("file_url") or post.get("large_file_url")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-new", type=int, default=40)
    ap.add_argument("--min-score", type=int, default=MIN_SCORE)
    args = ap.parse_args()

    existing = load_items()
    seen_ids = {it.get("id") for it in existing}
    accepted: list[Item] = []
    today = date.today().isoformat()

    for tags in QUERIES:
        if len(accepted) >= args.max_new:
            break
        print(f"── {tags}")
        for page in range(1, MIN_PAGES + 1):
            if len(accepted) >= args.max_new:
                break
            for post in fetch(tags, page, limit=40):
                if len(accepted) >= args.max_new:
                    break
                if post.get("score", 0) < args.min_score:
                    continue
                url = image_url(post)
                if not url:
                    continue

                try:
                    resp = requests.get(url, headers={"User-Agent": UA}, timeout=60)
                    resp.raise_for_status()
                    raw = resp.content
                except requests.RequestException as e:
                    print(f"  ! download failed: {e}", file=sys.stderr)
                    continue

                title = (post.get("tag_string_character")
                         or post.get("tag_string_copyright")
                         or " ".join(post.get("tag_string_general", "").split()[:6])
                         or "untitled")

                item = ingest_image(
                    raw,
                    source="danbooru",
                    added=today,
                    title=title.replace("_", " "),
                    sub="danbooru",
                    author=post.get("tag_string_artist", "").replace("_", " "),
                    permalink=f"https://danbooru.donmai.us/posts/{post['id']}",
                    tags=post.get("tag_string_general", "").split()[:15],
                )
                if item is None:
                    continue

                if item.id in seen_ids or is_duplicate(item, existing + [a.to_dict() for a in accepted]):
                    discard(item)
                    continue

                accepted.append(item)
                seen_ids.add(item.id)
                print(f"  + {item.id}  {item.w}×{item.h}  {item.title[:48]}")
                time.sleep(0.5)          # polite pacing on an anonymous, unauthenticated client

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
