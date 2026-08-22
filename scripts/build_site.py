#!/usr/bin/env python3
"""Stamp the manifest and generate everything a crawler reads.

The gallery is rendered client-side, which means a crawler that does not run
JavaScript would otherwise see an empty page. This script injects a real
static gallery and a JSON-LD description of every item into index.html, then
writes sitemap.xml, robots.txt and llms.txt.

    python scripts/build_site.py
"""

from __future__ import annotations

import hashlib
import html
import io
import json
import os
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

# _rgb_to_hsl is pipeline's, not a copy: the colour pages must bucket a swatch
# by exactly the same maths that produced it, or an item's page and its palette
# would disagree about what colour it is.
from pipeline import MANIFEST, ROOT, _rgb_to_hsl, load_items

SITE = "https://wallpapers.okeyamy.xyz"
INDEX = ROOT / "index.html"
HUB_DIR = ROOT / "w"

STATIC_CELLS = 24        # items rendered as real <img> in the no-JS fallback
STATIC_LINKS = 120       # additional text-only links for crawl depth
LD_ITEMS = 100           # items described in JSON-LD

# A character needs this many wallpapers before it gets its own page. One
# image and a heading is a thin page — a hundred of them is a thin *site*,
# which is the failure mode that gets a gallery ignored wholesale rather than
# ranked per character. Characters below the line still appear on the
# homepage and in the image sitemap; they just don't get a page of their own.
MIN_HUB_ITEMS = 4        # a 2-image character page is thin content: it competes
                         # with nothing, and a sitemap full of them is the
                         # scaled-content shape Google penalises. Sparse
                         # characters stay reachable through the colour pages.
MIN_FACET_ITEMS = 8      # colour/format pages are denser by nature; hold them
                         # to a higher bar still
HUB_GRID_CELLS = 60      # real <img> per hub page before falling back to links

# Cells above the fold that should load eagerly. lazy-loading every cell
# means the browser also defers the one image that's actually the page's LCP
# candidate, which is a direct hit to a Core Web Vitals ranking signal.
EAGER_CELLS = 4


def asset_base() -> str:
    """Public URL prefix for images.

    Empty when images sit in the repo (served by Pages alongside the page);
    the R2 bucket's public hostname once storage has moved there.
    """
    return os.getenv("R2_PUBLIC_BASE", "").rstrip("/")


def asset_url(rel: str) -> str:
    base = asset_base()
    return f"{base}/{rel}" if base else f"{SITE}/{rel}"


def stamp_manifest(items: list[dict]) -> tuple[str, bool]:
    """Write the manifest, keeping the old timestamp when nothing changed.

    A fresh timestamp on every run would make each scheduled build produce a
    diff even when the sync found nothing, so the archive would collect an
    empty commit and trigger a pointless redeploy every single day. The
    timestamp only moves when the item set actually moves.
    """
    previous = {}
    if MANIFEST.exists():
        try:
            previous = json.loads(MANIFEST.read_text())
        except json.JSONDecodeError:
            previous = {}

    cdn = asset_base()
    unchanged = (previous.get("items") == items
                 and previous.get("cdn", "") == cdn
                 and bool(previous.get("generated")))
    generated = previous["generated"] if unchanged else \
        datetime.now(timezone.utc).isoformat(timespec="seconds")

    # `cdn` tells the frontend where images live; empty means "next to the page"
    MANIFEST.write_text(json.dumps(
        {"generated": generated, "cdn": cdn, "items": items}, indent=1))
    return generated, not unchanged


def asset_versions() -> dict[str, str]:
    """sha256[:8] of each fingerprinted asset's current bytes.

    /assets/* is cached for a day (build_headers), so a code change can serve
    stale JS to a returning visitor for up to 24h — lowering the TTL doesn't
    help anyone already holding a cached entry, since a shorter header on the
    server doesn't shorten a freshness lifetime the browser already computed.
    A content hash in the URL sidesteps the problem instead of shortening it:
    index.html is revalidated on every load, so a changed hash there is a
    guaranteed cache miss on the new URL, whatever the old one's TTL says.
    """
    return {
        name: hashlib.sha256((ROOT / "assets" / name).read_bytes()).hexdigest()[:8]
        for name in ("app.js", "style.css")
    }


def versioned_asset(path: str, versions: dict[str, str]) -> str:
    """/assets/<name> with its content hash appended, e.g. /assets/app.js?v=1a2b3c4d."""
    name = path.rsplit("/", 1)[-1]
    return f"{path}?v={versions[name]}" if name in versions else path


def stamp_asset_versions(html: str, versions: dict[str, str]) -> str:
    """Rewrite /assets/app.js and /assets/style.css references to carry ?v=<hash>.

    The pattern matches an existing ?v=... too, so this is idempotent — safe
    to run against index.html's checked-in copy, which already carries
    whatever hash the last build produced.
    """
    for name, digest in versions.items():
        html = re.sub(
            rf'(/assets/{re.escape(name)})(\?v=[0-9a-f]+)?',
            rf'\1?v={digest}',
            html,
        )
    return html


def replace_block(text: str, tag: str, payload: str) -> str:
    """Swap the content between <!-- TAG --> and <!-- /TAG -->."""
    pattern = re.compile(rf"(<!-- {tag} -->).*?(<!-- /{tag} -->)", re.S)
    if not pattern.search(text):
        raise SystemExit(f"marker <!-- {tag} --> missing from index.html")
    return pattern.sub(lambda m: m.group(1) + payload + m.group(2), text)


