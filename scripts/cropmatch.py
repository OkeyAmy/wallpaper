"""Duplicate detection that survives cropping and zooming.

`dhash` in pipeline.py catches the same picture re-saved at another size or
JPEG quality, because it hashes the *whole frame*. It is blind to a crop: a
Wallhaven uploader who takes a portrait Danbooru illustration and cuts a 16:9
band out of it produces a frame whose dhash has nothing in common with the
original's, and the archive ends up with both.

This uses local feature matching instead — the standard technique for
near-duplicate detection under crops. Each image is reduced to a few hundred
ORB keypoints (corners of line-art, highlights, texture), each with a 256-bit
descriptor. A candidate is matched against every stored keypoint at once; the
few stored images that share the most descriptors are then checked
geometrically: their shared points must agree on *one* scale + shift between
the two frames (RANSAC). A crop always passes that, because every shared point
moved the same way. Two different pictures essentially never do — chance
descriptor matches point in random directions.

Calibrated 2026-10-02 on synthetic cel-shaded art — 300 stored pictures, 70
crops in both directions (60-100% zoom, random offset, resized, brightened,
JPEG q70), 2,745 pairs of *different* pictures: unrelated pairs never produced
more than 7 consistent matches, true crops a median of 49. Two unrelated
pictures reached 13-14 at 1,000 stored items, both from perfect circles (a
circle is a scaled copy of any other circle — an artefact of the generator),
so MIN_INLIERS is 16: ~90% of test crops caught, none falsely. Real art has
far more distinctive detail than the synthetic set, so expect better.

Simpler approaches were measured first and rejected on 2026-10-02: comparing
64px greyscale copies at sliding offsets either flagged half of a set of
unrelated pictures as duplicates (plain brightness — any sky correlates with
any other sky) or missed a third of true crops (edges only), and no setting
separated the two.

The index lives in storage (R2) as one file, not in git: ~7 KB per image would
grow the repository by tens of megabytes. Features of removed items are kept,
so a culled picture is also refused when it comes back cropped.
"""

from __future__ import annotations

import io
import sys

import cv2
import numpy as np
from PIL import Image

INDEX_KEY = "index/features.npz"

WORK = 768               # long edge features are computed at
STORE_FEATURES = 500     # keypoints kept per stored image (~18 KB)
QUERY_FEATURES = 1000    # keypoints taken from a candidate
VOTE_FEATURES = 500      # strongest of those used for the shortlist (cost is linear)
VOTE_DIST = 48           # Hamming bits: a descriptor "agrees" below this
SHORTLIST = 15           # stored images that get the geometric check
MIN_VOTES = 8
RATIO = 0.8              # Lowe ratio test for the per-pair matches
RANSAC_PX = 6.0          # at WORK resolution
MIN_INLIERS = 16         # geometrically consistent matches == same picture
MIN_INLIER_SHARE = 0.15  # ...and a meaningful share of the attempted matches
SCALE_RANGE = (0.25, 4.0)
MAX_ROTATION_DEG = 8     # crops and resizes do not rotate


def _read_strict(store, key: str) -> bytes | None:
    """Like store.read, but only *absence* returns None.

    R2Storage.read swallows every error as "not found". For images that is
    fine; here it is not — a transient failure read as "no index yet" would
    let the end-of-run save replace every fingerprint with this run's few.
    """
    if getattr(store, "kind", "") != "r2":
        return store.read(key)
    from botocore.exceptions import ClientError
    try:
        return store.client.get_object(Bucket=store.bucket, Key=key)["Body"].read()
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
            return None
        raise


def _grey(img: Image.Image) -> np.ndarray:
    g = img.convert("L")
    if max(g.size) > 2 * WORK:          # cheap pre-shrink of 4K sources
        g = g.copy()
        g.thumbnail((2 * WORK, 2 * WORK), Image.Resampling.BOX)
    a = np.asarray(g)
    s = WORK / max(a.shape)
    return cv2.resize(a, (max(1, round(a.shape[1] * s)), max(1, round(a.shape[0] * s))),
                      interpolation=cv2.INTER_AREA)


def features(img: Image.Image, n: int | None = None):
    """(points float32 Nx2, descriptors uint8 Nx32) at WORK resolution."""
    orb = cv2.ORB_create(nfeatures=n or STORE_FEATURES, fastThreshold=10)
    kps, desc = orb.detectAndCompute(_grey(img), None)
    if desc is None or not kps:
        return np.zeros((0, 2), np.float32), np.zeros((0, 32), np.uint8)
    order = np.argsort([-k.response for k in kps])     # strongest first
    return np.array([kps[i].pt for i in order], dtype=np.float32), desc[order]


