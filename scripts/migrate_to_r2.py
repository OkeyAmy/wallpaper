#!/usr/bin/env python3
"""One-off: move images already committed to the repo into R2.

Uploads every file the manifest references, verifies each landed, and then
optionally deletes the local copies so the working tree stops carrying them.

Note this shrinks the *working tree*, not git history — the blobs already
committed stay in the pack forever. That is fine: the point is that history
stops growing from here on.

    R2_ACCOUNT_ID=... R2_ACCESS_KEY_ID=... R2_SECRET_ACCESS_KEY=... \
    R2_BUCKET=wallpapers python scripts/migrate_to_r2.py --delete-local
"""

from __future__ import annotations

import argparse
import sys

from pipeline import ROOT, load_items
from storage import LocalStorage, get_storage


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--delete-local", action="store_true",
                    help="remove local copies once the upload is verified")
    args = ap.parse_args()

    store = get_storage()
    if isinstance(store, LocalStorage):
        print("R2 is not configured — set R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, "
              "R2_SECRET_ACCESS_KEY and R2_BUCKET first.", file=sys.stderr)
        return 1

    items = load_items()
    local = LocalStorage()
    uploaded = skipped = 0
    migrated_keys: list[str] = []

    for it in items:
        for key in (it.get("file"), it.get("thumb")):
            if not key:
                continue
            blob = local.read(key)
            if blob is None:
                print(f"  ? no local copy: {key}")
                continue
            if store.exists(key):
                skipped += 1
                migrated_keys.append(key)
                continue
            store.put(key, blob)
            uploaded += 1
            migrated_keys.append(key)
            print(f"  ↑ {key}  ({len(blob) / 1024:.0f} KB)")

    print(f"\nuploaded {uploaded}, already present {skipped}")

    if args.delete_local:
        # verify before deleting anything — a failed upload must not lose data
        missing = [k for k in migrated_keys if not store.exists(k)]
        if missing:
            print(f"! {len(missing)} objects are not in R2; keeping local copies",
                  file=sys.stderr)
            return 1
        removed = 0
        for key in migrated_keys:
            path = ROOT / key
            if path.exists():
                path.unlink()
                removed += 1
        print(f"removed {removed} local files (verified present in R2)")

    print("\nNext: set R2_PUBLIC_BASE to the bucket's public URL and rerun "
          "build_site.py so the manifest points at it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
