"""Real-Qt check: enable/disable and result delivery from a worker thread.

Picard installs and updates plugins on a QThreadPool worker, so ``enable()`` and
``disable()`` may run outside the GUI thread.  Run separately from the stub
suite (PyQt6 required):

    QT_QPA_PLATFORM=offscreen python tests/qt_dispatcher_thread_smoke.py
"""

import gc
import importlib.util
import logging
from pathlib import Path
import sys
import threading
import time
import types
from unittest.mock import Mock

from PyQt6 import QtCore, QtGui, QtWidgets


class PluginConfig(dict):
    def register_option(self, key, default):
        self.setdefault(key, default)


def load_plugin():
    qt = types.ModuleType("PyQt6")
    qt.QtCore = QtCore
    qt.QtGui = QtGui
    qt.QtWidgets = QtWidgets
    api_module = types.ModuleType("picard.plugin3.api")
    api_module.Album = type("Album", (), {})
    api_module.BaseAction = type("BaseAction", (), {})
    api_module.File = type("File", (), {})
    api_module.Metadata = dict
    api_module.OptionsPage = type("OptionsPage", (), {})
    api_module.PluginApi = type("PluginApi", (), {"get_api": staticmethod(lambda: None)})
    api_module.Track = type("Track", (), {})
    sys.modules.update({
        "PyQt6": qt,
        "PyQt6.QtGui": qt.QtGui,
        "PyQt6.QtWidgets": QtWidgets,
        "picard": types.ModuleType("picard"),
        "picard.plugin3": types.ModuleType("picard.plugin3"),
        "picard.plugin3.api": api_module,
    })
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root))
    spec = importlib.util.spec_from_file_location("lyrics_qt_dispatcher_smoke", root / "__init__.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    module._lyrics_store_path = lambda: ":memory:"
    return module


def spin(app, predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


def main():
    app = QtWidgets.QApplication([])
    app.setQuitOnLastWindowClosed(False)
    errors = []
    sys.excepthook = lambda *args: errors.append(args)
    plugin = load_plugin()
    plugin.log = logging.getLogger("qt-dispatcher-smoke")
    api = Mock(plugin_config=PluginConfig())
    api.logger = logging.getLogger("qt-dispatcher-smoke.api")

    worker_ids = {}

    def enable_on_worker():
        worker_ids["enable"] = threading.get_ident()
        plugin.enable(api)

    thread = threading.Thread(target=enable_on_worker)
    thread.start()
    thread.join()
    assert worker_ids["enable"] != threading.main_thread().ident
    assert not errors, errors
    assert plugin.FETCH_QUEUE.is_running

    applied = {}

    def job():
        assert threading.current_thread() is not threading.main_thread()
        return "bundle"

    def apply(value):
        applied["value"] = value
        applied["thread"] = threading.get_ident()

    assert plugin.FETCH_QUEUE.submit(job, apply_fn=apply, key="smoke")
    assert spin(app, lambda: "value" in applied), "worker result never reached the GUI thread"
    assert applied["value"] == "bundle"
    assert applied["thread"] == threading.main_thread().ident, "result applied off the GUI thread"

    errors_before_disable = len(errors)
    disable_ids = {}

    def disable_on_worker():
        disable_ids["disable"] = threading.get_ident()
        plugin.disable()

    thread = threading.Thread(target=disable_on_worker)
    thread.start()
    thread.join()
    assert disable_ids["disable"] != threading.main_thread().ident
    assert not plugin.FETCH_QUEUE.is_running
    assert len(errors) == errors_before_disable, errors
    gc.collect()
    app.processEvents()

    plugin.FETCH_QUEUE.submit(job, apply_fn=apply, key="late")
    spin(app, lambda: False, timeout=0.2)
    assert applied["value"] == "bundle", "post-disable result was applied"
    print(
        "PASS: enable/disable off-GUI thread and result delivery on GUI thread; "
        f"Python {sys.version.split()[0]}, PyQt {QtCore.PYQT_VERSION_STR}, Qt {QtCore.qVersion()}",
        flush=True,
    )


if __name__ == "__main__":
    main()