# General tags that describe what a picture *shows*. Used to say something
# about wallpapers with no named character, where the alternative is calling
# every one of them "anime wallpaper" and making them interchangeable.
SCENE_TAGS = {
    "scenery", "landscape", "cityscape", "sky", "clouds", "night", "sunset",
    "sunrise", "mountain", "ocean", "sea", "beach", "forest", "field",
    "flower", "flowers", "rain", "snow", "stars", "starry_sky", "moon",
    "space", "ruins", "architecture", "building", "bridge", "train",
    "cherry_blossoms", "autumn", "winter", "summer", "spring", "fog",
    "reflection", "silhouette", "sunlight", "waterfall", "river", "lake",
    "temple", "shrine", "castle", "street", "road", "window", "mecha",
    "robot", "dragon", "fire", "lightning", "aurora", "desert", "island",
}


def orientation(it: dict) -> str:
    """How the wallpaper is meant to be used — the thing people search for."""
    w, h = it["w"], it["h"]
    if h > w:
        return "phone"
    return "ultrawide" if w / h >= 2.2 else "desktop"


def display_name(name: str) -> str:
    return " ".join(w.capitalize() for w in str(name).split())


def join_names(names: list[str]) -> str:
    pretty = [display_name(n) for n in names]
    if len(pretty) == 1:
        return pretty[0]
    return ", ".join(pretty[:-1]) + " and " + pretty[-1]


def subject(it: dict) -> str:
    """What this wallpaper is *of*, best-effort, in falling order of quality.

    Named characters beat a free-text title, which beats scene tags. Anything
    is better than nothing here: this text is the alt attribute, the sitemap
    caption and the JSON-LD name, so an item that resolves to "" is one Google
    has no reason to distinguish from any other picture on the page.
    """
    names = it.get("character") or []
    if names:
        return join_names(names[:3])

    title = (it.get("title") or "").strip()
    if title:
        return title

    scene = [t.replace("_", " ") for t in (it.get("tags") or []) if t in SCENE_TAGS]
    return ", ".join(scene[:3])


def describe(it: dict) -> str:
    """Human-readable alt/name text. Crawlers and screen readers read this."""
    what = subject(it)
    spec = f"{it['w']}×{it['h']} {orientation(it)} anime wallpaper"
    return f"{what} — {spec}" if what else spec.capitalize()


def credit_label(it: dict) -> str:
    """Who to credit, in that source's own vocabulary.

    Reddit posts are credited to a subreddit and a u/ account; Danbooru posts
    to the artist; a hand-added image to whoever supplied it. The `r/` and
    `u/` prefixes were once applied to all three, which labelled Danbooru
    artists as Reddit accounts.
    """
    author = (it.get("author") or "").strip()
    source = it.get("source") or ""

    if source == "reddit":
        if author:
            return f"u/{author}"
        return f"r/{it['sub']}" if it.get("sub") else "reddit"

    if source == "danbooru":
        return f"{author} (Danbooru)" if author else "Danbooru"

    return author or "direct upload"


def hub_slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", str(name).lower()).strip("-")
    return s[:60]


def hub_url(slug: str) -> str:
    """Public URL for a character page.

    No `.html`: the assets runtime serves w/ganyu.html at /w/ganyu and
    *redirects* /w/ganyu.html there (confirmed — /index.html 307s to /), so
    linking the filename would point every canonical and sitemap entry at a
    redirect instead of at the page.
    """
    return f"/w/{slug}"


def collect_hubs(items: list[dict]) -> list[dict]:
    """One entry per character with enough wallpapers to justify a page.

    Keyed by slug rather than by the raw name so two spellings that normalise
    to the same URL end up on one page instead of silently overwriting each
    other's file.
    """
    groups: dict[str, dict] = {}
    for it in items:
        for name in it.get("character") or []:
            slug = hub_slug(name)
            if not slug:
                continue
            g = groups.setdefault(slug, {"slug": slug, "name": name,
                                         "kind": "character", "items": []})
            if it not in g["items"]:
                g["items"].append(it)

    hubs = [g for g in groups.values() if len(g["items"]) >= MIN_HUB_ITEMS]
    for g in hubs:
        # widest first: the biggest wallpaper is the best thing to lead with
        g["items"].sort(key=lambda x: x["w"] * x["h"], reverse=True)
    hubs.sort(key=lambda g: (-len(g["items"]), g["slug"]))
    return hubs


# --- facet hubs ------------------------------------------------------------
# Colour and size, the two things a person actually searches for when they
# want a wallpaper rather than a picture of a character: "dark anime
# wallpaper", "blue anime wallpaper", "4k anime wallpaper", "phone anime
# wallpaper". Every wallpaper site competes on character names; almost none of
# them can group by extracted palette, because almost none of them extract one.
# This archive already does, for every item, at ingest — so these pages are
# built from data nobody else has rather than from another template.
#
# They are also much denser than the character pages: a colour bucket holds
# tens of items where a character holds two or three, and a dense page is the
# one worth indexing.

COLOUR_NAMES = [
    (345, 15, "red"), (15, 45, "orange"), (45, 70, "yellow"),
    (70, 165, "green"), (165, 200, "cyan"), (200, 255, "blue"),
    (255, 290, "purple"), (290, 345, "pink"),
]