class Index:
    def __init__(self):
        self.ids: list[str] = []
        self.pts: list[np.ndarray] = []
        self.desc: list[np.ndarray] = []
        self._pos: dict[str, int] = {}
        self._flat = None                 # (matcher, owner per train image), lazily
        self.dirty = False
        # False when a stored index exists but could not be read. Saving in
        # that state would overwrite every fingerprint with a partial set.
        self.writable = True

    def __len__(self) -> int:
        return len(self.ids)

    def __contains__(self, ident: str) -> bool:
        return ident in self._pos

    # --- persistence -------------------------------------------------------
    @classmethod
    def load(cls, store) -> "Index":
        idx = cls()
        try:
            raw = _read_strict(store, INDEX_KEY)
        except Exception as e:                  # noqa: BLE001 — any read failure
            print(f"  ! crop index unreadable ({e}); crop matching off this run",
                  file=sys.stderr)
            idx.writable = False
            return idx
        if not raw:
            return idx                          # first run: nothing stored yet
        try:
            z = np.load(io.BytesIO(raw), allow_pickle=False)
            ids, counts = [str(i) for i in z["ids"]], z["counts"]
            pts, desc = z["pts"].astype(np.float32), z["desc"]
        except Exception as e:                  # noqa: BLE001
            print(f"  ! crop index corrupt ({e}); not overwriting it", file=sys.stderr)
            idx.writable = False
            return idx
        at = 0
        for ident, c in zip(ids, counts):
            idx._append(ident, pts[at:at + c], desc[at:at + c])
            at += c
        return idx

    def save(self, store) -> None:
        if not (self.dirty and self.writable):
            return
        buf = io.BytesIO()
        np.savez_compressed(
            buf,
            ids=np.array(self.ids),
            counts=np.array([len(p) for p in self.pts], dtype=np.int32),
            pts=(np.concatenate(self.pts) if self.pts
                 else np.zeros((0, 2))).astype(np.float16),
            desc=(np.concatenate(self.desc) if self.desc
                  else np.zeros((0, 32), np.uint8)),
        )
        store.put(INDEX_KEY, buf.getvalue(), content_type="application/octet-stream")
        self.dirty = False

    # --- building ----------------------------------------------------------
    def _append(self, ident, pts, desc) -> None:
        self._pos[ident] = len(self.ids)
        self.ids.append(ident)
        self.pts.append(pts)
        self.desc.append(desc)
        self._flat = None

    def add(self, ident: str, img: Image.Image) -> None:
        if ident in self:
            return
        self._append(ident, *features(img))
        self.dirty = True

    # --- matching ----------------------------------------------------------
    def _flatten(self):
        # One train image per stored picture: OpenCV caps a single train
        # matrix at 2^18 rows, and per-image registration hands back the
        # owner directly as `imgIdx`.
        if self._flat is None:
            owner = [i for i, d in enumerate(self.desc) if len(d)]
            matcher = cv2.BFMatcher(cv2.NORM_HAMMING)
            if owner:
                matcher.add([self.desc[i] for i in owner])
            self._flat = (matcher, owner)
        return self._flat

    def _verify(self, q_pts, q_desc, i: int) -> int:
        """Inlier count if stored image i is the same picture under one
        scale + shift, else 0."""
        s_pts, s_desc = self.pts[i], self.desc[i]
        if len(s_desc) < MIN_INLIERS:
            return 0
        pairs = cv2.BFMatcher(cv2.NORM_HAMMING).knnMatch(q_desc, s_desc, k=2)
        # One query point per stored point. Without this, line art's repeated
        # strokes pile many query points onto one stored point, and RANSAC
        # "explains" them all with a transform that shrinks the frame to a dot.
        best_for: dict[int, cv2.DMatch] = {}
        for p in pairs:
            if len(p) == 2 and p[0].distance < RATIO * p[1].distance:
                m = p[0]
                if m.trainIdx not in best_for or m.distance < best_for[m.trainIdx].distance:
                    best_for[m.trainIdx] = m
        good = list(best_for.values())
        if len(good) < MIN_INLIERS:
            return 0
        src = np.float32([q_pts[m.queryIdx] for m in good])
        dst = np.float32([s_pts[m.trainIdx] for m in good])
        M, mask = cv2.estimateAffinePartial2D(src, dst, method=cv2.RANSAC,
                                              ransacReprojThreshold=RANSAC_PX)
        if M is None:
            return 0
        scale = float(np.hypot(M[0, 0], M[1, 0]))
        angle = abs(float(np.degrees(np.arctan2(M[1, 0], M[0, 0]))))
        if not (SCALE_RANGE[0] <= scale <= SCALE_RANGE[1]) or angle > MAX_ROTATION_DEG:
            return 0
        inliers = int(mask.sum())
        if inliers < MIN_INLIERS or inliers < MIN_INLIER_SHARE * len(good):
            return 0
        return inliers

    def match(self, img: Image.Image) -> tuple[str, int] | None:
        """(id, inliers) of a stored picture this is a crop/zoom/resize of —
        or that is a crop of this one — else None."""
        matcher, owner = self._flatten()
        if not owner:
            return None
        q_pts, q_desc = features(img, QUERY_FEATURES)
        if len(q_desc) < MIN_INLIERS:
            return None

        # Shortlist by votes: each query descriptor votes for the image owning
        # its nearest stored descriptor, if that one is close enough.
        nearest = matcher.match(q_desc[:VOTE_FEATURES])
        votes = np.bincount([owner[m.imgIdx] for m in nearest if m.distance < VOTE_DIST],
                            minlength=len(self.ids))
        best = None
        for i in np.argsort(votes)[::-1][:SHORTLIST]:
            if votes[i] < MIN_VOTES:
                break
            n = self._verify(q_pts, q_desc, int(i))
            if n and (best is None or n > best[1]):
                best = (self.ids[int(i)], n)
        return best


_INDEX: Index | None = None


def index(store) -> Index:
    """The process-wide index, loaded from storage on first use."""
    global _INDEX
    if _INDEX is None:
        _INDEX = Index.load(store)
    return _INDEX
