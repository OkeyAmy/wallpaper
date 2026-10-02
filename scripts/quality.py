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

from PIL import Image, ImageChops, ImageStat

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
    # photographed merchandise. Added 2026-09-03 after post 8158469 (score 352,
    # tagged `scenery`) turned out to be a photo of a shop shelf of figurines
    # and passed every gate. Both tags are nouns for a physical object, so
    # neither fires on a drawing of one.
    "nendoroid", "merchandise",
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

# --- composition policy ----------------------------------------------------
# The archive drifted to 523-of-587 items carrying a character tag, and the
# reflex reading of that was "characters are the problem". Twelve top-scoring
# posts per query, rendered as contact sheets and actually looked at on
# 2026-09-03, said otherwise:
#
#   rating:general scenery    order:score -> 10/12 usable
#   rating:general no_humans  order:score ->  4/12 usable
#   rating:general sky|building|night|nature order:score -> ~1/4 usable
#
# The best results in the `scenery` set were *not* empty landscapes — they were
# a lone figure small inside a large atmospheric scene. So the thing to select
# against is not the presence of a person, it is the **portrait**: subject
# centred, filling the frame, background absent or flat. These lists encode
# that distinction, and nothing else here uses them as a hard reject.
#
# Matched as substrings, so "cloud" covers "clouds" and "tree" covers "trees".
# The vocabulary is deliberately union-of-sources rather than Danbooru's alone:
# Konachan says `scenic` and `nobody` where Danbooru says `scenery` and
# `no_humans`, and Wallhaven says `trees`/`water`/`sky`. A tag that is scenic on
# one board is scenic on all of them, and a scorer that only spoke Danbooru
# would rank every other source near zero for no reason but dialect.
SCENIC_TAGS = (
    "scenery", "scenic", "landscape", "cityscape", "no_humans", "nobody",
    "outdoors", "horizon", "skyline", "field", "forest", "mountain", "ocean",
    "sea", "river", "lake", "ruins", "starry_sky", "night_sky", "sunset",
    "sunrise", "snow", "rain", "cloud", "fog", "mist", "desert", "waterfall",
    "island", "valley", "shrine", "torii", "temple", "castle", "bridge",
    "railroad", "train", "street", "alley", "rooftop", "skyscraper", "neon",
    "cyberpunk", "fantasy", "floating_island", "planet", "space", "nebula",
    "aurora", "sky", "tree", "water", "grass", "flower", "star", "city",
    "moon", "petal", "cherry_blossom", "nature",
)

# How the frame is built, independent of what is in it. These are the tags a
# photographer would call "the shot" rather than "the subject".
COMPOSITION_TAGS = (
    "wide_shot", "very_wide_shot", "from_above", "from_below", "from_behind",
    "from_side", "dutch_angle", "perspective", "foreshortening",
    "atmospheric_perspective", "depth_of_field", "blurry_foreground",
    "backlighting", "sunlight", "god_rays", "lens_flare", "light_particles",
    "silhouette", "reflection", "chromatic_aberration", "vignetting",
    "scenery_focus", "absurdly_detailed_composition", "detailed_background",
)

# Spectacle: effects, action and craft that make a picture worth a screen when
# it is *not* a landscape. Added 2026-09-04 after measuring the series feed —
# `naruto_(series)`, `one_piece`, `bleach` and `kimetsu_no_yaiba` all scored a
# median of 0.00, because the original scorer only knew how to recognise
# scenery. A Naruto wallpaper is rarely a landscape and never tagged one; it is
# a character mid-technique with fire and motion in the frame, and that is a
# composed picture by any reading. Without this the archive would have gone on
# ranking every franchise piece at zero and calling it a measurement.
SPECTACLE_TAGS = (
    "glowing", "glow", "fire", "flame", "lightning", "electricity", "explosion",
    "smoke", "sparks", "energy", "aura", "magic", "spell", "light_rays",
    "sparkle", "particles", "wind", "splash", "shockwave", "motion_blur",
    "speed_lines", "action", "fighting", "battle", "combat", "attack",
    "sword", "katana", "weapon", "armor", "cape", "wings", "dragon",
    "mecha", "robot", "monster", "crystal", "ice", "blood_splatter",
    "dynamic_pose", "midair", "jumping", "running", "flying", "floating",
)

