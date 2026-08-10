#!/usr/bin/env python3
"""Keep the archive permanently inside its free-tier ceilings.

Two independent caps, whichever bites first:

  * **Bytes** — Cloudflare R2's free tier is 10 GB of storage. The default
    cap is 8 GB, leaving headroom so a large run can never tip you into
    billing. This is the cap that matters once images live in R2.
  * **Count** — Cloudflare Pages allows 20,000 files per deployment. Only
    relevant in local-storage mode, where images ship inside the deploy.

Oldest entries are dropped first, so the archive behaves as a rolling window
that cannot grow without bound no matter how long the sync runs.

    python scripts/prune.py --max-bytes 8589934592 --max-items 4000
    python scripts/prune.py --dry-run
"""

from __future__ import annotations

import argparse
import json

from pipeline import MANIFEST, load_items
from storage import get_storage

DEFAULT_MAX_BYTES = 8 * 1024**3      # 8 GiB, under R2's 10 GB free tier
DEFAULT_MAX_ITEMS = 4000


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
    ap.add_argument("--max-items", type=int, default=DEFAULT_MAX_ITEMS)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    items = load_items()
    if not items:
        print("manifest empty")
        return 0

    # newest first, so the tail is always the oldest material
    items.sort(key=lambda x: (x.get("added", ""), x.get("id", "")), reverse=True)

    keep, drop = [], []
    running = 0
    for it in items:
        # thumbnails are roughly a tenth of the full image; approximating
        # rather than stat-ing every object keeps this a single pass
        size = int(it.get("bytes", 0) * 1.1)
        if len(keep) >= args.max_items or running + size > args.max_bytes:
            drop.append(it)
        else:
            keep.append(it)
            running += size

    if not drop:
        print(f"{len(items)} items / {running / 1e9:.2f} GB — within caps, nothing to prune")
        return 0

    store = get_storage()
    freed = 0
    for it in drop:
        freed += int(it.get("bytes", 0) * 1.1)
        print(f"  - {it.get('id')}  {it.get('title', '')[:40]}")
        if not args.dry_run:
            for key in (it.get("file"), it.get("thumb")):
                if key:
                    store.delete(key)

    if args.dry_run:
        print(f"\nDRY RUN — would drop {len(drop)} items / ~{freed / 1e6:.1f} MB")
        return 0

    data = json.loads(MANIFEST.read_text()) if MANIFEST.exists() else {}
    data["items"] = keep
    MANIFEST.write_text(json.dumps(data, indent=1))
    print(f"\npruned {len(drop)} items / ~{freed / 1e6:.1f} MB freed / "
          f"{len(keep)} kept / {running / 1e9:.2f} GB stored")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
