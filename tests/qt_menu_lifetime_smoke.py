"""Isolated real-Qt regression for Picard's album-menu teardown crash.

Default runs the protected path. --baseline deliberately exercises the old
crashing helper in a child process. Never loads a music library or uses network.
Uses an importable Picard installation, or --picard-binary to inspect a frozen
installation via PyInstaller. No upstream implementation is bundled here.
"""

import argparse
import builtins
from functools import partial, wraps
import gc
from pathlib import Path
import signal
import subprocess
import sys
import types
import weakref

from PyQt6 import QtCore, QtWidgets, sip

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import lyrics_qt_compat as compat


def original_helper(binary):
    if not binary:
        from picard.util.qt import cancel_on_destroyed
        return cancel_on_destroyed
    from PyInstaller.archive.readers import CArchiveReader
    archive = CArchiveReader(binary)
    code = archive.open_embedded_archive("PYZ.pyz").extract("picard.util.qt")
    helper_code = next(c for c in code.co_consts if isinstance(c, types.CodeType)
                       and c.co_name == "cancel_on_destroyed")
    return types.FunctionType(helper_code, {
        "wraps": wraps, "__builtins__": builtins.__dict__, "__name__": "picard.util.qt",
    }, argdefs=(None,))


def run_cycles(original, unsafe=False):
    app = QtWidgets.QApplication([])
    parent = QtWidgets.QWidget()
    errors = []
    sys.excepthook = lambda *args: errors.append(args)
    qtutil = types.ModuleType("picard.util.qt")
    treeview = types.ModuleType("picard.ui.itemviews.basetreeview")
    qtutil.cancel_on_destroyed = treeview.cancel_on_destroyed = original
    sys.modules[qtutil.__name__] = qtutil
    sys.modules[treeview.__name__] = treeview
    if not unsafe:
        assert compat.install_menu_lifetime_workaround()
        assert compat.install_menu_lifetime_workaround()  # idempotent
        assert qtutil.cancel_on_destroyed is original  # no global utility patch
    aborted = []
    refs = []

    class Request:
        pass

    def create_menu():
        menu = QtWidgets.QMenu(parent)
        child = QtWidgets.QMenu(menu)
        request = None

        def abort():
            # Reentrancy must see cancellation before response delivery.
            assert request.callback() is None
            aborted.append(True)
            gc.collect()

        callback = treeview.cancel_on_destroyed(child, partial(lambda widget: 42, child), abort)
        assert callback() == 42
        request = Request()
        request.callback = callback
        refs.extend((weakref.ref(request), weakref.ref(child)))
        menu.deleteLater()

    for _ in range(300):
        create_menu()
        gc.collect()
        QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
        app.processEvents()
    assert len(aborted) == 300
    assert not errors, errors
    if not unsafe:
        assert compat.active_menu_guard_count() == 0
        gc.collect()
        assert all(ref() is None for ref in refs), "retained menu/request cycle"
        # Uninstall while a watched object lives must not invalidate its slot.
        obj = QtCore.QObject()
        calls = []
        callback = treeview.cancel_on_destroyed(obj, lambda x: x + 1,
                                               lambda: calls.append("destroyed"))
        assert callback(41) == 42
        compat.uninstall_menu_lifetime_workaround()
        assert treeview.cancel_on_destroyed is original
        sip.delete(obj)
        assert callback(41) is None
        assert calls == ["destroyed"]
        assert compat.active_menu_guard_count() == 0
        # More than one request on one QObject uses one rooted dispatcher;
        # cancel them all before any abort callback can reenter another wrapper.
        obj = QtCore.QObject()
        callbacks = []
        aborts = []
        callbacks.append(compat.safe_cancel_on_destroyed(
            obj, lambda: "first", lambda: aborts.append(callbacks[1]())))
        callbacks.append(compat.safe_cancel_on_destroyed(obj, lambda: "second"))
        sip.delete(obj)
        assert aborts == [None]
        assert all(callback() is None for callback in callbacks)
        assert compat.active_menu_guard_count() == 0
        # Never restore over somebody else's later patch.
        assert compat.install_menu_lifetime_workaround()
        replacement = lambda *args: None
        treeview.cancel_on_destroyed = replacement
        compat.uninstall_menu_lifetime_workaround()
        assert treeview.cancel_on_destroyed is replacement
        assert not compat.install_menu_lifetime_workaround()
        # Future, unrecognized implementations are intentionally untouched.
        qtutil.cancel_on_destroyed = replacement
        assert not compat.install_menu_lifetime_workaround()
        assert treeview.cancel_on_destroyed is replacement
    print("PASS: 300 cyclic menu teardowns, cancellation, cleanup and uninstall", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", action="store_true")
    parser.add_argument("--unsafe-child", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--picard-binary")
    args = parser.parse_args()
    if args.baseline:
        command = [sys.executable, "-X", "faulthandler", __file__, "--unsafe-child"]
        if args.picard_binary:
            command.extend(("--picard-binary", args.picard_binary))
        result = subprocess.run(command, capture_output=True, text=True, timeout=30)
        assert result.returncode == -signal.SIGSEGV, (
            f"Expected baseline SIGSEGV; got {result.returncode}: {result.stdout} {result.stderr}"
        )
        print("Confirmed: original helper child exited with SIGSEGV (-11)", flush=True)
    run_cycles(original_helper(args.picard_binary), unsafe=args.unsafe_child)


if __name__ == "__main__":
    main()
