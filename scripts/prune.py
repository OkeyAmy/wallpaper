#!/usr/bin/env python3
"""Cap how many wallpapers the deployed site carries.

This exists for exactly one limit: **Cloudflare Pages allows 20,000 files per
deployment.** Every wallpaper is two files (full + thumbnail), so the ceiling
is roughly 9,900 wallpapers once the site's own files are counted. At forty a
day that arrives in well under a year.

What this does NOT fix: repository size. Deleting a file removes it from the
working tree, not from git history — the blob stays in the pack forever, so
the clone keeps growing at the same rate whether this runs or not. GitHub
starts warning above 1 GB. The real answer to that is moving images out of
git and into object storage (Cloudflare R2 gives 10 GB free with no egress
charge, and the frontend only needs the URL prefix changed). Treat this
script as a Pages-limit guard and a way to keep deploys fast — not as a
solution to repo growth.

    python scripts/prune.py --max-items 2500
    python scripts/prune.py --max-items 2500 --dry-run
"""

from __future__ import annotations

import argparse
import json

from pipeline import MANIFEST, ROOT, load_items

DEFAULT_MAX = 2500


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-items", type=int, default=DEFAULT_MAX)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    items = load_items()
    if len(items) <= args.max_items:
        print(f"{len(items)} items / limit {args.max_items} — nothing to prune")
        return 0

    # newest first, so the tail is the oldest material
    items.sort(key=lambda x: (x.get("added", ""), x.get("id", "")), reverse=True)
    keep, drop = items[: args.max_items], items[args.max_items:]

    freed = 0
    for it in drop:
        for rel in (it.get("file"), it.get("thumb")):
            if not rel:
                continue
            path = ROOT / rel
            if path.exists():
                freed += path.stat().st_size
                if not args.dry_run:
                    path.unlink()
        print(f"  - {it.get('id')}  {it.get('title', '')[:40]}")

    if args.dry_run:
        print(f"\nDRY RUN — would drop {len(drop)} items / {freed / 1e6:.1f} MB")
        return 0

    data = json.loads(MANIFEST.read_text()) if MANIFEST.exists() else {}
    data["items"] = keep
    MANIFEST.write_text(json.dumps(data, indent=1))
    print(f"\npruned {len(drop)} items / {freed / 1e6:.1f} MB freed / {len(keep)} kept")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
