import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from test_matching import load_plugin


plugin = load_plugin()
BUNDLE = {"ttml": "<tt><body>fetched lyrics</body></tt>", "lrc": "[00:01.00]fetched lyrics"}


class DeferredFile(plugin.File):
    """Picard save returns before its post-save processor is invoked."""

    def __init__(self, filename, metadata=None):
        self.filename = str(filename)
        self.metadata = {"title": "Song", "artist": "Artist", **(metadata or {})}
        self.update_calls = 0
        self.save_calls = 0
        self.pending_saves = []

    def update(self):
        self.update_calls += 1

    def save(self):
        self.save_calls += 1
        self.pending_saves.append(lambda: plugin.on_file_saved(self))

    def finish_save(self, filename=None):
        if filename is not None:
            self.filename = str(filename)
        self.pending_saves.pop(0)()


class DeferredQueue:
    def __init__(self):
        self.tasks = []

    @property
    def pending(self):
        return len(self.tasks)

    def submit(self, **task):
        self.tasks.append(task)
        return True

    def finish_lookup(self, bundle):
        self.tasks.pop(0)["apply_fn"](bundle)


class AutoApplyTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.file = DeferredFile(self.root / "song.flac")
        self.queue = DeferredQueue()
        self.options = {
            **plugin.PLUGIN_OPTIONS,
            "betterlyrics_save_ttml_file": False,
            "betterlyrics_save_lrc_file": False,
            "betterlyrics_get_on_save": True,
        }
        for name, value in (("config", self.options), ("FETCH_QUEUE", self.queue),
                            ("_ensure_queue_ready", Mock()), ("log", Mock())):
            patcher = patch.object(plugin, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        plugin.files_processing.clear()
        self.addCleanup(plugin.files_processing.clear)

    def track(self, metadata=None):
        track = plugin.Track()
        track.metadata = {"title": "Song", **(metadata or {})}
        track.update = Mock()
        track.files = [self.file]
        return track

    def test_existing_sidecar_does_not_block_empty_tags(self):
        self.options["betterlyrics_save_ttml_file"] = True
        sidecar = self.root / "song.ttml"
        sidecar.write_text("existing sidecar", encoding="utf-8")
        self.assertTrue(plugin.save_lyrics_to_files([self.file], BUNDLE))
        self.assertEqual(sidecar.read_text(encoding="utf-8"), "existing sidecar")
        self.assertEqual(self.file.metadata["lyrics"], BUNDLE["ttml"])
        self.assertEqual(self.file.metadata["ttml"], BUNDLE["ttml"])

    def test_overwrite_disabled_protects_each_destination_and_fills_missing(self):
        self.options.update(betterlyrics_embed_syncedlyrics=True, betterlyrics_save_lrc_file=True,
                            betterlyrics_save_ttml_file=True)
        self.file.metadata.update(lyrics="existing tag", syncedlyrics="existing sync")
        (self.root / "song.ttml").write_text("existing sidecar", encoding="utf-8")
        self.assertTrue(plugin.save_lyrics_to_files([self.file], BUNDLE))
        self.assertEqual(self.file.metadata["lyrics"], "existing tag")
        self.assertEqual(self.file.metadata["syncedlyrics"], "existing sync")
        self.assertEqual(self.file.metadata["ttml"], BUNDLE["ttml"])
        self.assertEqual((self.root / "song.lrc").read_text(), BUNDLE["lrc"])
        self.assertEqual((self.root / "song.ttml").read_text(), "existing sidecar")

    def test_overwrite_enabled_replaces_tags_and_sidecars(self):
        self.options.update(betterlyrics_auto_overwrite=True, betterlyrics_save_ttml_file=True,
                            betterlyrics_save_lrc_file=True, betterlyrics_embed_syncedlyrics=True)
        self.file.metadata.update(lyrics="old", ttml="old", syncedlyrics="old")
        for extension in ("ttml", "lrc"):
            (self.root / f"song.{extension}").write_text("old", encoding="utf-8")
        self.assertTrue(plugin.save_lyrics_to_files([self.file], BUNDLE))
        for tag in ("lyrics", "ttml"):
            self.assertEqual(self.file.metadata[tag], BUNDLE["ttml"])
        self.assertEqual(self.file.metadata["syncedlyrics"], BUNDLE["lrc"])
        for extension in ("ttml", "lrc"):
            self.assertEqual((self.root / f"song.{extension}").read_text(), BUNDLE[extension])

    def test_format_does_not_enable_sidecar_writes(self):
        for format in ("ttml", "lrc", "both"):
            with self.subTest(format=format):
                self.options["betterlyrics_format"] = format
                plugin.save_lyrics_to_files([self.file], BUNDLE)
                self.assertFalse((self.root / "song.ttml").exists())
                self.assertFalse((self.root / "song.lrc").exists())

    def test_embed_switches_are_independent(self):
        for enabled in ("lyrics", "ttml", "syncedlyrics", None):
            with self.subTest(enabled=enabled):
                self.file.metadata = {}
                self.options.update(
                    betterlyrics_embed_lyrics=enabled == "lyrics",
                    betterlyrics_embed_ttml_tag=enabled == "ttml",
                    betterlyrics_embed_syncedlyrics=enabled == "syncedlyrics",
                )
                plugin.save_lyrics_to_files([self.file], BUNDLE)
                self.assertEqual(set(self.file.metadata), {enabled} if enabled else set())

    def test_no_file_track_obeys_protection_and_overwrite(self):
        track = self.track({"lyrics": "existing", "ttml": "existing"})
        self.assertFalse(plugin.save_lyrics_to_files([], BUNDLE, track=track))
        track.update.assert_not_called()
        self.assertEqual(track.metadata["lyrics"], "existing")
        self.options["betterlyrics_auto_overwrite"] = True
        self.assertTrue(plugin.save_lyrics_to_files([], BUNDLE, track=track))
        self.assertEqual(track.metadata["lyrics"], BUNDLE["ttml"])

    def test_interactive_confirmation_controls_existing_only(self):
        for accepted in (False, True):
            with self.subTest(accepted=accepted), patch.object(plugin, "confirm_replace", return_value=accepted) as confirm:
                track = self.track({"lyrics": "existing"})
                self.assertTrue(plugin.save_lyrics_to_files([], BUNDLE, track=track, interactive=True))
                confirm.assert_called_once()
                self.assertEqual(track.metadata["lyrics"], BUNDLE["ttml"] if accepted else "existing")
                self.assertEqual(track.metadata["ttml"], BUNDLE["ttml"])

    def test_identical_metadata_is_a_noop_even_with_overwrite(self):
        self.options["betterlyrics_auto_overwrite"] = True
        self.file.metadata.update(lyrics=BUNDLE["ttml"], ttml=BUNDLE["ttml"])
        changed_files = []
        self.assertFalse(plugin.save_lyrics_to_files([self.file], BUNDLE, changed_files=changed_files))
        self.assertEqual(changed_files, [])
        self.assertEqual(self.file.update_calls, 0)
        self.assertIn("no-op", plugin.log.info.call_args.args[0])

    def test_existing_protected_destinations_skip_lookup_without_queue(self):
        self.file.metadata.update(lyrics="existing", ttml="existing")
        plugin.on_file_saved(self.file)
        self.assertEqual(self.queue.pending, 0)
        plugin._ensure_queue_ready.assert_not_called()
        self.assertNotIn(self.file, plugin.files_processing)
        self.assertIn("protected", plugin.log.info.call_args.args[0])

    def test_no_enabled_destinations_skip_lookup(self):
        self.options.update(betterlyrics_embed_lyrics=False, betterlyrics_embed_ttml_tag=False)
        self.assertFalse(plugin.fetch_and_apply_lyrics("get", None, [self.file], self.file.metadata))
        plugin._ensure_queue_ready.assert_not_called()

    def test_save_guard_survives_deferred_callback_and_rename(self):
        self.options["betterlyrics_auto_overwrite"] = True
        plugin.on_file_saved(self.file)
        self.assertEqual(self.queue.pending, 1)
        self.assertEqual(plugin.files_processing[self.file], "fetching")
        # Another user save while lookup is pending must not consume the guard.
        self.file.filename = str(self.root / "renamed-before-result.flac")
        plugin.on_file_saved(self.file)
        plugin.on_file_saved(self.file)
        self.assertEqual(self.queue.pending, 1)
        self.queue.finish_lookup(BUNDLE)
        self.assertEqual(self.file.save_calls, 1)
        self.assertEqual(plugin.files_processing[self.file], "saving")
        self.file.finish_save(self.root / "renamed-after-save.flac")
        self.assertEqual(self.queue.pending, 0)
        self.assertNotIn(self.file, plugin.files_processing)
        # A later genuine user save can look up again, but identical metadata
        # must never create another plugin save / post-save feedback loop.
        plugin.on_file_saved(self.file)
        self.queue.finish_lookup(BUNDLE)
        self.assertEqual(self.file.save_calls, 1)
        self.assertNotIn(self.file, plugin.files_processing)

    def test_callback_consumes_guard_even_if_save_option_was_disabled(self):
        plugin._apply_auto_bundle("save", "Song", [self.file], None, BUNDLE)
        self.options["betterlyrics_get_on_save"] = False
        self.file.finish_save()
        self.assertNotIn(self.file, plugin.files_processing)

    def test_load_and_get_stage_without_audio_save(self):
        for method in ("load", "get"):
            with self.subTest(method=method):
                self.file.metadata = {}
                plugin._apply_auto_bundle(method, "Song", [self.file], None, BUNDLE)
                self.assertEqual(self.file.metadata["lyrics"], BUNDLE["ttml"])
                self.assertEqual(self.file.save_calls, 0)
                self.assertNotIn(self.file, plugin.files_processing)

    def test_save_only_persists_files_with_changed_metadata(self):
        unchanged = DeferredFile(self.root / "existing.flac", {"lyrics": "keep", "ttml": "keep"})
        plugin.files_processing[unchanged] = "fetching"
        plugin.files_processing[self.file] = "fetching"
        plugin._apply_auto_bundle("save", "Song", [unchanged, self.file], None, BUNDLE)
        self.assertEqual(unchanged.save_calls, 0)
        self.assertEqual(self.file.save_calls, 1)
        self.assertNotIn(unchanged, plugin.files_processing)
        self.assertEqual(plugin.files_processing[self.file], "saving")

    def test_sidecar_only_changes_never_save_audio(self):
        self.options.update(betterlyrics_embed_lyrics=False, betterlyrics_embed_ttml_tag=False,
                            betterlyrics_save_lrc_file=True)
        plugin.on_file_saved(self.file)
        self.queue.finish_lookup(BUNDLE)
        self.assertEqual((self.root / "song.lrc").read_text(), BUNDLE["lrc"])
        self.assertEqual(self.file.save_calls, 0)
        self.assertNotIn(self.file, plugin.files_processing)

    def test_no_result_and_apply_exception_release_guard(self):
        plugin.on_file_saved(self.file)
        self.queue.finish_lookup(None)
        self.assertNotIn(self.file, plugin.files_processing)
        plugin.on_file_saved(self.file)
        with patch.object(plugin, "save_lyrics_to_files", side_effect=RuntimeError("apply failed")):
            self.queue.finish_lookup(BUNDLE)
        self.assertNotIn(self.file, plugin.files_processing)

    def test_save_exception_releases_guard(self):
        self.file.save = Mock(side_effect=RuntimeError("save failed"))
        plugin.on_file_saved(self.file)
        self.queue.finish_lookup(BUNDLE)
        self.assertNotIn(self.file, plugin.files_processing)

    def test_audio_save_not_submitted_releases_guard(self):
        self.file.save = Mock(return_value=False)
        plugin.on_file_saved(self.file)
        self.queue.finish_lookup(BUNDLE)
        self.assertNotIn(self.file, plugin.files_processing)

    def test_sidecar_created_during_lookup_is_protected_when_result_arrives(self):
        self.options["betterlyrics_save_ttml_file"] = True
        plugin.on_file_saved(self.file)
        sidecar = self.root / "song.ttml"
        sidecar.write_text("created during lookup", encoding="utf-8")
        self.queue.finish_lookup(BUNDLE)
        self.assertEqual(sidecar.read_text(), "created during lookup")
        self.assertEqual(self.file.metadata["lyrics"], BUNDLE["ttml"])

    def test_identical_sidecar_is_noop_and_non_utf8_sidecar_can_be_overwritten(self):
        self.options.update(betterlyrics_embed_lyrics=False, betterlyrics_embed_ttml_tag=False,
                            betterlyrics_save_ttml_file=True, betterlyrics_auto_overwrite=True)
        sidecar = self.root / "song.ttml"
        sidecar.write_text(BUNDLE["ttml"], encoding="utf-8")
        self.assertFalse(plugin.save_lyrics_to_files([self.file], BUNDLE))
        sidecar.write_bytes(b"\xffold content")
        self.assertTrue(plugin.save_lyrics_to_files([self.file], BUNDLE))
        self.assertEqual(sidecar.read_text(), BUNDLE["ttml"])

    def test_multivalue_metadata_compares_all_values(self):
        class Metadata(dict):
            def getall(self, key):
                value = self.get(key)
                return value if isinstance(value, list) else [value]

        self.options["betterlyrics_auto_overwrite"] = True
        self.file.metadata = Metadata(lyrics=[BUNDLE["ttml"], "extra"], ttml=BUNDLE["ttml"])
        self.assertTrue(plugin.save_lyrics_to_files([self.file], BUNDLE))
        self.assertEqual(self.file.metadata["lyrics"], BUNDLE["ttml"])
        self.assertFalse(plugin.save_lyrics_to_files([self.file], BUNDLE))

    def test_no_submission_and_submission_exception_release_guard(self):
        for outcome in (False, RuntimeError("submit failed")):
            with self.subTest(outcome=outcome):
                submit = Mock(side_effect=outcome) if isinstance(outcome, Exception) else Mock(return_value=False)
                with patch.object(self.queue, "submit", submit):
                    plugin.on_file_saved(self.file)
                self.assertNotIn(self.file, plugin.files_processing)

    def test_missing_title_releases_guard_without_submission(self):
        self.file.metadata.pop("title")
        plugin.on_file_saved(self.file)
        self.assertNotIn(self.file, plugin.files_processing)
        self.assertEqual(self.queue.pending, 0)

    def test_parent_item_and_legacy_parent_used_by_hooks_and_actions(self):
        for attr in ("parent_item", "parent"):
            with self.subTest(attr=attr):
                track = self.track()
                setattr(self.file, attr, track)
                with patch.object(plugin, "fetch_and_apply_lyrics", return_value=False) as fetch:
                    plugin.on_file_saved(self.file)
                    self.assertIs(fetch.call_args.args[1], track)
                tracks, files = plugin.extract_items([self.file])
                self.assertEqual(tracks, [track])
                self.assertEqual(files, [])
                delattr(self.file, attr)

    def test_save_dedup_is_per_file_even_under_the_same_track(self):
        track = self.track()
        other = DeferredFile(self.root / "other.flac")
        self.assertNotEqual(plugin._dedup_key(track, [self.file], "save"),
                            plugin._dedup_key(track, [other], "save"))
        before = plugin._dedup_key(track, [self.file], "save")
        self.file.filename = str(self.root / "renamed.flac")
        self.assertEqual(before, plugin._dedup_key(track, [self.file], "save"))

    def test_single_legacy_provider_does_not_query_optional_providers(self):
        with patch.object(plugin, "search_lrcred", return_value=[]) as lrcred, \
                patch.object(plugin, "search_unison", return_value=[]) as unison, \
                patch.object(plugin, "search_provider") as optional, \
                patch.object(plugin, "_PROVIDER_HTTP", object()):
            plugin.query_all_sources("Song", "Artist", source_preference="lrcred")
            lrcred.assert_called_once()
            unison.assert_not_called()
            optional.assert_not_called()
            lrcred.reset_mock()
            plugin.query_all_sources("Song", "Artist", source_preference="unison")
            unison.assert_called_once()
            lrcred.assert_not_called()
            optional.assert_not_called()

    def test_provider_status_logs_do_not_include_response_messages_or_keys(self):
        self.options["betterlyrics_api_key"] = "private-api-key"
        with patch.object(plugin, "_PROVIDER_HTTP", object()), \
                patch.object(plugin, "search_provider", return_value={
                    "status": "auth_required", "message": "private-api-key and lyric payload"
                }):
            plugin.query_all_sources("Song", "Artist", source_preference="boidu")
        messages = " ".join(call.args[0] for call in plugin.log.info.call_args_list)
        self.assertIn("auth_required", messages)
        self.assertIn("0 candidate", messages)
        self.assertNotIn("private-api-key", messages)
        self.assertNotIn("lyric payload", messages)

    def test_lookup_logs_distinguish_no_candidates_rejected_and_empty_content(self):
        cases = (
            ([], "providers returned no candidates"),
            ([{"title": "Unrelated", "artist": "Wrong", "duration": 180}], "rejected; reason="),
            ([{"title": "Song", "artist": "Artist", "duration": 180}], "empty lyrics bundle"),
        )
        for candidates, expected in cases:
            with self.subTest(expected=expected), \
                    patch.object(plugin, "query_all_sources", return_value=candidates), \
                    patch.object(plugin, "resolve_lyrics_bundle", return_value={}), \
                    patch.object(plugin, "_LYRICS_STORE", None):
                plugin.log.reset_mock()
                result = plugin._find_best_bundle("Song", "Artist", "", 180, "", "all", True, 5, True)
                self.assertIsNone(result)
                messages = " ".join(call.args[0] for call in plugin.log.info.call_args_list)
                self.assertIn(expected, messages)
                self.assertNotIn(BUNDLE["ttml"], messages)


if __name__ == "__main__":
    unittest.main()