def colour_bucket(hex_colour: str) -> str | None:
    """Name for a palette swatch, or None if it isn't a usable one.

    Lightness and saturation are checked before hue because the hue of a
    near-black or near-grey pixel is noise — "dark" and "monochrome" are the
    honest labels for those, and they happen to be what people search for.
    """
    try:
        r, g, b = (int(hex_colour[i:i + 2], 16) for i in (1, 3, 5))
    except (ValueError, IndexError):
        return None
    _h, s, lightness = _rgb_to_hsl(r, g, b)
    if lightness < 0.18:
        return "dark"
    if lightness > 0.88:
        return "light"
    if s < 0.12:
        return "monochrome"
    hue = _h % 360
    for lo, hi, name in COLOUR_NAMES:
        if lo > hi:                          # the red wedge wraps past 360
            if hue >= lo or hue < hi:
                return name
        elif lo <= hue < hi:
            return name
    return None


def orientation_bucket(it: dict) -> str | None:
    w, h = it["w"], it["h"]
    if h > w and h / w >= 1.5:
        return "phone"
    if w / h >= 2.2:
        return "ultrawide"
    if w >= 3840 or h >= 3840:
        return "4k"
    return None


FACET_COPY = {
    "dark": "dark", "light": "light", "monochrome": "black and white",
    "phone": "phone", "ultrawide": "ultrawide", "4k": "4K",
}


def collect_facet_hubs(items: list[dict]) -> list[dict]:
    """Colour and format pages, built from the palette extracted at ingest."""
    groups: dict[str, dict] = {}

    def add(key: str, label: str, kind: str, it: dict) -> None:
        slug = f"{hub_slug(key)}-anime-wallpapers"
        g = groups.setdefault(slug, {"slug": slug, "name": label,
                                     "kind": kind, "facet": key, "items": []})
        if it not in g["items"]:
            g["items"].append(it)

    for it in items:
        palette = it.get("palette") or []
        # Only the dominant swatch decides the colour page. Indexing an item
        # under every colour in its palette would put the same wallpaper on
        # five pages and make each of them a near-duplicate of the others.
        if palette:
            bucket = colour_bucket(palette[0])
            if bucket:
                add(bucket, FACET_COPY.get(bucket, bucket), "colour", it)
        fmt = orientation_bucket(it)
        if fmt:
            add(fmt, FACET_COPY[fmt], "format", it)

    hubs = [g for g in groups.values() if len(g["items"]) >= MIN_FACET_ITEMS]
    for g in hubs:
        g["items"].sort(key=lambda x: x["w"] * x["h"], reverse=True)
    hubs.sort(key=lambda g: (g["kind"], -len(g["items"])))
    return hubs


def credit_link(it: dict) -> str:
    """Attribution back to where the wallpaper came from.

    Followed, like any other editorial credit — the archive is pointing at the
    thing it is quoting. Rendered here rather than only in the JS lightbox so
    the credit exists for readers who never run the script.
    """
    url = (it.get("permalink") or "").strip()
    label = html.escape(credit_label(it))
    if not url.startswith(("http://", "https://")):
        return f'<span class="cell__sub">{label}</span>'
    return f'<a class="cell__sub" href="{html.escape(url, quote=True)}" rel="noopener">{label} ↗</a>'


def static_cell(it: dict, href: str, eager: bool = False) -> str:
    alt = html.escape(describe(it))
    # loading="eager" alone doesn't raise priority — without fetchpriority the
    # browser still schedules it behind render-blocking CSS/JS at the same rung
    # as a lazy image that happened to be requested early.
    load_attrs = 'loading="eager" fetchpriority="high"' if eager else 'loading="lazy"'
    return (
        f'<div class="cell is-ready" role="listitem">'
        f'<a href="{html.escape(href, quote=True)}">'
        f'<img class="cell__img is-loaded" src="{asset_url(it["thumb"])}" alt="{alt}" '
        f'width="{it["w"]}" height="{it["h"]}" {load_attrs} decoding="async"></a>'
        f'<span class="cell__hud"><span style="min-width:0">'
        f'<span class="cell__name">{html.escape(subject(it) or "Untitled")}</span><br>'
        f'{credit_link(it)}</span>'
        f'<span class="cell__res">{it["w"]}×{it["h"]}</span></span></div>'
    )


def build_hub_nav(hubs: list[dict]) -> str:
    """Homepage links into every character page.

    Without these the hub pages are orphans — in the sitemap but linked from
    nothing, which is the weakest possible signal that they matter.
    """
    if not hubs:
        return ""

    def section(title: str, group: list[dict], suffix: str) -> str:
        if not group:
            return ""
        links = "".join(
            f'<li><a href="{hub_url(g["slug"])}">'
            f'{html.escape(display_name(g["name"]))} {suffix} ({len(g["items"])})</a></li>'
            for g in group
        )
        return (f'<nav class="seolist"><h2 class="foot__h">[ {title} ]</h2>'
                f"<ul>{links}</ul></nav>")

    by_kind: dict[str, list[dict]] = {}
    for g in hubs:
        by_kind.setdefault(g.get("kind", "character"), []).append(g)

    return (section("BY COLOUR", by_kind.get("colour", []), "anime wallpapers")
            + section("BY SIZE", by_kind.get("format", []), "anime wallpapers")
            + section("BY CHARACTER", by_kind.get("character", []), "wallpapers"))


