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

from pipeline import (
    DATA_DIR, MANIFEST, Item, clean_character_tags, ingest_image, load_items,
)
from quality import reject_reasons

UA = "web:okeyamy-wallpaper-archive:1.0 (by anonymous)"
API = "https://danbooru.donmai.us/posts.json"

# rating:general only — this is a public archive, keep it safe-for-work.
# order:score surfaces well-received art rather than a raw firehose.
#
# Anonymous (unauthenticated) requests are capped at 2 real tags — `order:`
# is a search modifier and doesn't count against that, but a literal
# "wallpaper" tag does not exist on Danbooru and silently returns zero
# results, so each entry here is rating:general + exactly one real tag.
#
# `highres` and `absurdres` look like the obvious tags for a wallpaper
# archive, but they cover millions of posts each, and Danbooru 500s on
# sorting a tag set that large for anonymous requests (order:score AND
# order:random both fail, confirmed directly against the API — it isn't
# transient). Left out on purpose. The list below is deliberately wide
# rather than long, since a short list queried daily with order:score
# mostly returns the same top posts run after run.
QUERIES = [
    "rating:general scenery order:score",
    "rating:general landscape order:score",
    "rating:general sky order:score",
    "rating:general nature order:score",
    "rating:general cityscape order:score",
    "rating:general night order:score",
    "rating:general sunset order:score",
    "rating:general mountain order:score",
    "rating:general building order:score",
    "rating:general architecture order:score",
]

MIN_SCORE = 15
MIN_PAGES = 3          # pages fetched per query per run

# Posts already judged and refused, keyed by Danbooru post id.
#
# Without this the daily run re-examines the same rejects forever. That is
# nearly free for a post refused on its tags — the decision is made from the
# search response, before any download — but a post refused on *pixels*
# (letterboxed, near-blank) can only be judged after the full image is fetched,
# so it would be downloaded and thrown away every single day. Worse, the run
# has a fixed page budget, so as the archive absorbs the good posts an
# ever-larger share of that budget goes to re-downloading known junk and the
# sync makes less progress each day. This ledger is what stops that decay.
REJECTS = DATA_DIR / "rejected.json"


def load_rejects() -> dict:
    if not REJECTS.exists():
        return {}
    try:
        return json.loads(REJECTS.read_text()).get("posts", {})
    except json.JSONDecodeError:
        return {}


def save_rejects(posts: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    REJECTS.write_text(json.dumps({"posts": posts}, indent=1, sort_keys=True))

# `rating:general` in the query is a floor, not a filter for "is this a
# wallpaper" — it admits comic pages, character sheets, screencaps and
# 4-panel gags, all of which scored well and all of which landed in the
# archive before this check existed. The post JSON already carries the full
# tag string and the pixel dimensions, so the policy in quality.py can be
# applied here, before the image is downloaded: a rejected post costs one
# already-paid API response instead of a multi-megabyte fetch, an encode and
# two uploads.
#
# Note this sees *every* tag on the post, while the archived item keeps only
# the first 15 — so this gate is strictly better informed than any check run
# against the manifest afterwards.


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
    rejects = load_rejects()
    accepted: list[Item] = []
    rejected = 0
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
                pid = str(post["id"])
                if pid in rejects:
                    continue
                url = image_url(post)
                if not url:
                    continue

                bad = reject_reasons(
                    tags=(post.get("tag_string_general", "") + " "
                          + post.get("tag_string_meta", "")).split(),
                    w=post.get("image_width", 0),
                    h=post.get("image_height", 0),
                )
                if bad:
                    rejects[pid] = bad[0]
                    rejected += 1
                    print(f"  - {pid}: {bad[0]}")
                    continue

                try:
                    resp = requests.get(url, headers={"User-Agent": UA}, timeout=60)
                    resp.raise_for_status()
                    raw = resp.content
                except requests.RequestException as e:
                    print(f"  ! download failed: {e}", file=sys.stderr)
                    continue

                characters = clean_character_tags(post.get("tag_string_character", ""))

                title = (post.get("tag_string_character")
                         or post.get("tag_string_copyright")
                         or " ".join(post.get("tag_string_general", "").split()[:6])
                         or "untitled")

                # duplicate check happens inside ingest_image, before upload —
                # rejecting after the fact would delete the live file when the
                # candidate's content-addressed key collides with the original's
                item = ingest_image(
                    raw,
                    source="danbooru",
                    added=today,
                    title=title.replace("_", " "),
                    sub="danbooru",
                    author=post.get("tag_string_artist", "").replace("_", " "),
                    permalink=f"https://danbooru.donmai.us/posts/{post['id']}",
                    tags=post.get("tag_string_general", "").split()[:15],
                    character=characters,
                    existing=existing + [a.to_dict() for a in accepted],
                )
                if item is None:
                    # Either a duplicate or a pixel-level policy failure the
                    # metadata could not predict. Both mean "never fetch this
                    # post again" — a duplicate stays a duplicate, and a
                    # letterboxed image stays letterboxed.
                    rejects[pid] = "rejected after download (duplicate or pixel policy)"
                    rejected += 1
                    continue

                accepted.append(item)
                print(f"  + {item.id}  {item.w}×{item.h}  {item.title[:48]}")
                time.sleep(0.5)          # polite pacing on an anonymous, unauthenticated client

    # Written whether or not anything was accepted: a run that found only
    # rejects still learned something worth not relearning tomorrow.
    save_rejects(rejects)

    if not accepted:
        print(f"no new wallpapers ({rejected} rejected on policy, "
              f"{len(rejects)} known-bad posts skipped in future runs)")
        return 0

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    payload = {"items": existing + [a.to_dict() for a in accepted]}
    MANIFEST.write_text(json.dumps(payload, indent=1))
    print(f"\n{len(accepted)} added / {rejected} rejected on policy / "
          f"{len(payload['items'])} total")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
