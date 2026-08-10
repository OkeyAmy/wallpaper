#!/usr/bin/env python3
"""Ingest wallpapers you dropped into ./incoming/ yourself.

    cp ~/Pictures/nice.png incoming/
    python scripts/ingest_local.py

Accepted files are converted, indexed and deleted from incoming/ so the
folder stays a queue rather than a second copy of the archive.
"""

from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path

from pipeline import DATA_DIR, MANIFEST, ROOT, discard, ingest_image, is_duplicate, load_items

INCOMING = ROOT / "incoming"
SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tiff"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true", help="don't delete source files after ingest")
    ap.add_argument("--tag", action="append", default=[], help="tag to attach (repeatable)")
    args = ap.parse_args()

    INCOMING.mkdir(exist_ok=True)
    files = sorted(p for p in INCOMING.iterdir() if p.suffix.lower() in SUFFIXES)
    if not files:
        print(f"nothing queued in {INCOMING.relative_to(ROOT)}/")
        return 0

    existing = load_items()
    added = []
    today = date.today().isoformat()

    for path in files:
        item = ingest_image(
            path.read_bytes(),
            source="local",
            added=today,
            title=path.stem.replace("_", " ").replace("-", " "),
            tags=args.tag,
        )
        if item is None:
            print(f"  ! skipped (too small or unreadable): {path.name}")
            continue

        if is_duplicate(item, existing + [a.to_dict() for a in added]):
            discard(item)
            print(f"  = duplicate, skipped: {path.name}")
            continue

        added.append(item)
        print(f"  + {item.id}  {item.w}×{item.h}  {path.name}")
        if not args.keep:
            path.unlink()

    if not added:
        print("no new wallpapers")
        return 0

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    payload = {"items": existing + [a.to_dict() for a in added]}
    MANIFEST.write_text(json.dumps(payload, indent=1))
    print(f"\n{len(added)} added / {len(payload['items'])} total")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