# Subject centred and filling the frame. Each is individually innocent, which
# is why they only ever subtract from a score and never reject on their own.
# `solo` is deliberately absent: it says how many people are in the picture,
# not how the picture is framed, and the strongest results measured on
# 2026-09-03 were single figures inside large scenes — exactly the posts a
# `solo` penalty would have demoted.
PORTRAIT_TAGS = (
    "portrait", "upper_body", "close-up", "bust", "head_only", "face_focus",
    "looking_at_viewer", "cowboy_shot", "profile", "expressionless",
)

# No background at all. This is the one composition signal strong enough to
# stand nearly alone: a subject floated on a flat field is a sticker, not a
# wallpaper, whatever else is true of it.
FLAT_BG_TAGS = (
    "simple_background", "white_background", "grey_background",
    "gradient_background", "black_background", "blue_background",
    "pink_background", "yellow_background", "two-tone_background",
)

# Where the community score saturates. Measured against `order:score` depth on
# 2026-09-03: page 1 median 118, page 3 median 74, page 5 median 61. A score of
# 300 is comfortably inside the top page of any query here, so past that the
# extra votes say more about how long a post has existed than how good it is.
SCORE_SATURATION = 300


def creative_score(tags, *, w: int = 0, h: int = 0,
                   score: int = 0, fav_count: int = 0) -> float:
    """How much this looks like a composed scene rather than a centred subject.

    Returns 0.0–1.0. **This is a stored rank, not a gate.** Nothing in the
    ingest path rejects on it, deliberately: quality.py's whole position is
    that taste is not measurable, and an unvalidated tag-weight threshold
    would either starve the feed or admit everything with no way to tell
    which from the logs. It is recorded on every item so a threshold can be
    calibrated later against a hand-reviewed set — the same way the tag
    blocklist above was calibrated against 502 items.

    Callers must pass the *full* tag string. Danbooru returns tags
    alphabetically, so a truncated list silently loses `scenery`, `solo`,
    `sky` and every `*_background` tag — see `select_tags` in pipeline.py.
    """
    scenic = len(_match(tags, SCENIC_TAGS))
    comp = len(_match(tags, COMPOSITION_TAGS))
    spectacle = len(_match(tags, SPECTACLE_TAGS))
    portrait = len(_match(tags, PORTRAIT_TAGS))
    flat = len(_match(tags, FLAT_BG_TAGS))

    # Caps stop a heavily-tagged post from outscoring a better one purely by
    # being tagged more thoroughly.
    s = 0.0
    s += min(scenic * 0.10, 0.30)
    s += min(comp * 0.10, 0.25)
    s += min(spectacle * 0.10, 0.25)
    s -= min(portrait * 0.12, 0.30)
    s -= 0.35 if flat else 0.0

    # Community vote, saturating. Votes are the only signal here produced by
    # humans looking at the picture, so they carry real weight — but they
    # measure popularity, which is why they cannot carry all of it. This is
    # also the only term that says anything at all about a franchise piece
    # tagged purely with character names, so it is weighted to matter.
    s += min(max(score, fav_count) / SCORE_SATURATION, 1.0) * 0.35

    # A wallpaper has to fit a screen. Square-ish art is usually an
    # illustration plate rather than something built to sit behind icons.
    if w and h:
        ar = max(w, h) / min(w, h)
        if ar < 1.15:
            s -= 0.10

    return round(min(max(s, 0.0), 1.0), 3)