def build_static_grid(items: list[dict], hubs: list[dict]) -> str:
    # an item that has a page of its own is better pointed at that page than
    # at a bare .webp — the page is the thing that can actually rank
    page_of = {}
    for g in hubs:
        for it in g["items"]:
            page_of.setdefault(it["id"], hub_url(g["slug"]))

    cells = [
        static_cell(it, page_of.get(it["id"], asset_url(it["file"])), eager=i < EAGER_CELLS)
        for i, it in enumerate(items[:STATIC_CELLS])
    ]

    nav = build_hub_nav(hubs)
    if nav:
        cells.append(nav)

    # The index links straight to the image files, never to the character
    # pages: thirty of these are Naruto, and thirty links to /w/naruto.html
    # with near-identical anchor text is a spam signal, not thirty votes.
    # Character pages are reached once each, from the nav above.
    rest = items[STATIC_CELLS:STATIC_CELLS + STATIC_LINKS]
    if rest:
        links = "".join(
            f'<li><a href="{asset_url(it["file"])}">{html.escape(describe(it))}</a></li>'
            for it in rest
        )
        cells.append(
            '<div class="seolist"><h2 class="foot__h">[ FULL INDEX ]</h2>'
            f"<ul>{links}</ul></div>"
        )
    return "\n".join(cells)


def hub_summary(name: str, items: list[dict], kind: str = "character") -> str:
    """A sentence about what is actually on this page.

    Written from the items rather than from a template so two pages never read
    identically — a page whose only unique word is the character's name is a
    page Google has grounds to treat as a duplicate of the others. The colour
    and format pages get their own phrasing for the same reason: they would
    otherwise be the character template with a different noun in it.
    """
    n = len(items)
    counts = Counter(orientation(it) for it in items)
    parts = [f"{c} {shape}" for shape, c in counts.most_common()]
    shapes = ", ".join(parts[:-1]) + (" and " + parts[-1] if len(parts) > 1 else parts[0])
    biggest = max(items, key=lambda x: x["w"] * x["h"])
    label = display_name(name) if kind == "character" else name

    if kind == "colour":
        subjects = Counter()
        for it in items:
            for c in (it.get("character") or [])[:1]:
                subjects[display_name(c)] += 1
        featured = ", ".join(s for s, _ in subjects.most_common(3))
        tail = f" Featuring {featured}." if featured else ""
        return (
            f"{n} anime wallpapers whose dominant colour is {label}, measured "
            f"from the image itself rather than tagged by hand — {shapes}, up to "
            f"{biggest['w']}×{biggest['h']}.{tail} Free, no account needed."
        )

    if kind == "format":
        return (
            f"{n} anime wallpapers sized for {label} — up to "
            f"{biggest['w']}×{biggest['h']}, free to download, no account and no "
            "sign-up. Every one lists its extracted colour palette so you can "
            "match it to a theme."
        )

    return (
        f"{n} {label} wallpapers — {shapes} — "
        f"up to {biggest['w']}×{biggest['h']}, free to download. "
        "Each one lists its dominant colour palette and links back to the "
        "original artist's post."
    )


def build_hub_page(hub: dict, generated: str, versions: dict[str, str]) -> str:
    kind = hub.get("kind", "character")
    items = hub["items"]
    summary = hub_summary(hub["name"], items, kind)
    url = SITE + hub_url(hub["slug"])

    if kind == "character":
        name = display_name(hub["name"])
        heading = f"{name} wallpapers"
        # kept under ~60 chars so search results show the whole thing
        title = f"{name} Wallpapers — 4K, Phone & Desktop"[:60]
    else:
        name = hub["name"]
        heading = f"{display_name(name)} anime wallpapers"
        title = f"{display_name(name)} Anime Wallpapers — 4K & Phone"[:60]

    cells = "\n".join(
        static_cell(it, asset_url(it["file"]), eager=i < EAGER_CELLS)
        for i, it in enumerate(items[:HUB_GRID_CELLS])
    )

    rest = items[HUB_GRID_CELLS:]
    if rest:
        links = "".join(
            f'<li><a href="{asset_url(it["file"])}">{html.escape(describe(it))}</a></li>'
            for it in rest
        )
        cells += ('\n<div class="seolist"><h2 class="foot__h">[ MORE ]</h2>'
                  f"<ul>{links}</ul></div>")

    ld = {
        "@context": "https://schema.org",
        "@type": "ImageGallery",
        "name": heading,
        "description": summary,
        "url": url,
        "dateModified": generated,
        "isAccessibleForFree": True,
        "isPartOf": {"@type": "WebSite", "name": "Anime Wallpaper Archive", "url": SITE + "/"},
        "image": [image_object(it) for it in items[:LD_ITEMS]],
    }
    breadcrumb = {
        "@context": "https://schema.org",
        "@type": "BreadcrumbList",
        "itemListElement": [
            {"@type": "ListItem", "position": 1, "name": "Anime Wallpaper Archive", "item": SITE + "/"},
            {"@type": "ListItem", "position": 2, "name": heading, "item": url},
        ],
    }

    def ld_script(obj: dict) -> str:
        return ('<script type="application/ld+json">'
                + json.dumps(obj, separators=(",", ":")).replace("</", "<\\/")
                + "</script>")

    ld_block = ld_script(ld) + "\n" + ld_script(breadcrumb)

    return f"""<!DOCTYPE html>
<html lang="en" data-theme="tty">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>{html.escape(title)}</title>
<meta name="description" content="{html.escape(summary)}">
<meta name="color-scheme" content="dark">
<meta name="theme-color" content="#0A0A0A">
<link rel="canonical" href="{url}">
<meta property="og:title" content="{html.escape(heading)}">
<meta property="og:description" content="{html.escape(summary)}">
<meta property="og:type" content="website">
<meta property="og:url" content="{url}">
<meta property="og:image" content="{asset_url(items[0]['file'])}">
<meta name="twitter:card" content="summary_large_image">
<link rel="stylesheet" href="{versioned_asset('/assets/style.css', versions)}">
<link rel="icon" href="/assets/favicon.svg" type="image/svg+xml">
{ld_block}
</head>
<body>
<header class="bar" role="banner">
  <a href="/" class="bar__logo">WALLPAPER ARCHIVE</a>
</header>
<main class="wrap">
  <h1 class="hub__h">{html.escape(heading)}</h1>
  <p class="hub__lede">{html.escape(summary)}</p>
  <div class="grid" role="list">
{cells}
</div>
  <a class="hub__back" href="/">← Browse the full wallpaper archive</a>
</main>
<footer class="foot">
  <p><a href="/w/">All characters</a> — <a href="/">Browse the full wallpaper archive</a>
  · Copyright remains with the original artists. Every wallpaper links back to
  the post it came from.</p>
</footer>
</body>
</html>
"""


