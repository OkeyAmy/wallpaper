#!/usr/bin/env python3
"""Tell search engines a page changed, instead of waiting to be recrawled.

IndexNow is a push protocol: one POST naming the changed URLs, and Bing,
Yandex, Seznam and Naver fetch them promptly rather than on whatever schedule
they would otherwise have chosen. It is free, needs no account, and the whole
of the authentication is a file at the site root whose name is its contents.

Google is not a participant and its old sitemap-ping endpoint was retired in
2023, so there is deliberately no Google call here — anything claiming to
"submit to Google" on a schedule is either using Search Console (which needs
OAuth and reports, it does not request indexing) or doing nothing at all.
For Google the levers are the sitemap, internal links and being worth
indexing, none of which are a script that runs at 6am.

Submitting the whole site every day would be noise; a host that spams IndexNow
gets its submissions throttled. So this only runs when the manifest actually
changed, and sends the pages whose contents move when it does: the homepage,
the hub index, and every hub page.

    python scripts/indexnow.py            # submit if the manifest changed
    python scripts/indexnow.py --all      # submit regardless
    python scripts/indexnow.py --dry-run
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import requests

from build_site import SITE, collect_facet_hubs, collect_hubs, hub_url
from pipeline import ROOT, load_items

ENDPOINT = "https://api.indexnow.org/indexnow"


def find_key() -> str | None:
    """The key is the name of the <key>.txt file sitting at the site root.

    Storing it as a repo file rather than a secret is the design of the
    protocol, not an oversight: the file has to be publicly fetchable for the
    search engine to verify the submission came from someone who controls the
    host. Keeping it in the repo also means the key survives whoever set it up
    — there is nothing to remember and nothing to rotate.
    """
    for path in ROOT.glob("*.txt"):
        name = path.stem
        if len(name) >= 8 and all(c in "0123456789abcdef" for c in name.lower()):
            if path.read_text().strip() == name:
                return name
    return None


def manifest_changed() -> bool:
    """True when data/wallpapers.json differs from the last commit.

    Uses git rather than a timestamp because the workflow commits the manifest
    in the same run — "did this build change anything" is exactly the question
    git already answers.
    """
    r = subprocess.run(["git", "diff", "HEAD~1", "--name-only"],
                       capture_output=True, text=True, cwd=ROOT)
    if r.returncode != 0:                      # shallow clone, first commit, etc.
        return True                            # fail towards submitting
    return "data/wallpapers.json" in r.stdout


def urls() -> list[str]:
    items = load_items()
    hubs = collect_hubs(items) + collect_facet_hubs(items)
    return [f"{SITE}/", f"{SITE}/w/"] + [SITE + hub_url(h["slug"]) for h in hubs]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true",
                    help="submit even when the manifest did not change")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    key = find_key()
    if not key:
        print("! no IndexNow key file at the repo root — expected <32-hex>.txt "
              "whose contents are the same hex string. Skipping.", file=sys.stderr)
        return 0                               # never fail a deploy over this

    if not args.all and not manifest_changed():
        print("manifest unchanged — nothing to submit")
        return 0

    payload = {
        "host": SITE.split("//", 1)[1],
        "key": key,
        "keyLocation": f"{SITE}/{key}.txt",
        "urlList": urls(),
    }

    if args.dry_run:
        print(json.dumps({**payload, "urlList": payload["urlList"][:5]}, indent=1))
        print(f"... {len(payload['urlList'])} urls total (dry run)")
        return 0

    try:
        r = requests.post(ENDPOINT, json=payload, timeout=30)
    except requests.RequestException as e:
        print(f"! IndexNow unreachable: {e}", file=sys.stderr)
        return 0                               # a missed ping is not a failed build

    # 200 and 202 both mean accepted; 422 means the key or host didn't validate,
    # which is worth seeing in the log rather than swallowing.
    print(f"IndexNow: {len(payload['urlList'])} urls -> HTTP {r.status_code}")
    if r.status_code >= 400:
        print(f"  {r.text[:300]}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
