"""LRC.red contracts observed at /docs and /search.json (2026-10-11).

Network-free regression tests; Picard/Qt are stubbed by the existing harness.
"""
import io
import unittest
from unittest.mock import MagicMock, Mock, call, patch
from urllib.error import HTTPError, URLError
from urllib.request import Request

from test_matching import plugin


ISRC = "GBUM71029604"
BASE = "https://lrc.red"
METADATA = {
    "isrc": ISRC,
    "title": "Bohemian Rhapsody (Remastered 2011)",
    "artist": "Queen",
    "album": "A Night at the Opera",
    "duration": 356.52,
    "sync": "word",
    "files": {
        "ttml": f"{BASE}/s/{ISRC}.ttml",
        "lrc": f"{BASE}/s/{ISRC}.lrc",
    },
}


class LrcRedTests(unittest.TestCase):
    def test_single_object_isrc_resource_and_mapping(self):
        with patch.object(plugin, "fetch_json", return_value=METADATA) as fetch:
            results = plugin.search_lrcred(isrc="gb-um7-10-29604")
        fetch.assert_called_once_with(f"{BASE}/s/{ISRC}.json")
        self.assertEqual(len(results), 1)
        result = results[0]
        self.assertEqual(result["title"], METADATA["title"])
        self.assertEqual(result["artist"], "Queen")
        self.assertEqual(result["duration"], 356.52)
        self.assertEqual(result["timing_type"], "word")
        self.assertEqual(result["lyrics_url"], METADATA["files"]["ttml"])

    def test_missing_or_invalid_isrc_response_falls_back_to_text(self):
        for invalid in (None, {}, {"error": "not found"}, {"isrc": "GBUM71100986"}):
            with self.subTest(response=invalid), patch.object(
                plugin, "fetch_json", side_effect=[invalid, {"hits": [METADATA]}]
            ) as fetch:
                results = plugin.search_lrcred("Bohemian Rhapsody", "Queen", isrc=ISRC)
                self.assertEqual(results[0]["title"], METADATA["title"])
                self.assertEqual(fetch.call_count, 2)
                self.assertEqual(fetch.call_args, call(
                    f"{BASE}/search.json", {"q": "Queen Bohemian Rhapsody"}
                ))

    def test_explicit_query_does_not_use_old_isrc(self):
        with patch.object(plugin, "fetch_json", return_value={"hits": [METADATA]}) as fetch:
            plugin.search_lrcred("Old title", "Old artist", isrc=ISRC, query=" Queen Rhapsody ")
        fetch.assert_called_once_with(f"{BASE}/search.json", {"q": "Queen Rhapsody"})

    def test_hits_without_files_use_documented_isrc_urls(self):
        hit = {key: METADATA[key] for key in ("isrc", "title", "artist", "duration")}
        with patch.object(plugin, "fetch_json", return_value={"hits": [hit]}):
            result = plugin.search_lrcred(query="Queen")[0]
        self.assertEqual(result["timing_type"], "unknown")
        with patch.object(plugin, "fetch_text", side_effect=[None, "[00:01.00]test"]) as fetch:
            bundle = plugin.resolve_lyrics_bundle(result, clean_words=True)
        self.assertEqual(fetch.call_args_list, [
            call(f"{BASE}/s/{ISRC}.ttml"), call(f"{BASE}/s/{ISRC}.lrc")
        ])
        self.assertEqual(bundle["lrc"], "[00:01.000]test")

    def test_independent_files_urls_are_not_guessed_from_ttml(self):
        metadata = dict(METADATA, files={
            "ttml": "https://lrc.red/custom/timed.xml",
            "lrc": "https://lrc.red/custom/line-timed.txt",
        })
        with patch.object(plugin, "fetch_json", return_value=metadata):
            result = plugin.search_lrcred(isrc=ISRC)[0]
        with patch.object(plugin, "fetch_text", side_effect=[None, "[00:01.00]test"]) as fetch:
            bundle = plugin.resolve_lyrics_bundle(result, clean_words=True)
        self.assertTrue(bundle["is_synced"])
        self.assertEqual(fetch.call_args_list, [
            call(metadata["files"]["ttml"]), call(metadata["files"]["lrc"])
        ])

    def test_pagination_deduplicates_and_stops_repeated_cursor(self):
        other = dict(METADATA, isrc="GBUM71100986")
        pages = [
            {"hits": [METADATA], "next": "p24"},
            {"hits": [METADATA, other], "next": "p24"},
        ]
        with patch.object(plugin, "fetch_json", side_effect=pages) as fetch:
            results = plugin.search_lrcred(query="Queen")
        self.assertEqual(len(results), 2)
        self.assertEqual(fetch.call_args_list, [
            call(f"{BASE}/search.json", {"q": "Queen"}),
            call(f"{BASE}/search.json", {"q": "Queen", "after": "p24"}),
        ])

    def test_pagination_is_bounded(self):
        pages = [{"hits": [METADATA], "next": f"p{i}"} for i in range(10)]
        with patch.object(plugin, "fetch_json", side_effect=pages) as fetch:
            plugin.search_lrcred(query="Queen")
        self.assertEqual(fetch.call_count, 3)

    def test_malformed_rows_and_nonfinite_durations(self):
        hits = [None, [], "bad", {}, {"isrc": "../../secret"}, dict(METADATA, duration="NaN")]
        with patch.object(plugin, "fetch_json", return_value={"hits": hits}):
            results = plugin.search_lrcred(query="Queen")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["duration"], 0)

    def test_empty_input_does_not_make_requests(self):
        with patch.object(plugin, "fetch_json") as fetch:
            self.assertEqual(plugin.search_lrcred(isrc="../../invalid"), [])
        fetch.assert_not_called()


