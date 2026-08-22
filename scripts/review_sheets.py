#!/usr/bin/env python3
"""Render the archive as labelled contact sheets for a visual curation pass.

Tags and pixel heuristics catch structural problems (a comic page, a
letterboxed screencap, a panorama strip). They cannot judge the thing the
archive is actually judged on: whether a picture is *good enough to put on a
screen*. The 62 hand-dropped items carry no tags at all, and on Danbooru a
fully-clothed character is routinely tagged `breasts`, so a tag filter alone
is both blind in one eye and trigger-happy in the other.

So: montage every item into sheets, look at them, and write down the ids that
fail. Each cell carries a sheet-local label (`S03-07`) rather than its item
id, and `sheets/index.json` maps label -> id. Ids are 12 hex characters and
a single misread digit would delete the wrong wallpaper from R2, so nothing
in the review path asks a human (or a model) to transcribe one.

    python scripts/review_sheets.py                  # all items
    python scripts/review_sheets.py --filter local   # only hand-dropped items
    python scripts/review_sheets.py --ids a1b2,c3d4  # a specific set

Then, for whatever the review rejects:

    python scripts/cull.py --from-list rejects.txt --dry-run
"""

from __future__ import annotations

import argparse
import concurrent.futures
import io
import json
import os
import sys
from pathlib import Path

import requests
from PIL import Image, ImageDraw, ImageFont

from pipeline import load_items

OUT_DIR = Path(os.environ.get("REVIEW_OUT", "/tmp/wallpaper-review"))
CACHE_DIR = OUT_DIR / "thumbs"

COLS, ROWS = 4, 3
CELL = 384
LABEL_H = 22
BG = (16, 16, 16)
PER_SHEET = COLS * ROWS


def cdn_base() -> str:
    base = os.environ.get("R2_PUBLIC_BASE", "").rstrip("/")
    if not base:
        sys.exit("! R2_PUBLIC_BASE not set — nothing to fetch thumbnails from")
    return base


def fetch_thumb(item: dict, base: str) -> tuple[str, bytes | None]:
    """Thumbnail bytes, from the local cache when we already have them.

    Re-running the review after a partial pass is normal (cull a batch, look
    again), and re-downloading the whole archive each time is rude to the CDN
    for no gain — the objects are content-addressed, so a cached file can
    never be stale.
    """
    cached = CACHE_DIR / f"{item['id']}.webp"
    if cached.exists():
        return item["id"], cached.read_bytes()
    try:
        r = requests.get(f"{base}/{item['thumb']}", timeout=30)
        r.raise_for_status()
    except requests.RequestException as e:
        print(f"  ! {item['id']}: {e}", file=sys.stderr)
        return item["id"], None
    cached.write_bytes(r.content)
    return item["id"], r.content


def _font(size: int):
    for path in ("/usr/share/fonts/dejavu-sans-fonts/DejaVuSans-Bold.ttf",
                 "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"):
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def draw_cell(sheet: Image.Image, img: Image.Image, col: int, row: int, label: str) -> None:
    x0 = col * CELL
    y0 = row * (CELL + LABEL_H)

    fitted = img.copy()
    fitted.thumbnail((CELL, CELL), Image.Resampling.LANCZOS)
    sheet.paste(fitted,
                (x0 + (CELL - fitted.width) // 2,
                 y0 + LABEL_H + (CELL - fitted.height) // 2))

    d = ImageDraw.Draw(sheet)
    d.rectangle([x0, y0, x0 + CELL, y0 + LABEL_H], fill=(0, 0, 0))
    d.text((x0 + 6, y0 + 4), label, fill=(255, 255, 255), font=_font(14))
    d.rectangle([x0, y0, x0 + CELL - 1, y0 + LABEL_H + CELL - 1],
                outline=(60, 60, 60))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--filter", choices=("all", "local", "danbooru"), default="all",
                    help="restrict to one source")
    ap.add_argument("--ids", default="", help="comma-separated item ids")
    args = ap.parse_args()

    items = load_items()
    if args.filter != "all":
        items = [i for i in items if (i.get("source") == "danbooru")
                 == (args.filter == "danbooru")]
    if args.ids:
        wanted = {i.strip() for i in args.ids.split(",") if i.strip()}
        items = [i for i in items if i["id"] in wanted]
    if not items:
        print("nothing to review")
        return 0

    # Stable order, so a label means the same picture across re-runs.
    items.sort(key=lambda i: i["id"])

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    base = cdn_base()

    print(f"fetching {len(items)} thumbnails ...")
    blobs: dict[str, bytes] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
        for ident, data in pool.map(lambda i: fetch_thumb(i, base), items):
            if data:
                blobs[ident] = data

    mapping: dict[str, dict] = {}
    n_sheets = (len(items) + PER_SHEET - 1) // PER_SHEET
    for s in range(n_sheets):
        chunk = items[s * PER_SHEET:(s + 1) * PER_SHEET]
        sheet = Image.new("RGB", (COLS * CELL, ROWS * (CELL + LABEL_H)), BG)
        for n, it in enumerate(chunk):
            data = blobs.get(it["id"])
            if not data:
                continue
            label = f"S{s + 1:02d}-{n + 1:02d}"
            try:
                img = Image.open(io.BytesIO(data))
                img.load()
            except Exception:
                continue
            draw_cell(sheet, img, n % COLS, n // COLS, label)
            mapping[label] = {
                "id": it["id"],
                "source": it.get("source"),
                "wh": f"{it['w']}x{it['h']}",
                "ratio": it.get("ratio"),
                "title": it.get("title", ""),
            }
        path = OUT_DIR / f"sheet-{s + 1:02d}.jpg"
        sheet.save(path, "JPEG", quality=88)
        print(f"  {path}  ({len(chunk)} items)")

    (OUT_DIR / "index.json").write_text(json.dumps(mapping, indent=1))
    print(f"\n{n_sheets} sheets, {len(mapping)} cells -> {OUT_DIR}")
    print("label -> id map: index.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
