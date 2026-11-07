#!/usr/bin/env python3
"""Tests for the AniList catalogue and the per-series rotation.

    python scripts/test_anime_series.py

No network: the catalogue is synthetic, so this locks in the title cleaning
and the deterministic rotation that decides what the sync asks Wallhaven for.
"""

from __future__ import annotations

import sys
import unittest
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from anime_series import (
    _stale, clean_title, main_title, pick, series_axes, title_terms, with_seeds,
)


class TitleTests(unittest.TestCase):
    def test_year_disambiguator_is_dropped(self):
        self.assertEqual(clean_title("Hunter x Hunter (2011)"), "hunter x hunter")
        self.assertEqual(clean_title("Bleach (2004)"), "bleach")

    def test_season_and_part_suffixes_are_dropped(self):
        self.assertEqual(clean_title("JUJUTSU KAISEN Season 2"), "jujutsu kaisen")
        self.assertEqual(clean_title("Attack on Titan Final Season"), "attack on titan")
        self.assertEqual(
            clean_title("Bleach: Thousand-Year Blood War Part 2"),
            "bleach thousand year blood war")
        self.assertEqual(
            clean_title("Don't Toy with Me, Miss Nagatoro 2nd Attack"),
            "dont toy with me miss nagatoro")

    def test_real_numbers_are_kept(self):
        self.assertEqual(clean_title("86"), "86")
        self.assertEqual(clean_title("Steins;Gate 0"), "steins gate 0")

    def test_main_title_splits_on_a_title_colon(self):
        self.assertEqual(main_title("Frieren: Beyond Journey's End"), "Frieren")
        self.assertEqual(main_title("Dr. STONE: New World"), "Dr. STONE")

    def test_main_title_keeps_re_zero(self):
        # A colon inside the name is not a title separator; the spaced dash is.
        self.assertEqual(
            main_title("Re:ZERO -Starting Life in Another World-"), "Re:ZERO")

    def test_main_title_keeps_hyphenated_names(self):
        self.assertEqual(main_title("One-Punch Man"), "One-Punch Man")

    def test_terms_prefer_the_short_title_then_the_full_one(self):
        media = {
            "title": {"english": "Demon Slayer: Kimetsu no Yaiba",
                      "romaji": "Kimetsu no Yaiba"},
            "synonyms": [],
        }
        self.assertEqual(title_terms(media)[:2],
                         ["demon slayer", "demon slayer kimetsu no yaiba"])

    def test_non_ascii_synonyms_are_dropped(self):
        media = {"title": {"english": "Attack on Titan", "romaji": ""},
                 "synonyms": ["進撃の巨人", "SnK"]}
        terms = title_terms(media)
        self.assertIn("snk", terms)
        self.assertNotIn("進撃の巨人", terms)


def catalogue() -> dict:
    return {"series": [
        {"key": "a", "terms": ["a"], "popularity": 100, "trending": False},
        {"key": "b", "terms": ["b"], "popularity": 90, "trending": True},
        {"key": "c", "terms": ["c"], "popularity": 80, "trending": False, "seed": True},
        {"key": "d", "terms": ["d"], "popularity": 70, "trending": False},
    ]}


class PickTests(unittest.TestCase):
    def test_pick_is_deterministic(self):
        day = date(2026, 11, 7)
        first = pick(catalogue(), day, count=2, trending_count=1, seed_count=1)
        second = pick(catalogue(), day, count=2, trending_count=1, seed_count=1)
        self.assertEqual([e["key"] for e in first], [e["key"] for e in second])

    def test_pick_has_no_duplicates(self):
        entries = pick(catalogue(), date(2026, 11, 7), count=3,
                       trending_count=1, seed_count=1)
        keys = [e["key"] for e in entries]
        self.assertEqual(len(keys), len(set(keys)))

    def test_rotation_covers_the_whole_catalogue(self):
        series = [
            {"key": f"s{i}", "terms": [f"s{i}"], "popularity": 100 - i,
             "trending": False}
            for i in range(10)
        ]
        seen = set()
        for offset in range(10):
            day = date(2026, 1, 1) + timedelta(days=offset)
            for entry in pick({"series": series}, day, count=3,
                              trending_count=0, seed_count=0):
                seen.add(entry["key"])
        self.assertEqual(seen, {f"s{i}" for i in range(10)})

    def test_axes_search_all_time_by_views(self):
        axes = series_axes(catalogue(), date(2026, 11, 7), count=1,
                           trending_count=0, seed_count=0)
        self.assertTrue(axes)
        self.assertEqual(axes[0]["sorting"], "views")
        self.assertIn("fallback_terms", axes[0])


class SeedTests(unittest.TestCase):
    def test_seeds_are_added_idempotently(self):
        first = with_seeds({"series": []})
        keys = [s["key"] for s in first["series"]]
        self.assertIn("genshin impact", keys)
        again = with_seeds(first)
        self.assertEqual(len(again["series"]), len(first["series"]))

    def test_an_existing_entry_is_marked_as_a_seed(self):
        cat = {"series": [{"key": "genshin impact", "terms": ["genshin impact"],
                           "popularity": 5, "trending": False}]}
        with_seeds(cat)
        self.assertTrue(cat["series"][0]["seed"])


class StaleTests(unittest.TestCase):
    def test_fresh_and_stale(self):
        self.assertFalse(_stale("2026-01-01", date(2026, 1, 7), 7))
        self.assertTrue(_stale("2026-01-01", date(2026, 1, 8), 7))
        self.assertTrue(_stale("", date(2026, 1, 1), 7))


if __name__ == "__main__":
    unittest.main()