# --- ingest merit gate -----------------------------------------------------
# `creative_score` used to be record-only, and the archive paid for it: of the
# 1,628 items published, 46% scored under 0.10, 21% sat on a flat/white
# background and 21% were under 1920px — all of it passing every gate because
# nothing consulted the rank. Calibrated on 2026-10-01 against the stored
# items: 0.25 keeps ~1/3 of the existing catalogue, and a hand sample of items
# at 0.30-0.45 was scenic/atmospheric pieces (shrines, cityscapes, snow, sunset
# skies) while a sample under 0.20 was white-background character sheets and
# crowded fan-art plates.
#
# This applies to *new* ingest only. It is deliberately not part of
# `reject_reasons`, which audit.py feeds to the unattended weekly cull: a
# threshold change there would flag hundreds of published items at once, trip
# --max-remove and fail the job. Use rescore.py --out to review the back
# catalogue against it.
MIN_CREATIVE = 0.25

# Both feeds are anime by construction (Danbooru is an anime board; Wallhaven
# is queried with its anime category only), but each admits a little that is
# not anime *style*: 3D renders, photorealistic paintings, and characters
# pasted onto photographs. `cosplay` is deliberately absent — on Danbooru it
# means a drawn character wearing another's outfit (36 items here, all drawn).
NOT_ANIME_STYLE_TAGS = (
    "realistic", "photorealistic", "3d", "photo_background", "photograph",
    "real_life",
)

# Tags uploaders and moderators use to say "this file or drawing is poor".
# Most are Danbooru *meta* tags, which the sync reads (tag_string_meta) but the
# manifest never stores, so this can only ever run at ingest. Memes are here
# because a joke image is content, not a wallpaper: 41 items in the archive
# carried a bare `meme` tag on 2026-10-02. AI-generated work is refused too;
# delete those entries to allow it.
LOW_QUALITY_TAGS = (
    "lowres", "low_resolution", "jpeg_artifacts", "upscaled", "image_sample",
    "scan_artifacts", "bad_anatomy", "bad_hands", "bad_feet", "bad_proportions",
    "anatomical_nonsense", "poorly_drawn", "oekaki", "ms_paint",
    "(meme)", "shitpost",
    "ai-generated", "ai_generated", "ai-assisted", "ai_assisted", "ai_art",
)
# Matched whole rather than as substrings: "meme" as a substring would also
# catch `memento_mori`.
LOW_QUALITY_EXACT = {"meme", "joke", "parody"}
INGEST_MIN_LONG_EDGE = 1920   # 1080p-class; the 1280 floor admits upscaled thumbnails
INGEST_MIN_SHORT_EDGE = 1000


def merit_reasons(tags, *, w: int = 0, h: int = 0,
                  score: int = 0, fav_count: int = 0) -> list[str]:
    """Why a policy-clean post is still not worth a slot. Empty means keep."""
    reasons = []
    if w and h:
        if max(w, h) < INGEST_MIN_LONG_EDGE or min(w, h) < INGEST_MIN_SHORT_EDGE:
            reasons.append(f"low resolution {w}x{h}")
    low = _match(tags, LOW_QUALITY_TAGS) + sorted(
        {str(t).lower() for t in tags or ()} & LOW_QUALITY_EXACT)
    if low:
        reasons.append(f"low-quality content ({low[0]})")
    style = _match(tags, NOT_ANIME_STYLE_TAGS)
    if style:
        reasons.append(f"not anime style ({style[0]})")
    flat = _match(tags, FLAT_BG_TAGS)
    if flat:
        reasons.append(f"flat background ({flat[0]})")
    rank = creative_score(tags, w=w, h=h, score=score, fav_count=fav_count)
    if rank < MIN_CREATIVE:
        reasons.append(f"creative {rank:.2f} < {MIN_CREATIVE}")
    return reasons


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


