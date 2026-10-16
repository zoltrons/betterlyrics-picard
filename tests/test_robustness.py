"""Regression tests for queue lifecycle, saving, caching and provider hardening."""

import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from test_matching import plugin


TTML = (
    '<tt xmlns="http://www.w3.org/ns/ttml"><body><div>'
    '<p begin="1s" end="3s"><span begin="1s" end="2s">test</span> '
    '<span begin="2s" end="3s">line</span></p></div></body></tt>'
)
ISRC = "GBUM71029604"


def wait_until(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


class Invoker:
    def __init__(self):
        self.posted = []

    def post(self, fn):
        self.posted.append(fn)


class FetchQueueTests(unittest.TestCase):
    def setUp(self):
        patcher = patch.object(plugin, "log", Mock())
        patcher.start()
        self.addCleanup(patcher.stop)
        self.queue = plugin.FetchQueue(max_workers=2)
        self.queue._IDLE_POLL = 0.02
        self.invoker = Invoker()
        self.queue.set_invoker(self.invoker)
        self.addCleanup(self.queue.stop)

    def test_jobs_run_and_results_reach_the_invoker(self):
        self.queue.start()
        self.assertTrue(self.queue.submit(lambda: 41, lambda result: result + 1, key="a"))
        self.assertTrue(wait_until(lambda: self.invoker.posted))
        self.assertEqual(self.invoker.posted[0](), 42)
        self.assertTrue(wait_until(lambda: self.queue.outstanding == 0))

    def test_duplicate_keys_are_ignored_until_the_job_finishes(self):
        gate = threading.Event()
        self.queue.start()
        self.assertTrue(self.queue.submit(gate.wait, Mock(), key="same"))
        self.assertFalse(self.queue.submit(gate.wait, Mock(), key="same"))
        gate.set()
        self.assertTrue(wait_until(lambda: self.invoker.posted))
        self.assertTrue(wait_until(lambda: self.queue.submit(lambda: None, None, key="same")))

    def test_stop_discards_queued_work_and_drops_stale_results(self):
        self.queue.configure(1)
        gate = threading.Event()
        ran = []
        self.queue.start()
        self.queue.submit(lambda: gate.wait(2) and "late", Mock(), key="busy")
        self.assertTrue(wait_until(lambda: self.queue.active == 1))
        self.queue.submit(lambda: ran.append("queued"), Mock(), key="queued")
        started = time.monotonic()
        self.queue.stop(timeout=0.1)
        self.assertLess(time.monotonic() - started, 1.0)
        gate.set()
        time.sleep(0.1)
        self.assertEqual(ran, [])
        self.assertEqual(self.invoker.posted, [])
        self.assertEqual(self.queue.pending, 0)

    def test_restart_after_stop_processes_new_work(self):
        self.queue.start()
        self.queue.stop()
        self.queue.start()
        self.queue.submit(lambda: "again", lambda result: result, key="k")
        self.assertTrue(wait_until(lambda: self.invoker.posted))

    def test_configure_resizes_a_running_pool(self):
        self.queue.start()
        self.queue.configure(4)
        self.assertEqual(sum(w.is_alive() for w in self.queue._workers.values()), 4)
        self.queue.configure(1)
        self.assertTrue(wait_until(
            lambda: sum(w.is_alive() for w in self.queue._workers.values()) == 1
        ))


class SavingTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.options = {
            **plugin.PLUGIN_OPTIONS,
            "betterlyrics_save_ttml_file": True,
            "betterlyrics_save_lrc_file": True,
            "betterlyrics_embed_syncedlyrics": True,
        }
        for name, value in (("config", self.options), ("log", Mock())):
            patcher = patch.object(plugin, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def file(self, name="song.flac"):
        return SimpleNamespace(filename=str(self.root / name), metadata={}, update=Mock())

    def test_plain_lyrics_never_become_ttml_or_lrc(self):
        self.options["betterlyrics_plain_as_txt"] = True
        bundle = plugin.make_bundle(plugin.parse_lrc("first line\nsecond line"))
        self.assertEqual(bundle["quality"], "plain")
        file = self.file()
        self.assertTrue(plugin.save_lyrics_to_files([file], bundle))
        self.assertEqual(file.metadata, {"lyrics": "first line\nsecond line"})
        self.assertEqual((self.root / "song.txt").read_text(encoding="utf-8"), "first line\nsecond line")
        self.assertFalse((self.root / "song.ttml").exists())
        self.assertFalse((self.root / "song.lrc").exists())

    def test_synced_lyrics_do_not_write_a_txt_sidecar(self):
        self.options["betterlyrics_plain_as_txt"] = True
        bundle = plugin.make_bundle(plugin.parse_ttml(TTML))
        file = self.file()
        plugin.save_lyrics_to_files([file], bundle)
        self.assertTrue((self.root / "song.ttml").exists())
        self.assertTrue((self.root / "song.lrc").exists())
        self.assertFalse((self.root / "song.txt").exists())
        self.assertEqual(file.metadata["ttml"], TTML)

    def test_overwrite_replaces_sidecar_and_keeps_its_permissions(self):
        self.options["betterlyrics_auto_overwrite"] = True
        sidecar = self.root / "song.lrc"
        sidecar.write_text("old", encoding="utf-8")
        os.chmod(sidecar, 0o644)
        bundle = plugin.make_bundle(plugin.parse_ttml(TTML))
        plugin.save_lyrics_to_files([self.file()], bundle)
        self.assertEqual(sidecar.read_text(encoding="utf-8"), bundle["lrc"])
        self.assertEqual(os.stat(sidecar).st_mode & 0o777, 0o644)
        self.assertEqual([p.name for p in self.root.iterdir() if p.name.startswith(".betterlyrics-")], [])

    def test_sidecars_follow_the_audio_file_when_picard_renames_it(self):
        bundle = plugin.make_bundle(plugin.parse_ttml(TTML))
        file = plugin.File()
        file.filename = str(self.root / "incoming.flac")
        file.metadata = {}
        plugin.save_lyrics_to_files([file], bundle)
        target = self.root / "Artist" / "01 Song.flac"
        file.filename = str(target)
        plugin.on_file_saved(file)
        self.assertFalse((self.root / "incoming.ttml").exists())
        self.assertTrue((self.root / "Artist" / "01 Song.ttml").exists())
        self.assertTrue((self.root / "Artist" / "01 Song.lrc").exists())

    def test_sidecar_move_never_overwrites_an_existing_file(self):
        bundle = plugin.make_bundle(plugin.parse_ttml(TTML))
        file = plugin.File()
        file.filename = str(self.root / "incoming.flac")
        file.metadata = {}
        plugin.save_lyrics_to_files([file], bundle)
        (self.root / "final.ttml").write_text("keep me", encoding="utf-8")
        file.filename = str(self.root / "final.flac")
        plugin.on_file_saved(file)
        self.assertEqual((self.root / "final.ttml").read_text(encoding="utf-8"), "keep me")
        self.assertTrue((self.root / "incoming.ttml").exists())


class OrphanCleanerTests(unittest.TestCase):
    def test_audio_extensions_match_case_insensitively(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("Song.FLAC", "Song.lrc", "Song.ttml", "Gone.lrc", "notes.txt"):
                (root / name).write_text("x")
            (root / "sub").mkdir()
            (root / "sub" / "Other.ttml").write_text("x")
            orphans = sorted(Path(p).name for p in plugin.find_orphaned_lyrics_files(directory))
        self.assertEqual(orphans, ["Gone.lrc", "Other.ttml"])


class LookupTests(unittest.TestCase):
    def setUp(self):
        patcher = patch.object(plugin, "log", Mock())
        patcher.start()
        self.addCleanup(patcher.stop)

    def candidates(self, *timings):
        return [
            {"title": "Song", "artist": "Artist", "album": "Album", "duration": 180,
             "isrc": ISRC, "provider_id": "lrcred", "source": "LRC.red",
             "metadata_verified": True, "timing_type": timing, "id": timing}
            for timing in timings
        ]

    def test_synced_lyrics_win_within_the_same_recording(self):
        plain = plugin.make_bundle(plugin.parse_lrc("plain words"))
        synced = plugin.make_bundle(plugin.parse_ttml(TTML))
        bundles = {"none": plain, "line": synced}
        with patch.object(plugin, "query_all_sources", return_value=self.candidates("none", "line")), \
                patch.object(plugin, "resolve_lyrics_bundle", side_effect=lambda c, clean_words: bundles[c["id"]]), \
                patch.object(plugin, "_LYRICS_STORE", None):
            result = plugin._find_best_bundle("Song", "Artist", "Album", 180, ISRC, "all", True, 5, True)
        self.assertIs(result, synced)

    def test_plain_lyrics_are_the_fallback_when_nothing_is_synced(self):
        plain = plugin.make_bundle(plugin.parse_lrc("plain words"))
        with patch.object(plugin, "query_all_sources", return_value=self.candidates("none")), \
                patch.object(plugin, "resolve_lyrics_bundle", return_value=plain), \
                patch.object(plugin, "_LYRICS_STORE", None):
            result = plugin._find_best_bundle("Song", "Artist", "Album", 180, ISRC, "all", True, 5, True)
        self.assertIs(result, plain)

    def test_cached_bundle_follows_the_current_word_timestamp_setting(self):
        store = plugin.LyricsStore(":memory:")
        self.addCleanup(store.close)
        lrc = "[00:01.00]<00:01.00>test <00:02.00>line"
        bundle = plugin.make_bundle(plugin.parse_lrc(lrc), clean_words=False)
        target = {"title": "Song", "artist": "Artist", "album": "", "duration": 180, "isrc": ""}
        store.put(plugin._auto_cache_key(target, "all"), bundle)
        with patch.object(plugin, "_LYRICS_STORE", store), \
                patch.object(plugin, "query_all_sources") as search:
            cleaned = plugin._find_best_bundle("Song", "Artist", "", 180, "", "all", True, 5, True)
            enhanced = plugin._find_best_bundle("Song", "Artist", "", 180, "", "all", False, 5, True)
        search.assert_not_called()
        self.assertNotIn("<00:", cleaned["lrc"])
        self.assertIn("<00:02.000>", enhanced["lrc"])

    def test_manual_choice_takes_precedence_over_automatic_matches(self):
        store = plugin.LyricsStore(":memory:")
        self.addCleanup(store.close)
        chosen = plugin.make_bundle(plugin.parse_ttml(TTML))
        target = {"title": "Song", "artist": "Artist", "album": "", "duration": 180, "isrc": ""}
        store.put(plugin._auto_cache_key(target, "all"), plugin.make_bundle(plugin.parse_lrc("[00:01.00]auto")))
        store.put(plugin._manual_cache_key(target), chosen)
        with patch.object(plugin, "_LYRICS_STORE", store):
            result = plugin._find_best_bundle("Song", "Artist", "", 180, "", "all", True, 5, True)
        self.assertEqual(result["ttml"], chosen["ttml"])

    def test_auto_cache_is_scoped_to_the_source_preference(self):
        target = {"title": "Song", "artist": "Artist"}
        self.assertNotEqual(plugin._auto_cache_key(target, "all"), plugin._auto_cache_key(target, "lrclib"))

    def test_eligible_candidates_rank_before_higher_scoring_rejections(self):
        rows = [
            {"title": "Song", "artist": "Artist", "duration": 180, "isrc": "USRC99999999",
             "album": "Album", "source": "LRC.red", "provider_id": "lrcred"},
            {"title": "Song", "artist": "Artist", "duration": 181, "album": "Album",
             "source": "LRC.red", "provider_id": "lrcred"},
        ]
        with patch.object(plugin, "search_lrcred", return_value=rows):
            ranked = plugin.query_all_sources(
                "Song", "Artist", "Album", 180, "USRC12300001", source_preference="lrcred",
                duration_tolerance=5, prefer_synced=True, provider_options={},
            )
        self.assertEqual([item["match_eligible"] for item in ranked], [True, False])

    def test_a_failing_provider_does_not_abort_the_search(self):
        row = {"title": "Song", "artist": "Artist", "duration": 180, "source": "Unison",
               "provider_id": "unison"}
        with patch.object(plugin, "search_lrcred", side_effect=KeyError("boom")), \
                patch.object(plugin, "search_unison", return_value=[row]):
            ranked = plugin.query_all_sources(
                "Song", "Artist", duration=180, duration_tolerance=5, prefer_synced=True,
                provider_options={},
            )
        self.assertEqual(len(ranked), 1)


class ProviderHardeningTests(unittest.TestCase):
    def setUp(self):
        patcher = patch.object(plugin, "log", Mock())
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_lrcred_resolves_every_isrc_of_a_multi_valued_tag(self):
        def fetch(url, params=None):
            isrc = url.rsplit("/", 1)[-1].split(".")[0]
            return {"isrc": isrc, "title": "Song", "artist": "Artist", "duration": 180}

        with patch.object(plugin, "fetch_json", side_effect=fetch) as fetch_json:
            results = plugin.search_lrcred(isrc="USRC12300001; GBUM71029604")
        self.assertEqual(sorted(r["isrc"] for r in results), ["GBUM71029604", "USRC12300001"])
        self.assertEqual(fetch_json.call_count, 2)

    def test_unison_ignores_malformed_rows(self):
        payload = {"success": True, "data": [
            "junk", None, {"id": {"nested": 1}},
            {"id": 7, "song": "Song", "artist": "Artist", "syncType": "RICHSYNC", "duration": "x"},
        ]}
        with patch.object(plugin, "fetch_json", return_value=payload):
            results = plugin.search_unison("Song", "Artist")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["timing_type"], "richsync")
        self.assertEqual(results[0]["duration"], 0)

    def test_unison_with_non_list_data_returns_nothing(self):
        with patch.object(plugin, "fetch_json", return_value={"success": True, "data": "oops"}):
            self.assertEqual(plugin.search_unison("Song", "Artist"), [])

    def test_lrcred_falls_through_an_unparseable_ttml_response(self):
        candidate = {"source": "LRC.red", "provider_id": "lrcred", "isrc": ISRC}
        with patch.object(plugin, "fetch_text", side_effect=["<html>blocked</html>", "[00:01.00]test"]) as fetch:
            bundle = plugin.resolve_lyrics_bundle(candidate, clean_words=True)
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(bundle["lrc"], "[00:01.000]test")

    def test_unison_item_endpoint_with_unexpected_shape_is_a_miss(self):
        candidate = {"source": "Unison", "provider_id": "unison", "id": 7}
        with patch.object(plugin, "fetch_json", return_value=["not", "a", "dict"]):
            bundle = plugin.resolve_lyrics_bundle(candidate, clean_words=True)
        self.assertIsNone(bundle["ttml"])
        self.assertIsNone(bundle["lrc"])


class MetadataTests(unittest.TestCase):
    def test_millisecond_length_is_preferred_over_the_rounded_display_value(self):
        class Metadata(dict):
            length = 253077

        metadata = Metadata({"title": "Song", "~length": "4:13"})
        self.assertEqual(plugin._extract_track_query(metadata, None)[4], 253.077)

    def test_formatted_length_is_the_fallback(self):
        self.assertEqual(plugin._extract_track_query({"title": "Song", "~length": "4:13"}, None)[4], 253)

    def test_config_writes_without_picard_do_not_change_registered_defaults(self):
        proxy = plugin.ConfigProxy()
        proxy["betterlyrics_format"] = "lrc"
        self.assertEqual(proxy["betterlyrics_format"], "lrc")
        self.assertEqual(plugin.PLUGIN_OPTIONS["betterlyrics_format"], "ttml")


if __name__ == "__main__":
    unittest.main()
