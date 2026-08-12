#!/usr/bin/env python3
"""One-off repair of titles and character tags on already-ingested items.

The archive was built before `character` existed and before ingest refused to
name an image after its filename, so the manifest carries two kinds of damage:

  * Danbooru items have their character names mashed into `title` as one
    space-joined string ("fern frieren stark"), which can't be split back
    apart reliably — "artoria pendragon" is one character, "fern frieren" is
    two. The names are re-fetched from the API instead of guessed.

  * Local items are named after the file they came from: "IMG 20260810
    WA0012", "naruto x8jgro". The IMG ones say nothing and are cleared; the
    naruto ones do carry a real subject, which is kept and promoted to a
    character tag.

Re-running is safe: every step is idempotent and only touches fields it can
improve. Not wired into the pipeline — run it once, commit the manifest.

    python scripts/backfill_metadata.py [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time

import requests

from pipeline import MANIFEST, clean_character_tags

UA = "web:okeyamy-wallpaper-archive:1.0 (by anonymous)"
POST_API = "https://danbooru.donmai.us/posts/{id}.json"

# Filenames that were used as titles. These describe the file, not the image.
JUNK_TITLE = re.compile(
    r"""^(
        img[\s_-]*\d[\d\s_-]*        # IMG 20260810 WA0012
      | image[\s_-]*\d+
      | photo[\s_-]*\d+
      | screenshot.*
      | untitled
      | original                     # danbooru's "not from a franchise" tag
      | download.*
      | [a-z0-9]{16,}.*              # xde5r60c9hrmy0czy83tbevr30 result 0
    )$""",
    re.I | re.X,
)

# "naruto x8jgro" — a real subject plus a download-site cache-buster.
#
# The trailing token has to carry a digit to count as a hash. Without that
# rule an ordinary three-word title loses its last word: "fall echo tries"
# reads as subject "fall echo" + hash "tries". Requiring a digit costs a few
# genuine hashes that happen to be all letters, which the second pass in
# `main` recovers by matching against subjects the first pass established.
SUBJECT_PLUS_HASH = re.compile(
    r"^([a-z][a-z' -]{2,24}?)[\s_-]+(?=[a-z0-9]*\d)[a-z0-9]{5,8}$", re.I)

# Same shape, but the trailing token is unconstrained — only ever applied to a
# subject already confirmed by the strict pattern above.
SUBJECT_PLUS_ANY = re.compile(r"^([a-z][a-z' -]{2,24}?)[\s_-]+[a-z0-9]{5,8}$", re.I)


def post_id(permalink: str) -> str | None:
    m = re.search(r"/posts/(\d+)", permalink or "")
    return m.group(1) if m else None


def fetch_characters(pid: str) -> list[str] | None:
    """Character names for a Danbooru post, or None if the lookup failed.

    None and [] mean different things here: [] is a post with no character
    tags (original art, scenery), which is a real answer worth recording.
    None means we don't know, and the item is left alone.
    """
    try:
        r = requests.get(POST_API.format(id=pid), headers={"User-Agent": UA}, timeout=30)
    except requests.RequestException as e:
        print(f"  ! post {pid}: {e}", file=sys.stderr)
        return None
    if r.status_code != 200:
        print(f"  ! post {pid} -> HTTP {r.status_code}", file=sys.stderr)
        return None
    try:
        post = r.json()
    except ValueError:
        return None
    # strip the `_(series)` disambiguator: people search "ganyu", not
    # "ganyu (genshin impact)"
    return clean_character_tags(post.get("tag_string_character", ""))


def title_from_characters(names: list[str]) -> str:
    if not names:
        return ""
    pretty = [n.title() for n in names[:3]]
    return ", ".join(pretty)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="report changes without writing")
    args = ap.parse_args()

    doc = json.loads(MANIFEST.read_text())
    items = doc.get("items", [])
    if not items:
        print("manifest empty — nothing to backfill")
        return 0

    cleared = promoted = tagged = 0
    subjects: set[str] = set()      # confirmed by the strict hash pattern
    leftovers: list[dict] = []      # "<word> <word>" titles the strict pass declined

    for it in items:
        title = (it.get("title") or "").strip()

        # --- Danbooru: authoritative character names from the API ---------
        pid = post_id(it.get("permalink", "")) if it.get("source") == "danbooru" else None
        if pid and not it.get("character"):
            names = fetch_characters(pid)
            time.sleep(0.5)          # polite pacing on an anonymous client
            if names is not None:
                it["character"] = names
                if names:
                    tagged += 1
                    it["title"] = title_from_characters(names)
                    print(f"  ~ {it['id']}  characters: {', '.join(names)}")
                elif JUNK_TITLE.match(title):
                    it["title"] = ""
                    cleared += 1
                    print(f"  - {it['id']}  cleared title {title!r} (no character tags)")
                continue

        if it.get("character"):
            continue

        # --- local: filename-derived titles -------------------------------
        if JUNK_TITLE.match(title):
            it["title"] = ""
            cleared += 1
            print(f"  - {it['id']}  cleared filename title {title!r}")
            continue

        m = SUBJECT_PLUS_HASH.match(title)
        if m:
            subject = m.group(1).strip().lower()
            subjects.add(subject)
            it["title"] = subject.title()
            it["character"] = [subject]
            promoted += 1
            print(f"  ~ {it['id']}  {title!r} -> {subject.title()!r} (+ character)")
        elif SUBJECT_PLUS_ANY.match(title):
            leftovers.append(it)

    # Second pass: an all-letter hash ("naruto neeelo") is indistinguishable
    # from a real word on its own, but is safe to treat as one once the same
    # subject has been confirmed elsewhere by the strict pattern.
    for it in leftovers:
        title = (it.get("title") or "").strip()
        subject = SUBJECT_PLUS_ANY.match(title).group(1).strip().lower()
        if subject not in subjects:
            print(f"  . {it['id']}  left alone: {title!r} (unrecognised subject)")
            continue
        it["title"] = subject.title()
        it["character"] = [subject]
        promoted += 1
        print(f"  ~ {it['id']}  {title!r} -> {subject.title()!r} (+ character, by prior match)")

    if args.dry_run:
        print(f"\ndry run — {tagged} tagged / {promoted} promoted / {cleared} cleared, nothing written")
        return 0

    doc["items"] = items
    MANIFEST.write_text(json.dumps(doc, indent=1))
    print(f"\n{tagged} tagged / {promoted} promoted / {cleared} cleared / {len(items)} total")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
