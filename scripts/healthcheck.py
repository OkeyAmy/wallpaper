#!/usr/bin/env python3
"""Check the *live* site, not the build output.

The build can succeed and the site still be broken: a Worker route changes, a
cache rule starts serving a stale asset, the CDN hostname stops resolving, an
R2 object goes missing while the manifest still advertises it. None of that
shows up in a build log, and on a site nobody watches it can persist for
months — which for search means the pages quietly drop out of the index.

So this runs against the deployed URLs and exits non-zero when something is
wrong. That exit code is the whole alerting system: a red cross in the Actions
tab is a thing an owner who never opens the repo will still eventually see,
and it costs nothing to keep running for years.

Checks, in the order a crawler would hit them:

    robots.txt + sitemap.xml reachable and parseable
    every sitemap URL returns 200 (they are the pages being advertised)
    canonical tag on each page matches the URL it was fetched from
    JSON-LD parses
    a sample of CDN images actually exist
    the IndexNow key file is served

    python scripts/healthcheck.py
    python scripts/healthcheck.py --sample 40
"""

from __future__ import annotations

import argparse
import json
import random
import re

import requests

from build_site import SITE
from pipeline import MANIFEST, load_items

TIMEOUT = 25
UA = {"User-Agent": "wallpaper-archive-healthcheck/1.0"}

problems: list[str] = []
checked = 0


def fail(msg: str) -> None:
    problems.append(msg)
    print(f"  FAIL  {msg}")


def ok(msg: str) -> None:
    print(f"  ok    {msg}")


def get(url: str, **kw):
    global checked
    checked += 1
    return requests.get(url, headers=UA, timeout=TIMEOUT, **kw)


def check_sitemap() -> list[str]:
    try:
        r = get(f"{SITE}/sitemap.xml")
    except requests.RequestException as e:
        fail(f"sitemap.xml unreachable: {e}")
        return []
    if r.status_code != 200:
        fail(f"sitemap.xml -> HTTP {r.status_code}")
        return []

    # Deliberately not an XML parser. All that is needed here is the <loc>
    # values, and Python's stdlib XML parsers will happily process entity
    # declarations in whatever comes back over the network — an
    # exponential-entity ("billion laughs") document would hang this check.
    # A regex cannot be induced to do anything but fail to match.
    locs = [u.strip() for u in re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", r.text)]
    if not locs:
        fail("sitemap.xml contains no <loc> entries")
        return []
    ok(f"sitemap.xml parses, {len(locs)} urls")
    return locs


def check_page(url: str) -> None:
    try:
        r = get(url)
    except requests.RequestException as e:
        fail(f"{url} unreachable: {e}")
        return
    if r.status_code != 200:
        fail(f"{url} -> HTTP {r.status_code}")
        return

    # A canonical pointing somewhere else tells Google to index that other page
    # instead — a silent way for every hub page to deindex itself.
    m = re.search(r'<link rel="canonical" href="([^"]+)"', r.text)
    if not m:
        fail(f"{url} has no canonical")
    elif m.group(1).rstrip("/") != url.rstrip("/"):
        fail(f"{url} canonical points at {m.group(1)}")

    for block in re.findall(
            r'<script type="application/ld\+json"[^>]*>(.*?)</script>', r.text, re.S):
        try:
            json.loads(block.replace("<\\/", "</"))
        except json.JSONDecodeError as e:
            fail(f"{url} has malformed JSON-LD: {e}")
            break


def check_images(sample: int) -> None:
    items = load_items()
    if not items:
        fail("manifest is empty")
        return
    try:
        cdn = json.loads(MANIFEST.read_text()).get("cdn", "").rstrip("/")
    except (OSError, json.JSONDecodeError):
        cdn = ""

    for it in random.sample(items, min(sample, len(items))):
        for key in ("file", "thumb"):
            url = f"{cdn}/{it[key]}" if cdn else f"{SITE}/{it[key]}"
            try:
                r = requests.head(url, headers=UA, timeout=TIMEOUT)
            except requests.RequestException as e:
                fail(f"image {it['id']} unreachable: {e}")
                continue
            if r.status_code != 200:
                fail(f"image {url} -> HTTP {r.status_code} "
                     f"(manifest advertises an object that is not there)")
    ok(f"sampled {min(sample, len(items))} items' images")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", type=int, default=15,
                    help="how many items' images to spot-check")
    ap.add_argument("--max-pages", type=int, default=60,
                    help="cap on sitemap URLs fetched, so this stays quick")
    args = ap.parse_args()

    print(f"health check: {SITE}")

    try:
        r = get(f"{SITE}/robots.txt")
        if r.status_code != 200:
            fail(f"robots.txt -> HTTP {r.status_code}")
        elif "Sitemap:" not in r.text:
            fail("robots.txt does not point at the sitemap")
        else:
            ok("robots.txt")
    except requests.RequestException as e:
        fail(f"robots.txt unreachable: {e}")

    locs = check_sitemap()
    for url in locs[:args.max_pages]:
        check_page(url)
    if locs:
        ok(f"checked {min(len(locs), args.max_pages)} pages for status, canonical and JSON-LD")

    check_images(args.sample)

    # The key file is what makes IndexNow submissions valid; if it stops being
    # served every submission is silently rejected.
    from indexnow import find_key
    key = find_key()
    if key:
        try:
            r = get(f"{SITE}/{key}.txt")
            if r.status_code != 200 or r.text.strip() != key:
                fail(f"IndexNow key file /{key}.txt -> HTTP {r.status_code} "
                     "(submissions will be rejected)")
            else:
                ok("IndexNow key file")
        except requests.RequestException as e:
            fail(f"IndexNow key file unreachable: {e}")

    print(f"\n{checked} requests, {len(problems)} problem(s)")
    if problems:
        print("\n::error::live site health check failed:")
        for p in problems:
            print(f"  - {p}")
        return 1
    print("site is healthy")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
