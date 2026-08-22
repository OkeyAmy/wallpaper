#!/usr/bin/env python3
"""Remove items that don't belong in a wallpaper archive.

Two sources of removal:

  * --ids / --from-list : explicit item ids (e.g. from a visual review)
  * --orphans           : objects sitting in storage that no manifest item
                          references anymore (prune/discard leftovers) —
                          dead weight, invisible to the site

    python scripts/cull.py --dry-run
    python scripts/cull.py --ids id1,id2,...        # remove these items
    python scripts/cull.py --orphans                # purge unreferenced blobs
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys

from pipeline import MANIFEST, load_items
from storage import get_storage

# An allowlist, not a blocklist, and deliberately so: the safe default for an
# unattended deleter is "refuse", so a source added years from now is protected
# by having been forgotten rather than exposed by it. An item is only eligible
# if it can provably be fetched again from its permalink. A hand-dropped image
# cannot — once its R2 object is gone the bytes are gone, no cache, no history.
# This rule exists because 32 hand-added images were destroyed by a cull that
# did not have it.
CULLABLE_SOURCES = {"danbooru", "reddit"}


def bucket_objects(store):
    """Every object key currently in the bucket."""
    keys = set()
    token = None
    while True:
        kwargs = {"Bucket": store.bucket}
        if token:
            kwargs["ContinuationToken"] = token
        page = store.client.list_objects_v2(**kwargs)
        keys |= {o["Key"] for o in page.get("Contents", [])}
        if not page.get("IsTruncated"):
            return keys
        token = page.get("NextContinuationToken")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ids", default="",
                    help="comma-separated item ids to remove")
    ap.add_argument("--from-list", default="",
                    help="file with one item id per line")
    ap.add_argument("--orphans", action="store_true",
                    help="also delete storage objects no item references")
    ap.add_argument("--include-local", action="store_true",
                    help="allow removing items whose source isn't re-fetchable "
                         "(see CULLABLE_SOURCES)")
    ap.add_argument("--max-remove", type=int, default=0,
                    help="refuse to run if more than this many items would go. "
                         "0 disables the cap; the scheduled job always sets it")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    bad_ids = {i.strip() for i in args.ids.split(",") if i.strip()}
    if args.from_list:
        with open(args.from_list) as f:
            bad_ids |= {ln.strip() for ln in f if ln.strip()}

    items = load_items()
    store = get_storage()

    doomed = [it for it in items if it["id"] in bad_ids]
    unknown = bad_ids - {it["id"] for it in items}
    if unknown:
        print(f"! ids not in manifest (ignored): {', '.join(sorted(unknown))}")

    if not args.include_local:
        spared = [it for it in doomed if it.get("source") not in CULLABLE_SOURCES]
        if spared:
            doomed = [it for it in doomed if it.get("source") in CULLABLE_SOURCES]
            bad_ids -= {it["id"] for it in spared}
            print(f"! {len(spared)} item(s) kept — not re-fetchable "
                  f"(source not in {sorted(CULLABLE_SOURCES)}). "
                  f"Pass --include-local to remove them anyway.")

    # Blast-radius cap. A normal weekly run removes a handful of posts that
    # were re-rated or deleted upstream. A run that wants to remove hundreds
    # means the *policy* moved, not the archive — a threshold edited in
    # quality.py, or a tagging convention that drifted. Unattended, that is the
    # difference between routine maintenance and losing a third of the site
    # overnight, so past the ceiling this refuses and exits non-zero: a red
    # cross in the Actions tab is recoverable, a silent mass delete is not.
    if args.max_remove and len(doomed) > args.max_remove:
        print(f"\n! REFUSING: {len(doomed)} items is more than --max-remove "
              f"({args.max_remove}).")
        print("  Nothing was deleted. Either the policy changed or something is "
              "wrong with the audit.")
        print("  Review the list above, then re-run with a higher --max-remove "
              "if it is genuinely correct.")
        return 1

    # --- orphaned storage objects ---
    orphan_keys = []
    if args.orphans or args.dry_run:
        if store.kind != "r2":
            print("! orphan scan needs R2 storage; skipping" if args.orphans else "")
        else:
            referenced = set()
            for it in items:
                referenced.update((it["file"], it["thumb"]))
            referenced.discard("")
            orphan_keys = sorted(bucket_objects(store) - referenced)

    if not doomed and not orphan_keys:
        print("nothing to remove")
        return 0

    print(f"{len(doomed)} manifest items + {len(orphan_keys)} orphaned objects to remove:")
    for it in doomed:
        print(f"  x {it['id']}  {it['w']}x{it['h']}  [{it.get('source')}] {it.get('title', '')[:40]}")

    if args.dry_run:
        n_bytes = 0
        if store.kind == "r2":
            sizes = {}
            token = None
            while True:
                kw = {"Bucket": store.bucket}
                if token:
                    kw["ContinuationToken"] = token
                page = store.client.list_objects_v2(**kw)
                sizes.update({o["Key"]: o["Size"] for o in page.get("Contents", [])})
                if not page.get("IsTruncated"):
                    break
                token = page.get("NextContinuationToken")
            n_bytes = sum(sizes.get(k, 0) for k in orphan_keys)
            n_bytes += sum(it.get("bytes", 0) * 1.1 for it in doomed)
        print(f"\nDRY RUN — nothing deleted (~{n_bytes / 1e6:.0f} MB would be freed)")
        return 0

    # backup the manifest once, before any mutation
    shutil.copy(MANIFEST, str(MANIFEST) + ".bak")

    for it in doomed:
        for key in (it.get("file"), it.get("thumb")):
            store.delete(key)
        print(f"  - {it['id']} removed")

    for key in orphan_keys:
        store.delete(key)
    print(f"  - {len(orphan_keys)} orphans purged")

    keep = [it for it in items if it["id"] not in bad_ids]
    doc = json.loads(MANIFEST.read_text())
    doc["items"] = keep
    MANIFEST.write_text(json.dumps(doc, indent=1))
    print(f"\ndone: {len(keep)} items kept — run scripts/build_site.py next "
          f"(manifest backup at data/wallpapers.json.bak)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
