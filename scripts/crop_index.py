#!/usr/bin/env python3
"""Bring the crop-duplicate index (see cropmatch.py) up to date.

Adds a fingerprint for every manifest item that lacks one, computed from its
thumbnail in storage. The first run covers the whole back catalogue; after
that it only fills gaps — an item whose sync crashed before the index was
saved, or one added by a script that doesn't save it. Runs before the syncs in
sync.yml, so new candidates are always compared against everything published.

    python scripts/crop_index.py
    python scripts/crop_index.py --dry-run     # count what is missing
"""

from __future__ import annotations

import argparse
import io
import sys

from PIL import Image

import cropmatch
from pipeline import load_items
from storage import get_storage


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    store = get_storage()
    idx = cropmatch.index(store)
    if not idx.writable:
        # Already explained by Index.load. Exit cleanly: the sync can still
        # run, it just won't catch crops this time.
        return 0

    missing = [it for it in load_items() if it["id"] not in idx]
    print(f"crop index: {len(idx)} stored, {len(missing)} missing")
    if args.dry_run or not missing:
        return 0

    failed = 0
    for n, it in enumerate(missing, 1):
        raw = store.read(it["thumb"])
        if not raw:
            failed += 1
            continue
        try:
            with Image.open(io.BytesIO(raw)) as img:
                img.load()
                idx.add(it["id"], img)
        except Exception as e:                  # noqa: BLE001 — skip, retry next run
            print(f"  ! {it['id']}: {e}", file=sys.stderr)
            failed += 1
        if n % 200 == 0:
            print(f"  {n}/{len(missing)}")
            idx.save(store)                     # checkpoint long first runs

    idx.save(store)
    print(f"added {len(missing) - failed}, {failed} unreadable (retried next run)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
