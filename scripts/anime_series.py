#!/usr/bin/env python3
"""Discover anime series to search for on Wallhaven.

A hand-written query list goes stale twice: it misses older shows, and it never
notices a new one. This module keeps a catalogue of anime titles from AniList
(free, keyless) and rotates through it, so the Wallhaven sync asks about Hunter
x Hunter, Bleach and whatever is airing this season without anyone editing a
list.

Two groups supplement the catalogue:

  * trending — currently airing shows, so a new season enters within weeks
  * seeds    — game and classic franchises AniList does not list as anime
               (Genshin, Honkai, Blue Archive, Sailor Moon, ...)

The catalogue is refreshed weekly and committed, so the sync never depends on
AniList being reachable at run time. If the refresh fails the previous
catalogue is used; if there has never been one, the sync falls back to its
generic queries and carries on.

    python scripts/anime_series.py --refresh
    python scripts/anime_series.py --show 10
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
CATALOGUE = DATA_DIR / "anime_series.json"

ANILIST = "https://graphql.anilist.co"
UA = "okeyamy-wallpaper-archive/1.0 (+https://okeyamy.xyz)"

POPULAR_PAGES = 20          # 20 x 50 = the 1,000 most popular anime titles
TRENDING_PAGES = 1          # what is airing right now
PER_PAGE = 50
REFRESH_DAYS = 7
REQUEST_PACING = 1.2        # AniList allows ~90 requests/minute

# Franchises that are not `type: ANIME` on AniList but are anime-styled and
# large on Wallhaven. Genshin Impact alone has 5,000+ all-time results. These
# are a supplement, not the discovery mechanism: the catalogue still grows by
# itself.
EXTRA_TITLES = (
    "genshin impact", "honkai star rail", "honkai impact 3rd", "zenless zone zero",
    "wuthering waves", "blue archive", "azur lane", "arknights", "girls frontline",
    "punishing gray raven", "fate grand order", "touhou project", "vocaloid",
    "hatsune miku", "league of legends", "final fantasy", "nier automata",
    "persona 5", "fire emblem", "the legend of zelda", "pokemon", "digimon",
    "sailor moon", "cardcaptor sakura", "ranma 1 2", "urusei yatsura",
    "studio ghibli", "your name", "weathering with you", "suzume",
)

# Season/edition suffixes that produce a second AniList entry for the same
# franchise. Dropping them is what turns "JUJUTSU KAISEN Season 2" and
# "JUJUTSU KAISEN" into one query instead of two.
_EDITION = re.compile(
    r"\b(?:"
    r"(?:season|part|cour)\s*\d+|"
    r"\d+(?:st|nd|rd|th)\s+(?:season|attack|arc|chapter|stage)|"
    r"final\s+(?:season|chapter|arc)|"
    r"movie|ova|ona|special|specials|recap|picture\s+drama"
    r")\b",
    re.IGNORECASE,
)

# "(2011)" and a trailing " 2011" are disambiguators, not part of the name.
_YEAR = re.compile(r"\s*\((?:19|20)\d{2}\)\s*$|\s+(?:19|20)\d{2}\s*$")


def strip_edition(text: str) -> str:
    """Remove season/part/year markers so sequels collapse onto the franchise."""
    text = re.sub(r"\s+", " ", _YEAR.sub(" ", text or "")).strip(" :-")
    return re.sub(r"\s+", " ", _EDITION.sub(" ", text)).strip(" :-")


def clean_title(text: str) -> str:
    """Lowercase, punctuation-free form that Wallhaven's fuzzy search accepts."""
    text = strip_edition(text)
    text = text.replace("&", " and ")
    text = text.replace("'", "").replace("’", "")
    text = re.sub(r"[^0-9A-Za-z]+", " ", text)
    return re.sub(r"\s+", " ", text).strip().lower()


def main_title(text: str) -> str:
    """The part before a title colon or a spaced dash.

    The colon must be followed by whitespace: "Frieren: Beyond Journey's End"
    splits, but "Re:ZERO -Starting Life in Another World-" does not, so that
    show keeps its name instead of becoming "re".
    """
    return re.split(r":\s+|\s+-\s*", text or "", maxsplit=1)[0]