def build_character_index(hubs: list[dict], facets: list[dict],
                          generated: str, versions: dict[str, str]) -> str:
    """The w/ landing page: one link per character, so a crawler that lands on
    /w/ (via the homepage footer) can reach every /w/<character> page.

    A single column of links is deliberate — a grid of thumbnails here would
    duplicate every character's homepage cells and dilute the crawl budget on
    images that are already reachable from each character page.
    """
    links = "".join(
        f'<li><a href="{hub_url(g["slug"])}">{html.escape(display_name(g["name"]))} '
        f'wallpapers ({len(g["items"])})</a></li>'
        for g in hubs
    )
    facet_links = "".join(
        f'<li><a href="{hub_url(g["slug"])}">{html.escape(display_name(g["name"]))} '
        f'anime wallpapers ({len(g["items"])})</a></li>'
        for g in facets
    )
    facet_block = (
        '<div class="seolist"><h2 class="foot__h">[ BY COLOUR &amp; SIZE ]</h2>'
        f"<ul>{facet_links}</ul></div>" if facet_links else ""
    )
    title = "All Characters — Anime Wallpaper Archive"
    description = f"{len(hubs)} characters indexed in the archive, each with their own wallpaper page."
    ld = {
        "@context": "https://schema.org",
        "@type": "CollectionPage",
        "name": "All Characters",
        "description": description,
        "url": SITE + "/w/",
        "dateModified": generated,
        "isPartOf": {"@type": "WebSite", "name": "Anime Wallpaper Archive", "url": SITE + "/"},
    }

    def ld_script(obj: dict) -> str:
        return ('<script type="application/ld+json">'
                + json.dumps(obj, separators=(",", ":")).replace("</", "<\\/")
                + "</script>")

    return f"""<!DOCTYPE html>
<html lang="en" data-theme="tty">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>{html.escape(title)}</title>
<meta name="description" content="{html.escape(description)}">
<meta name="color-scheme" content="dark">
<meta name="theme-color" content="#0A0A0A">
<link rel="canonical" href="{SITE}/w/">
<meta property="og:title" content="{html.escape(title)}">
<meta property="og:description" content="{html.escape(description)}">
<meta property="og:type" content="website">
<meta property="og:url" content="{SITE}/w/">
<meta property="og:image" content="{SITE}/assets/og.png">
<meta name="twitter:card" content="summary_large_image">
<link rel="stylesheet" href="{versioned_asset('/assets/style.css', versions)}">
<link rel="icon" href="/assets/favicon.svg" type="image/svg+xml">
{ld_script(ld)}
</head>
<body>
<header class="bar" role="banner">
  <a href="/" class="bar__logo">WALLPAPER ARCHIVE</a>
</header>
<main class="wrap">
  <h1 class="hub__h">All characters</h1>
  <p class="hub__lede">{html.escape(description)}</p>
  <div class="seolist"><h2 class="foot__h">[ BY CHARACTER ]</h2><ul>{links}</ul></div>
  {facet_block}
  <a class="hub__back" href="/">← Browse the full wallpaper archive</a>
</main>
<footer class="foot">
  <p>Copyright remains with the original artists. Every wallpaper links back to
  the post it came from.</p>
</footer>
</body>
</html>
"""


def write_hub_pages(hubs: list[dict], generated: str, versions: dict[str, str]) -> None:
    """Write w/<slug>.html, removing pages whose character fell below the line.

    Stale files matter here: a character page that stops being generated but
    stays on disk keeps getting served and keeps claiming a canonical URL.
    w/index.html is the w/ landing page, not a character page — it is written
    separately by build_character_index() and must never be swept away here.
    """
    HUB_DIR.mkdir(exist_ok=True)
    wanted = {f"{g['slug']}.html" for g in hubs}
    wanted.add("index.html")

    for old in HUB_DIR.glob("*.html"):
        if old.name not in wanted:
            old.unlink()

    for g in hubs:
        (HUB_DIR / f"{g['slug']}.html").write_text(build_hub_page(g, generated, versions))


