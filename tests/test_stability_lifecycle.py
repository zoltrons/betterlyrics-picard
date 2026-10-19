"""Regression tests for the stability/lifecycle hardening.

These cover the failure modes that matter when Picard disables a plugin or a
provider misbehaves: late worker results, restarted epochs, queued jobs after
stop, uncancellable pacing, and config access from a worker thread.
"""

import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from test_matching import load_plugin


plugin = load_plugin()
BUNDLE = {"ttml": "<tt><body>lyrics</body></tt>", "lrc": "[00:01.00]lyrics"}
OPTIONS = {
    **plugin.PLUGIN_OPTIONS,
    "betterlyrics_save_ttml_file": False,
    "betterlyrics_save_lrc_file": False,
}
WORKER_OPTIONS = {
    "duration_tolerance": 5,
    "prefer_synced": True,
    "enable_lyricsplus": False,
    "enable_boidu": True,
    "enable_lrclib": False,
    "api_key": "secret-key",
}


def wait_until(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


class ExplodingConfig(dict):
    """Fails loudly if background code reads Picard's config off-thread."""

    def __getitem__(self, key):
        raise AssertionError(f"config[{key!r}] read from a worker path")


class FetchQueueLifecycleTests(unittest.TestCase):
    def test_submit_is_rejected_before_start_and_after_stop(self):
        queue = plugin.FetchQueue(1)
        self.assertFalse(queue.submit(lambda: "never"))
        queue.start()
        self.assertTrue(queue.submit(lambda: "ok", key="k"))
        queue.stop()
        self.assertFalse(queue.is_running)
        self.assertFalse(queue.submit(lambda: "late", key="late"))

    def test_stop_discards_pending_jobs_and_drops_the_running_result(self):
        queue = plugin.FetchQueue(1)
        posted = []
        queue.set_invoker(SimpleNamespace(post=posted.append))
        queue.start()
        started = threading.Event()
        release = threading.Event()

        def blocking_job():
            started.set()
            release.wait(5)
            return "slow"

        self.assertTrue(queue.submit(blocking_job, apply_fn=lambda value: posted.append(value), key="slow"))
        self.assertTrue(started.wait(2))
        for index in range(5):
            self.assertTrue(
                queue.submit(lambda: "queued", apply_fn=lambda value: posted.append(value), key=f"q{index}")
            )
        self.assertGreaterEqual(queue.pending, 5)

        stopped_at = time.monotonic()
        queue.stop(timeout=0.2)
        self.assertLess(time.monotonic() - stopped_at, 1.0)
        self.assertEqual(queue.pending, 0)

        release.set()
        self.assertTrue(wait_until(lambda: queue.active == 0))
        # A job that was already running must not apply after shutdown.
        self.assertEqual(posted, [])

    def test_restart_epoch_discards_a_stale_worker_result(self):
        queue = plugin.FetchQueue(1)
        applied = []
        queue.set_invoker(SimpleNamespace(post=lambda fn: fn()))
        queue.start()
        started = threading.Event()
        release = threading.Event()

        def blocking_job():
            started.set()
            release.wait(5)
            return "old session"

        queue.submit(blocking_job, apply_fn=lambda value: applied.append(("old", value)), key="old")
        self.assertTrue(started.wait(2))
        queue.stop(timeout=0.1)
        queue.start()

        release.set()
        time.sleep(0.2)
        self.assertEqual(applied, [])

        self.assertTrue(queue.submit(lambda: "new", apply_fn=applied.append, key="new"))
        self.assertTrue(wait_until(lambda: applied == ["new"]))
        queue.stop()

    def test_duplicate_keys_are_released_after_completion(self):
        queue = plugin.FetchQueue(1)
        applied = []
        queue.set_invoker(SimpleNamespace(post=lambda fn: fn()))
        queue.start()
        try:
            self.assertTrue(queue.submit(lambda: 1, apply_fn=applied.append, key="same"))
            self.assertFalse(queue.submit(lambda: 2, apply_fn=applied.append, key="same"))
            self.assertTrue(wait_until(lambda: applied == [1]))
            self.assertTrue(wait_until(lambda: queue.pending == 0 and queue.active == 0))
            self.assertTrue(queue.submit(lambda: 3, apply_fn=applied.append, key="same"))
            self.assertTrue(wait_until(lambda: applied == [1, 3]))
        finally:
            queue.stop()


class DisabledStateTests(unittest.TestCase):
    def test_queue_is_not_resurrected_after_disable(self):
        queue = Mock()
        with patch.object(plugin, "_DISABLED", True), \
                patch.object(plugin, "FETCH_QUEUE", queue), \
                patch.object(plugin, "_init_caches", Mock()), \
                patch.object(plugin, "_MAIN_INVOKER", None):
            self.assertFalse(plugin._ensure_queue_ready())
        queue.start.assert_not_called()

    def test_automatic_lookup_and_save_hook_noop_after_disable(self):
        queue = Mock()
        file = SimpleNamespace(filename="/tmp/song.flac", metadata={"title": "Song"})
        with patch.object(plugin, "_DISABLED", True), \
                patch.object(plugin, "FETCH_QUEUE", queue), \
                patch.object(plugin, "config", dict(OPTIONS)), \
                patch.object(plugin, "log", Mock()):
            self.assertFalse(
                plugin.fetch_and_apply_lyrics("get", None, [file], file.metadata)
            )
            queue.submit.assert_not_called()
            plugin.on_file_saved(file)
            self.assertNotIn(file, plugin.files_processing)

    def test_shutdown_cancels_paced_http_before_any_request(self):
        with patch.object(plugin, "_SHUTDOWN_EVENT", threading.Event()), \
                patch.object(plugin, "_init_caches", Mock()), \
                patch.object(plugin, "_RATE_LIMITER", Mock()), \
                patch.object(plugin, "_http_get", Mock()) as http_get:
            plugin._SHUTDOWN_EVENT.set()
            request = SimpleNamespace(full_url="https://example.invalid/x")
            self.assertIsNone(plugin._http_get_with_retry(request, 5, retries=2))
        http_get.assert_not_called()

    def test_rate_limiter_cancels_its_paced_wait(self):
        limiter = plugin.RateLimiter(0.5)
        self.assertTrue(limiter.acquire())
        cancel = threading.Event()
        result = []

        thread = threading.Thread(target=lambda: result.append(limiter.acquire(cancel)))
        thread.start()
        time.sleep(0.05)
        started = time.monotonic()
        cancel.set()
        thread.join(1.0)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result, [False])
        self.assertLess(time.monotonic() - started, 0.3)


