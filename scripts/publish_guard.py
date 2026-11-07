#!/usr/bin/env python3
"""Refuse to publish a manifest that lost items without saying so.

The manifest in the last commit is the last known good set. A sync may add
freely, and prune/cull may remove items — but only the number they explicitly
report. Anything else means a bad read, a partial write or a bug, and the job
must stop before `git commit` so the previous deploy keeps serving.

    python scripts/publish_guard.py \
        --after data/wallpapers.json \
        --deleted "$RUNNER_TEMP/prune.json"

`--deleted` is repeatable; each file is `{"deleted": N, ...}` as written by
prune.py / cull.py. A missing report counts as zero, which is the safe
direction: an unreported deletion fails the gate.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

# The grid and lightbox cannot render an item without these.
REQUIRED = ("id", "file", "thumb", "preview")


def read_manifest(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
    except OSError as err:
        raise ValueError(f"cannot read {path}: {err}") from err
    except json.JSONDecodeError as err:
        raise ValueError(f"{path} is not valid JSON: {err}") from err
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        raise ValueError(f"{path} has no items list")
    return data


def head_manifest(cwd: Path | None = None) -> dict:
    """The manifest in the last commit — what the live site was built from.

    A missing manifest in HEAD is a bootstrap run. A git failure is not: it
    raises, because treating an unreadable baseline as empty would let the
    gate wave through a shrink.
    """
    def git(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["git", *args], capture_output=True, text=True,
                              cwd=cwd)

    listed = git("ls-tree", "--name-only", "HEAD", "data/wallpapers.json")
    if listed.returncode != 0:
        raise ValueError(f"cannot read HEAD: {listed.stderr.strip()}")
    if not listed.stdout.strip():
        print("note: no committed manifest — treating this as a bootstrap run",
              file=sys.stderr)
        return {"items": []}

    proc = git("show", "HEAD:data/wallpapers.json")
    if proc.returncode != 0:
        raise ValueError(
            f"cannot read HEAD:data/wallpapers.json: {proc.stderr.strip()}")
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as err:
        raise ValueError(
            f"HEAD:data/wallpapers.json is not valid JSON: {err}") from err
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        raise ValueError("HEAD:data/wallpapers.json has no items list")
    return data


def read_deleted(paths: list[str]) -> int:
    total = 0
    for raw in paths:
        path = Path(raw)
        if not path.exists():
            print(f"note: {path} not found — assuming 0 deletions")
            continue
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as err:
            raise ValueError(f"cannot read deletion report {path}: {err}") from err
        value = data.get("deleted") if isinstance(data, dict) else data
        try:
            n = int(value)
        except (TypeError, ValueError) as err:
            raise ValueError(f"{path}: 'deleted' must be an integer") from err
        if n < 0:
            raise ValueError(f"{path}: 'deleted' must not be negative")
        total += n
    return total


def evaluate(before: dict, after: dict, deleted: int) -> tuple[bool, str]:
    """The whole policy, as a pure function so it can be tested."""
    b = before["items"]
    a = after["items"]

    if not isinstance(deleted, int) or deleted < 0:
        return False, "deletion allowance must be a non-negative integer"
    if deleted > len(b):
        return False, (f"deletion allowance {deleted} exceeds the "
                       f"{len(b)} committed items")
    if not a:
        if not b:
            return True, "bootstrap: both manifests are empty"
        return False, f"refusing to publish an empty manifest (was {len(b)} items)"

    seen = set()
    for item in a:
        if not isinstance(item, dict):
            return False, "manifest contains a non-object item"
        missing = [key for key in REQUIRED if not item.get(key)]
        if missing:
            return False, (f"item {item.get('id', '?')} is missing "
                           f"{', '.join(missing)}")
        if item["id"] in seen:
            return False, f"duplicate item id {item['id']}"
        seen.add(item["id"])

    if len(a) < len(b) - deleted:
        return False, (f"refusing to publish: {len(b)} -> {len(a)} items "
                       f"(allowed drop {deleted})")

    change = len(a) - len(b)
    sign = f"+{change}" if change > 0 else str(change)
    return True, f"{len(b)} -> {len(a)} ({sign}), allowed drop {deleted}"


def write_deletion_report(path: str, deleted: int, reason: str,
                          before: int, after: int) -> None:
    """Shared by prune.py and cull.py; publish_guard.py reads it back."""
    if not path:
        return
    Path(path).write_text(json.dumps({
        "deleted": deleted,
        "reason": reason,
        "before": before,
        "after": after,
    }, indent=1) + "\n")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--before", metavar="FILE",
                    help="baseline manifest (default: HEAD:data/wallpapers.json)")
    ap.add_argument("--after", default="data/wallpapers.json", metavar="FILE")
    ap.add_argument("--deleted", action="append", default=[], metavar="FILE",
                    help="deletion report from prune.py/cull.py; repeatable")
    args = ap.parse_args()

    try:
        before = read_manifest(Path(args.before)) if args.before else head_manifest()
        after = read_manifest(Path(args.after))
        deleted = read_deleted(args.deleted)
        ok, message = evaluate(before, after, deleted)
    except ValueError as err:
        print(f"publish guard: {err}", file=sys.stderr)
        return 1

    print(f"publish guard: {message}")
    if not ok:
        print("publish guard: refusing to publish", file=sys.stderr)
        if os.environ.get("GITHUB_ACTIONS"):
            print(f"::error::{message}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
