#!/usr/bin/env python3
"""Upscale a folder of small images with a real super-resolution model,
before queuing them for ingest_local.py.

This is a manual, one-off tool — not part of the CI pipeline, so its
dependency isn't in scripts/requirements.txt:

    pip install opencv-contrib-python

Uses FSRCNN (a small trained upscaling network, not plain interpolation) via
OpenCV's dnn_superres module. The model weights are ~40 KB and are
downloaded once into scripts/.cache/ on first run.

    python scripts/upscale_batch.py old/ incoming/
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from urllib.request import urlretrieve

MODEL_URL = "https://raw.githubusercontent.com/Saafke/FSRCNN_Tensorflow/master/models/FSRCNN_x4.pb"
MODEL_PATH = Path(__file__).parent / ".cache" / "FSRCNN_x4.pb"
SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tiff"}


def get_model() -> Path:
    MODEL_PATH.parent.mkdir(exist_ok=True)
    if not MODEL_PATH.exists():
        print(f"downloading FSRCNN model to {MODEL_PATH} ...")
        urlretrieve(MODEL_URL, MODEL_PATH)
    return MODEL_PATH


def main() -> int:
    try:
        import cv2
    except ImportError:
        print("! opencv-contrib-python is not installed. Run:\n"
              "    pip install opencv-contrib-python", file=sys.stderr)
        return 1

    ap = argparse.ArgumentParser()
    ap.add_argument("src", type=Path, help="folder of images to upscale")
    ap.add_argument("dst", type=Path, help="folder to write upscaled images into")
    ap.add_argument("--scale", type=int, default=4, choices=[2, 3, 4])
    ap.add_argument("--max-edge", type=int, default=3840,
                     help="cap the long edge after upscaling — matches pipeline.py's "
                          "own cap, so nothing is written that ingest would just shrink back down")
    args = ap.parse_args()

    if not args.src.is_dir():
        print(f"! not a directory: {args.src}", file=sys.stderr)
        return 1

    model = get_model()
    sr = cv2.dnn_superres.DnnSuperResImpl_create()
    sr.readModel(str(model))
    sr.setModel("fsrcnn", args.scale)

    args.dst.mkdir(parents=True, exist_ok=True)
    files = sorted(p for p in args.src.iterdir() if p.suffix.lower() in SUFFIXES)
    if not files:
        print(f"nothing to upscale in {args.src}")
        return 0

    for i, path in enumerate(files, 1):
        img = cv2.imread(str(path))
        if img is None:
            print(f"  ! unreadable, skipped: {path.name}")
            continue

        h, w = img.shape[:2]
        out = sr.upsample(img)
        oh, ow = out.shape[:2]

        if max(oh, ow) > args.max_edge:
            ratio = args.max_edge / max(oh, ow)
            out = cv2.resize(out, (round(ow * ratio), round(oh * ratio)),
                              interpolation=cv2.INTER_LANCZOS4)
            oh, ow = out.shape[:2]

        dest = args.dst / f"{path.stem}.png"
        cv2.imwrite(str(dest), out)
        print(f"  [{i}/{len(files)}] {path.name}: {w}×{h} -> {ow}×{oh}")

    print(f"\n{len(files)} upscaled into {args.dst}/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
