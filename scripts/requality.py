#!/usr/bin/env python3
"""Re-encode already-stored Danbooru wallpapers at the current FULL_QUALITY.

The archive was built at WebP q82, which on a 4 MB original was storing about
228 KB — enough loss to show in gradients and fine linework. Raising
FULL_QUALITY only helps images ingested afterwards, so this re-fetches the
originals that are still reachable and replaces the stored WebP in place.

Only Danbooru items qualify: their `permalink` still resolves to the original
file. Locally-ingested images can't be improved this way — `incoming/` is
consumed on ingest, so their originals are gone and q82 is all there is.

Nothing about an item's identity changes. The key is `sha256(original)[:12]`,
derived from bytes this script re-downloads rather than re-encodes, so the id,
filename and URL stay exactly as they were and only the object behind them
gets better. That sha256 is also checked against the manifest before anything
is written: a Danbooru post whose file has been replaced would otherwise
silently swap one wallpaper for a different picture at the same URL.

    python scripts/requality.py --dry-run
    python scripts/requality.py --limit 5      # try a few first
    python scripts/requality.py

Needs the R2_* variables set, same as the sync scripts.
"""

from __future__ import annotations

import argparse
import io
import json
import sys
import time

import requests
from PIL import Image

from pipeline import (
    FULL_QUALITY, MANIFEST, MAX_EDGE, _encode, sha256_bytes,
)
from storage import get_storage

UA = "web:okeyamy-wallpaper-archive:1.0 (by anonymous)"
POST_API = "https://danbooru.donmai.us/posts/{id}.json"


def post_id(permalink: str) -> str | None:
    import re
    m = re.search(r"/posts/(\d+)", permalink or "")
    return m.group(1) if m else None


def fetch_original(pid: str) -> bytes | None:
    try:
        r = requests.get(POST_API.format(id=pid), headers={"User-Agent": UA}, timeout=30)
        if r.status_code != 200:
            print(f"    ! post {pid} -> HTTP {r.status_code}", file=sys.stderr)
            return None
        post = r.json()
        url = post.get("file_url") or post.get("large_file_url")
        if not url:
            print(f"    ! post {pid} has no downloadable file", file=sys.stderr)
            return None
        resp = requests.get(url, headers={"User-Agent": UA}, timeout=60)
        resp.raise_for_status()
        return resp.content
    except (requests.RequestException, ValueError) as e:
        print(f"    ! post {pid}: {e}", file=sys.stderr)
        return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="report without uploading or writing")
    ap.add_argument("--limit", type=int, default=0, help="stop after this many items (0 = all)")
    args = ap.parse_args()

    doc = json.loads(MANIFEST.read_text())
    items = doc.get("items", [])
    todo = [it for it in items if it.get("source") == "danbooru" and it.get("permalink")]

    print(f"{len(items)} items, {len(todo)} re-encodable (Danbooru with a permalink)")
    print(f"target quality: WebP q{FULL_QUALITY}\n")

    store = None if args.dry_run else get_storage()
    done = skipped = 0
    before_bytes = after_bytes = 0

    for it in todo:
        if args.limit and done >= args.limit:
            break

        pid = post_id(it["permalink"])
        if not pid:
            continue

        raw = fetch_original(pid)
        time.sleep(0.5)                     # polite pacing, anonymous client
        if raw is None:
            skipped += 1
            continue

        # the post's file must still be the one this item was made from, or
        # we would be publishing a different image under an unchanged URL
        if it.get("sha256") and sha256_bytes(raw) != it["sha256"]:
            print(f"  ! {it['id']}  original has changed upstream — left alone")
            skipped += 1
            continue

        try:
            img = Image.open(io.BytesIO(raw))
            img.load()
        except Exception:
            print(f"  ! {it['id']}  original will not decode — left alone")
            skipped += 1
            continue

        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        if max(img.width, img.height) > MAX_EDGE:
            img.thumbnail((MAX_EDGE, MAX_EDGE), Image.Resampling.LANCZOS)

        new_bytes = _encode(img, FULL_QUALITY)
        old = it.get("bytes", 0)

        # a re-encode that comes out smaller means the stored copy was already
        # at least this good; replacing it would spend a write to lose detail
        if len(new_bytes) <= old:
            print(f"  = {it['id']}  {old/1024:.0f} KB already >= q{FULL_QUALITY} — left alone")
            skipped += 1
            continue

        print(f"  ~ {it['id']}  {img.width}x{img.height}  "
              f"{old/1024:.0f} KB -> {len(new_bytes)/1024:.0f} KB")

        before_bytes += old
        after_bytes += len(new_bytes)
        done += 1

        if not args.dry_run:
            store.put(it["file"], new_bytes)
            it["bytes"] = len(new_bytes)

    verb = "would re-encode" if args.dry_run else "re-encoded"
    print(f"\n{verb} {done} / skipped {skipped}")
    if done:
        print(f"storage {before_bytes/1e6:.1f} MB -> {after_bytes/1e6:.1f} MB "
              f"for those items ({after_bytes/max(before_bytes,1):.2f}x)")

    if args.dry_run:
        print("dry run — nothing uploaded, manifest untouched")
        return 0

    if done:
        doc["items"] = items
        MANIFEST.write_text(json.dumps(doc, indent=1))
        print("manifest updated — run scripts/build_site.py next")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
