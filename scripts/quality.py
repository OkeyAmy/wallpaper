"""What counts as a wallpaper — the single definition of it.

This policy used to exist in two places that disagreed: `sync_danbooru.py`
filtered on `rating:general` and a score floor at fetch time, and `audit.py`
carried its own tag blocklist and pixel heuristics for after the fact. An item
could therefore pass ingest and fail an audit run under rules that were never
reconciled. Both now import from here, so "is this a wallpaper" has exactly one
answer and tightening it improves the incoming feed and flags the back catalogue
in the same edit.

Three layers, cheapest first:

  * `tag_reasons`   — metadata. Free, and by far the most accurate signal for
                      Danbooru items, because a human already tagged the post.
  * `shape_reasons` — geometry. A picture that doesn't fit a screen isn't a
                      wallpaper regardless of what it depicts.
  * `pixel_reasons` — the image itself. The only layer that works for the
                      hand-dropped items, which arrive with no tags at all.

What none of these detect is a well-composed frame lifted out of an episode:
it has no bars, no text, real colour, ordinary proportions, and (when it was
uploaded as fan art rather than a screenshot) no `screencap` tag either. That
is a judgement call about provenance, not a measurable property.

Be clear about what that means for an archive nobody is watching: those will
accumulate. Roughly one in eight of the items removed in the 2026-08-22 clean-up
were of that kind, and nothing here would have caught them. `review_sheets.py`
renders the archive as contact sheets so a person can find them in a few
minutes, but it is a thing someone has to choose to run — it is not part of any
scheduled job, and the automated layers do not substitute for it. The honest
summary is that this file keeps the archive free of structural junk forever,
and free of taste-level junk only as often as somebody looks.
"""

from __future__ import annotations

from PIL import Image

# --- tag policy ------------------------------------------------------------
# Matched as substrings of underscored Danbooru tags, so "comic" also catches
# "comic_panel" but never "cosplay". Kept to tags that were *precise* on this
# archive rather than merely correlated: measured against a hand-reviewed set
# of 502 items, every entry here appeared almost exclusively on items the
# review rejected. Tags like `1boy` or `6+girls` were commoner among rejects
# too, but they sit on plenty of good wallpapers, so they are deliberately out.
TAG_BLOCKLIST = (
    # printed page / sequential art
    "comic", "4koma", "manga", "speech_bubble", "spoken_", "translated",
    # burned-in lettering. `artist_name`, `copyright_name` and `dated` are
    # deliberately absent: a signature or a date in the corner is normal on
    # good art and they were the single largest source of wrong rejects when
    # this was measured (23 of them, all keepers).
    "english_text", "watermark", "text_focus", "subtitled", "fake_screenshot",
    # sheets, line-ups and other non-single-image layouts
    "multiple_views", "character_sheet", "reference_sheet", "chart",
    "absolutely_everyone", "album_cover", "cover_page",
    # not a finished picture
    "sketch", "lineart", "monochrome", "greyscale", "screencap",
    "photo_(medium)", "letterboxed", "pillarboxed", "transparent_background",
)

# Suggestive tags. Deliberately narrow: on Danbooru a fully-clothed character
# is routinely tagged `breasts` (79 items in this archive carried it, and the
# visual review rejected six of them), so the merely anatomical tags are not
# here. These describe what the picture is *about*.
SUGGESTIVE_TAGS = (
    "cleavage", "underboob", "sideboob", "downblouse", "cameltoe",
    "upskirt", "skirt_lift", "panties", "underwear", "lingerie", "bra_",
    "no_bra", "nipples", "topless", "bottomless", "nude", "naked_",
    "wet_clothes", "see-through", "spread_legs", "ass_focus", "breast_focus",
    "swimsuit", "bikini",
)

# --- geometry policy -------------------------------------------------------
MIN_LONG_EDGE = 1280         # below this there is no screen it fills
MAX_W_OVER_H = 4.00          # past a dual-monitor panorama; a strip, not a picture
MAX_H_OVER_W = 3.00          # past any phone; the tallest keeper measured 2.83

# --- pixel policy ----------------------------------------------------------
MIN_SATURATION = 0.035       # mean HSV saturation. Low on purpose: a muted,
                             # near-monochrome palette is a legitimate look, so this
                             # only fires on something with essentially no hue at all.
BAR_TOLERANCE = 6.0          # per-line stddev under this counts as "flat"
BAR_DARK, BAR_BRIGHT = 22, 233
MIN_BAR_FRACTION = 0.10      # a flat edge band thicker than this is a bar, not a dark sky
MIN_DETAIL = 1.2             # mean |gradient|; below this the frame is empty.
                             # Minimalist art is deliberately sparse, so this is set
                             # where only near-blank cards fall under it.