def image_object(it: dict) -> dict:
    """Schema.org ImageObject for one wallpaper."""
    return {
        "@type": "ImageObject",
        "@id": asset_url(it["file"]),
        "contentUrl": asset_url(it["file"]),
        "thumbnailUrl": asset_url(it["thumb"]),
        "name": describe(it),
        "width": {"@type": "QuantitativeValue", "value": it["w"], "unitCode": "E37"},
        "height": {"@type": "QuantitativeValue", "value": it["h"], "unitCode": "E37"},
        "encodingFormat": "image/webp",
        "uploadDate": it.get("added", ""),
        **({"creditText": credit_label(it)} if it.get("author") or it.get("sub") else {}),
        **({"isBasedOn": it["permalink"]} if it.get("permalink") else {}),
        **({"keywords": [display_name(c) for c in it["character"]]} if it.get("character") else {}),
    }


def build_jsonld(items: list[dict], generated: str) -> str:
    gallery = {
        "@context": "https://schema.org",
        "@type": "ImageGallery",
        "name": "Anime Wallpaper Archive",
        "description": (
            "A curated, automatically synced archive of anime wallpapers in 4K desktop, "
            "ultrawide and phone resolutions, indexed by extracted colour palette."
        ),
        "url": SITE + "/",
        "dateModified": generated,
        "isAccessibleForFree": True,
        "keywords": [
            "anime wallpapers", "4k anime wallpaper", "phone anime wallpaper",
            "ultrawide wallpaper", "desktop wallpaper", "linux rice wallpaper",
            "colour palette wallpaper",
        ],
        "image": [image_object(it) for it in items[:LD_ITEMS]],
    }

    faq = {
        "@context": "https://schema.org",
        "@type": "FAQPage",
        "mainEntity": [
            {
                "@type": "Question",
                "name": "What resolutions are available?",
                "acceptedAnswer": {
                    "@type": "Answer",
                    "text": "Desktop 16:9 and 16:10 up to 3840×2160 (4K), ultrawide 21:9 for "
                            "3440×1440 panels, and vertical 9:16 sizes for phones.",
                },
            },
            {
                "@type": "Question",
                "name": "Are the wallpapers free to download?",
                "acceptedAnswer": {
                    "@type": "Answer",
                    "text": "Yes. Browsing and downloading are free, with no account and no "
                            "tracking. Copyright remains with the original artists and each "
                            "item links back to the post it came from.",
                },
            },
            {
                "@type": "Question",
                "name": "What does palette-indexed mean?",
                "acceptedAnswer": {
                    "@type": "Answer",
                    "text": "Each wallpaper's dominant colours are extracted at ingest, so you "
                            "can filter by hue and copy the hex values into a desktop theme, "
                            "terminal colourscheme or editor config.",
                },
            },
        ],
    }

    def block(obj: dict) -> str:
        # </script> inside JSON would close the tag early
        return ('<script type="application/ld+json">'
                + json.dumps(obj, separators=(",", ":")).replace("</", "<\\/")
                + "</script>")

    return "\n" + block(gallery) + "\n" + block(faq) + "\n"


def build_headers() -> str:
    """Cloudflare Pages header rules.

    Generated rather than hand-written because the Content-Security-Policy
    has to name the image host: once images move to R2 a hardcoded
    ``img-src 'self'`` would silently block every wallpaper on the site.
    """
    base = asset_base()
    img_src = f"'self' data: {base}" if base else "'self' data:"

    csp = (
        "default-src 'self'; "
        f"img-src {img_src}; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src https://fonts.gstatic.com; "
        "script-src 'self'; "
        "connect-src 'self'; "
        "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    )

    return f"""# Generated by scripts/build_site.py — edit that, not this file.

/*
  X-Content-Type-Options: nosniff
  Referrer-Policy: strict-origin-when-cross-origin
  X-Frame-Options: DENY
  Permissions-Policy: geolocation=(), microphone=(), camera=(), interest-cohort=()
  Content-Security-Policy: {csp}

# Content-hashed filenames — safe to cache hard and forever. Only reached
# when images are served from this origin; once R2_PUBLIC_BASE is set they
# come from the CDN and this rule (and any X-Robots-Tag on it) is moot, which
# is why indexing is not declared here.
/wallpapers/*
  Cache-Control: public, max-age=31536000, immutable

# Character pages are regenerated on every sync — an item can join or leave
# one — so they must not be served stale.
/w/*
  Cache-Control: public, max-age=0, must-revalidate

# The index changes on every sync; revalidate so new wallpapers show up.
/data/wallpapers.json
  Cache-Control: public, max-age=0, must-revalidate

# Enumerated rather than a /assets/* wildcard on principle, not preference:
# confirmed live that Cloudflare merges every matching rule's headers instead
# of the more specific one overriding — a wildcard entry plus a specific one
# for the same path produced a single Cache-Control value with "public,
# max-age=86400" and "public, max-age=31536000, immutable" both present,
# which is not a header any cache is required to resolve sanely. Distinct,
# non-overlapping paths sidestep the merge entirely rather than depending on
# a precedence rule that turned out not to hold.
#
# app.js and style.css are requested with ?v=<content-hash>
# (asset_versions / stamp_asset_versions) — a code change is a new URL, not
# a cache problem.
/assets/app.js
  Cache-Control: public, max-age=31536000, immutable

/assets/style.css
  Cache-Control: public, max-age=31536000, immutable

# Unversioned — og.png is redrawn whenever the newest items change, and
# favicon.svg has no build-time hash to key off. Short TTLs, not immutable.
/assets/og.png
  Cache-Control: public, max-age=3600

/assets/favicon.svg
  Cache-Control: public, max-age=86400
"""


# Google caps a single <url> at 1000 images.
SITEMAP_IMAGES_PER_URL = 1000


def image_entries(items: list[dict]) -> str:
    """<image:image> children for one <url>.

    This is what actually gets wallpapers into Google Images. The image URLs
    point at the CDN host rather than at this one; that's allowed, but only
    because cdn.okeyamy.xyz serves them without a robots.txt or an
    X-Robots-Tag telling crawlers to stay away. If that host ever grows a
    restrictive robots.txt, these entries stop counting.
    """
    out = []
    for it in items[:SITEMAP_IMAGES_PER_URL]:
        out.append(
            "    <image:image>"
            f"<image:loc>{html.escape(asset_url(it['file']))}</image:loc>"
            f"<image:title>{html.escape(describe(it))}</image:title>"
            "</image:image>\n"
        )
    return "".join(out)


def build_sitemap(generated: str, items: list[dict], hubs: list[dict]) -> str:
    day = generated[:10]
    urls = [
        f"  <url>\n    <loc>{SITE}/</loc>\n"
        f"    <lastmod>{day}</lastmod>\n"
        "    <changefreq>daily</changefreq>\n    <priority>1.0</priority>\n"
        f"{image_entries(items)}  </url>\n"
    ]
    urls.append(
        f"  <url>\n    <loc>{SITE}/w/</loc>\n"
        f"    <lastmod>{day}</lastmod>\n"
        "    <changefreq>weekly</changefreq>\n    <priority>0.9</priority>\n"
        "  </url>\n"
    )
    for g in hubs:
        urls.append(
            f"  <url>\n    <loc>{SITE}{hub_url(g['slug'])}</loc>\n"
            f"    <lastmod>{day}</lastmod>\n"
            "    <changefreq>weekly</changefreq>\n    <priority>0.8</priority>\n"
            f"{image_entries(g['items'])}  </url>\n"
        )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"\n'
        '        xmlns:image="http://www.google.com/schemas/sitemap-image/1.1">\n'
        + "".join(urls)
        + "</urlset>\n"
    )


