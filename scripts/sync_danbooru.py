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
    select_tags,
)
from quality import creative_score, reject_reasons

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
# The anonymous tag budget, measured directly against the API on 2026-09-03
# rather than inferred: `rating:` is free, but **`order:` costs a slot**. So a
# query may carry either two real tags, or one real tag and an `order:`.
# Anything more is a hard HTTP 422 — `scenery no_humans order:score` fails, and
# negation is no cheaper, so `-1girl` is not available at any price.
#
# Which tag to spend the budget on was settled by rendering the top 12 results
# of each candidate as a contact sheet and looking at them:
#
#   rating:general scenery   order:score -> 10/12 usable
#   rating:general no_humans order:score ->  4/12 usable  (a Pingu screencap, a
#                                            cat on white, pillow sketches, a
#                                            cake render, a subtitled meme)
#   rating:general sky       order:score ->  ~1/4  (a western comic strip)
#   rating:general building  order:score ->  ~1/4  (a *photograph of a man in a
#                                            hotel room*, score 248)
#   rating:general night|nature order:score -> ~1/4, and returning the same
#                                            posts as `sky` — the ambient tags
#                                            overlap each other heavily, so the
#                                            old ten-query list was spending its
#                                            page budget re-fetching duplicates.
#
# `scenery` on Danbooru means "this picture has a drawn environment", which is
# exactly the property wanted, and it does not exclude people — the strongest
# results were a lone figure small inside a large scene. `sky`, `night`,
# `building` and `nature` are ambient tags that sit on ordinary character art,
# which is how an archive queried entirely for landscapes ended up 89% portraits.
#
# Depth on a good tag beats breadth across bad ones, so the list is now short
# and paged deep. Each axis carries its own floor because their score
# distributions are nothing alike: `order:score` page 5 still has a median of
# 61, while the unordered newest-first axis has a median of 0–2, and one global
# MIN_SCORE would either admit everything from the first or reject all of the
# second.
QUERIES = [
    # Every anime, no list to maintain. `rating:general order:score` spends the
    # whole budget on `order:` and names no tag at all, which is the point:
    # measured 2026-09-04, one page of 40 carries **27 distinct series**, page 5
    # carries 25, and suggestive hits were 2 and 0 respectively.
    #
    # This axis exists because the archive was asked for Naruto, One Piece "and
    # other interesting animes", and the honest way to serve that is not a
    # hand-written list of franchises — there are thousands, the popular ones
    # change, and any list is a guess about taste that goes stale. Ranking the
    # whole safe corpus by community vote returns the series people actually
    # care about this year, whichever those turn out to be, and costs one query.
    #
    # Note the floor: this axis reaches scores of 1719, so 150 still leaves
    # plenty of depth while keeping the bar far above the per-series feeds.
    {"tags": "rating:general order:score", "pages": 10, "min_score": 150},
    # What is being voted on *now* rather than all-time. Scores here are tiny
    # (5..36 measured) because the posts are days old, so the floor is nominal
    # and the quality gate carries it. Without this the archive only ever sees
    # art that has already had years to accumulate votes.
    {"tags": "rating:general order:rank", "pages": 3, "min_score": 4},
    # Proven spine for landscapes. Eight pages of 40 is the usable depth.
    {"tags": "rating:general scenery order:score", "pages": 8, "min_score": 60},
    # `order:score` is deterministic: run it daily and it returns the same top
    # posts until the archive has absorbed them. This axis samples the whole
    # corpus instead, so the feed keeps finding things after the top pages are
    # exhausted. Each "page" here is an independent draw, not an offset.
    # (`rating:general order:random` with no tag is not available — Danbooru
    # 500s on randomising a set that large for an anonymous client.)
    {"tags": "rating:general scenery order:random", "pages": 4, "min_score": 40},
    # Two real tags, which costs the `order:` slot and leaves the default sort
    # (newest first). Nothing here has had time to accumulate votes, so the
    # floor is nominal and the quality gate does the work — this is the only
    # axis that can ever surface art posted this week.
    {"tags": "rating:general no_humans scenery", "pages": 3, "min_score": 3},
]

MIN_SCORE = 60         # default floor; each axis above may override it

