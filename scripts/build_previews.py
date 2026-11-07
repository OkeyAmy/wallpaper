#!/usr/bin/env python3
"""Generate display-size previews for items that don't have one yet.

The lightbox used to point straight at the full-resolution WebP. A 4K
wallpaper is routinely 1-5 MB, so the image appeared only after the whole
file had crossed the network. The preview is the same picture capped at
PREVIEW_WIDTH (2560px) and encoded at q78 — roughly a third of the bytes —
which is indistinguishable at the size the lightbox can actually draw it.
The full file is still what the download button serves.

New ingests get a preview automatically (pipeline.ingest_image). This script
is for the back catalogue, and it is resumable: items already carrying a
`preview` field are skipped, and a preview object that exists in storage is
recorded without re-downloading the full image.

    python scripts/build_previews.py --dry-run
    python scripts/build_previews.py --workers 6
    python scripts/build_previews.py --limit 20      # try a batch first

Needs the R2_* variables set, same as the sync scripts.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from PIL import Image

from pipeline import (
    MANIFEST, PREVIEW_METHOD, PREVIEW_QUALITY, PREVIEW_SMALL_BYTES,
    PREVIEW_WIDTH, _encode, make_preview,
)
from storage import get_storage

SAVE_EVERY = 25


def preview_key(ident: str) -> str:
    return f"wallpapers/previews/{ident}.webp"


def needs_preview(it: dict) -> bool:
    """True when this item should be (re)built.

    An alias to the full file is fine only when the full file is genuinely
    small; otherwise it is a placeholder waiting for the threshold change in
    pipeline.make_preview to be applied by this backfill.
    """
    current = it.get("preview")
    if not current:
        return True
    if current != it.get("file"):
        return False
    return int(it.get("bytes", 0)) > PREVIEW_SMALL_BYTES


def build_one(it: dict, store, dry_run: bool) -> tuple[str, str, int]:
    """Return (status, id, preview_bytes) for one manifest item.

    Status is one of: small (full is already display-sized), present (the
    object was already in storage), built, or failed.
    """
    ident = it["id"]

    # A small file is already what the lightbox would draw; re-encoding it
    # would only add a lossy pass. A display-sized file that is nonetheless
    # heavy (q92, lots of detail) still gets a preview — see pipeline.py.
    if (max(int(it.get("w", 0)), int(it.get("h", 0))) <= PREVIEW_WIDTH
            and int(it.get("bytes", 0)) <= PREVIEW_SMALL_BYTES):
        return ("small", ident, 0)

    key = preview_key(ident)
    if store.exists(key):
        return ("present", ident, store.size(key))

    if dry_run:
        return ("would-build", ident, 0)

    # R2 reads occasionally time out or reset mid-stream; those are transient,
    # not a reason to leave the item on the slow full-image path forever.
    blob = None
    for attempt in range(3):
        try:
            blob = store.read(it["file"])
            break
        except Exception as exc:              # noqa: BLE001 - network errors vary
            if attempt == 2:
                print(f"  ! {ident}: read failed after retries ({exc})",
                      file=sys.stderr)
                return ("failed", ident, 0)
            time.sleep(2 * (attempt + 1))
    if not blob:
        return ("missing", ident, 0)

    try:
        img = Image.open(io.BytesIO(blob))
        img.load()
    except Exception as exc:
        print(f"  ! {ident}: unreadable full image ({exc})", file=sys.stderr)
        return ("failed", ident, 0)

    preview = make_preview(img, len(blob))
    if preview is None:                     # dimensions/bytes changed under us
        return ("small", ident, 0)

    data = _encode(preview, PREVIEW_QUALITY, PREVIEW_METHOD)
    for attempt in range(3):
        try:
            store.put(key, data)
            break
        except Exception as exc:              # noqa: BLE001 - network errors vary
            if attempt == 2:
                print(f"  ! {ident}: upload failed after retries ({exc})",
                      file=sys.stderr)
                return ("failed", ident, 0)
            time.sleep(2 * (attempt + 1))
    return ("built", ident, len(data))


def write_manifest(doc: dict) -> None:
    """Atomic replace, so an interrupted run never leaves a truncated index."""
    tmp = MANIFEST.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(doc, indent=1))
    os.replace(tmp, MANIFEST)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0,
                    help="process at most this many items (0 = all)")
    ap.add_argument("--workers", type=int, default=6,
                    help="parallel download/encode/upload workers")
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would be built, without touching storage")
    args = ap.parse_args()

    if not MANIFEST.exists():
        print("manifest not found", file=sys.stderr)
        return 1

    doc = json.loads(MANIFEST.read_text())
    items = doc.get("items", [])
    pending = [it for it in items if needs_preview(it)]
    todo = pending[:args.limit] if args.limit else pending

    total = len(items)
    print(f"{total} items, {len(pending)} need a preview "
          f"({total - len(pending)} already have one)")
    if args.limit and len(pending) > len(todo):
        print(f"  limiting this run to {len(todo)}")
    if not todo:
        return 0

    store = get_storage()
    by_id = {it["id"]: it for it in items}
    counts = {"small": 0, "present": 0, "built": 0, "missing": 0,
              "failed": 0, "would-build": 0}
    saved = 0
    started = time.time()

    def record(it: dict, status: str, preview_bytes: int) -> None:
        nonlocal saved
        counts[status] = counts.get(status, 0) + 1
        if status in ("small",):
            it["preview"] = it["file"]
            it["preview_bytes"] = 0
        elif status in ("present", "built"):
            it["preview"] = preview_key(it["id"])
            it["preview_bytes"] = preview_bytes
        elif status in ("missing", "failed"):
            print(f"  ! {it['id']}: {status} — no preview written",
                  file=sys.stderr)
        if status in ("small", "present", "built"):
            saved += 1
            if not args.dry_run and saved % SAVE_EVERY == 0:
                write_manifest(doc)
                print(f"  … {saved}/{len(todo)} recorded", flush=True)

    if args.workers <= 1:
        for n, it in enumerate(todo, 1):
            status, ident, size = build_one(it, store, args.dry_run)
            record(it, status, size)
            if n % 50 == 0 or n == len(todo):
                print(f"  [{n}/{len(todo)}] {status:12} {ident}", flush=True)
    else:
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = {pool.submit(build_one, it, store, args.dry_run): it
                       for it in todo}
            done = 0
            for fut in as_completed(futures):
                it = futures[fut]
                done += 1
                try:
                    status, ident, size = fut.result()
                except Exception as exc:      # noqa: BLE001 - keep the batch alive
                    print(f"  ! {it['id']}: {exc}", file=sys.stderr)
                    status, ident, size = "failed", it["id"], 0
                record(it, status, size)
                if done % 50 == 0 or done == len(todo):
                    print(f"  [{done}/{len(todo)}] {status:12} {ident}", flush=True)

    if not args.dry_run:
        write_manifest(doc)

    elapsed = time.time() - started
    built_kb = sum(it.get("preview_bytes", 0) for it in items) / 1024
    print(
        f"\nbuilt {counts['built']}, already present {counts['present']}, "
        f"small {counts['small']}, missing {counts['missing']}, "
        f"failed {counts['failed']} in {elapsed:.0f}s"
    )
    print(f"preview bytes in manifest: {built_kb / 1024:.1f} MB")
    if args.dry_run:
        print("DRY RUN — manifest and storage untouched")
    else:
        print("next: python scripts/build_site.py")
    return 1 if counts["failed"] or counts["missing"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
