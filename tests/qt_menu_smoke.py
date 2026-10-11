"""Optional real-Qt menu/worker smoke test; no network or audio writes.

Run separately from the stub-based unit suite:
QT_QPA_PLATFORM=offscreen python tests/qt_menu_smoke.py
Requires PyQt6 in the chosen interpreter. Does not require installed Picard.
"""

import gc
import importlib.util
import logging
from pathlib import Path
import sys
import threading
import time
import types

from PyQt6 import QtCore, QtGui, QtWidgets, sip


def main():
    app = QtWidgets.QApplication([])
    app.setQuitOnLastWindowClosed(False)
    errors = []
    sys.excepthook = lambda *args: errors.append(args)

    class Item:
        def __init__(self):
            self.metadata = {"title": "Test", "artist": "Artist", "~length": "3:00"}
            self.files = []

        def update(self):
            assert threading.current_thread() is threading.main_thread()

    class Action(QtGui.QAction):
        def __init__(self, parent=None):
            super().__init__(self.TITLE, parent)
            self.selection = []
            self.triggered.connect(self._invoke)

        def _invoke(self):
            self.callback(self.selection)

    api = types.ModuleType("picard.plugin3.api")
    api.Album = type("Album", (Item,), {})
    api.Track = type("Track", (Item,), {})
    api.File = type("File", (Item,), {})
    api.Metadata = dict
    api.BaseAction = Action
    api.OptionsPage = QtWidgets.QWidget
    api.PluginApi = type("PluginApi", (), {"get_api": staticmethod(lambda: None)})
    sys.modules.update({"picard": types.ModuleType("picard"),
                        "picard.plugin3": types.ModuleType("picard.plugin3"),
                        "picard.plugin3.api": api})
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root))
    spec = importlib.util.spec_from_file_location("lyrics_qt_smoke", root / "__init__.py")
    plugin = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = plugin
    spec.loader.exec_module(plugin)
    plugin.log = logging.getLogger("qt-smoke")
    plugin.config = {**plugin.PLUGIN_OPTIONS, "betterlyrics_save_ttml_file": False,
                     "betterlyrics_save_lrc_file": False}
    invoked = []

    def resolve(*args):
        assert threading.current_thread() is not threading.main_thread()
        invoked.append(args[0])
        return {"ttml": "<tt><body>test</body></tt>", "lrc": "test"}

    plugin._find_best_bundle = resolve
    tracks = []
    plugin._ensure_queue_ready()
    for _ in range(100):
        track = api.Track()
        tracks.append(track)
        menu = QtWidgets.QMenu()
        submenu = menu.addMenu("Plugins")
        action = plugin.BetterLyricsGetAction()
        action.setParent(submenu)
        action.selection = [track]
        submenu.addAction(action)
        action.trigger()
        menu.deleteLater()
        QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
        assert sip.isdeleted(menu) and sip.isdeleted(action)
        app.processEvents()
        del action, submenu, menu
        gc.collect()

    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        app.processEvents()
        if all(track.metadata.get("lyrics") for track in tracks):
            break
        time.sleep(.005)
    assert len(invoked) == 100, len(invoked)
    assert all(track.metadata.get("lyrics") for track in tracks)
    assert not errors, errors
    plugin.disable()
    app.processEvents()
    print(f"PASS: 100 menu destructions and worker results; Python {sys.version.split()[0]}, "
          f"PyQt {QtCore.PYQT_VERSION_STR}, Qt {QtCore.qVersion()}", flush=True)


if __name__ == "__main__":
    main()