def title_terms(media: dict) -> list[str]:
    """Search terms for one AniList entry, most specific first.

    The main title comes first because it is what Wallhaven tags usually use
    ("Frieren", "Demon Slayer", "Re:ZERO"). The full English and romaji titles
    follow as fallbacks for shows whose tag is the long form.
    """
    titles = media.get("title") or {}
    english = titles.get("english") or ""
    romaji = titles.get("romaji") or ""
    candidates = [
        main_title(english), english,
        main_title(romaji), romaji,
    ]
    # ASCII-only synonyms ("AoT", "SnK") occasionally match; translated titles
    # in other scripts never do, and add noise.
    for synonym in media.get("synonyms") or []:
        if isinstance(synonym, str) and synonym.isascii():
            candidates.append(synonym)

    terms: list[str] = []
    for raw in candidates:
        term = clean_title(raw)
        if len(term) < 3 or term in terms:
            continue
        terms.append(term)
        if len(terms) >= 3:
            break
    return terms


def _query(sort: str, page: int, tries: int = 3) -> dict:
    body = json.dumps({
        "query": """
            query($page: Int, $sort: [MediaSort]) {
              Page(page: $page, perPage: %d) {
                pageInfo { hasNextPage }
                media(type: ANIME, sort: $sort) {
                  id popularity isAdult
                  title { romaji english }
                  synonyms
                }
              }
            }
        """ % PER_PAGE,
        "variables": {"page": page, "sort": [sort]},
    }).encode()
    last: Exception | None = None
    for attempt in range(tries):
        try:
            req = urllib.request.Request(
                ANILIST, data=body,
                headers={"Content-Type": "application/json", "User-Agent": UA})
            with urllib.request.urlopen(req, timeout=40) as res:
                payload = json.load(res)
            if payload.get("errors"):
                raise ValueError(
                    payload["errors"][0].get("message", "AniList error"))
            return payload["data"]["Page"]
        except (urllib.error.URLError, TimeoutError, ValueError, OSError) as err:
            last = err
            if attempt < tries - 1:
                time.sleep(3 * (attempt + 1))
    raise ValueError(str(last))


def _seed_entry(term: str) -> dict:
    return {"id": None, "key": term, "terms": [term], "popularity": 0,
            "trending": False, "seed": True}


def with_seeds(catalogue: dict) -> dict:
    """Add the game/classic seeds, idempotently.

    Applied on read as well as on refresh, so adding a seed in code does not
    wait for the weekly AniList fetch to take effect. If an AniList entry
    already covers the seed, it is marked as a seed and keeps its title list.
    """
    series = list(catalogue.get("series") or [])
    by_key = {s.get("key"): s for s in series if s.get("key")}
    for raw in EXTRA_TITLES:
        key = clean_title(raw)
        if not key:
            continue
        if key in by_key:
            by_key[key]["seed"] = True
            continue
        entry = _seed_entry(key)
        series.append(entry)
        by_key[key] = entry
    catalogue["series"] = series
    return catalogue


def fetch(popular_pages: int = POPULAR_PAGES,
          trending_pages: int = TRENDING_PAGES) -> dict:
    """Build the catalogue from AniList's popular and trending lists."""
    media: dict[int, dict] = {}

    def add(page: dict, trending: bool) -> None:
        for entry in page.get("media") or []:
            mid = entry.get("id")
            if not mid or entry.get("isAdult"):
                continue
            terms = title_terms(entry)
            if not terms:
                continue
            current = media.get(mid)
            if current is None:
                media[mid] = {
                    "id": mid,
                    "key": terms[0],
                    "terms": terms,
                    "popularity": int(entry.get("popularity") or 0),
                    "trending": trending,
                }
            else:
                current["trending"] = current["trending"] or trending
                for term in terms:
                    if term not in current["terms"] and len(current["terms"]) < 3:
                        current["terms"].append(term)

    for page_no in range(1, popular_pages + 1):
        page = _query("POPULARITY_DESC", page_no)
        add(page, trending=False)
        if not page.get("pageInfo", {}).get("hasNextPage"):
            break
        time.sleep(REQUEST_PACING)

    for page_no in range(1, trending_pages + 1):
        page = _query("TRENDING_DESC", page_no)
        add(page, trending=True)
        if not page.get("pageInfo", {}).get("hasNextPage"):
            break
        time.sleep(REQUEST_PACING)

    # Merge season entries that normalize to the same key, keeping the most
    # popular one and the union of their search terms.
    merged: dict[str, dict] = {}
    for entry in media.values():
        key = entry["key"]
        old = merged.get(key)
        if old is None:
            merged[key] = entry
            continue
        old["trending"] = old["trending"] or entry["trending"]
        old["popularity"] = max(old["popularity"], entry["popularity"])
        for term in entry["terms"]:
            if term not in old["terms"] and len(old["terms"]) < 3:
                old["terms"].append(term)

    series = sorted(merged.values(), key=lambda e: (-e["popularity"], e["key"]))
    return with_seeds({
        "source": "anilist",
        "fetched": date.today().isoformat(),
        "series": series,
    })