def build_robots() -> str:
    """Search and AI crawlers are welcome on the site; generic rippers are not.

    robots.txt is advisory, so it is not the defence against bulk cloning —
    the Cloudflare rate-limit rule is. This just tells the well-behaved
    crawlers we actually want indexed traffic from that they may proceed.
    """
    allowed = [
        "Googlebot", "Googlebot-Image", "Bingbot", "DuckDuckBot", "Applebot",
        "GPTBot", "OAI-SearchBot", "ChatGPT-User",
        "ClaudeBot", "Claude-SearchBot", "Claude-User",
        "PerplexityBot", "Perplexity-User", "Applebot-Extended", "Google-Extended",
    ]
    blocks = [f"User-agent: {ua}\nAllow: /\n" for ua in allowed]
    blocks.append(
        # everything else: index the page, skip the bulk asset directories
        "User-agent: *\n"
        "Allow: /$\n"
        "Allow: /w/\n"
        "Allow: /assets/\n"
        "Allow: /data/wallpapers.json\n"
        "Disallow: /wallpapers/\n"
        "Crawl-delay: 10\n"
    )
    return "\n".join(blocks) + f"\nSitemap: {SITE}/sitemap.xml\n"


def source_feeds(items: list[dict]) -> str:
    """Where the archive's images came from, named the way each source names
    itself — `r/` belongs to Reddit and nothing else."""
    names = []
    for src in sorted({it.get("source") or "" for it in items}):
        if src == "reddit":
            subs = sorted({it["sub"] for it in items if it.get("source") == "reddit" and it.get("sub")})
            names.extend("r/" + s for s in subs)
        elif src == "danbooru":
            names.append("Danbooru")
        elif src:
            names.append("manual ingest")
    return ", ".join(names) or "manual ingest"


def build_llms(items: list[dict], generated: str, hubs: list[dict]) -> str:
    """llms.txt — a plain-text brief for AI crawlers and answer engines."""
    wide = sum(1 for it in items if it["w"] / it["h"] >= 1.2)
    tall = len(items) - wide
    top = ", ".join(display_name(g["name"]) for g in hubs[:12])
    return f"""# Anime Wallpaper Archive

> A curated, automatically synced archive of {len(items)} anime wallpapers,
> published as a static site at {SITE} and indexed by extracted colour palette.

Last synced: {generated}

## What it is

An open archive of anime wallpapers. A scheduled job reads curated community
feeds daily, rejects anything below 1280x720, converts each image to WebP,
extracts a five-colour dominant palette, and appends it to a static JSON index.
No accounts, no tracking, no ads, no paywall.

## Collection

- Total wallpapers: {len(items)}
- Desktop / ultrawide (landscape): {wide}
- Phone (portrait): {tall}
- Maximum resolution: up to 3840x2160 (4K)
- Format: WebP, quality 82, with a 640px thumbnail per item
- Source feeds: {source_feeds(items)}
- Character pages: {len(hubs)} at {SITE}/w/<character>{f" — {top}" if top else ""}

## Distinctive feature

Every wallpaper is indexed by the colours actually present in the image rather
than by manual tags. Users filter by hue to find a wallpaper matching a desktop
theme, and copy the extracted hex values directly into terminal, window manager
or editor colourschemes. This is aimed at the Linux ricing use case
(Hyprland, i3, AwesomeWM, Waybar, Kitty, Neovim).

## Machine-readable index

- {SITE}/data/wallpapers.json — full index: id, dimensions, aspect ratio, byte
  size, five-colour palette, source subreddit, original poster, permalink.
- {SITE}/sitemap.xml
- JSON-LD (ImageGallery + FAQPage) is embedded in the homepage.

## Rights

Copyright remains with the original artists. Each item retains a link to the
source post and the account that posted it. Takedown requests are handled via
an issue on the project repository and are applied at the next sync.
"""


