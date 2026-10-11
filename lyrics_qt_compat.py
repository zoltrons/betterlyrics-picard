"""Scoped workaround for Picard's cyclic menu/destroyed-slot lifetime bug.

Qt delivers ``destroyed(QObject*)`` with the native object's address even when
the original Python wrapper is being collected.  A module-lived dispatcher can
therefore cancel per-menu guards without giving PyQt a collectable closure or a
bound method whose lifetime depends on the menu being destroyed.
"""

from __future__ import annotations

import importlib
import inspect
import sys
from types import CodeType, FunctionType


class _Guard:
    __slots__ = ("cancelled", "on_destroyed")

    def __init__(self, on_destroyed):
        self.cancelled = False
        self.on_destroyed = on_destroyed


# These roots outlive individual menus and installation/uninstallation.  Guards
# do not store the watched QObject or the callback; only the optional abort
# action needs retention until destruction, as in Picard's original contract.
_active_guards: dict[int, list[_Guard]] = {}
_sip = None
_patched_module = None
_original_cancel_on_destroyed = None


def active_menu_guard_count() -> int:
    """Return retained guards without exposing menus or mutable guard state."""
    return sum(len(guards) for guards in _active_guards.values())


def _dispatch_destroyed(qobject):
    # SIP may supply a different Python wrapper to this signal, so Python id()
    # or weak references cannot reliably identify the original watched object.
    key = int(_sip.unwrapinstance(qobject))
    guards = _active_guards.get(key)
    if guards is None:
        return

    actions = []
    for guard in guards:
        # Cancel every callback before aborting any request: abort can reenter
        # Python, deliver a response, destroy another menu, or run collection.
        guard.cancelled = True
        action = guard.on_destroyed
        guard.on_destroyed = None
        if action is not None:
            actions.append(action)

    first_error = None
    try:
        for action in actions:
            try:
                action()
            except BaseException as error:
                # One failed abort must not leave the other requests live.
                # Re-raising afterward preserves Qt's usual exception route.
                if first_error is None:
                    first_error = error
    finally:
        # Abort actions and guards stay strongly rooted through all reentrant
        # work above.  Releasing them here is safe even before Qt finishes its
        # destructor: PyQt's actual slot is the module-lived function, never a
        # guard-owned closure.  No timer/event-loop turn is needed for cleanup.
        if _active_guards.get(key) is guards:
            del _active_guards[key]
    if first_error is not None:
        raise first_error


def safe_cancel_on_destroyed(qobject, callback, on_destroyed=None):
    """Picard-compatible callback cancellation with a rooted destroyed slot."""
    global _sip
    if _sip is None:
        _sip = importlib.import_module("PyQt6.sip")
    key = int(_sip.unwrapinstance(qobject))
    guard = _Guard(on_destroyed)
    guards = _active_guards.get(key)
    if guards is None:
        guards = [guard]
        _active_guards[key] = guards
        try:
            # A single module-level slot per QObject avoids both per-menu
            # callable cycles and duplicate dispatch for multiple requests.
            qobject.destroyed.connect(_dispatch_destroyed)
        except BaseException:
            if _active_guards.get(key) is guards:
                del _active_guards[key]
            raise
    else:
        # Reentrant registration during an abort must not resurrect a callback
        # on an object whose destroyed signal is already being processed.
        if guards[0].cancelled:
            guard.cancelled = True
            guard.on_destroyed = None
            if on_destroyed is not None:
                on_destroyed()
        else:
            guards.append(guard)

    def wrapper(*args, **kwargs):
        if not guard.cancelled:
            return callback(*args, **kwargs)
        return None

    return wrapper


