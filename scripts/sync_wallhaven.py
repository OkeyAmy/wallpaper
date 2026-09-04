#!/usr/bin/env python3
"""Pull anime wallpapers from Wallhaven — no account, no key.

A second source was wanted because Danbooru alone is one community's taste,
and because `order:score` is deterministic: once the archive has absorbed the
top pages of a tag, that axis stops finding anything. Four candidates were
measured against this archive's own suggestive-tag policy on 2026-09-04, since
"safe" means something different on every board:

    Konachan      rating:safe order:score   58% suggestive  (cleavage 78,
                                            swimsuit 48, bikini 46, nipples 6)
    yande.re      rating:safe order:score   59% suggestive  (no_bra 22)
    Wallhaven     categories=anime,sfw       9% suggestive  (cleavage 3/34)
    Safebooru     scenery                    1% suggestive
    (Danbooru     rating:general scenery     7% — the existing baseline)

Konachan and yande.re are wallpaper boards built around fanservice; their
`rating:safe` is nowhere near Danbooru's `rating:general`, so both are refused
outright rather than filtered. Safebooru scored best but is a Danbooru mirror,
so it would mostly re-fetch what the existing sync already has and be rejected
by the dhash dedup after paying for the download.

Wallhaven is the only candidate that is both clean enough and a genuinely
different corpus: a community wallpaper site rather than a booru, so every
image is already wallpaper-shaped (33 of 34 sampled were >=1920px on the long
edge, the top result 4096x2304). anime-pictures.net was also tried and
rejected — its list API returns `tags_count` but no tag *names*, which would
leave the blocklist and creative_score blind.

    python scripts/sync_wallhaven.py --dry-run --max-new 20
    python scripts/sync_wallhaven.py --max-new 20
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date

import requests

from pipeline import (
    DATA_DIR, MANIFEST, Item, ingest_image, load_items, select_tags,
)
from quality import creative_score, reject_reasons

UA = "okeyamy-wallpaper-archive/1.0 (+https://okeyamy.xyz)"
SEARCH = "https://wallhaven.cc/api/v1/search"
DETAIL = "https://wallhaven.cc/api/v1/w/{id}"

# categories is a three-bit mask over general/anime/people: "010" is anime
# only. purity "100" is sfw only — the two NSFW bits are what an API key
# would unlock, and not sending one makes them unreachable rather than merely
# unrequested. Both are enforced server-side, which is the point: the archive
# is not relying on its own tag filter to be the first line of defence.
CATEGORIES = "010"
PURITY = "100"

# Wallhaven allows roughly 45 requests a minute and answers 429 past that.
# Every accepted wallpaper costs two calls (search page, then detail for its
# tags), so the pacing here is not politeness, it is the documented limit.
REQUEST_PACING = 1.5

# Deterministic and popularity-ranked respectively, plus a random draw so the
# feed keeps finding things after the top of the list is exhausted — the same
# three-axis shape sync_danbooru.py uses, for the same reason.
QUERIES = [
    {"sorting": "toplist", "topRange": "1y", "pages": 6},
    {"sorting": "toplist", "topRange": "1M", "pages": 3},
    {"sorting": "random", "pages": 4},
]

# Wallhaven ids are short alphanumeric strings ("9oo8k1") and Danbooru's are
# integers, so they could safely share a ledger — they get their own anyway,
# because a single file keyed by two id schemes is a thing someone would later
# have to reason about for no benefit.
REJECTS = DATA_DIR / "rejected_wallhaven.json"

MIN_VIEWS = 2000          # a floor on attention, not on quality


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


def normalise(name: str) -> str:
    """Wallhaven tag names are display strings ("Digital art", "Anime girls").

    quality.py matches underscored booru-style tags as substrings, so the two
    vocabularies have to be spelled the same way before any policy applies.
    """
    return str(name or "").strip().lower().replace(" ", "_")


def get(url: str, params: dict | None = None) -> dict | None:
    try:
        r = requests.get(url, params=params, headers={"User-Agent": UA}, timeout=40)
    except requests.RequestException as e:
        print(f"  ! {url}: {e}", file=sys.stderr)
        return None
    if r.status_code == 429:
        print("  ! rate limited — backing off 60s", file=sys.stderr)
        time.sleep(60)
        return None
    if r.status_code != 200:
        print(f"  ! HTTP {r.status_code} for {url}", file=sys.stderr)
        return None
    try:
        return r.json()
    except json.JSONDecodeError:
        return None


def search_page(axis: dict, page: int) -> list[dict]:
    params = {"categories": CATEGORIES, "purity": PURITY,
              "sorting": axis["sorting"], "page": page}
    if axis.get("topRange"):
        params["topRange"] = axis["topRange"]
    body = get(SEARCH, params)
    return (body or {}).get("data", []) or []


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-new", type=int, default=20)
    ap.add_argument("--min-views", type=int, default=MIN_VIEWS)
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would be accepted without downloading, "
                         "encoding, uploading or writing the manifest")
    args = ap.parse_args()

    existing = load_items()
    rejects = load_rejects()
    accepted: list[Item] = []
    previewed: list[dict] = []
    rejected = 0
    today = date.today().isoformat()

    def taken() -> int:
        return len(previewed) if args.dry_run else len(accepted)

    for axis in QUERIES:
        if taken() >= args.max_new:
            break
        label = f"{axis['sorting']}{'/' + axis['topRange'] if axis.get('topRange') else ''}"
        print(f"── wallhaven {label}  ({axis['pages']} pages)")
        for page in range(1, axis["pages"] + 1):
            if taken() >= args.max_new:
                break
            for post in search_page(axis, page):
                if taken() >= args.max_new:
                    break
                wid = str(post.get("id") or "")
                if not wid or wid in rejects:
                    continue
                if (post.get("views") or 0) < args.min_views:
                    continue

                w = post.get("dimension_x", 0)
                h = post.get("dimension_y", 0)

                # Shape is decided from the search response, before spending a
                # detail call on tags — the cheapest possible rejection.
                shape_bad = reject_reasons(w=w, h=h)
                if shape_bad:
                    rejects[wid] = shape_bad[0]
                    rejected += 1
                    continue

                time.sleep(REQUEST_PACING)
                detail = (get(DETAIL.format(id=wid)) or {}).get("data") or {}
                tags = [normalise(t.get("name")) for t in detail.get("tags", [])]
                if not tags:
                    # No tags means no policy can be applied. Refusing is the
                    # conservative reading: purity=sfw is Wallhaven's judgement,
                    # and this archive does not publish on that alone.
                    rejects[wid] = "no tags returned"
                    rejected += 1
                    continue

                bad = reject_reasons(tags=tags, w=w, h=h)
                if bad:
                    rejects[wid] = bad[0]
                    rejected += 1
                    print(f"  - {wid}: {bad[0]}")
                    continue

                views = detail.get("views") or post.get("views") or 0
                # `favourites` is not returned to a keyless client, so views are
                # the only community signal available. They run about two orders
                # of magnitude above a Danbooru score (a well-liked wallpaper
                # here sits around 150k), and creative_score saturates at 300, so
                # they are divided into the same range rather than fed in raw —
                # otherwise every wallpaper would max the term and it would carry
                # no information at all.
                pseudo_score = views // 500
                rank = creative_score(tags, w=w, h=h,
                                      score=pseudo_score, fav_count=pseudo_score)

                if args.dry_run:
                    previewed.append({"id": wid, "creative": rank, "views": views,
                                      "favs": pseudo_score, "wh": f"{w}x{h}",
                                      "url": post.get("url", "")})
                    print(f"  ? {wid}  creative={rank:.2f}  views={views}  {w}×{h}")
                    continue

                url = post.get("path")
                if not url:
                    continue
                try:
                    resp = requests.get(url, headers={"User-Agent": UA}, timeout=90)
                    resp.raise_for_status()
                    raw = resp.content
                except requests.RequestException as e:
                    print(f"  ! download failed: {e}", file=sys.stderr)
                    continue

                title = " ".join(t.replace("_", " ") for t in tags[:5]) or "anime wallpaper"

                item = ingest_image(
                    raw,
                    source="wallhaven",
                    added=today,
                    title=title,
                    sub="wallhaven",
                    author="",
                    permalink=post.get("url", f"https://wallhaven.cc/w/{wid}"),
                    tags=select_tags(tags),
                    character=[],
                    existing=existing + [a.to_dict() for a in accepted],
                    score=pseudo_score,
                    fav_count=pseudo_score,
                    creative=rank,
                )
                if item is None:
                    rejects[wid] = "rejected after download (duplicate or pixel policy)"
                    rejected += 1
                    continue

                accepted.append(item)
                print(f"  + {item.id}  {item.w}×{item.h}  creative={item.creative:.2f}"
                      f"  {item.title[:40]}")
                time.sleep(REQUEST_PACING)

    if args.dry_run:
        if not previewed:
            print("\nnothing would be accepted")
            return 0
        ranks = sorted(p["creative"] for p in previewed)
        print(f"\n{len(previewed)} would be accepted / {rejected} rejected on policy")
        print(f"creative rank: min {ranks[0]:.2f}  median {ranks[len(ranks) // 2]:.2f}  "
              f"max {ranks[-1]:.2f}")
        for p in sorted(previewed, key=lambda x: -x["creative"])[:10]:
            print(f"  {p['creative']:.2f}  {p['wh']:>10}  {p['url']}")
        return 0

    save_rejects(rejects)

    if not accepted:
        print(f"no new wallpapers ({rejected} rejected on policy, "
              f"{len(rejects)} known-bad ids skipped in future runs)")
        return 0

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    payload = {"items": existing + [a.to_dict() for a in accepted]}
    MANIFEST.write_text(json.dumps(payload, indent=1))
    print(f"\n{len(accepted)} added / {rejected} rejected on policy / "
          f"{len(payload['items'])} total")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
