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