def _known_vulnerable_helper(helper) -> bool:
    # Match the known closure shape as well as its provenance and signature;
    # a future Picard implementation must be reviewed rather than overwritten.
    if not isinstance(helper, FunctionType) or helper.__module__ != "picard.util.qt":
        return False
    code = helper.__code__
    if (
        code.co_argcount != 3
        or code.co_posonlyargcount
        or code.co_kwonlyargcount
        or code.co_flags & (inspect.CO_VARARGS | inspect.CO_VARKEYWORDS)
        or code.co_varnames[:3] != ("qobject", "callback", "on_destroyed")
        or helper.__defaults__ != (None,)
        or helper.__kwdefaults__
        or set(code.co_cellvars) != {"callback", "cancelled", "on_destroyed"}
    ):
        return False
    nested = [value for value in code.co_consts if isinstance(value, CodeType)]
    cancel = [value for value in nested if value.co_name == "_cancel"]
    wrappers = [
        value for value in nested
        if value.co_name != "_cancel"
        and set(value.co_freevars) == {"callback", "cancelled"}
        and value.co_flags & inspect.CO_VARARGS
        and value.co_flags & inspect.CO_VARKEYWORDS
    ]
    return (
        len(nested) == 2
        and len(cancel) == 1
        and set(cancel[0].co_freevars) == {"cancelled", "on_destroyed"}
        and bool(cancel[0].co_flags & inspect.CO_VARARGS)
        and len(wrappers) == 1
    )


def _version_pair(value):
    try:
        major, minor, *_ = value.split(".")
        return int(major), int(minor)
    except (AttributeError, TypeError, ValueError):
        return None


def _log(log, message):
    if log is None:
        return
    try:
        emit = getattr(log, "info", None)
        if callable(emit):
            emit(message)
        elif callable(log):
            log(message)
    except Exception:
        # Optional diagnostics must not break plugin startup or installation.
        pass


def install_menu_lifetime_workaround(log=None) -> bool:
    """Patch only basetreeview's known helper on the affected runtime.

    Return whether this workaround is installed.  Missing dependencies and
    incomplete Picard/Qt test stubs simply leave the application untouched.
    """
    global _sip, _patched_module, _original_cancel_on_destroyed
    if (
        _patched_module is not None
        and getattr(_patched_module, "cancel_on_destroyed", None) is safe_cancel_on_destroyed
    ):
        return True
    if sys.version_info[:2] != (3, 14):
        return False
    try:
        qtcore = importlib.import_module("PyQt6.QtCore")
        if (
            _version_pair(qtcore.PYQT_VERSION_STR) != (6, 11)
            or _version_pair(qtcore.qVersion()) != (6, 11)
        ):
            return False
        sip = importlib.import_module("PyQt6.sip")
        if not callable(getattr(sip, "unwrapinstance", None)):
            return False
        qtutil = importlib.import_module("picard.util.qt")
        basetreeview = importlib.import_module("picard.ui.itemviews.basetreeview")
        original = basetreeview.cancel_on_destroyed
        if original is not qtutil.cancel_on_destroyed or not _known_vulnerable_helper(original):
            return False
    except Exception:
        # Picard imports can be unavailable during plugin discovery, and test
        # stubs need not implement the complete Qt module or item-view package.
        return False

    _sip = sip
    _original_cancel_on_destroyed = original
    _patched_module = basetreeview
    basetreeview.cancel_on_destroyed = safe_cancel_on_destroyed
    _log(log, "BetterLyrics: installed scoped Picard menu lifetime workaround")
    return True


def uninstall_menu_lifetime_workaround() -> None:
    """Restore our alias without invalidating slots on existing live menus."""
    global _patched_module, _original_cancel_on_destroyed
    if (
        _patched_module is not None
        and getattr(_patched_module, "cancel_on_destroyed", None) is safe_cancel_on_destroyed
    ):
        _patched_module.cancel_on_destroyed = _original_cancel_on_destroyed
    _patched_module = None
    _original_cancel_on_destroyed = None
    # Existing menus still own native signal connections.  The dispatcher and
    # registry must survive uninstall and release each guard on destruction.