def _match(tags, needles) -> list[str]:
    hits = []
    for tag in tags or ():
        low = str(tag).lower()
        for n in needles:
            if n in low and n not in hits:
                hits.append(n)
    return hits


def tag_reasons(tags, *, include_suggestive: bool = True) -> list[str]:
    """Policy failures visible in the tags alone.

    ``include_suggestive`` exists because the two blocklists answer different
    questions — "this is not a wallpaper" versus "this is not what this site
    publishes" — and a caller auditing the archive may want to count them
    separately.
    """
    reasons = []
    hit = _match(tags, TAG_BLOCKLIST)
    if hit:
        reasons.append(f"tagged {','.join(hit)}")
    if include_suggestive:
        sugg = _match(tags, SUGGESTIVE_TAGS)
        if sugg:
            reasons.append(f"suggestive tags: {','.join(sugg)}")
    return reasons


def shape_reasons(w: int, h: int) -> list[str]:
    reasons = []
    if not w or not h:
        return ["no dimensions"]
    if max(w, h) < MIN_LONG_EDGE:
        reasons.append(f"long edge {max(w, h)}px < {MIN_LONG_EDGE}")
    if w / h > MAX_W_OVER_H:
        reasons.append(f"aspect {w / h:.2f}:1 too wide")
    if h / w > MAX_H_OVER_W:
        reasons.append(f"aspect {h / w:.2f}:1 too tall")
    return reasons


def _flat_band(lines: list[list[int]]) -> int:
    """How many consecutive near-uniform, near-black/white lines lead this edge.

    Counts inward from one edge and stops at the first line with real content,
    which is what separates a letterbox bar from a picture that merely opens on
    a dark sky: the bar is uniform *and* extreme for its whole depth.
    """
    n = 0
    for line in lines:
        count = len(line) or 1
        mean = sum(line) / count
        var = sum((p - mean) ** 2 for p in line) / count
        if var ** 0.5 > BAR_TOLERANCE or BAR_DARK < mean < BAR_BRIGHT:
            break
        n += 1
    return n


def _rows(grey: Image.Image) -> list[list[int]]:
    px = list(grey.getdata())
    w = grey.width
    return [px[y * w:(y + 1) * w] for y in range(grey.height)]


def pixel_reasons(img: Image.Image) -> list[str]:
    """Policy failures measurable from the image, for items with no metadata."""
    reasons = []

    hsv = img.convert("HSV")
    hist = hsv.getchannel("S").histogram()
    total = sum(hist) or 1
    mean_s = sum(i * c for i, c in enumerate(hist)) / total / 255
    if mean_s < MIN_SATURATION:
        reasons.append(f"near-zero colour (sat {mean_s:.3f})")

    # Downscale once: bars and flatness survive it, and it keeps this cheap
    # enough to run over the whole archive.
    small = img.convert("L")
    small.thumbnail((256, 256), Image.Resampling.LANCZOS)
    rows = _rows(small)
    cols = _rows(small.transpose(Image.Transpose.ROTATE_90))

    top, bottom = _flat_band(rows), _flat_band(rows[::-1])
    left, right = _flat_band(cols), _flat_band(cols[::-1])
    v_bars = (top + bottom) / max(1, len(rows))
    h_bars = (left + right) / max(1, len(cols))
    if v_bars > MIN_BAR_FRACTION:
        reasons.append(f"letterboxed ({v_bars:.0%} of height is flat bar)")
    if h_bars > MIN_BAR_FRACTION:
        reasons.append(f"pillarboxed ({h_bars:.0%} of width is flat bar)")

    # Mean absolute gradient. A wallpaper has texture somewhere; a logo on a
    # flat field, a mostly-empty gradient or a blank card does not.
    gx = sum(abs(r[x + 1] - r[x]) for r in rows for x in range(len(r) - 1))
    gy = sum(abs(rows[y + 1][x] - rows[y][x])
             for y in range(len(rows) - 1) for x in range(len(rows[0])))
    pairs = max(1, len(rows) * (len(rows[0]) - 1) + (len(rows) - 1) * len(rows[0]))
    detail = (gx + gy) / pairs
    if detail < MIN_DETAIL:
        reasons.append(f"almost no detail (gradient {detail:.1f})")

    return reasons


def reject_reasons(*, tags=(), w: int = 0, h: int = 0,
                   img: Image.Image | None = None,
                   include_suggestive: bool = True) -> list[str]:
    """Every reason this image fails policy; empty means it passes.

    Callers pass whatever they have. `sync_danbooru.py` calls it with the tags
    and dimensions from the API response *before downloading the file*, which
    is the cheapest possible place to say no.
    """
    reasons = tag_reasons(tags, include_suggestive=include_suggestive)
    if w and h:
        reasons += shape_reasons(w, h)
    if img is not None:
        reasons += pixel_reasons(img)
    return reasons
