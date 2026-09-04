#!/usr/bin/env python3
"""Re-fetch full tags for already-ingested Danbooru items and rank them.

Every item synced before 2026-09-03 stored `tag_string_general.split()[:15]`.
Danbooru returns tags alphabetically, so that slice was not a cap but a bias:
it kept `1girl`, `animal_ears`, `blonde_hair`, `blush`, `breasts` and dropped
everything from roughly "s" onward — `scenery`, `sky`, `solo`, `screencap`,
`sketch`, `watermark`, `subtitled`, and every `*_background` tag.

Two consequences worth repairing:

  * `audit.py` re-checks the archive against TAG_BLOCKLIST using stored tags,
    so it has been blind to more than half of its own blocklist. An item that
    is a screencap or a sketch could pass ingest and then never be flagged.
  * `build_site.py` builds tag hubs from stored tags, which is why the site's
    facets are hair colours and body parts. The metadata made it read as a
    character-portrait gallery independently of the pictures in it.

This restores the real tags, records `creative` (see quality.creative_score)
and the upstream vote counts, and reports what changed.

**It never deletes anything, and by default it does not even write.** The
reason is specific: restoring full tags makes every previously-invisible
blocklist hit visible *at once*, across the whole archive. `maintain.yml` runs
`audit.py` into `cull.py` weekly, and R2 deletion is not recoverable — the
2026-08-22 incident lost 32 files permanently because the CDN cache was empty.
`cull.py --max-remove` fails the job rather than over-deleting, so the blast is
contained, but the sequence still has to be deliberate:

    python scripts/rescore.py                     # look at the delta first
    python scripts/rescore.py --apply             # then write the manifest
    python scripts/audit.py --out flagged.txt     # then see what it flags
    python scripts/cull.py --from-list flagged.txt --dry-run

Only after reading that list should anything be removed.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections import Counter

import requests

from pipeline import MANIFEST, load_items, select_tags
from quality import creative_score, tag_reasons

UA = "web:okeyamy-wallpaper-archive:1.0 (by anonymous)"
POST_API = "https://danbooru.donmai.us/posts/{id}.json"


def post_id(permalink: str) -> str | None:
    m = re.search(r"/posts/(\d+)", permalink or "")
    return m.group(1) if m else None


def fetch_post(pid: str) -> dict | None:
    try:
        r = requests.get(POST_API.format(id=pid),
                         headers={"User-Agent": UA}, timeout=30)
    except requests.RequestException:
        return None
    if r.status_code != 200:
        return None
    try:
        post = r.json()
    except json.JSONDecodeError:
        return None
    return post if isinstance(post, dict) else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="write the enriched tags back to the manifest "
                         "(default is report-only)")
    ap.add_argument("--limit", type=int, default=0,
                    help="only process the first N Danbooru items")
    ap.add_argument("--out", default="",
                    help="write ids scoring below --floor to this file, "
                         "lowest first. Written for review; nothing reads it "
                         "automatically and cull.py is not invoked.")
    ap.add_argument("--floor", type=float, default=0.30,
                    help="creative rank below which an item is listed by --out")
    args = ap.parse_args()

    items = load_items()
    targets = [i for i in items if post_id(i.get("permalink", ""))]
    if args.limit:
        targets = targets[:args.limit]

    # Hand-dropped items have no permalink, so their tags cannot be restored
    # and their creative rank stays 0.0. That is not a quality judgement about
    # them — it is the absence of any evidence to judge on, and it is exactly
    # why cull.py refuses sources it cannot re-fetch.
    skipped = len(items) - len(targets)
    print(f"{len(targets)} Danbooru items to re-fetch "
          f"({skipped} local items skipped — no permalink, not scorable)\n")

    by_id = {i["id"]: i for i in items}
    ranks: list[tuple[float, str, str]] = []
    newly_flagged: list[tuple[str, str, str]] = []
    reasons = Counter()
    gone = 0
    changed = 0

    for n, it in enumerate(targets, 1):
        pid = post_id(it["permalink"])
        post = fetch_post(pid)
        if post is None or post.get("is_deleted"):
            gone += 1
            continue

        full = (post.get("tag_string_general", "") + " "
                + post.get("tag_string_meta", "")).split()
        if not full:
            continue

        rank = creative_score(
            full, w=it.get("w", 0), h=it.get("h", 0),
            score=post.get("score", 0), fav_count=post.get("fav_count", 0),
        )

        before = set(it.get("tags", []))
        after = select_tags(full)
        if set(after) != before:
            changed += 1

        # Policy hits that the truncated tag list could not have shown.
        hidden = tag_reasons([t for t in full if t not in before])
        if hidden:
            newly_flagged.append((it["id"], it["permalink"], "; ".join(hidden)))
            for r in hidden:
                reasons[r.split(":")[0].split(",")[0]] += 1

        target = by_id[it["id"]]
        target["tags"] = after
        target["score"] = post.get("score", 0)
        target["fav_count"] = post.get("fav_count", 0)
        target["creative"] = rank
        ranks.append((rank, it["id"], it.get("title", "")[:44]))

        if n % 25 == 0:
            print(f"  {n}/{len(targets)} ...", file=sys.stderr)
        time.sleep(0.4)          # anonymous client, no key — stay polite

    if not ranks:
        print("nothing re-fetched")
        return 0

    ranks.sort()
    vals = [r for r, _, _ in ranks]
    mid = vals[len(vals) // 2]
    below = [r for r in vals if r < args.floor]

    print(f"\nre-fetched {len(ranks)}  ({gone} deleted upstream, "
          f"{changed} with corrected tags)")
    print(f"creative rank: min {vals[0]:.2f}  median {mid:.2f}  max {vals[-1]:.2f}")
    print(f"below {args.floor:.2f}: {len(below)} items "
          f"({len(below) / len(vals):.0%} of the re-fetched archive)")

    print(f"\nnewly visible policy hits (invisible to audit.py until now): "
          f"{len(newly_flagged)}")
    for reason, count in reasons.most_common(12):
        print(f"  {count:4}  {reason}")

    print("\nlowest ranked:")
    for rank, ident, title in ranks[:10]:
        print(f"  {rank:.2f}  {ident}  {title}")
    print("highest ranked:")
    for rank, ident, title in reversed(ranks[-10:]):
        print(f"  {rank:.2f}  {ident}  {title}")

    if args.out:
        lines = [ident for rank, ident, _ in ranks if rank < args.floor]
        with open(args.out, "w") as fh:
            fh.write("\n".join(lines) + ("\n" if lines else ""))
        print(f"\n{len(lines)} ids -> {args.out}  (review before culling; "
              f"nothing has been deleted)")

    if args.apply:
        MANIFEST.write_text(json.dumps({"items": items}, indent=1))
        print(f"\nmanifest updated — {changed} items have corrected tags.")
        print("audit.py can now see the full blocklist. Run it with "
              "cull.py --dry-run before letting maintain.yml near this.")
    else:
        print("\nreport only — manifest untouched. Re-run with --apply to write.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
