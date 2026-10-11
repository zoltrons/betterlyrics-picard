import importlib.util
import sys
import types
import unittest
from pathlib import Path


def load_plugin():
    """Load the pure matching code without requiring a Picard installation."""
    qt = types.ModuleType("PyQt6")
    qt_core = types.ModuleType("PyQt6.QtCore")
    qt_gui = types.ModuleType("PyQt6.QtGui")
    qt_widgets = types.ModuleType("PyQt6.QtWidgets")

    class QObject:
        pass

    class QTimer:
        pass

    qt_core.QObject = QObject
    qt_core.QTimer = QTimer
    qt_core.Qt = types.SimpleNamespace()
    qt_gui.QColor = object
    qt_widgets.QHeaderView = types.SimpleNamespace(
        ResizeMode=types.SimpleNamespace(ResizeToContents=1, Stretch=2, Interactive=3)
    )
    qt_widgets.QAbstractItemView = types.SimpleNamespace(
        SelectionBehavior=types.SimpleNamespace(SelectRows=1),
        SelectionMode=types.SimpleNamespace(SingleSelection=1),
        EditTrigger=types.SimpleNamespace(NoEditTriggers=1),
    )
    qt.QtCore = qt_core
    qt.QtGui = qt_gui
    qt.QtWidgets = qt_widgets

    class Qt:
        AlignmentFlag = types.SimpleNamespace(AlignCenter=1, AlignLeft=2, AlignRight=4)

    api_module = types.ModuleType("picard.plugin3.api")

    class PluginApi:
        @classmethod
        def get_api(cls):
            return None

    class BaseAction:
        pass

    class OptionsPage:
        pass

    api_module.Album = type("Album", (), {})
    api_module.BaseAction = BaseAction
    api_module.File = type("File", (), {})
    api_module.Metadata = dict
    api_module.OptionsPage = OptionsPage
    api_module.PluginApi = PluginApi
    api_module.Track = type("Track", (), {})

    picard = types.ModuleType("picard")
    plugin3 = types.ModuleType("picard.plugin3")
    sys.modules.update(
        {
            "PyQt6": qt,
            "PyQt6.QtCore": qt_core,
            "PyQt6.QtGui": qt_gui,
            "PyQt6.QtWidgets": qt_widgets,
            "picard": picard,
            "picard.plugin3": plugin3,
            "picard.plugin3.api": api_module,
        }
    )

    # The production module imports Qt from PyQt6.QtCore as ``Qt``.
    qt_core.Qt = Qt
    spec = importlib.util.spec_from_file_location(
        "betterlyrics_test_plugin", Path(__file__).parents[1] / "__init__.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


plugin = load_plugin()


class MatchingTests(unittest.TestCase):
    def test_explicit_weights_keep_exact_identity_at_one(self):
        item = {
            "title": "Song",
            "artist": "Artist",
            "album": "Album",
            "duration": 180,
            "isrc": "US-RC1-23-00001",
        }
        score = plugin.calculate_match_score(
            "Song", "Artist", "Album", 180, "USRC12300001", item
        )
        self.assertEqual(score, 1.0)

    def test_missing_metadata_does_not_receive_a_false_bonus(self):
        score = plugin.calculate_match_score(
            "Song", "Artist", "", 0, "", {"title": "Song", "artist": "Artist"}
        )
        self.assertAlmostEqual(score, 0.45)

    def test_conflicting_isrc_and_duration_are_rejected(self):
        item = {
            "title": "Song",
            "artist": "Artist",
            "duration": 210,
            "isrc": "USRC99999999",
            "score": 1.0,
        }
        self.assertFalse(
            plugin._candidate_is_compatible("Song", "Artist", 180, "USRC12300001", item, 5)
        )

    def test_exact_isrc_allows_provider_version_suffix(self):
        candidate = {
            "title": "Bohemian Rhapsody (Remastered 2011)",
            "artist": "Queen",
            "album": "A Night at the Opera",
            "duration": 356.52,
            "isrc": "GBUM71029604",
            "metadata_verified": True,
        }
        result = plugin.assess_match(
            {
                "title": "Bohemian Rhapsody",
                "artist": "Queen",
                "album": "A Night at the Opera",
                "duration": 356,
                "isrc": "GBUM71029604",
            },
            candidate,
            tolerance=5,
        )
        self.assertTrue(result["eligible"])
        self.assertEqual(result["reason"], "exact_isrc")

    def test_synced_preference_is_only_a_tie_breaker(self):
        candidates = [
            {"title": "Song", "artist": "Artist", "timing_type": "none"},
            {"title": "Song", "artist": "Artist", "timing_type": "line"},
        ]
        original_lrcred = plugin.search_lrcred
        original_unison = plugin.search_unison
        try:
            plugin.search_lrcred = lambda *args: candidates
            plugin.search_unison = lambda *args: []
            results = plugin.query_all_sources(
                "Song", "Artist", source_preference="lrcred", prefer_synced=True
            )
        finally:
            plugin.search_lrcred = original_lrcred
            plugin.search_unison = original_unison
        self.assertEqual(results[0]["timing_type"], "line")
        self.assertEqual(results[0]["score"], results[1]["score"])

    def test_empty_top_candidate_falls_back_to_next_candidate(self):
        candidates = [
            {"title": "Song", "artist": "Artist", "duration": 180, "score": 0.60},
            {"title": "Song", "artist": "Artist", "duration": 180, "score": 0.55},
        ]
        resolved = []
        original_query = plugin.query_all_sources
        original_resolve = plugin.resolve_lyrics_bundle
        plugin.query_all_sources = lambda **kwargs: candidates

        def resolve(candidate, clean_words=None):
            resolved.append(candidate)
            return {"ttml": None, "lrc": "[00:01.00]lyrics"} if len(resolved) == 2 else {"ttml": None, "lrc": None}

        plugin.resolve_lyrics_bundle = resolve
        try:
            bundle = plugin._find_best_bundle(
                "Song", "Artist", "", 180, "", "all", True, 5, True
            )
        finally:
            plugin.query_all_sources = original_query
            plugin.resolve_lyrics_bundle = original_resolve
        self.assertEqual(bundle["lrc"], "[00:01.00]lyrics")
        self.assertEqual(len(resolved), 2)


if __name__ == "__main__":
    unittest.main()
