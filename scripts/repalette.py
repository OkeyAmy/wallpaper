#!/usr/bin/env python3
"""Recompute palettes for everything already in the archive.

Run this after changing the palette algorithm — it re-reads the stored WebP
files, so nothing is re-downloaded and no image is re-encoded.

    python scripts/repalette.py
"""

from __future__ import annotations

import json

from PIL import Image

from pipeline import MANIFEST, ROOT, extract_palette, load_items


def main() -> int:
    items = load_items()
    if not items:
        print("manifest empty")
        return 0

    changed = 0
    for it in items:
        path = ROOT / it["file"]
        if not path.exists():
            print(f"  ! missing file, skipped: {it['file']}")
            continue
        with Image.open(path) as img:
            new = extract_palette(img)
        if new != it.get("palette"):
            print(f"  ~ {it['id']}  {it.get('palette', [])[:3]} -> {new[:3]}")
            it["palette"] = new
            changed += 1

    if changed:
        data = json.loads(MANIFEST.read_text())
        data["items"] = items
        MANIFEST.write_text(json.dumps(data, indent=1))
    print(f"\n{changed} palettes updated / {len(items)} total")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