class ProviderAndThreadingTests(unittest.TestCase):
    def test_one_provider_failure_does_not_suppress_fallbacks(self):
        candidate = {
            "title": "Song", "artist": "Artist", "album": "",
            "duration": 180, "isrc": "", "metadata_verified": True,
            "provider_id": "unison", "source": "Unison",
        }
        with patch.object(plugin, "search_lrcred", side_effect=RuntimeError("boom")), \
                patch.object(plugin, "search_unison", return_value=[candidate]), \
                patch.object(plugin, "search_provider", return_value={"status": "hit", "candidates": []}), \
                patch.object(plugin, "_PROVIDER_HTTP", object()), \
                patch.object(plugin, "config", ExplodingConfig()), \
                patch.object(plugin, "log", Mock()):
            found = plugin.query_all_sources(
                "Song", "Artist", duration=180, duration_tolerance=5,
                prefer_synced=True, options=WORKER_OPTIONS,
            )
        self.assertEqual([item["title"] for item in found], ["Song"])

    def test_optional_provider_exception_is_contained(self):
        candidate = {
            "title": "Song", "artist": "Artist", "duration": 180,
            "isrc": "", "metadata_verified": True, "provider_id": "unison",
            "source": "Unison",
        }
        with patch.object(plugin, "search_lrcred", return_value=[candidate]), \
                patch.object(plugin, "search_unison", return_value=[]), \
                patch.object(plugin, "search_provider", side_effect=ValueError("bad payload")), \
                patch.object(plugin, "_PROVIDER_HTTP", object()), \
                patch.object(plugin, "config", ExplodingConfig()), \
                patch.object(plugin, "log", Mock()):
            found = plugin.query_all_sources(
                "Song", "Artist", duration=180, duration_tolerance=5,
                prefer_synced=True, options=WORKER_OPTIONS,
            )
        self.assertEqual(len(found), 1)

    def test_worker_options_snapshot_is_used_without_touching_config(self):
        with patch.object(plugin, "search_lrcred", return_value=[]), \
                patch.object(plugin, "search_unison", return_value=[]), \
                patch.object(plugin, "search_provider", return_value={"status": "hit", "candidates": []}) as provider, \
                patch.object(plugin, "_PROVIDER_HTTP", object()), \
                patch.object(plugin, "config", ExplodingConfig()), \
                patch.object(plugin, "log", Mock()):
            plugin.query_all_sources(
                "Song", "Artist", duration=180, duration_tolerance=5,
                prefer_synced=True, options=WORKER_OPTIONS,
            )
        boidu_calls = [call for call in provider.call_args_list if call.args[0] == "boidu"]
        self.assertEqual(len(boidu_calls), 1)
        self.assertEqual(boidu_calls[0].kwargs["api_key"], "secret-key")
        self.assertEqual(len(provider.call_args_list), 1)  # lyricsplus/lrclib disabled

    def test_find_best_bundle_passes_the_snapshot_to_the_query(self):
        with patch.object(plugin, "query_all_sources", return_value=[]) as query, \
                patch.object(plugin, "_LYRICS_STORE", None), \
                patch.object(plugin, "log", Mock()):
            self.assertIsNone(
                plugin._find_best_bundle(
                    "Song", "Artist", "", 180, "", "all", True, 5, True, WORKER_OPTIONS
                )
            )
        self.assertEqual(query.call_args.kwargs["options"], WORKER_OPTIONS)


