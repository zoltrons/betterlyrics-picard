"""Offline regression coverage for automatic recording selection."""

import unittest
from itertools import permutations

from lyrics_matching import (
    ambiguity,
    assess_match,
    choose_identity_group,
    rank_candidates,
    same_recording,
)


class AutoMatchingTests(unittest.TestCase):
    def setUp(self):
        self.target = {"title": "Kiss It Better", "artist": "Rihanna", "album": "ANTI", "duration": 253}
        self.candidates = [
            dict(self.target, duration=253.077, isrc=isrc, metadata_verified=True, provider_id="lrcred")
            for isrc in ("QM5FT1600115", "QM5FT1600102")
        ]

    def test_real_anti_release_ties_select_one_stable_identity(self):
        cases = (
            ("Kiss It Better", 253, (253.077, 253.077), ("QM5FT1600115", "QM5FT1600102")),
            ("Needed Me", 192, (191.605, 191.605), ("QM5FT1600118", "QM5FT1600106")),
            ("Love on the Brain", 224, (223.997, 223.996), ("QM5FT1600120", "QM5FT1600110")),
        )
        for title, duration, lengths, isrcs in cases:
            target = dict(self.target, title=title, duration=duration)
            candidates = [
                dict(target, duration=length, isrc=isrc, metadata_verified=True)
                for length, isrc in zip(lengths, isrcs)
            ]
            expected = sorted(candidates, key=lambda c: (abs(c["duration"] - duration), c["isrc"]))[0]["isrc"]
            for order in permutations(candidates):
                with self.subTest(title=title, order=[c["isrc"] for c in order]):
                    selected = choose_identity_group(target, order)
                    self.assertEqual(len(selected), 1)
                    self.assertEqual(selected[0]["isrc"], expected)
                    self.assertEqual(selected[0]["match_reason"], "exact_metadata_tie_break")
                    self.assertTrue(selected[0]["match_eligible"])
                    self.assertFalse(same_recording(*order))
                    # Selection does not erase the underlying ISRC ambiguity.
                    self.assertTrue(ambiguity(target, order))
                    self.assertNotIn("match_reason", order[0])

    def test_reproduced_score_is_preserved(self):
        ranked = rank_candidates(self.target, self.candidates)
        self.assertAlmostEqual(ranked[0]["score"], 0.499538)
        self.assertEqual(ranked[0]["score"], ranked[1]["score"])

    def test_fallback_requires_complete_exact_metadata_and_close_lengths(self):
        cases = (
            (dict(self.target, duration=None), self.candidates),
            (self.target, [dict(c, duration=None) for c in self.candidates]),
            (self.target, [self.candidates[0], dict(self.candidates[1], duration=None)]),
            (dict(self.target, album=""), self.candidates),
            (self.target, [dict(c, album="") for c in self.candidates]),
            (self.target, [self.candidates[0], dict(self.candidates[1], album="ANTI Deluxe")]),
            (self.target, [dict(c, album="ANTII") for c in self.candidates]),
            (self.target, [dict(self.candidates[0], duration=249), dict(self.candidates[1], duration=257)]),
        )
        for target, candidates in cases:
            with self.subTest(target=target, candidates=candidates):
                self.assertEqual(choose_identity_group(target, candidates), [])

    def test_invalid_target_isrc_does_not_disable_metadata_fallback(self):
        self.assertEqual(len(choose_identity_group(dict(self.target, isrc="invalid"), self.candidates)), 1)

    def test_exact_target_isrc_remains_authoritative(self):
        for candidate in self.candidates:
            target = dict(self.target, isrc=candidate["isrc"])
            selected = choose_identity_group(target, self.candidates)
            self.assertEqual([c["isrc"] for c in selected], [candidate["isrc"]])
        target = dict(self.target, isrc="USRC12300001")
        self.assertEqual(choose_identity_group(target, self.candidates), [])
        self.assertEqual(assess_match(target, self.candidates[0])["reason"], "isrc_conflict")

    def test_same_recording_still_groups_one_identity(self):
        candidates = [self.candidates[0], dict(self.candidates[0], provider_id="other")]
        self.assertTrue(same_recording(*candidates))
        self.assertEqual(len(choose_identity_group(self.target, candidates)), 2)

    def test_metadata_only_candidate_cannot_bridge_conflicting_isrcs(self):
        for bridge in (
            dict(self.candidates[0], isrc=None),
            dict(self.candidates[0], isrc=[c["isrc"] for c in self.candidates]),
        ):
            candidates = [bridge, *self.candidates]
            self.assertTrue(ambiguity(self.target, candidates))
            self.assertEqual(len(choose_identity_group(self.target, candidates)), 1)

    def test_wrong_title_artist_duration_and_versions_stay_rejected(self):
        changes = (
            {"title": "Needed Me"}, {"artist": "Beyonce"}, {"duration": 260},
            {"title": "Kiss It Better (Remix)"}, {"title": "Kiss It Better (Live)"},
            {"title": "Kiss It Better (Radio Edit)"}, {"album": "ANTI (Live)"},
        )
        for changeset in changes:
            with self.subTest(changes=changeset):
                candidate = dict(self.candidates[0], **changeset)
                self.assertFalse(assess_match(self.target, candidate)["eligible"])
                self.assertEqual(choose_identity_group(self.target, [candidate]), [])
        # Matching ISRCs never excuse a live/remix/edit contradiction.
        for version in ("Live", "Remix", "Radio Edit"):
            candidate = dict(self.candidates[0], title=f"Kiss It Better ({version})")
            target = dict(self.target, isrc=candidate["isrc"])
            self.assertEqual(assess_match(target, candidate)["reason"], "version_conflict")

    def test_fuzzy_distinct_titles_remain_ambiguous(self):
        target = dict(self.target, title="Walking Home")
        candidates = [
            dict(self.candidates[0], title="Walking Home"),
            dict(self.candidates[1], title="Talking Home"),
        ]
        self.assertTrue(all(c["match_eligible"] for c in rank_candidates(target, candidates)))
        self.assertTrue(ambiguity(target, candidates))
        self.assertEqual(choose_identity_group(target, candidates), [])

    def test_feature_credits_move_between_title_and_artist(self):
        target = dict(self.target, title="Work", artist="Rihanna feat. Drake")
        for marker in ("feat.", "ft.", "featuring"):
            candidate = dict(self.candidates[0], title=f"Work ({marker} Drake)")
            for left, right in ((target, candidate), (candidate, target)):
                with self.subTest(marker=marker, left=left):
                    result = assess_match(left, right)
                    self.assertTrue(result["eligible"])
                    self.assertTrue(same_recording(left, right))
                    self.assertEqual(len(choose_identity_group(left, [left, right])), 2)
        # The exact-evidence release fallback uses the same credit normalization.
        candidates = [dict(c, title="Work (feat. Drake)") for c in self.candidates]
        selected = choose_identity_group(target, candidates)
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0]["match_reason"], "exact_metadata_tie_break")

    def test_moved_feature_credits_share_metadata_identity(self):
        left = dict(self.target, title="Work", artist="Rihanna feat. Drake")
        right = dict(self.target, title="Work (feat. Drake)")
        self.assertEqual(assess_match(left, left)["identity"], assess_match(left, right)["identity"])
        self.assertEqual(assess_match(left, left)["score"], assess_match(left, right)["score"])
        self.assertTrue(same_recording(left, right))

    def test_guest_contradictions_and_unrelated_primary_artists_are_rejected(self):
        target = dict(self.target, title="Work", artist="Rihanna feat. Drake")
        cases = (
            {"title": "Work (feat. Future)", "artist": "Rihanna"},
            {"title": "Work", "artist": "Rihanna"},
            {"title": "Work (feat. Drake)", "artist": "Beyonce"},
            {"title": "Work (feat. Drake)", "artist": "Rihanna feat. Future"},
            {"title": "Work (feat. Drake and Future)", "artist": "Rihanna"},
            {"title": "Work", "artist": "Drake feat. Rihanna"},
            {"title": "Work (feat. Drake) (Live)", "artist": "Rihanna"},
        )
        for changes in cases:
            candidate = dict(self.candidates[0], **changes)
            with self.subTest(candidate=candidate):
                self.assertFalse(assess_match(target, candidate)["eligible"])
                self.assertFalse(same_recording(target, candidate))
                self.assertEqual(choose_identity_group(target, [candidate]), [])

    def test_generic_provider_confidence_is_not_identity_evidence(self):
        for confidence in (0.8, 1.0, float("inf"), True):
            candidate = dict(self.candidates[0], metadata_verified=False, provider_confidence=confidence)
            self.assertEqual(assess_match(self.target, candidate)["reason"], "metadata_unverified")

    def test_limited_query_contracts_support_missing_metadata(self):
        for provider, flag, reason in (
            ("boidu", "query_bound", "provider_query_bound"),
            ("lrclib", "exact_lookup", "provider_exact_lookup"),
        ):
            candidate = {
                "title": self.target["title"], "artist": self.target["artist"],
                "metadata_verified": False, "provider_id": provider, flag: True,
            }
            with self.subTest(provider=provider):
                self.assertEqual(assess_match(self.target, candidate)["reason"], reason)
                self.assertEqual(len(choose_identity_group(self.target, [candidate])), 1)
                self.assertFalse(assess_match(self.target, dict(candidate, provider_id="other"))["eligible"])
                self.assertFalse(assess_match(self.target, dict(candidate, **{flag: False}))["eligible"])

    def test_query_contracts_never_skip_known_conflicts(self):
        target = dict(self.target, isrc="QM5FT1600115")
        for provider, flag in (("boidu", "query_bound"), ("lrclib", "exact_lookup")):
            base = dict(self.candidates[0], metadata_verified=False, provider_id=provider, **{flag: True})
            for changes, reason in (
                ({"isrc": "QM5FT1600102"}, "isrc_conflict"),
                ({"duration": 270}, "duration_conflict"),
                ({"title": "Kiss It Better (Live)"}, "version_conflict"),
                ({"title": "Kiss It Better (Remix)"}, "version_conflict"),
                ({"album": "ANTI (Live)"}, "version_conflict"),
                ({"artist": "Beyonce"}, "artist_conflict"),
                ({"title": "Needed Me"}, "title_conflict"),
            ):
                with self.subTest(provider=provider, changes=changes):
                    result = assess_match(target, dict(base, **changes))
                    self.assertFalse(result["eligible"])
                    self.assertEqual(result["reason"], reason)

    def test_unverified_query_candidates_cannot_join_metadata_tie_fallback(self):
        candidates = [dict(c, metadata_verified=False, provider_id="boidu", query_bound=True) for c in self.candidates]
        self.assertTrue(all(c["match_eligible"] for c in rank_candidates(self.target, candidates)))
        self.assertEqual(choose_identity_group(self.target, candidates), [])


if __name__ == "__main__":
    unittest.main()
