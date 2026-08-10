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
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from pipeline import MANIFEST, ROOT, load_items

SITE = "https://wallpapers.okeyamy.xyz"
INDEX = ROOT / "index.html"

STATIC_CELLS = 24        # items rendered as real <img> in the no-JS fallback
STATIC_LINKS = 120       # additional text-only links for crawl depth
LD_ITEMS = 100           # items described in JSON-LD


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

    unchanged = previous.get("items") == items and bool(previous.get("generated"))
    generated = previous["generated"] if unchanged else \
        datetime.now(timezone.utc).isoformat(timespec="seconds")

    MANIFEST.write_text(json.dumps({"generated": generated, "items": items}, indent=1))
    return generated, not unchanged


def replace_block(text: str, tag: str, payload: str) -> str:
    """Swap the content between <!-- TAG --> and <!-- /TAG -->."""
    pattern = re.compile(rf"(<!-- {tag} -->).*?(<!-- /{tag} -->)", re.S)
    if not pattern.search(text):
        raise SystemExit(f"marker <!-- {tag} --> missing from index.html")
    return pattern.sub(lambda m: m.group(1) + payload + m.group(2), text)


def describe(it: dict) -> str:
    """Human-readable alt/name text. Crawlers and screen readers read this."""
    title = it.get("title") or "Anime wallpaper"
    return f"{title} — {it['w']}×{it['h']} anime wallpaper"


def build_static_grid(items: list[dict]) -> str:
    cells = []
    for it in items[:STATIC_CELLS]:
        alt = html.escape(describe(it))
        cells.append(
            f'<a class="cell" role="listitem" href="/{it["file"]}">'
            f'<img class="cell__img is-loaded" src="/{it["thumb"]}" alt="{alt}" '
            f'width="{it["w"]}" height="{it["h"]}" loading="lazy" decoding="async">'
            f'<span class="cell__hud"><span class="cell__name">{html.escape(it.get("title") or "Untitled")}</span>'
            f'<span class="cell__res">{it["w"]}×{it["h"]}</span></span></a>'
        )

    rest = items[STATIC_CELLS:STATIC_CELLS + STATIC_LINKS]
    if rest:
        links = "".join(
            f'<li><a href="/{it["file"]}">{html.escape(describe(it))}</a></li>' for it in rest
        )
        cells.append(
            '<div class="seolist"><h2 class="foot__h">[ FULL INDEX ]</h2>'
            f"<ul>{links}</ul></div>"
        )
    return "\n".join(cells)


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
        "image": [
            {
                "@type": "ImageObject",
                "@id": f"{SITE}/{it['file']}",
                "contentUrl": f"{SITE}/{it['file']}",
                "thumbnailUrl": f"{SITE}/{it['thumb']}",
                "name": describe(it),
                "width": {"@type": "QuantitativeValue", "value": it["w"], "unitCode": "E37"},
                "height": {"@type": "QuantitativeValue", "value": it["h"], "unitCode": "E37"},
                "encodingFormat": "image/webp",
                "uploadDate": it.get("added", ""),
                **({"creditText": f"u/{it['author']}"} if it.get("author") else {}),
                **({"isBasedOn": it["permalink"]} if it.get("permalink") else {}),
            }
            for it in items[:LD_ITEMS]
        ],
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


def build_sitemap(generated: str) -> str:
    day = generated[:10]
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        f"  <url><loc>{SITE}/</loc><lastmod>{day}</lastmod>"
        "<changefreq>daily</changefreq><priority>1.0</priority></url>\n"
        "</urlset>\n"
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
        "Allow: /assets/\n"
        "Allow: /data/wallpapers.json\n"
        "Disallow: /wallpapers/\n"
        "Crawl-delay: 10\n"
    )
    return "\n".join(blocks) + f"\nSitemap: {SITE}/sitemap.xml\n"


def build_llms(items: list[dict], generated: str) -> str:
    """llms.txt — a plain-text brief for AI crawlers and answer engines."""
    subs = sorted({it["sub"] for it in items if it.get("sub")})
    wide = sum(1 for it in items if it["w"] / it["h"] >= 1.2)
    tall = len(items) - wide
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
- Source feeds: {", ".join("r/" + s for s in subs) or "manual ingest"}

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

    tiles = [it for it in items if (ROOT / it["thumb"]).exists()][: COLS * ROWS]
    if tiles:
        tw, th = W // COLS, H // ROWS
        for idx, it in enumerate(tiles):
            with Image.open(ROOT / it["thumb"]) as t:
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

    doc = INDEX.read_text()
    doc = replace_block(doc, "SEO:LD", build_jsonld(items, generated))
    doc = replace_block(doc, "SEO:GRID", "\n" + build_static_grid(items) + "\n")
    INDEX.write_text(doc)

    (ROOT / "sitemap.xml").write_text(build_sitemap(generated))
    (ROOT / "robots.txt").write_text(build_robots())
    (ROOT / "llms.txt").write_text(build_llms(items, generated))

    # the OG card is a pure function of the newest items, so only redraw it
    # when they changed — PNG output is not byte-stable enough to rely on
    if items and (changed or not (ROOT / "assets" / "og.png").exists()):
        build_og(items)

    total = sum(it.get("bytes", 0) for it in items)
    state = "changed" if changed else "unchanged"
    print(f"built {len(items)} items / {total / 1e6:.1f} MB / {state} / generated {generated}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