# --- series coverage -------------------------------------------------------
# `rating:general order:score` above is broad but not neutral: measured over
# five pages it returned 96 posts across 115 series, and the head of that
# distribution was Genshin, Zenless Zone Zero, Blue Archive, Fate and Hololive.
# Rendering those results as a contact sheet showed the problem plainly — no
# Naruto, no One Piece, and a McDonald's advert. Danbooru's global vote ranking
# reflects who votes on Danbooru, which is gacha and idol fandom.
#
# Naming the wanted series in a list here was the obvious fix and is the wrong
# one: there are thousands of anime, the popular ones turn over every season,
# and a hand-written list is a guess about taste that is stale the day it ships.
#
# So the list is asked for instead of assumed. Danbooru's own tag index, ordered
# by post count, *is* the ranking of which series people draw — `one_piece` sits
# at ~55k posts, `jojo_no_kimyou_na_bouken` at ~55k, `boku_no_hero_academia` at
# ~44k. Reading it costs one request, needs no key, and re-orders itself as
# fandoms rise and fall without anyone editing this file.
SERIES_API = "https://danbooru.donmai.us/tags.json"
SERIES_CACHE = DATA_DIR / "series.json"
SERIES_CACHE_DAYS = 7
# 200 rather than 100 because the first hundred stops just short of several
# series that were specifically wanted — `kimetsu_no_yaiba`, `hunter_x_hunter`
# and `spy_x_family` all rank below 100 on raw post count, since Danbooru's
# volume skews to gacha and idol fandoms rather than shonen.
SERIES_POOL = 200
SERIES_PER_RUN = 10        # how many to query on any one run; full cycle 20 days
SERIES_MIN_SCORE = 60

# Series axes run first, so without a cap they would spend the entire --max-new
# budget every day and the scenery and global axes would never execute — the
# mirror image of the original bug, where scenery ran first and the rest never
# got a turn. Half the run is reserved for everything else.
SERIES_BUDGET_SHARE = 0.5

# `original` is the largest copyright tag (1.5M posts) but is not a series, and
# Danbooru 500s when sorting a set that large for an anonymous client anyway.
SERIES_SKIP = {"original"}


def fetch_series() -> list[str]:
    """The most-drawn series on Danbooru, most first, cached for a week.

    Cached because this is a slow-moving ranking — the top hundred series do
    not reorder day to day — and a daily sync should not spend a request
    re-learning something that changes on the scale of seasons.
    """
    if SERIES_CACHE.exists():
        try:
            blob = json.loads(SERIES_CACHE.read_text())
            fetched = date.fromisoformat(blob.get("fetched", "1970-01-01"))
            if (date.today() - fetched).days < SERIES_CACHE_DAYS and blob.get("series"):
                return blob["series"]
        except (json.JSONDecodeError, ValueError):
            pass

    try:
        r = requests.get(SERIES_API, params={
            "search[category]": 3,          # 3 = copyright
            "search[order]": "count",
            "limit": SERIES_POOL + len(SERIES_SKIP),
        }, headers={"User-Agent": UA}, timeout=30)
        r.raise_for_status()
        rows = r.json()
    except (requests.RequestException, json.JSONDecodeError) as e:
        print(f"  ! series list unavailable ({e}) — continuing without", file=sys.stderr)
        return []

    names = [t["name"] for t in rows
             if isinstance(t, dict) and t.get("name") not in SERIES_SKIP][:SERIES_POOL]
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SERIES_CACHE.write_text(json.dumps(
        {"fetched": date.today().isoformat(), "series": names}, indent=1))
    return names