class GuiDispatchTests(unittest.TestCase):
    def test_callback_drain_is_bounded_per_tick(self):
        with patch.object(plugin, "log", Mock()):
            dispatcher = plugin.MainThreadDispatcher(poll_ms=1000, max_callbacks_per_tick=5)
        applied = []
        try:
            for index in range(12):
                dispatcher.post(lambda index=index: applied.append(index))
            dispatcher.poll()
            self.assertEqual(len(applied), 5)
            dispatcher.poll()
            self.assertEqual(len(applied), 10)
            dispatcher.poll()
            self.assertEqual(len(applied), 12)
            dispatcher.poll()
            self.assertEqual(len(applied), 12)
        finally:
            dispatcher.stop()

    def test_stop_drops_callbacks_posted_before_and_after(self):
        with patch.object(plugin, "log", Mock()):
            dispatcher = plugin.MainThreadDispatcher(poll_ms=1000, max_callbacks_per_tick=5)
        applied = []
        dispatcher.post(lambda: applied.append("before"))
        dispatcher.stop()
        dispatcher.post(lambda: applied.append("after"))
        dispatcher.poll()
        self.assertEqual(applied, [])


class ApplyContainmentTests(unittest.TestCase):
    def test_one_broken_file_does_not_block_the_rest(self):
        class ExplodingMetadata(dict):
            def __setitem__(self, key, value):
                raise RuntimeError("metadata is read-only")

        with tempfile.TemporaryDirectory() as directory:
            broken = SimpleNamespace(
                filename=str(Path(directory) / "broken.flac"),
                metadata=ExplodingMetadata(title="Broken"),
            )
            healthy = SimpleNamespace(
                filename=str(Path(directory) / "healthy.flac"),
                metadata={"title": "Healthy"},
                update=Mock(),
            )
            with patch.object(plugin, "config", dict(OPTIONS)), patch.object(plugin, "log", Mock()):
                self.assertTrue(plugin.save_lyrics_to_files([broken, healthy], BUNDLE))
            self.assertEqual(healthy.metadata["lyrics"], BUNDLE["ttml"])
            healthy.update.assert_called_once()


if __name__ == "__main__":
    unittest.main()