def _bold_font(size: int):
    """Find a bold sans on this machine.

    Distros disagree on font paths, so try the common locations, then fall
    back to searching the font tree. Returns None if nothing is installed,
    in which case the caller uses PIL's built-in bitmap font.
    """
    from PIL import ImageFont

    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/dejavu-sans-fonts/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/usr/share/fonts/liberation-sans-fonts/LiberationSans-Bold.ttf",
    ]
    for pattern in ("**/*Sans-Bold.ttf", "**/*Sans_Bold.ttf", "**/*-Bold.ttf"):
        candidates.extend(str(p) for p in sorted(Path("/usr/share/fonts").glob(pattern))[:4])

    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except (OSError, ValueError):
            continue
    return None


def build_og(items: list[dict]) -> None:
    """Compose the social preview from the newest wallpapers.

    Generated rather than hand-made so the card always reflects what is
    actually in the archive today.
    """
    from PIL import Image, ImageDraw

    W, H, COLS, ROWS = 1200, 630, 4, 2
    card = Image.new("RGB", (W, H), (10, 10, 10))

    # thumbnails may live in R2 rather than on disk, so read through storage
    from storage import get_storage
    store = get_storage()

    tiles = []
    for it in items:
        if len(tiles) >= COLS * ROWS:
            break
        blob = store.read(it["thumb"])
        if blob:
            tiles.append(blob)

    if tiles:
        tw, th = W // COLS, H // ROWS
        for idx, blob in enumerate(tiles):
            with Image.open(io.BytesIO(blob)) as t:
                t = t.convert("RGB")
                scale = max(tw / t.width, th / t.height)
                t = t.resize((max(1, int(t.width * scale)), max(1, int(t.height * scale))),
                             Image.Resampling.LANCZOS)
                left = (t.width - tw) // 2
                top = (t.height - th) // 2
                card.paste(t.crop((left, top, left + tw, top + th)),
                           ((idx % COLS) * tw, (idx // COLS) * th))

    # darken so the wordmark stays legible over whatever the tiles happen to be
    card = Image.blend(card, Image.new("RGB", (W, H), (8, 8, 8)), 0.62)

    d = ImageDraw.Draw(card)
    d.rectangle([0, H - 96, W, H], fill=(10, 10, 10))
    d.rectangle([0, H - 100, W, H - 96], fill=(230, 25, 25))
    d.rectangle([48, 48, 52, 148], fill=(230, 25, 25))

    def text(xy, s, size, fill):
        font = _bold_font(size)
        d.text(xy, s, fill=fill, **({"font": font} if font else {}))

    text((76, 60), "WALLPAPER", 78, (234, 234, 234))
    text((76, 146), "ARCHIVE", 78, (230, 25, 25))
    text((76, H - 74), f"{len(items)} ANIME WALLPAPERS  ·  4K / ULTRAWIDE / PHONE  ·  PALETTE-INDEXED",
         22, (150, 150, 150))

    (ROOT / "assets").mkdir(exist_ok=True)
    card.save(ROOT / "assets" / "og.png", "PNG", optimize=True)


def main() -> int:
    items = load_items()
    if not items:
        print("! manifest empty — nothing to build. Run an ingest script first.")

    # newest first, so the static fallback and JSON-LD show current work
    items.sort(key=lambda x: (x.get("added", ""), x.get("id", "")), reverse=True)

    generated, changed = stamp_manifest(items)
    characters = collect_hubs(items)
    facets = collect_facet_hubs(items)
    # One list for everything that gets a page, because the sitemap, the stale
    # page sweep and the hub writer should not each need to know how many kinds
    # of hub exist.
    hubs = characters + facets
    versions = asset_versions()

    doc = INDEX.read_text()
    doc = replace_block(doc, "SEO:LD", build_jsonld(items, generated))
    doc = replace_block(doc, "SEO:GRID", "\n" + build_static_grid(items, hubs) + "\n")
    doc = stamp_asset_versions(doc, versions)
    INDEX.write_text(doc)

    write_hub_pages(hubs, generated, versions)
    (HUB_DIR / "index.html").write_text(
        build_character_index(characters, facets, generated, versions))
    (ROOT / "sitemap.xml").write_text(build_sitemap(generated, items, hubs))
    (ROOT / "robots.txt").write_text(build_robots())
    (ROOT / "_headers").write_text(build_headers())
    (ROOT / "llms.txt").write_text(build_llms(items, generated, hubs))

    # the OG card is a pure function of the newest items, so only redraw it
    # when they changed — PNG output is not byte-stable enough to rely on
    if items and (changed or not (ROOT / "assets" / "og.png").exists()):
        build_og(items)

    total = sum(it.get("bytes", 0) for it in items)
    state = "changed" if changed else "unchanged"
    untitled = sum(1 for it in items if not subject(it))
    print(f"built {len(items)} items / {total / 1e6:.1f} MB / {state} / generated {generated}")
    print(f"      {len(characters)} character + {len(facets)} colour/size pages in w/")
    if untitled:
        print(f"    ! {untitled} items have nothing to describe them — they index as "
              f"generic wallpapers. Give them a title or a --character at ingest.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