class SearchFlowTests(unittest.TestCase):
    def test_manual_query_retains_track_identity_for_ranking(self):
        widgets = MagicMock()
        widgets.QLineEdit.return_value.text.return_value = " edited search "
        with patch.object(plugin, "QtWidgets", widgets), patch.object(
            plugin, "query_all_sources", return_value=[]
        ) as search:
            plugin.show_search_dialog(object(), "Title", "Artist", "Album", 180, ISRC, [])
            on_search = widgets.QPushButton.return_value.clicked.connect.call_args_list[0].args[0]
            on_search()
        search.assert_called_once_with(
            title="Title", artist="Artist", album="Album", duration=180,
            isrc=ISRC, query="edited search", source_preference="all",
        )


class HttpRetryTests(unittest.TestCase):
    def setUp(self):
        # No real timers, network calls or queues in this test.
        for name, value in (("_init_caches", Mock()), ("_RATE_LIMITER", Mock()), ("log", Mock())):
            patcher = patch.object(plugin, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch.object(plugin, "_wait_for_retry", return_value=False)
        self.wait = patcher.start()
        self.addCleanup(patcher.stop)
        self.request = Request(f"{BASE}/s/{ISRC}.json")


    def test_permanent_http_errors_are_not_retried(self):
        for status in (400, 403, 404):
            with self.subTest(status=status):
                body = io.BytesIO(b"not found")
                error = HTTPError(self.request.full_url, status, "error", {}, body)
                with patch.object(plugin, "_http_get", side_effect=error) as get:
                    self.assertIsNone(plugin._http_get_with_retry(self.request, 10, retries=2))
                get.assert_called_once()
                self.assertTrue(body.closed)
        self.wait.assert_not_called()

    def test_transient_http_and_network_errors_still_retry(self):
        for error in (
            HTTPError(self.request.full_url, 429, "rate limit", {}, io.BytesIO()),
            HTTPError(self.request.full_url, 503, "unavailable", {}, io.BytesIO()),
            URLError("temporary network error"),
        ):
            with self.subTest(error=error), patch.object(
                plugin, "_http_get", side_effect=[error, (200, "ok")]
            ) as get:
                self.assertEqual(plugin._http_get_with_retry(self.request, 10, retries=2), "ok")
                self.assertEqual(get.call_count, 2)
        self.assertEqual(self.wait.call_count, 3)


if __name__ == "__main__":
    unittest.main()
