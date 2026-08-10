"""Shared ingest primitives: hashing, palette extraction, WebP encoding.

Every wallpaper — scraped or hand-dropped — enters the archive through
`ingest_image`, so the on-disk shape of an item is defined in exactly one place.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, asdict, field
from pathlib import Path

from PIL import Image

from storage import get_storage

# Pillow refuses very large images by default (decompression-bomb guard).
# Wallpapers are legitimately big, so raise the ceiling rather than remove it.
Image.MAX_IMAGE_PIXELS = 120_000_000

ROOT = Path(__file__).resolve().parent.parent
FULL_DIR = ROOT / "wallpapers"
THUMB_DIR = FULL_DIR / "thumbs"
DATA_DIR = ROOT / "data"
MANIFEST = DATA_DIR / "wallpapers.json"

# --- ingest policy ---------------------------------------------------------
MIN_PIXELS = 1280 * 720          # anything smaller isn't a usable wallpaper
MAX_EDGE = 3840                  # cap the long edge; 4K is the useful ceiling
FULL_QUALITY = 82
THUMB_WIDTH = 640
THUMB_QUALITY = 72
PALETTE_SIZE = 5
DHASH_DISTANCE = 6               # <= this many differing bits == duplicate


@dataclass
class Item:
    """One wallpaper as it appears in the public manifest."""
    id: str
    file: str
    thumb: str
    w: int
    h: int
    ratio: str
    bytes: int
    palette: list[str]
    source: str
    added: str
    title: str = ""
    sub: str = ""
    author: str = ""
    permalink: str = ""
    sha256: str = ""
    dhash: str = ""
    tags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


# --- hashing ---------------------------------------------------------------

def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def dhash(img: Image.Image, size: int = 8) -> str:
    """Difference hash: robust to rescaling and re-encoding, unlike sha256.

    Reddit reposts are usually the same picture at a different size or JPEG
    quality, which sha256 cannot catch but this can.
    """
    small = img.convert("L").resize((size + 1, size), Image.Resampling.LANCZOS)
    px = list(small.getdata())
    bits = 0
    for row in range(size):
        base = row * (size + 1)
        for col in range(size):
            bits = (bits << 1) | int(px[base + col] < px[base + col + 1])
    return f"{bits:016x}"


def hamming(a: str, b: str) -> int:
    if not a or not b:
        return 64
    return bin(int(a, 16) ^ int(b, 16)).count("1")


# --- colour ----------------------------------------------------------------

def _rgb_to_hsl(r: int, g: int, b: int) -> tuple[float, float, float]:
    r_, g_, b_ = r / 255, g / 255, b / 255
    hi, lo = max(r_, g_, b_), min(r_, g_, b_)
    l = (hi + lo) / 2
    d = hi - lo
    if d == 0:
        return 0.0, 0.0, l
    s = d / (1 - abs(2 * l - 1)) if l not in (0, 1) else 0.0
    if hi == r_:
        h = 60 * (((g_ - b_) / d) % 6)
    elif hi == g_:
        h = 60 * ((b_ - r_) / d + 2)
    else:
        h = 60 * ((r_ - g_) / d + 4)
    return h % 360, s, l


def _distance(a: tuple[int, int, int], b: tuple[int, int, int]) -> float:
    """Weighted RGB distance that tracks perceived difference better than
    plain Euclidean, without pulling in a colour-science dependency."""
    rm = (a[0] + b[0]) / 2
    dr, dg, db = a[0] - b[0], a[1] - b[1], a[2] - b[2]
    return ((2 + rm / 256) * dr * dr + 4 * dg * dg + (2 + (255 - rm) / 256) * db * db) ** 0.5


def extract_palette(img: Image.Image, n: int = PALETTE_SIZE) -> list[str]:
    """Dominant colours, chosen for usefulness as a theme palette.

    Frequency alone is a poor selector: most wallpapers are mostly dark or
    mostly sky, so a purely frequency-ranked palette returns five nearly
    identical muddy swatches and throws away the accent colours that make
    the image recognisable.

    So: over-quantise, then rank clusters by area *and* colourfulness, and
    reject candidates too close to one already picked. The result keeps the
    true dominant tone in first position while still surfacing the accents.
    """
    small = img.convert("RGB")
    small.thumbnail((160, 160), Image.Resampling.LANCZOS)

    # MEDIANCUT splits the colour cube by actual distribution rather than a
    # fixed lattice, so accents survive the reduction instead of being
    # merged into the nearest bulk colour.
    over = max(n * 4, 16)
    quantised = small.quantize(colors=over, method=Image.Quantize.MEDIANCUT)
    palette = quantised.getpalette() or []
    colours = quantised.getcolors() or []
    if not colours:
        return ["#808080"]

    total = sum(c for c, _ in colours) or 1
    scored = []
    for count, idx in colours:
        rgb = tuple(palette[idx * 3: idx * 3 + 3])
        if len(rgb) < 3:
            continue
        _h, s, l = _rgb_to_hsl(*rgb)
        share = count / total

        # near-black and near-white carry no theme information; keep them
        # eligible but heavily penalised so they only win when the image
        # genuinely is monochrome.
        extremity = 1.0 if 0.06 < l < 0.94 else 0.15
        # sqrt(share) compresses the gap between a huge background and a
        # small but vivid accent; the saturation term then lets the accent
        # overtake it when it is much more colourful.
        score = (share ** 0.5) * (0.30 + 0.70 * s) * extremity
        scored.append((score, share, rgb))

    scored.sort(reverse=True, key=lambda x: x[0])

    picked: list[tuple[int, int, int]] = []
    for _score, _share, rgb in scored:
        if all(_distance(rgb, prev) > 60 for prev in picked):   # enforce spread
            picked.append(rgb)
        if len(picked) == n:
            break

    # relax the spread rule if the image genuinely lacks that many distinct tones
    for _score, _share, rgb in scored:
        if len(picked) >= n:
            break
        if rgb not in picked:
            picked.append(rgb)

    # lead with the largest area so palette[0] stays a sane "dominant colour"
    # for hue bucketing, then order the rest by score
    if picked:
        by_area = max(picked, key=lambda c: next(sh for _s, sh, r in scored if r == c))
        picked.remove(by_area)
        picked.insert(0, by_area)

    return [f"#{r:02x}{g:02x}{b:02x}" for r, g, b in picked] or ["#808080"]


# --- geometry --------------------------------------------------------------

def aspect_label(w: int, h: int) -> str:
    from math import gcd
    d = gcd(w, h) or 1
    a, b = w // d, h // d
    if a > 40 or b > 40:                     # unreducible oddity -> decimal
        return f"{w / h:.2f}:1"
    return f"{a}:{b}"


def slugify(text: str, limit: int = 70) -> str:
    s = re.sub(r"\[[^\]]*\]|\([^)]*\)", " ", text or "")     # drop [4K] / (1920x1080)
    s = re.sub(r"\s+", " ", s).strip(" -–—|")
    return s[:limit].strip()


# --- ingest ----------------------------------------------------------------

def _encode(img: Image.Image, quality: int) -> bytes:
    """WebP bytes for an image, without touching the filesystem — the storage
    backend decides where (or whether) they land on disk."""
    import io
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "WEBP", quality=quality, method=5)
    return buf.getvalue()


def ingest_image(
    raw: bytes,
    *,
    source: str,
    added: str,
    title: str = "",
    sub: str = "",
    author: str = "",
    permalink: str = "",
    tags: list[str] | None = None,
    existing: list[dict] | None = None,
    storage=None,
) -> Item | None:
    """Decode, validate, re-encode and describe one image.

    Returns None when the image fails policy (too small, corrupt, not an
    image at all) or is a duplicate of something in ``existing`` — callers
    treat that as "skip", not as an error.

    Duplicate detection runs *before* anything is written to storage. The
    encoded key is content-addressed (``sha256(raw)[:12]``), so a duplicate
    upload would land on the exact key the original occupies — deleting it
    afterwards as "this rejected candidate's own file" would actually delete
    the live, already-published image. Checking first means a duplicate
    never touches storage at all, which avoids that failure mode entirely
    rather than relying on cleanup after the fact.
    """
    import io

    try:
        img = Image.open(io.BytesIO(raw))
        img.load()
    except Exception:
        return None

    if img.width * img.height < MIN_PIXELS:
        return None

    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")

    digest = sha256_bytes(raw)
    ident = digest[:12]
    fingerprint = dhash(img)

    if existing is not None and is_duplicate_hash(digest, fingerprint, existing):
        return None

    palette = extract_palette(img)

    # cap the long edge before encoding — 8K sources are bandwidth, not value
    full = img.copy()
    if max(full.width, full.height) > MAX_EDGE:
        full.thumbnail((MAX_EDGE, MAX_EDGE), Image.Resampling.LANCZOS)

    full_key = f"wallpapers/{ident}.webp"
    thumb_key = f"wallpapers/thumbs/{ident}.webp"

    full_bytes = _encode(full, FULL_QUALITY)
    thumb = img.copy()
    thumb.thumbnail((THUMB_WIDTH, THUMB_WIDTH * 3), Image.Resampling.LANCZOS)
    thumb_bytes = _encode(thumb, THUMB_QUALITY)

    store = storage or get_storage()
    store.put(full_key, full_bytes)
    store.put(thumb_key, thumb_bytes)

    return Item(
        id=ident,
        file=full_key,
        thumb=thumb_key,
        w=full.width,
        h=full.height,
        ratio=aspect_label(full.width, full.height),
        bytes=len(full_bytes),
        palette=palette,
        source=source,
        added=added,
        title=slugify(title),
        sub=sub,
        author=author,
        permalink=permalink,
        sha256=digest,
        dhash=fingerprint,
        tags=tags or [],
    )


# --- manifest --------------------------------------------------------------

def load_items() -> list[dict]:
    if not MANIFEST.exists():
        return []
    try:
        return json.loads(MANIFEST.read_text()).get("items", [])
    except json.JSONDecodeError:
        return []


def is_duplicate_hash(sha256: str, dhash_value: str, existing: list[dict]) -> bool:
    """Pure hash comparison — used by ingest_image *before* it uploads
    anything, which is what lets duplicates be rejected without ever
    writing (and therefore never needing to unwrite) a file."""
    for old in existing:
        if old.get("sha256") == sha256:
            return True
        if hamming(old.get("dhash", ""), dhash_value) <= DHASH_DISTANCE:
            return True
    return False


def is_duplicate(candidate: Item, existing: list[dict]) -> bool:
    """Back-compat wrapper around is_duplicate_hash for an already-built Item.

    Prefer passing ``existing=`` to ingest_image instead: that checks before
    upload. Calling this *after* ingest_image and then discard()-ing on a
    hit is the pattern that used to delete live files — see discard()'s
    docstring.
    """
    return is_duplicate_hash(candidate.sha256, candidate.dhash, existing)


def discard(item: Item, storage=None) -> None:
    """Delete an item's files from storage.

    Danger: if ``item`` was built from bytes identical to something already
    in the archive, its key (content-addressed as sha256[:12]) is the same
    key the original occupies — calling discard() on it deletes the live
    file, not a orphaned one. ingest_image() avoids this by checking
    ``existing`` before it ever uploads, so a duplicate never reaches this
    function in the first place. Only call discard() on an item you are
    certain was actually written by *this* call (e.g. cleaning up a test
    ingest), never as a generic "reject this candidate" step.
    """
    store = storage or get_storage()
    for key in (item.file, item.thumb):
        store.delete(key)