def series_axes(today: date) -> list[dict]:
    """Today's slice of the series rotation.

    Querying a hundred series every run would be a hundred requests for a feed
    that only accepts a few dozen images, so each run takes a window and the
    window advances with the date. The whole pool is covered every
    ``SERIES_POOL / SERIES_PER_RUN`` days without storing a cursor, which means
    a missed or repeated run cannot corrupt the rotation.
    """
    pool = fetch_series()
    if not pool:
        return []
    slots = max(1, len(pool) // SERIES_PER_RUN)
    start = (today.toordinal() % slots) * SERIES_PER_RUN
    window = (pool + pool)[start:start + SERIES_PER_RUN]
    return [{"tags": f"rating:general {name} order:score", "pages": 2,
             "min_score": SERIES_MIN_SCORE, "group": "series"} for name in window]

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
    ap.add_argument("--min-score", type=int, default=0,
                    help="override every axis floor (0 = use per-axis floors)")
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would be accepted without downloading, "
                         "encoding, uploading or writing the manifest")
    ap.add_argument("--no-series", action="store_true",
                    help="skip the rotating per-series axes and run only the "
                         "fixed global/scenery queries")
    args = ap.parse_args()

    existing = load_items()
    rejects = load_rejects()
    accepted: list[Item] = []
    previewed: list[dict] = []
    rejected = 0
    today = date.today().isoformat()

    def taken() -> int:
        return len(previewed) if args.dry_run else len(accepted)

    # Series first: the fixed axes below are deep and would otherwise consume
    # the whole --max-new budget before the rotation ever ran, which is how the
    # archive ended up with no shonen in it in the first place.
    axes = ([] if args.no_series else series_axes(date.today())) + QUERIES
    if not args.no_series:
        names = [a["tags"].split()[1] for a in axes[:SERIES_PER_RUN]]
        print(f"series rotation today: {', '.join(names) or '(unavailable)'}\n")

    series_cap = max(1, int(args.max_new * SERIES_BUDGET_SHARE))
    series_taken = 0
    # Spread the series budget across the whole window instead of letting it be
    # drained by whichever series happens to sort first. Without this the run
    # stops partway down the rotation — an observed dry run listed one_piece and
    # jojo in the day's window and then never queried either, because the two
    # series ahead of them had already taken the group's entire allowance.
    per_series = max(2, series_cap // max(1, SERIES_PER_RUN))

    for axis in axes:
        if taken() >= args.max_new:
            break
        if axis.get("group") == "series" and series_taken >= series_cap:
            continue
        before = taken()
        tags = axis["tags"]
        floor = args.min_score or axis["min_score"]
        cap = per_series if axis.get("group") == "series" else args.max_new

        # `before` and `cap` are bound as defaults rather than closed over:
        # a closure would read whatever the loop variables hold at call time,
        # so hoisting this definition out of the loop later would silently
        # disable the per-series cap instead of failing.
        def full(before: int = before, cap: int = cap) -> bool:
            return taken() >= args.max_new or (taken() - before) >= cap

        print(f"── {tags}   (floor {floor}, {axis['pages']} pages)")
        for page in range(1, axis["pages"] + 1):
            if full():
                break
            for post in fetch(tags, page, limit=40):
                if full():
                    break
                if post.get("score", 0) < floor:
                    continue
                pid = str(post["id"])
                if pid in rejects:
                    continue
                url = image_url(post)
                if not url:
                    continue

                full_tags = (post.get("tag_string_general", "") + " "
                             + post.get("tag_string_meta", "")).split()

                bad = reject_reasons(
                    tags=full_tags,
                    w=post.get("image_width", 0),
                    h=post.get("image_height", 0),
                )
                if bad:
                    rejects[pid] = bad[0]
                    rejected += 1
                    print(f"  - {pid}: {bad[0]}")
                    continue

                # Scored here, where the *complete* tag list is in hand. After
                # ingest only a selected subset survives on the item, so this
                # number can never be reproduced from the manifest alone.
                rank = creative_score(
                    full_tags,
                    w=post.get("image_width", 0), h=post.get("image_height", 0),
                    score=post.get("score", 0),
                    fav_count=post.get("fav_count", 0),
                )

                if args.dry_run:
                    previewed.append({
                        "id": pid, "score": post.get("score", 0),
                        "creative": rank,
                        "wh": f'{post.get("image_width")}x{post.get("image_height")}',
                        "url": f"https://danbooru.donmai.us/posts/{pid}",
                    })
                    print(f"  ? {pid}  creative={rank:.2f}  score={post.get('score')}"
                          f"  {post.get('image_width')}×{post.get('image_height')}")
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
                    # Relevance-ranked, not alphabetically truncated — see
                    # select_tags(). The old [:15] slice dropped `scenery` from
                    # every post it ever stored.
                    tags=select_tags(post.get("tag_string_general", "").split()),
                    character=characters,
                    existing=existing + [a.to_dict() for a in accepted],
                    score=post.get("score", 0),
                    fav_count=post.get("fav_count", 0),
                    creative=rank,
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
                print(f"  + {item.id}  {item.w}×{item.h}  creative={item.creative:.2f}"
                      f"  {item.title[:40]}")
                time.sleep(0.5)          # polite pacing on an anonymous, unauthenticated client

        if axis.get("group") == "series":
            series_taken += taken() - before

    if args.dry_run:
        # Nothing is persisted, not even the reject ledger: a dry run that
        # recorded rejects would make the next real run skip posts it never
        # actually examined.
        if not previewed:
            print("\nnothing would be accepted")
            return 0
        ranks = sorted(p["creative"] for p in previewed)
        mid = ranks[len(ranks) // 2]
        print(f"\n{len(previewed)} would be accepted / {rejected} rejected on policy")
        print(f"creative rank: min {ranks[0]:.2f}  median {mid:.2f}  max {ranks[-1]:.2f}")
        for p in sorted(previewed, key=lambda x: -x["creative"])[:10]:
            print(f"  {p['creative']:.2f}  s{p['score']:<4} {p['wh']:>10}  {p['url']}")
        return 0

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
