#!/usr/bin/env python3
"""Ingest wallpapers you dropped into ./incoming/ yourself.

    cp ~/Pictures/nice.png incoming/
    python scripts/ingest_local.py

Accepted files are converted, indexed and deleted from incoming/ so the
folder stays a queue rather than a second copy of the archive.

To credit wherever an image actually came from (someone sent it to you, you
saved it from a post, etc.), drop a same-named sidecar file next to it:

    nice.png
    nice.png.source        <- plain text

    https://twitter.com/artist/status/12345
    artist_handle

Line 1 is the source link (required), line 2 is the artist/handle (optional).
The sidecar's name must match the image's name *exactly*, including the
extension (nice.png -> nice.png.source, not nice.jpg.source) — a mismatch
isn't treated as an error, since a missing sidecar is a normal, silent case
when a wallpaper simply has no source. To catch a rename typo instead of
losing the credit silently, watch the run's output for a "no .source found"
line, and check for a leftover "orphaned .source file" warning at the end.

For a whole batch that shares one source (e.g. a folder of forwards from the
same channel), skip per-image sidecars and pass it once:

    python scripts/ingest_local.py --source https://t.me/somechannel --author "some channel"

A per-image .source sidecar still wins over these if both are present.
"""

from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path

from pipeline import DATA_DIR, MANIFEST, ROOT, ingest_image, load_items

INCOMING = ROOT / "incoming"
SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tiff"}


def read_sidecar(image_path: Path) -> tuple[str, str]:
    """(permalink, author) from a `<image>.source` sidecar, or ("", "") if none."""
    sidecar = image_path.with_name(image_path.name + ".source")
    if not sidecar.exists():
        return "", ""
    lines = [ln.strip() for ln in sidecar.read_text().splitlines() if ln.strip()]
    permalink = lines[0] if lines else ""
    author = lines[1] if len(lines) > 1 else ""
    if permalink and not permalink.startswith(("http://", "https://")):
        print(f"  ! ignoring malformed source link in {sidecar.name}: {permalink!r}")
        return "", author
    return permalink, author


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true", help="don't delete source files after ingest")
    ap.add_argument("--tag", action="append", default=[], help="tag to attach (repeatable)")
    ap.add_argument("--source", default="", help="fallback source link for images with no .source sidecar")
    ap.add_argument("--author", default="", help="fallback author/handle, used the same way")
    args = ap.parse_args()

    if args.source and not args.source.startswith(("http://", "https://")):
        print(f"! --source must be a URL, got: {args.source!r}")
        return 1

    INCOMING.mkdir(exist_ok=True)
    files = sorted(p for p in INCOMING.iterdir() if p.suffix.lower() in SUFFIXES)
    if not files:
        print(f"nothing queued in {INCOMING.relative_to(ROOT)}/")
        return 0

    existing = load_items()
    added = []
    today = date.today().isoformat()

    for path in files:
        permalink, author = read_sidecar(path)
        permalink = permalink or args.source
        author = author or args.author
        sidecar = path.with_name(path.name + ".source")

        # duplicate check happens inside ingest_image, before anything is
        # uploaded — a duplicate's content-addressed key would otherwise
        # collide with the original's, so rejecting after upload would
        # delete the live file instead of a copy of it
        item = ingest_image(
            path.read_bytes(),
            source="local",
            added=today,
            title=path.stem.replace("_", " ").replace("-", " "),
            author=author,
            permalink=permalink,
            tags=args.tag,
            existing=existing + [a.to_dict() for a in added],
        )
        if item is None:
            print(f"  ! skipped (too small, unreadable, or a duplicate already in the archive): {path.name}")
            continue

        added.append(item)
        credit = f"  (source: {permalink})" if permalink else "  (no .source found — no credit attached)"
        print(f"  + {item.id}  {item.w}×{item.h}  {path.name}{credit}")
        if not args.keep:
            path.unlink()
            sidecar.unlink(missing_ok=True)

    # a .source file whose image was never in `files` (wrong extension, typo,
    # or the image failed to ingest) sits here unused — that's the exact
    # silent failure this file names in its docstring, so surface it loudly
    orphans = sorted(INCOMING.glob("*.source"))
    if orphans:
        print(f"\n! {len(orphans)} orphaned .source file(s) — no matching image, so their credit was NOT attached:")
        for o in orphans:
            print(f"    {o.name}  (expects an image literally named {o.stem!r})")

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
