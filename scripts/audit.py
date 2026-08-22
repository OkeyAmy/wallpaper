#!/usr/bin/env python3
"""Re-check the published archive against current policy and its sources.

Ingest validates a post once. Things change afterwards: Danbooru moderators
re-rate, re-tag, delete and ban posts, and the policy in quality.py gets
tightened. Neither is visible until someone looks, so this looks:

  * rating drift      — post was `g` at sync, has since been re-rated s/q/e
  * deletion / ban    — post removed upstream but still served here
  * policy drift      — item no longer passes quality.py (tags, shape, pixels)

**This script does not delete anything.** It writes a list of ids and hands
it to `cull.py`, which is the one place in this repo that removes objects
from R2. That split is not tidiness: an earlier version culled directly, and
because an unreachable API counted as a reason to flag, a single network
timeout mid-run would have permanently deleted a good wallpaper — from a
manifest it rewrote with no backup. Upstream failures are now reported in
their own section and can never reach a delete path.

    python scripts/audit.py                    # full report
    python scripts/audit.py --offline          # skip the API, pixels+tags only
    python scripts/audit.py --out rejects.txt  # write flagged ids for cull.py
    python scripts/cull.py --from-list rejects.txt --dry-run
"""

from __future__ import annotations

import argparse
import io
import re
import sys
import time
from collections import Counter

import requests
from PIL import Image

from pipeline import load_items
from quality import reject_reasons
from storage import get_storage

UA = "web:okeyamy-wallpaper-audit:1.0 (by anonymous)"
POST_API = "https://danbooru.donmai.us/posts/{id}.json"


def post_id(permalink: str) -> str | None:
    m = re.search(r"/posts/(\d+)", permalink or "")
    return m.group(1) if m else None


def fetch_post(pid: str) -> dict | None:
    """Post JSON, `{"_http": code}` for a definite answer, None for no answer.

    The None case is load-bearing: it means "we do not know", and the caller
    must not turn it into a reason to remove anything.
    """
    for attempt in (0, 1):
        try:
            r = requests.get(POST_API.format(id=pid), headers={"User-Agent": UA}, timeout=30)
            if r.status_code == 429:
                time.sleep(5 + attempt * 10)
                continue
            return r.json() if r.status_code == 200 else {"_http": r.status_code}
        except requests.RequestException:
            time.sleep(2)
    return None


def load_thumb(item: dict, store) -> Image.Image | None:
    data = store.read(item["thumb"])
    if not data:
        return None
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
        return img
    except Exception:
        return None


def upstream_reasons(post: dict) -> list[str]:
    reasons = []
    rating = post.get("rating")
    if rating and rating != "g":
        reasons.append(f"re-rated to '{rating}'")
    if post.get("is_deleted"):
        reasons.append("deleted upstream")
    if post.get("is_banned"):
        reasons.append("banned upstream")
    # The manifest keeps only the first 15 tags; the live post has all of
    # them, so this can catch a policy violation the stored copy cannot show.
    tags = (post.get("tag_string_general", "") + " "
            + post.get("tag_string_meta", "")).split()
    return reasons + reject_reasons(tags=tags)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", action="store_true",
                    help="skip the Danbooru re-check; audit tags, shape and pixels only")
    ap.add_argument("--out", default="",
                    help="write flagged ids here, one per line, for cull.py --from-list")
    args = ap.parse_args()

    items = load_items()
    if not items:
        print("manifest empty")
        return 0

    store = get_storage()
    flagged: list[tuple[dict, list[str]]] = []
    unknown: list[dict] = []
    checked = 0

    for it in items:
        reasons = reject_reasons(tags=it.get("tags", []), w=it.get("w", 0), h=it.get("h", 0))

        thumb = load_thumb(it, store)
        if thumb is None:
            unknown.append((it, "thumbnail unreadable"))
        else:
            reasons += reject_reasons(img=thumb)

        pid = None if args.offline else post_id(it.get("permalink", ""))
        if pid:
            checked += 1
            post = fetch_post(pid)
            time.sleep(0.35)                     # polite pacing, anonymous client
            if post is None:
                unknown.append((it, "upstream unreachable"))
            elif "_http" in post:
                if post["_http"] == 404:
                    reasons.append("post 404s upstream")
                else:
                    unknown.append((it, f"upstream HTTP {post['_http']}"))
            else:
                reasons += upstream_reasons(post)

        if reasons:
            flagged.append((it, sorted(set(reasons))))

    counts = Counter(r.split("(")[0].split(":")[0].strip()
                     for _it, rs in flagged for r in rs)

    print(f"\n{len(items)} items audited"
          + (f" ({checked} re-checked against Danbooru)" if checked else " (offline)"))
    print(f"{len(flagged)} flagged:")
    for reason, n in counts.most_common():
        print(f"  {n:4d}  {reason}")

    print()
    for it, rs in flagged:
        print(f"  {it['id']}  {it['w']}x{it['h']}  [{it.get('source')}] {it.get('title', '')[:36]}")
        for r in rs:
            print(f"      - {r}")

    if unknown:
        print(f"\n{len(unknown)} could not be checked — NOT flagged, retry later:")
        for it, why in unknown[:20]:
            print(f"  ? {it['id']}  {why}")
        if len(unknown) > 20:
            print(f"  ... and {len(unknown) - 20} more")

    if args.out and flagged:
        with open(args.out, "w") as f:
            for it, _rs in flagged:
                f.write(it["id"] + "\n")
        print(f"\n{len(flagged)} ids -> {args.out}")
        print(f"review them, then: python scripts/cull.py --from-list {args.out} --dry-run")
    elif flagged:
        print("\nnothing removed — rerun with --out <file> to hand these to cull.py")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