# --- pixel sharpness (ingest only) -----------------------------------------
# Dimensions say how many pixels a file has, not how many it *earned*. A 960px
# image upscaled to 2400 passes every size floor and still looks soft on a
# screen, and a JPEG re-saved at low quality carries visible 8x8 blocks. Both
# are measured here on the full-resolution original, which is why this is not
# part of `pixel_reasons`: audit.py only has the 640px thumbnail, where every
# image looks sharp and no JPEG grid survives.
#
# Calibrated 2026-10-01 on synthetic cel-shaded art (crisp 1-3px outlines on
# flat fills), since neither Danbooru nor the CDN was reachable from the
# calibration machine:
#
#   original 0.46-0.51   1.5x upscale ~0.30   2x/3x upscale 0.13-0.14
#   gaussian blur r1.5 0.09
#   blockiness: original/q90 ~1.0, q20 ~1.5, q8 ~2.1
#
# Thresholds sit well clear of the originals on purpose. Painterly art and
# heavy depth of field are legitimately soft, and a false reject costs a good
# wallpaper permanently (it goes in the reject ledger). Re-check with
# `python scripts/quality.py <files>` against real downloads and tighten.
MIN_DETAIL_RATIO = 0.20
MAX_BLOCKINESS = 1.35
_SHARP_CROP = 768


def detail_ratio(img: Image.Image) -> float:
    """Finest-scale detail relative to coarse structure, best of five crops.

    Each crop is shrunk 2x and blown back up; a picture that was upscaled or
    blurred loses almost nothing in that round trip, while genuinely sharp art
    loses its line edges. Dividing by the 8x round-trip loss normalises out how
    busy the region is. The best crop is used so a sharp subject in front of an
    intentionally blurred background still passes.
    """
    g = img.convert("L")
    cw, ch = min(_SHARP_CROP, g.width // 2), min(_SHARP_CROP, g.height // 2)
    if cw < 64 or ch < 64:
        return 1.0
    best = 0.0
    for fx, fy in ((.5, .5), (.25, .25), (.75, .25), (.25, .75), (.75, .75)):
        x, y = int(g.width * fx - cw / 2), int(g.height * fy - ch / 2)
        crop = g.crop((x, y, x + cw, y + ch))

        def loss(f: int) -> float:
            back = crop.resize((cw // f, ch // f), Image.Resampling.BOX) \
                       .resize((cw, ch), Image.Resampling.BICUBIC)
            return ImageStat.Stat(ImageChops.difference(crop, back)).mean[0]

        coarse = loss(8)
        if coarse < 1.5:            # flat sky or fill — says nothing either way
            continue
        best = max(best, loss(2) / coarse)
    return best if best else 1.0


def blockiness(img: Image.Image) -> float:
    """Step size across JPEG 8x8 block boundaries vs. inside blocks. ~1 is clean."""
    g = img.convert("L")
    w, h = min(g.width, 1024) // 8 * 8, min(g.height, 1024) // 8 * 8
    if w < 16 or h < 16:
        return 1.0
    g = g.crop((0, 0, w, h))
    # |p[x+1] - p[x]| for every x at once, then split boundary columns out.
    diff = ImageChops.difference(g.crop((1, 0, w, h)), g.crop((0, 0, w - 1, h)))
    px = diff.load()
    edge = inner = 0
    edge_n = inner_n = 0
    for y in range(0, h, 2):
        for x in range(w - 1):
            if x % 8 == 7:
                edge += px[x, y]
                edge_n += 1
            else:
                inner += px[x, y]
                inner_n += 1
    return (edge / edge_n) / max(inner / inner_n, 0.5)


def sharpness_reasons(img: Image.Image) -> list[str]:
    """Pixel-quality failures on the full-size original. Empty means keep."""
    reasons = []
    ratio = detail_ratio(img)
    if ratio < MIN_DETAIL_RATIO:
        reasons.append(f"soft or upscaled (detail {ratio:.2f} < {MIN_DETAIL_RATIO})")
    if (img.format or "").upper() == "JPEG":
        blk = blockiness(img)
        if blk > MAX_BLOCKINESS:
            reasons.append(f"JPEG compression blocks ({blk:.2f} > {MAX_BLOCKINESS})")
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


if __name__ == "__main__":
    # Calibration aid: print the pixel measures for real files.
    #   python scripts/quality.py a.jpg b.png ...
    import sys
    for path in sys.argv[1:]:
        with Image.open(path) as im:
            im.load()
            print(f"{path}: {im.width}x{im.height}  detail={detail_ratio(im):.2f}"
                  f"  blockiness={blockiness(im):.2f}  {sharpness_reasons(im) or 'ok'}")
