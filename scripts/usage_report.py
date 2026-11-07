#!/usr/bin/env python3
"""Turn the wallpapers_usage Analytics Engine dataset into a readable report.

Cloudflare stores each event as positional columns, which is why the
dashboard table looks cryptic:

    blob1    event name: open | download | filter | preview
    blob2    subject: wallpaper id, or ratio:tall / hue:red / sort:rand / search
    double1  milliseconds (preview events only; 0 for everything else)
    index1   same as blob1 — only used by Analytics Engine for sampling

This script queries the SQL API, resolves wallpaper ids to titles from the
canonical manifest, and prints plain-English tables.

Usage:
    CF_API_TOKEN=... python scripts/usage_report.py
    CF_API_TOKEN=... python scripts/usage_report.py --days 30 --top 25

Create a token at https://dash.cloudflare.com/profile/api-tokens:
Create Custom Token -> Permissions: Account | Account Analytics | Read.
Put it in .env.local as CF_API_TOKEN=... and it is picked up automatically.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "data" / "wallpapers.json"
DATASET = "wallpapers_usage"


def load_env() -> None:
    """Fill os.environ from .env.local / .env without overriding real env vars."""
    for name in (".env.local", ".env"):
        path = ROOT / name
        if not path.exists():
            continue
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def query(sql: str, account: str, token: str) -> list[dict]:
    url = f"https://api.cloudflare.com/client/v4/accounts/{account}/analytics_engine/sql"
    req = urllib.request.Request(
        url,
        data=sql.encode(),
        headers={"Authorization": f"Bearer {token}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as res:
            payload = json.loads(res.read())
    except urllib.error.HTTPError as err:
        body = err.read().decode(errors="replace")
        print(f"! Cloudflare returned HTTP {err.code}:\n{body}", file=sys.stderr)
        sys.exit(1)
    except urllib.error.URLError as err:
        print(f"! Could not reach Cloudflare: {err.reason}", file=sys.stderr)
        sys.exit(1)
    if "data" not in payload:
        print(f"! Unexpected response: {payload}", file=sys.stderr)
        sys.exit(1)
    return payload["data"]


def count(value) -> int:
    return int(float(value or 0))


def titles() -> dict[str, str]:
    """id -> a short label, so the report says what was opened, not a hash."""
    if not MANIFEST.exists():
        return {}
    items = json.loads(MANIFEST.read_text())["items"]
    out = {}
    for it in items:
        label = it.get("title") or ""
        character = ", ".join(it.get("character") or [])
        if character:
            label = f"{label} ({character})" if label else character
        out[it["id"]] = label.strip() or it["id"]
    return out


def label(subject: str, names: dict[str, str]) -> str:
    text = names.get(subject)
    if not text:
        return subject
    return text if len(text) <= 68 else text[:65] + "…"


def bar(n: int, biggest: int, width: int = 24) -> str:
    filled = round(width * n / biggest) if biggest else 0
    return "█" * filled


def section(name: str) -> None:
    print(f"\n{name}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--days", type=int, default=7, help="window in days (default 7)")
    parser.add_argument("--top", type=int, default=15, help="rows per popularity list")
    parser.add_argument("--dataset", default=DATASET)
    args = parser.parse_args()

    load_env()
    account = (os.environ.get("CF_ACCOUNT_ID")
               or os.environ.get("CLOUDFLARE_ACCOUNT_ID")
               or os.environ.get("R2_ACCOUNT_ID"))
    token = os.environ.get("CF_API_TOKEN") or os.environ.get("CLOUDFLARE_API_TOKEN")

    if not account or not token:
        print("Missing credentials.\n"
              "  CF_API_TOKEN  — create one at dash.cloudflare.com/profile/api-tokens\n"
              "                  (Account | Account Analytics | Read)\n"
              "  CF_ACCOUNT_ID — falls back to R2_ACCOUNT_ID from .env.local\n"
              "Put both in .env.local, or export them in the shell.", file=sys.stderr)
        return 1

    where = f"timestamp >= NOW() - INTERVAL '{args.days}' DAY"
    ds = args.dataset

    rows = query(
        f"SELECT blob1 AS event, sum(_sample_interval) AS n FROM {ds} "
        f"WHERE {where} GROUP BY event ORDER BY n DESC", account, token)
    if not rows:
        print(f"No data in {ds} for the last {args.days} day(s).\n"
              "Open the live site in a browser, wait a minute, then re-run.")
        return 0

    names = titles()
    totals = {r["event"]: count(r["n"]) for r in rows}
    total = sum(totals.values())

    print(f"Usage report — {ds} — last {args.days} day(s)")
    print(f"{total} events total")

    section("WHAT PEOPLE DID")
    biggest = max(totals.values()) or 1
    for event, n in sorted(totals.items(), key=lambda x: -x[1]):
        print(f"  {event:10} {n:7}  {bar(n, biggest)}")

    for event, heading in (("open", "MOST-OPENED WALLPAPERS"),
                           ("download", "MOST-DOWNLOADED WALLPAPERS")):
        if event not in totals:
            continue
        rows = query(
            f"SELECT blob2 AS id, sum(_sample_interval) AS n FROM {ds} "
            f"WHERE blob1 = '{event}' AND {where} "
            f"GROUP BY id ORDER BY n DESC LIMIT {args.top}", account, token)
        section(heading)
        biggest = max((count(r["n"]) for r in rows), default=0) or 1
        for r in rows:
            n = count(r["n"])
            print(f"  {n:5}  {bar(n, biggest)}  {label(r['id'], names)}  [{r['id']}]")

    if "filter" in totals:
        rows = query(
            f"SELECT blob2 AS subject, sum(_sample_interval) AS n FROM {ds} "
            f"WHERE blob1 = 'filter' AND {where} "
            f"GROUP BY subject ORDER BY n DESC LIMIT 50", account, token)
        section("FILTERS AND SEARCH")
        biggest = max((count(r["n"]) for r in rows), default=0) or 1
        for r in rows:
            n = count(r["n"])
            subject = r["subject"]
            if subject == "search":
                subject = "search (query text is never recorded)"
            print(f"  {n:5}  {bar(n, biggest)}  {subject}")

    if "preview" in totals:
        rows = query(
            f"SELECT sum(_sample_interval) AS n, "
            f"quantileExactWeighted(0.5)(double1, _sample_interval) AS p50, "
            f"quantileExactWeighted(0.9)(double1, _sample_interval) AS p90, "
            f"quantileExactWeighted(0.95)(double1, _sample_interval) AS p95, "
            f"max(double1) AS worst FROM {ds} "
            f"WHERE blob1 = 'preview' AND {where}", account, token)
        if rows:
            r = rows[0]
            print("\nLIGHTBOX SPEED (time until the sharp preview is painted)")
            print(f"  samples {count(r['n'])}   "
                  f"p50 {count(r['p50'])} ms   p90 {count(r['p90'])} ms   "
                  f"p95 {count(r['p95'])} ms   worst {count(r['worst'])} ms")
            print("  p50 includes cache hits; p90/p95 show what slow connections feel.")

    rows = query(
        f"SELECT toStartOfInterval(timestamp, INTERVAL '1' DAY) AS day, "
        f"blob1 AS event, sum(_sample_interval) AS n FROM {ds} "
        f"WHERE {where} GROUP BY day, event ORDER BY day ASC", account, token)
    if rows:
        by_day: dict[str, dict[str, int]] = {}
        for r in rows:
            day = str(r["day"])[:10]
            by_day.setdefault(day, {})[r["event"]] = count(r["n"])
        events = sorted({e for d in by_day.values() for e in d})
        section("PER DAY")
        print("  day          " + "".join(f"{e:>10}" for e in events))
        for day in sorted(by_day):
            cells = "".join(f"{by_day[day].get(e, 0):>10}" for e in events)
            print(f"  {day}  {cells}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
