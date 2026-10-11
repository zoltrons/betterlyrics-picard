"""Exercise automatic hooks through provider ranking, parsing and application."""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from test_matching import load_plugin


plugin = load_plugin()
TTML = (
    '<tt xmlns="http://www.w3.org/ns/ttml"><body><div>'
    '<p begin="1s" end="3s"><span begin="1s" end="2s">test</span> '
    '<span begin="2s" end="3s">line</span></p></div></body></tt>'
)


class AutoPipelineTests(unittest.TestCase):
    def test_anti_load_hook_selects_parses_and_embeds_despite_existing_sidecar(self):
        with tempfile.TemporaryDirectory() as directory:
            file = SimpleNamespace(
                filename=str(Path(directory) / "track.m4a"),
                metadata={"title": "Kiss It Better"}, update=Mock(), save=Mock(),
            )
            sidecar = Path(directory) / "track.ttml"
            sidecar.write_text("existing sidecar", encoding="utf-8")
            track = SimpleNamespace(
                metadata={"title": "Kiss It Better", "artist": "Rihanna",
                          "album": "ANTI", "~length": "4:13"},
                files=[file], update=Mock(),
            )
            candidates = [
                {"title": "Kiss It Better", "artist": "Rihanna", "album": "ANTI",
                 "duration": 253.077, "isrc": isrc, "provider_id": "lrcred",
                 "source": "LRC.red", "metadata_verified": True, "timing_type": "word"}
                for isrc in ("QM5FT1600115", "QM5FT1600102")
            ]
            tasks = []
            queue = SimpleNamespace(pending=1, submit=lambda **task: tasks.append(task) or True)
            options = {**plugin.PLUGIN_OPTIONS, "betterlyrics_source": "lrcred"}
            with patch.object(plugin, "config", options), \
                    patch.object(plugin, "FETCH_QUEUE", queue), \
                    patch.object(plugin, "_ensure_queue_ready"), \
                    patch.object(plugin, "_LYRICS_STORE", None), \
                    patch.object(plugin, "search_lrcred", return_value=candidates), \
                    patch.object(plugin, "fetch_text", return_value=TTML) as download:
                plugin.on_file_added(track, file)
                self.assertEqual(len(tasks), 1)
                # Execute the real worker body, then its GUI-thread consumer.
                bundle = tasks[0]["job_fn"]()
                self.assertEqual(bundle["quality"], "word")
                tasks[0]["apply_fn"](bundle)
                self.assertIn("<span", file.metadata["lyrics"])
                self.assertEqual(file.metadata["ttml"], bundle["ttml"])
                self.assertEqual(track.metadata["lyrics"], file.metadata["lyrics"])
                self.assertEqual(sidecar.read_text(), "existing sidecar")
                file.update.assert_called_once()
                file.save.assert_not_called()
                download.assert_called_once()


if __name__ == "__main__":
    unittest.main()
