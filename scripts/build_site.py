#!/usr/bin/env python3
"""Stamp the manifest and generate everything a crawler reads.

The gallery is rendered client-side, which means a crawler that does not run
JavaScript would otherwise see an empty page. This script injects a real
static gallery and a JSON-LD description of every item into index.html, then
writes sitemap.xml, robots.txt and llms.txt.

    python scripts/build_site.py
"""

from __future__ import annotations

import html
import io
import json
import os
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from pipeline import MANIFEST, ROOT, load_items

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
MIN_HUB_ITEMS = 2
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
            g = groups.setdefault(slug, {"slug": slug, "name": name, "items": []})
            if it not in g["items"]:
                g["items"].append(it)

    hubs = [g for g in groups.values() if len(g["items"]) >= MIN_HUB_ITEMS]
    for g in hubs:
        # widest first: the biggest wallpaper is the best thing to lead with
        g["items"].sort(key=lambda x: x["w"] * x["h"], reverse=True)
    hubs.sort(key=lambda g: (-len(g["items"]), g["slug"]))
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
    links = "".join(
        f'<li><a href="{hub_url(g["slug"])}">{html.escape(display_name(g["name"]))} '
        f'wallpapers ({len(g["items"])})</a></li>'
        for g in hubs
    )
    return ('<nav class="seolist"><h2 class="foot__h">[ BY CHARACTER ]</h2>'
            f"<ul>{links}</ul></nav>")


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


def hub_summary(name: str, items: list[dict]) -> str:
    """A sentence about what is actually on this page.

    Written from the items rather than from a template so two character pages
    never read identically — a page whose only unique word is the character's
    name is a page Google has grounds to treat as a duplicate of the others.
    """
    n = len(items)
    counts = Counter(orientation(it) for it in items)
    parts = [f"{c} {kind}" for kind, c in counts.most_common()]
    shapes = ", ".join(parts[:-1]) + (" and " + parts[-1] if len(parts) > 1 else parts[0])
    biggest = max(items, key=lambda x: x["w"] * x["h"])
    return (
        f"{n} {display_name(name)} wallpapers — {shapes} — "
        f"up to {biggest['w']}×{biggest['h']}, free to download. "
        "Each one lists its dominant colour palette and links back to the "
        "original artist's post."
    )


def build_hub_page(hub: dict, generated: str) -> str:
    name = display_name(hub["name"])
    items = hub["items"]
    summary = hub_summary(hub["name"], items)
    url = SITE + hub_url(hub["slug"])

    # kept under ~60 chars so search results show the whole thing
    title = f"{name} Wallpapers — 4K, Phone & Desktop"[:60]

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
        "name": f"{name} wallpapers",
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
            {"@type": "ListItem", "position": 2, "name": f"{name} wallpapers", "item": url},
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
<meta property="og:title" content="{html.escape(name)} wallpapers">
<meta property="og:description" content="{html.escape(summary)}">
<meta property="og:type" content="website">
<meta property="og:url" content="{url}">
<meta property="og:image" content="{asset_url(items[0]['file'])}">
<meta name="twitter:card" content="summary_large_image">
<link rel="stylesheet" href="/assets/style.css">
<link rel="icon" href="/assets/favicon.svg" type="image/svg+xml">
{ld_block}
</head>
<body>
<header class="bar" role="banner">
  <a href="/" class="bar__logo">WALLPAPER ARCHIVE</a>
</header>
<main class="wrap">
  <h1 class="hub__h">{html.escape(name)} wallpapers</h1>
  <p class="hub__lede">{html.escape(summary)}</p>
  <div class="grid" role="list">
{cells}
  </div>
  <a class="hub__back" href="/">← Browse the full wallpaper archive</a>
</main>
<footer class="foot">
  <p>Copyright remains with the original artists. Every wallpaper links back to
  the post it came from.</p>
</footer>
</body>
</html>
"""


def write_hub_pages(hubs: list[dict], generated: str) -> None:
    """Write w/<slug>.html, removing pages whose character fell below the line.

    Stale files matter here: a character page that stops being generated but
    stays on disk keeps getting served and keeps claiming a canonical URL.
    """
    HUB_DIR.mkdir(exist_ok=True)
    wanted = {f"{g['slug']}.html" for g in hubs}

    for old in HUB_DIR.glob("*.html"):
        if old.name not in wanted:
            old.unlink()

    for g in hubs:
        (HUB_DIR / f"{g['slug']}.html").write_text(build_hub_page(g, generated))


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

/assets/*
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
    hubs = collect_hubs(items)

    doc = INDEX.read_text()
    doc = replace_block(doc, "SEO:LD", build_jsonld(items, generated))
    doc = replace_block(doc, "SEO:GRID", "\n" + build_static_grid(items, hubs) + "\n")
    INDEX.write_text(doc)

    write_hub_pages(hubs, generated)
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
    print(f"      {len(hubs)} character pages in w/")
    if untitled:
        print(f"    ! {untitled} items have nothing to describe them — they index as "
              f"generic wallpapers. Give them a title or a --character at ingest.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