def load() -> dict:
    if not CATALOGUE.exists():
        return with_seeds({"series": []})
    try:
        data = json.loads(CATALOGUE.read_text())
    except (OSError, json.JSONDecodeError):
        return with_seeds({"series": []})
    if not isinstance(data, dict) or not isinstance(data.get("series"), list):
        return with_seeds({"series": []})
    return with_seeds(data)


def save(catalogue: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CATALOGUE.write_text(json.dumps(catalogue, indent=1, ensure_ascii=False) + "\n")


def _stale(fetched: str, today: date, days: int) -> bool:
    try:
        then = date.fromisoformat(fetched)
    except (TypeError, ValueError):
        return True
    return (today - then).days >= days


def load_or_refresh(today: date | None = None, save_new: bool = True,
                    refresh_days: int = REFRESH_DAYS) -> dict:
    """Use the cache when it is fresh; refresh when it is not.

    A failed refresh never discards a usable catalogue: an old list of anime
    titles is still a good list. Only a first-ever run with no network falls
    back to the generic queries.
    """
    today = today or date.today()
    current = load()
    if current.get("series") and not _stale(current.get("fetched", ""), today,
                                            refresh_days):
        return current
    try:
        fresh = fetch()
    except (urllib.error.URLError, ValueError, OSError) as err:
        print(f"! anime catalogue refresh failed ({err}) — "
              f"{'using the cached list' if current.get('series') else 'continuing without series queries'}",
              file=sys.stderr)
        return current
    if save_new:
        save(fresh)
    return fresh


def pick(catalogue: dict, day: date, count: int = 5,
         trending_count: int = 1, seed_count: int = 2) -> list[dict]:
    """Choose the series for one run, deterministically.

    A rotating window is used instead of a cursor file: it needs no state, it
    is reproducible if the job is retried on the same day, and it still walks
    the whole catalogue over time. Trending titles are picked first so a new
    season enters the archive without waiting for the popular-list rotation to
    reach it; seeds keep the big game/classic franchises in the mix.
    """
    series = catalogue.get("series") or []
    if not series:
        return []
    day_index = day.toordinal()

    def window(items: list[dict], want: int) -> list[dict]:
        if not items or want <= 0:
            return []
        start = (day_index * want) % len(items)
        return [items[(start + i) % len(items)] for i in range(min(want, len(items)))]

    groups = [
        window([s for s in series if s.get("trending")], trending_count),
        window([s for s in series if s.get("seed")], seed_count),
        window(series, count),
    ]
    seen, out = set(), []
    for group in groups:
        for entry in group:
            key = entry.get("key")
            if not key or key in seen:
                continue
            seen.add(key)
            out.append(entry)
    return out


def series_axes(catalogue: dict, day: date, count: int = 5,
                trending_count: int = 1, seed_count: int = 2,
                max_pages: int = 3) -> list[dict]:
    """Wallhaven search axes for this run's series, best title first."""
    axes = []
    for entry in pick(catalogue, day, count, trending_count, seed_count):
        terms = list(entry.get("terms") or [])
        if not terms:
            continue
        axes.append({
            "q": terms[0],
            "fallback_terms": terms[1:],
            "sorting": "views",       # all-time, popularity-ranked
            "max_pages": max_pages,
            "adaptive": True,
            "label": entry.get("key", terms[0]),
        })
    return axes


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--refresh", action="store_true",
                    help="fetch a fresh catalogue from AniList and save it")
    ap.add_argument("--show", type=int, default=0, metavar="N",
                    help="print the next N series that would be queried")
    args = ap.parse_args()

    if args.refresh:
        catalogue = fetch()
        save(catalogue)
        print(f"catalogue: {len(catalogue['series'])} series "
              f"({sum(1 for s in catalogue['series'] if s['trending'])} trending, "
              f"{sum(1 for s in catalogue['series'] if s.get('seed'))} seeds) "
              f"-> {CATALOGUE}")
    else:
        catalogue = load_or_refresh()

    if args.show:
        for entry in pick(catalogue, date.today(), args.show, 0, 0):
            print(f"{entry['popularity']:>9}  "
                  f"{'TREND' if entry['trending'] else '     '}  "
                  f"{entry['key']:35}  {', '.join(entry['terms'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
