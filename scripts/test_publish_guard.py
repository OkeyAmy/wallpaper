#!/usr/bin/env python3
"""Tests for the publish invariant.

    python scripts/test_publish_guard.py

The guard is the only thing standing between a bad read and a live deploy, so
its policy is tested as a pure function rather than through the filesystem.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from publish_guard import evaluate, head_manifest, read_deleted, read_manifest


def item(n: int) -> dict:
    return {
        "id": f"id{n}",
        "file": f"wallpapers/id{n}.webp",
        "thumb": f"wallpapers/thumbs/id{n}.webp",
        "preview": f"wallpapers/id{n}.webp",
    }


def manifest(n: int) -> dict:
    return {"items": [item(i) for i in range(n)]}


class EvaluateTests(unittest.TestCase):
    def test_unchanged_passes(self):
        ok, _ = evaluate(manifest(5), manifest(5), 0)
        self.assertTrue(ok)

    def test_growth_passes(self):
        ok, msg = evaluate(manifest(5), manifest(8), 0)
        self.assertTrue(ok, msg)

    def test_reported_deletion_passes(self):
        ok, msg = evaluate(manifest(5), manifest(3), 2)
        self.assertTrue(ok, msg)

    def test_unreported_shrink_fails(self):
        ok, msg = evaluate(manifest(5), manifest(3), 0)
        self.assertFalse(ok)
        self.assertIn("5 -> 3", msg)

    def test_shrink_beyond_reported_deletion_fails(self):
        ok, _ = evaluate(manifest(5), manifest(3), 1)
        self.assertFalse(ok)

    def test_empty_after_fails(self):
        ok, msg = evaluate(manifest(5), manifest(0), 5)
        self.assertFalse(ok)
        self.assertIn("empty", msg)

    def test_bootstrap_passes(self):
        ok, msg = evaluate(manifest(0), manifest(4), 0)
        self.assertTrue(ok, msg)

    def test_duplicate_id_fails(self):
        after = manifest(2)
        after["items"].append(item(0))
        ok, msg = evaluate(manifest(2), after, 0)
        self.assertFalse(ok)
        self.assertIn("duplicate", msg)

    def test_missing_required_field_fails(self):
        after = manifest(2)
        del after["items"][1]["thumb"]
        ok, msg = evaluate(manifest(2), after, 0)
        self.assertFalse(ok)
        self.assertIn("thumb", msg)

    def test_negative_allowance_fails(self):
        ok, _ = evaluate(manifest(2), manifest(2), -1)
        self.assertFalse(ok)

    def test_allowance_above_baseline_fails(self):
        ok, _ = evaluate(manifest(2), manifest(1), 3)
        self.assertFalse(ok)


class ReaderTests(unittest.TestCase):
    def test_corrupt_json_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "manifest.json"
            path.write_text("{not json")
            with self.assertRaises(ValueError):
                read_manifest(path)

    def test_missing_items_list_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "manifest.json"
            path.write_text(json.dumps({"cdn": "https://cdn.example"}))
            with self.assertRaises(ValueError):
                read_manifest(path)

    def test_missing_deletion_report_counts_zero(self):
        self.assertEqual(read_deleted(["/nonexistent/deletions.json"]), 0)

    def test_deletion_reports_sum(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = Path(tmp) / "prune.json"
            second = Path(tmp) / "cull.json"
            first.write_text(json.dumps({"deleted": 3}))
            second.write_text(json.dumps({"deleted": 4}))
            self.assertEqual(read_deleted([str(first), str(second)]), 7)


class HeadManifestTests(unittest.TestCase):
    """A git failure must never be mistaken for a bootstrap run."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.git("init", "-q")
        (self.root / "README").write_text("x")
        self.git("add", "README")
        self.commit("init")

    def tearDown(self):
        self.tmp.cleanup()

    def git(self, *args: str, check: bool = True):
        return subprocess.run(["git", *args], cwd=self.root, check=check,
                              capture_output=True, text=True)

    def commit(self, message: str) -> None:
        self.git("-c", "user.name=t", "-c", "user.email=t@example.com",
                 "commit", "-qm", message)

    def commit_manifest(self, text: str) -> None:
        (self.root / "data").mkdir(exist_ok=True)
        (self.root / "data" / "wallpapers.json").write_text(text)
        self.git("add", "data/wallpapers.json")
        self.commit("manifest")

    def test_missing_manifest_in_head_is_bootstrap(self):
        self.assertEqual(head_manifest(self.root), {"items": []})

    def test_committed_manifest_is_read(self):
        self.commit_manifest(json.dumps(manifest(2)))
        self.assertEqual(len(head_manifest(self.root)["items"]), 2)

    def test_corrupt_committed_manifest_raises(self):
        self.commit_manifest("{not json")
        with self.assertRaises(ValueError):
            head_manifest(self.root)

    def test_repo_without_commits_raises(self):
        empty = self.root / "empty"
        empty.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=empty, check=True)
        with self.assertRaises(ValueError):
            head_manifest(empty)


if __name__ == "__main__":
    unittest.main()
