import json
import ssl
import tempfile
import unittest
from pathlib import Path

from lyrics_cache import LyricsStore, track_key
from lyrics_http import HttpClient, HttpResult, create_ssl_context
from lyrics_model import make_bundle, parse_kpoe, parse_lrc, parse_ttml
from lyrics_matching import rank_candidates
from lyrics_providers import search_provider


class TimedModelTests(unittest.TestCase):
    def test_absolute_ttml_spans_round_trip_without_doubling_time(self):
        document = parse_ttml(
            '<tt xmlns="http://www.w3.org/ns/ttml"><body><div>'
            '<p begin="00:01.000" end="00:03.000">'
            '<span begin="00:01.000" end="00:02.000">one</span> '
            '<span begin="00:02.000" end="00:03.000">two</span>'
            '</p></div></body></tt>'
        )
        words = document["lines"][0]["words"]
        self.assertEqual([word["start_ms"] for word in words], [1000, 2000])
        self.assertEqual(document["quality"], "word")
        bundle = make_bundle(document, clean_words=False)
        self.assertIn("<span", bundle["ttml"])
        self.assertIn("<00:01.000>one", bundle["enhanced_lrc"])

    def test_kpoe_word_syllabus_and_transliteration_are_preserved(self):
        document = parse_kpoe({
            "type": "Word",
            "metadata": {"totalDuration": "0:03.000"},
            "lyrics": [{
                "time": 1000,
                "duration": 2000,
                "text": "merhaba",
                "syllabus": [{"time": 1000, "duration": 900, "text": "mer"}],
                "transliteration": {
                    "text": "hello",
                    "syllabus": [{"time": 1000, "duration": 900, "text": "hel"}],
                },
            }],
        })
        line = document["lines"][0]
        self.assertEqual(line["words"][0]["start_ms"], 1000)
        self.assertEqual(line["romanized_words"][0]["text"], "hel")

    def test_lrc_prefix_and_unknown_word_end_are_preserved(self):
        document = parse_lrc("[00:01.00]prefix <00:01.50>word")
        line = document["lines"][0]
        self.assertIn("prefix ", line["text"])
        self.assertIsNone(line["words"][0]["end_ms"])


class ProviderIntegrationTests(unittest.TestCase):
    def test_lyricsplus_maps_kpoe_and_does_not_fabricate_identity(self):
        payload = {
            "type": "Word",
            "metadata": {"totalDuration": "0:03.000"},
            "lyrics": [{"time": 0, "duration": 3000, "text": "hello", "syllabus": []}],
        }

        def request(url, params=None, headers=None):
            self.assertEqual(url, "https://lyricsplus.binimum.org/v2/lyrics/get")
            self.assertEqual(params["duration"], "180")
            return HttpResult("hit", payload, status_code=200)

        result = search_provider(
            "lyricsplus",
            {"title": "Hello", "artist": "Adele", "duration": 180, "isrc": "USRC17607839"},
            request,
        )
        self.assertEqual(result["status"], "hit")
        candidate = result["candidates"][0]
        self.assertIsNone(candidate["isrc"])
        self.assertEqual(candidate["duration"], 3.0)
        self.assertFalse(candidate["metadata_verified"])

    def test_boidu_uses_public_endpoint_without_key_and_marks_query_bound(self):
        payload = {
            "ttml": (
                '<tt xmlns="http://www.w3.org/ns/ttml"><body><p begin="0s">'
                '<span begin="0s" end="1s">Hello</span></p></body></tt>'
            )
        }

        def request(url, params=None, headers=None):
            self.assertEqual(url, "https://lyrics-api.boidu.dev/getLyrics")
            self.assertNotIn("X-API-Key", headers)
            return HttpResult("hit", payload, status_code=200)

        result = search_provider(
            "boidu",
            {"title": "Hello", "artist": "Adele", "duration": 180},
            request,
        )
        self.assertEqual(result["status"], "hit")
        candidate = result["candidates"][0]
        self.assertTrue(candidate["query_bound"])
        self.assertEqual(candidate["timing_type"], "word")
        self.assertEqual(
            rank_candidates(
                {"title": "Hello", "artist": "Adele", "album": "", "duration": 180, "isrc": ""},
                [candidate],
                5,
            )[0]["match_reason"],
            "provider_query_bound",
        )

    def test_lrclib_get_is_an_exact_lookup_and_requires_an_id(self):
        payload = {
            "id": 123,
            "trackName": "Hello",
            "artistName": "Adele",
            "albumName": "Album",
            "duration": 180,
            "syncedLyrics": "[00:01.00]Hello",
            "plainLyrics": "Hello",
        }

        def request(url, params=None, headers=None):
            self.assertEqual(url, "https://lrclib.net/api/get")
            self.assertEqual(params["track_name"], "Hello")
            self.assertEqual(params["artist_name"], "Adele")
            self.assertEqual(params["duration"], "180")
            return HttpResult("hit", payload, status_code=200)

        result = search_provider(
            "lrclib",
            {"title": "Hello", "artist": "Adele", "duration": 180},
            request,
        )
        self.assertEqual(result["status"], "hit")
        self.assertTrue(result["candidates"][0]["exact_lookup"])

    def test_http_transport_json_is_bounded_and_ssl_is_verified(self):
        client = HttpClient(transport=lambda *args, **kwargs: (200, json.dumps({"ok": True})), min_interval=0)
        result = client.request_json("https://example.test/data")
        self.assertEqual(result.status, "hit")
        self.assertEqual(result.data["ok"], True)
        self.assertEqual(create_ssl_context().verify_mode, ssl.CERT_REQUIRED)
        client.close()


class CacheTests(unittest.TestCase):
    def test_bundle_cache_and_choice_are_separate(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "lyrics.sqlite3"
            target = {"title": "Song", "artist": "Artist", "duration": 180}
            key = track_key(target)
            with LyricsStore(path) as store:
                bundle = {"ttml": "<tt />", "lrc": "[00:01.000]Song"}
                store.put(key, bundle)
                store.set_choice(key, "lyricsplus", offset_ms=120)
                self.assertEqual(store.get(key)["lrc"], bundle["lrc"])
                self.assertEqual(store.get_choice(key)["offset_ms"], 120)


if __name__ == "__main__":
    unittest.main()
