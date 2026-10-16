# -*- coding: utf-8 -*-
#
# Better Lyrics Plugin for MusicBrainz Picard 3.0+
#
# Fetches synchronized (word-level and line-level) TTML and LRC lyrics from the
# Better Lyrics ecosystem (LRC.red, Unison crowdsourced database & Better Lyrics API).
#
# Designed specifically for MusicBrainz Picard 3.0 Plugin v3 Architecture.
#

from __future__ import annotations

from functools import partial
import os
import queue
import re
import shutil
import tempfile
import threading
import time
from weakref import WeakKeyDictionary

from PyQt6 import QtCore, QtGui, QtWidgets
from PyQt6.QtCore import Qt

from picard.plugin3.api import (
    Album,
    BaseAction,
    File,
    Metadata,
    OptionsPage,
    PluginApi,
    Track,
)

try:
    from .lyrics_cache import LyricsStore, track_key
    from .lyrics_http import HttpClient, create_ssl_context
    from .lyrics_matching import (
        assess_match,
        choose_identity_group,
        normalize_isrcs,
        rank_candidates,
    )
    from .lyrics_model import make_bundle, parse_kpoe, parse_lrc, parse_ttml
    from .lyrics_providers import search_provider
    from .lyrics_qt_compat import (
        install_menu_lifetime_workaround,
        uninstall_menu_lifetime_workaround,
    )
except ImportError:  # Picard loads a plugin directory as a top-level module.
    from lyrics_cache import LyricsStore, track_key
    from lyrics_http import HttpClient, create_ssl_context
    from lyrics_matching import (
        assess_match,
        choose_identity_group,
        normalize_isrcs,
        rank_candidates,
    )
    from lyrics_model import make_bundle, parse_kpoe, parse_lrc, parse_ttml
    from lyrics_providers import search_provider
    from lyrics_qt_compat import (
        install_menu_lifetime_workaround,
        uninstall_menu_lifetime_workaround,
    )

# ---------------------------------------------------------------------------
# Plugin Constants & Default Options
# ---------------------------------------------------------------------------
PLUGIN_NAME = "Better Lyrics"

PLUGIN_OPTIONS = {
    "betterlyrics_get_on_load": True,
    "betterlyrics_get_on_save": False,
    "betterlyrics_auto_overwrite": False,
    # Primary format: "ttml", "lrc", or "both"
    "betterlyrics_format": "ttml",
    # Sidecar files
    "betterlyrics_save_ttml_file": True,
    "betterlyrics_save_lrc_file": False,
    # Metadata Tag embedding
    "betterlyrics_embed_lyrics": True,
    "betterlyrics_embed_ttml_tag": True,
    "betterlyrics_embed_syncedlyrics": False,
    # Format options
    "betterlyrics_clean_word_timestamps": True,
    "betterlyrics_prefer_synced": True,
    "betterlyrics_ignore_instrumental": True,
    "betterlyrics_plain_as_txt": False,
    # Sources & Matching
    "betterlyrics_source": "all",  # all, lrcred, unison, lyricsplus, boidu, lrclib
    "betterlyrics_enable_lyricsplus": True,
    "betterlyrics_enable_boidu": True,
    "betterlyrics_enable_lrclib": True,
    "betterlyrics_duration_tolerance": 5,  # seconds
    "betterlyrics_api_key": "",
    # Stability / anti-abuse tuning (background fetch queue)
    "betterlyrics_max_workers": 3,  # concurrent background fetch jobs
    "betterlyrics_rate_limit_ms": 200,  # min delay between outbound HTTP requests
}

# Service URLs
LRC_RED_BASE_URL = "https://lrc.red"
LRC_RED_SEARCH_URL = f"{LRC_RED_BASE_URL}/search.json"
UNISON_API_URL = "https://unison.betterlyrics.org/lyrics"
UNISON_SEARCH_URL = "https://unison.betterlyrics.org/lyrics/search"

PROVIDER_IDS = ("lrcred", "unison", "lyricsplus", "boidu", "lrclib")
OPTIONAL_PROVIDER_IDS = ("lyricsplus", "boidu", "lrclib")
# LRC.red search hits omit their sync type; they are usually timed, so an
# unknown type is tried before a provider that explicitly reports plain text.
_TIMING_RANK = {"word": 0, "richsync": 0, "syllable": 0, "line": 1, "linesync": 1, "unknown": 2}
_PLAIN_TIMING_RANK = 3

ISRC_PATTERN = re.compile(r"[A-Z]{2}[A-Z0-9]{3}[0-9]{7}")
USER_AGENT = "betterlyrics-picard/1.3.0 (+https://github.com/zoltrons/betterlyrics-picard)"
# Manual choices outlive ordinary cache entries; lyrics_cache caps TTLs at a year.
MANUAL_CHOICE_TTL = 365 * 24 * 60 * 60.0

# PyQt6 Layout & View Alignment Constants
ALIGN_CENTER = Qt.AlignmentFlag.AlignCenter
ALIGN_LEFT = Qt.AlignmentFlag.AlignLeft
ALIGN_RIGHT = Qt.AlignmentFlag.AlignRight
RESIZE_TO_CONTENTS = QtWidgets.QHeaderView.ResizeMode.ResizeToContents
RESIZE_STRETCH = QtWidgets.QHeaderView.ResizeMode.Stretch
RESIZE_INTERACTIVE = QtWidgets.QHeaderView.ResizeMode.Interactive
SELECT_ROWS = QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows
SINGLE_SELECTION = QtWidgets.QAbstractItemView.SelectionMode.SingleSelection
NO_EDIT_TRIGGERS = QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers

# Object identity survives Picard's file renames. Keep the guard until the
# asynchronous plugin save reaches its post-save hook, not merely save()'s return.
files_processing = WeakKeyDictionary()

# Sidecars written per file, so they can follow the audio when Picard renames it.
_WRITTEN_SIDECARS = WeakKeyDictionary()

# Common audio extensions for orphaned file scanning
AUDIO_EXTENSIONS = {
    ".aac", ".ac3", ".aif", ".aifc", ".aiff", ".ape", ".asf", ".dff",
    ".dsf", ".eac3", ".flac", ".m2a", ".m4a", ".m4b", ".m4p", ".mp2",
    ".mp3", ".mp4", ".mpc", ".ofr", ".ofs", ".oga", ".ogg", ".opus",
    ".spx", ".tak", ".tta", ".wav", ".wma", ".wv"
}

# Global reference to PluginApi instance
CURRENT_PLUGIN_API: PluginApi | None = None


def get_plugin_api() -> PluginApi | None:
    """Retrieves the active PluginApi instance."""
    if CURRENT_PLUGIN_API is not None:
        return CURRENT_PLUGIN_API
    try:
        return PluginApi.get_api()
    except Exception:
        return None


# ---------------------------------------------------------------------------
# API, Logger & Config Proxies
# ---------------------------------------------------------------------------
class LoggerProxy:
    """Routes log calls directly to the PluginApi logger in Picard 3.0+."""

    @property
    def _logger(self):
        api = get_plugin_api()
        if api and hasattr(api, "logger"):
            return api.logger
        import logging
        return logging.getLogger("better_lyrics")

    def info(self, msg, *args, **kwargs):
        self._logger.info(msg, *args, **kwargs)

    def debug(self, msg, *args, **kwargs):
        self._logger.debug(msg, *args, **kwargs)

    def warning(self, msg, *args, **kwargs):
        self._logger.warning(msg, *args, **kwargs)

    def error(self, msg, *args, **kwargs):
        self._logger.error(msg, *args, **kwargs)


log = LoggerProxy()


class ConfigProxy:
    """Routes configuration lookups and updates to api.plugin_config.

    Picard's config section is not safe to read from worker threads, so
    background jobs receive a snapshot taken on the GUI thread instead.
    """

    def __init__(self):
        # Used only when no PluginApi is available (tests, discovery); writes
        # must never alter PLUGIN_OPTIONS, which holds the registered defaults.
        self._local: dict = {}

    def get(self, key: str, default=None):
        api = get_plugin_api()
        if api is not None and hasattr(api, "plugin_config"):
            try:
                # Picard's ConfigSection has no .get(); use item access, which
                # returns the persisted value or the registered default.
                value = api.plugin_config[key]
                if value is not None:
                    return value
            except Exception:
                pass
        if key in self._local:
            return self._local[key]
        fallback = PLUGIN_OPTIONS.get(key)
        if fallback is not None:
            return fallback
        return default

    def __getitem__(self, key: str):
        return self.get(key, PLUGIN_OPTIONS.get(key))

    def __setitem__(self, key: str, value):
        api = get_plugin_api()
        if api and hasattr(api, "plugin_config"):
            try:
                api.plugin_config[key] = value
                return
            except Exception:
                pass
        self._local[key] = value


config = ConfigProxy()


def _config_int(api: PluginApi | None, key: str, default: int) -> int:
    """Reads an integer plugin option defensively from ``api.plugin_config``."""
    value = None
    if api is not None and hasattr(api, "plugin_config"):
        try:
            value = api.plugin_config[key]  # ConfigSection item access, not .get()
        except Exception:
            value = None
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _ensure_queue_ready() -> None:
    """Lazily (idempotently) starts the background fetch queue.

    Called from the GUI thread on the automatic paths, guaranteeing that a
    submitted job always has workers and a main-thread invoker available.
    """
    global _MAIN_INVOKER
    _init_caches()
    if _MAIN_INVOKER is None:
        _MAIN_INVOKER = MainThreadDispatcher()
    FETCH_QUEUE.set_invoker(_MAIN_INVOKER)
    if not FETCH_QUEUE.is_running:
        FETCH_QUEUE.start()


# ---------------------------------------------------------------------------
# Duration & Text Formatting Utilities
# ---------------------------------------------------------------------------
def format_duration(seconds: int | float | None) -> str:
    """Formats duration in seconds to mm:ss or hh:mm:ss."""
    try:
        total_sec = max(0, int(round(float(seconds or 0))))
    except (TypeError, ValueError, OverflowError):
        total_sec = 0
    hours, remainder = divmod(total_sec, 3600)
    minutes, sec = divmod(remainder, 60)
    if hours > 0:
        return f"{hours}:{minutes:02d}:{sec:02d}"
    return f"{minutes}:{sec:02d}"


def parse_duration(time_val) -> int:
    """Parses duration string ('mm:ss', 'hh:mm:ss' or milliseconds) into seconds."""
    if isinstance(time_val, (int, float)):
        if time_val > 10000:
            return int(round(time_val / 1000.0))
        return int(round(time_val))

    time_str = str(time_val).strip()
    if not time_str:
        return 0

    if time_str.isdigit():
        val = int(time_str)
        if val > 10000:
            return int(round(val / 1000.0))
        return val

    parts = time_str.split(":")
    try:
        if len(parts) == 2:
            return int(parts[0]) * 60 + int(float(parts[1]))
        elif len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(float(parts[2]))
    except Exception:
        pass
    return 0


def _metadata_seconds(metadata) -> float:
    """Return a precise length in seconds from Picard metadata, or 0 when unknown."""
    if metadata is None:
        return 0
    # ``Metadata.length`` holds milliseconds; ``~length`` is rounded to whole
    # seconds for display, so it is only the fallback.
    length_ms = getattr(metadata, "length", None)
    if isinstance(length_ms, (int, float)) and not isinstance(length_ms, bool) and length_ms > 0:
        return round(length_ms / 1000.0, 3)
    try:
        formatted = metadata.get("~length")
    except Exception:
        formatted = None
    return parse_duration(str(formatted)) if formatted else 0


def get_track_duration(track: Track) -> int | float:
    """Extracts duration in seconds from track or file metadata."""
    length = _metadata_seconds(getattr(track, "metadata", None))
    if length:
        return length
    for file in getattr(track, "files", None) or []:
        length = _metadata_seconds(getattr(file, "metadata", None))
        if length:
            return length
    return 0


def truncate_text(text: str, max_lines: int = 6, max_chars_per_line: int = 55) -> str:
    """Truncates text for preview / confirmation popups."""
    if not text:
        return ""
    lines = []
    for i, line in enumerate(text.splitlines()):
        if i >= max_lines:
            lines.append("…")
            break
        if len(line) > max_chars_per_line:
            line = line[: max_chars_per_line - 1].rstrip() + "…"
        lines.append(line)
    return "\n".join(lines)


def is_ttml_content(text: str) -> bool:
    """Checks whether the given string is TTML XML."""
    if not text:
        return False
    stripped = text.lstrip("\ufeff \t\r\n")
    return stripped.startswith("<tt") or stripped.startswith("<?xml") or "<tt xmlns=" in text


def _parse_lyrics_text(raw: str) -> dict:
    return parse_ttml(raw) if is_ttml_content(raw) else parse_lrc(raw)


def _normalize_isrc(value) -> str:
    """Normalizes ISRC formatting without changing its identity."""
    return re.sub(r"[^A-Z0-9]", "", str(value or "").upper())


# ---------------------------------------------------------------------------
# Shared State: HTTP Client, Persistent Cache & Queue Globals
# ---------------------------------------------------------------------------
# Foreground/background fetch queue (created lazily, started from ``enable``).
FETCH_QUEUE = None

# Main-thread dispatcher used by worker threads to apply results safely.
_MAIN_INVOKER = None

# Shared HTTP client and persistent bundle cache.  They are created only after
# Picard enables the plugin, so importing the module stays side-effect free.
# Closing the client on disable cancels every in-flight request.
_PROVIDER_HTTP = None
_LYRICS_STORE = None


def _lyrics_store_path() -> str:
    """Return a user-data path, never a path inside the installed plugin."""
    app_data = None
    try:
        app_data = QtCore.QStandardPaths.writableLocation(
            QtCore.QStandardPaths.StandardLocation.AppDataLocation
        )
    except Exception:
        pass
    if not app_data:
        app_data = os.path.join(os.path.expanduser("~"), ".betterlyrics-picard")
    return os.path.join(app_data, "betterlyrics_cache.sqlite3")


def _init_caches() -> None:
    """Creates the fetch queue singleton exactly once."""
    global FETCH_QUEUE
    if FETCH_QUEUE is None:
        FETCH_QUEUE = FetchQueue()


def _provider_request_json(url: str, params=None, headers=None):
    if _PROVIDER_HTTP is None:
        return None
    return _PROVIDER_HTTP.request_json(url, params=params, headers=headers)


def _log_http_failure(url: str, result) -> None:
    # A miss or a cancellation during disable is expected; anything else is
    # worth a warning.  Only the host and path are logged, never the query.
    if result.status in ("not_found", "cancelled"):
        return
    parts = re.match(r"^(https://[^/?#]+[^?#]*)", url)
    where = parts.group(1) if parts else "provider"
    code = f" HTTP {result.status_code}" if result.status_code else ""
    log.warning(f"{PLUGIN_NAME}: request to {where} failed: {result.status}{code}")


def fetch_json(url: str, params: dict | None = None):
    """Paced, cached, retrying HTTPS GET that parses a JSON response.

    Returns ``None`` on any failure, or when the plugin is not enabled.
    """
    client = _PROVIDER_HTTP
    if client is None:
        return None
    result = client.request_json(url, params=params, headers={"Accept": "application/json"})
    if result.status == "hit":
        return result.data
    _log_http_failure(url, result)
    return None


def fetch_text(url: str) -> str | None:
    """Paced, cached, retrying HTTPS GET returning the raw text response."""
    client = _PROVIDER_HTTP
    if client is None:
        return None
    result = client.request_text(url)
    if result.status == "hit" and isinstance(result.data, str):
        return result.data
    _log_http_failure(url, result)
    return None


# ---------------------------------------------------------------------------
# Main-Thread Bridge & Background Fetch Queue (stability + anti-abuse)
# ---------------------------------------------------------------------------
def _get_main_window(api: PluginApi | None = None):
    """Best-effort lookup of Picard's main window (GUI only)."""
    try:
        api = api or get_plugin_api()
        tagger = getattr(api, "tagger", None) if api is not None else None
        window = getattr(tagger, "window", None)
        if window is not None:
            return window
    except Exception:
        pass
    # Fallback: locate the application's main window among top-level widgets.
    try:
        app = QtWidgets.QApplication.instance()
        if app is not None:
            for widget in app.topLevelWidgets():
                if type(widget).__name__ == "MainWindow":
                    return widget
    except Exception:
        pass
    return None


def _set_status_message(text: str) -> None:
    """Shows a transient message in Picard's status bar, if accessible."""
    window = _get_main_window()
    if window is None:
        return
    helper = getattr(window, "set_statusbar_message", None)
    if callable(helper):
        try:
            helper(text)
            return
        except Exception:
            pass
    status_bar = getattr(window, "statusBar", None)
    if callable(status_bar):
        try:
            status_bar().showMessage(text)
            return
        except Exception:
            pass
    status_bar = getattr(window, "statusbar", None)
    if status_bar is not None and hasattr(status_bar, "showMessage"):
        try:
            status_bar.showMessage(text)
        except Exception:
            pass


def _clear_status_message() -> None:
    """Clears the transient status-bar message shown by this plugin."""
    window = _get_main_window()
    if window is None:
        return
    helper = getattr(window, "clear_statusbar_message", None)
    if callable(helper):
        try:
            helper()
            return
        except Exception:
            pass
    status_bar = getattr(window, "statusBar", None)
    if callable(status_bar):
        try:
            status_bar().clearMessage()
            return
        except Exception:
            pass
    status_bar = getattr(window, "statusbar", None)
    if status_bar is not None and hasattr(status_bar, "clearMessage"):
        try:
            status_bar.clearMessage()
        except Exception:
            pass


class MainThreadDispatcher(QtCore.QObject):
    """Delivers worker results to the GUI thread and mirrors queue progress.

    Instead of marshalling results through cross-thread Qt signals, worker
    threads push plain callables onto a thread-safe queue which a ``QTimer``
    drains on the GUI thread.  This is robust regardless of how the worker
    threads are created, and the same timer keeps Picard's status bar updated
    with the number of tracks still waiting in the lyrics queue.
    """

    def __init__(self, poll_ms: int = 100):
        super().__init__()
        self._callbacks: "queue.Queue" = queue.Queue()
        self._post_lock = threading.Lock()
        self._stopped = False
        self._last_status: str | None = None
        self._timer = None
        try:
            self._timer = QtCore.QTimer(self)
            self._timer.setInterval(max(20, int(poll_ms)))
            self._timer.timeout.connect(self.poll)
            self._timer.start()
        except Exception as e:
            log.warning(f"{PLUGIN_NAME}: progress timer unavailable: {e}")

    def post(self, fn) -> None:
        """Queues a callable to run on the GUI thread (thread-safe)."""
        with self._post_lock:
            if not self._stopped:
                self._callbacks.put(fn)

    def stop(self) -> None:
        with self._post_lock:
            self._stopped = True
            while True:
                try:
                    self._callbacks.get_nowait()
                except queue.Empty:
                    break
        try:
            if self._timer is not None:
                self._timer.stop()
        except Exception:
            pass
        try:
            if self._last_status is not None:
                _clear_status_message()
        except Exception:
            pass
        self._last_status = None

    def poll(self) -> None:
        """Runs pending callbacks, then refreshes the queue status message."""
        while True:
            try:
                fn = self._callbacks.get_nowait()
            except queue.Empty:
                break
            try:
                fn()
            except Exception as e:
                log.error(f"{PLUGIN_NAME}: error applying queued result: {e}", exc_info=True)
        self._update_status()

    def _update_status(self) -> None:
        try:
            outstanding = FETCH_QUEUE.pending + FETCH_QUEUE.active if FETCH_QUEUE is not None else 0
        except Exception:
            outstanding = 0
        text = f"{PLUGIN_NAME}: {outstanding} track(s) in lyrics queue..." if outstanding > 0 else None
        if text == self._last_status:
            return
        self._last_status = text
        if text is None:
            _clear_status_message()
        else:
            _set_status_message(text)


class _FetchTask:
    """A unit of background work: a producer (``job_fn``) and a main-thread consumer."""

    __slots__ = ("job_fn", "apply_fn", "key")

    def __init__(self, job_fn, apply_fn, key):
        self.job_fn = job_fn
        self.apply_fn = apply_fn
        self.key = key


class FetchQueue:
    """A bounded worker pool that moves lyrics fetching off the UI thread.

    Stability & anti-abuse features:
      * a configurable, bounded worker pool that can be resized while running,
      * deduplication of identical in-flight jobs (``key``),
      * a non-blocking stop that discards queued work,
      * results delivered back to the GUI thread via :class:`MainThreadDispatcher`.

    Every ``start`` opens a new generation with its own queue.  A worker still
    finishing a request from an earlier generation can therefore never take
    new work, release a new job's dedup key, or post a stale result.
    """

    _IDLE_POLL = 0.5  # seconds between an idle worker's retirement checks

    def __init__(self, max_workers: int = 3):
        self._max_workers = max(1, int(max_workers))
        self._lock = threading.Lock()
        self._queue: "queue.Queue" = queue.Queue()
        self._workers: dict[int, threading.Thread] = {}
        self._pending_keys: set = set()
        self._running = False
        self._generation = 0
        self._invoker = None
        self._active = 0

    def configure(self, max_workers: int | None = None) -> None:
        """Sets the pool size; a running pool grows now and shrinks as workers go idle."""
        if max_workers is None:
            return
        with self._lock:
            self._max_workers = max(1, int(max_workers))
            if self._running:
                self._spawn_missing_workers()

    def set_invoker(self, invoker) -> None:
        self._invoker = invoker

    @property
    def is_running(self) -> bool:
        with self._lock:
            return self._running

    @property
    def pending(self) -> int:
        """Approximate number of queued (not yet started) jobs."""
        return self._queue.qsize()

    @property
    def active(self) -> int:
        """Number of jobs currently being processed by a worker."""
        with self._lock:
            return self._active

    @property
    def outstanding(self) -> int:
        """Total jobs waiting or in progress (used for the progress indicator)."""
        return self.pending + self.active

    def start(self) -> None:
        with self._lock:
            if self._running:
                return
            self._running = True
            self._generation += 1
            self._queue = queue.Queue()
            self._workers = {}
            self._spawn_missing_workers()
        log.debug(f"{PLUGIN_NAME}: fetch queue started ({self._max_workers} worker(s))")

    def _spawn_missing_workers(self) -> None:
        # Caller holds ``self._lock``.
        for slot in range(self._max_workers):
            worker = self._workers.get(slot)
            if worker is not None and worker.is_alive():
                continue
            worker = threading.Thread(
                target=self._worker_loop,
                args=(self._generation, slot, self._queue),
                name=f"BetterLyricsFetch-{self._generation}-{slot}",
                daemon=True,
            )
            self._workers[slot] = worker
            worker.start()

    def stop(self, timeout: float = 1.0) -> None:
        """Discards queued jobs and lets workers exit without blocking Picard.

        Requests already in flight are cancelled by closing the HTTP client;
        workers are joined only briefly, so disabling the plugin never hangs.
        """
        with self._lock:
            if not self._running:
                return
            self._running = False
            self._generation += 1
            old_queue = self._queue
            self._queue = queue.Queue()
            workers = list(self._workers.values())
            self._workers = {}
            self._pending_keys.clear()
            self._active = 0
        discarded = 0
        while True:
            try:
                old_queue.get_nowait()
            except queue.Empty:
                break
            discarded += 1
        deadline = time.monotonic() + max(0.0, timeout)
        for worker in workers:
            worker.join(timeout=max(0.0, deadline - time.monotonic()))
        log.debug(f"{PLUGIN_NAME}: fetch queue stopped ({discarded} queued job(s) discarded)")

    def submit(self, job_fn, apply_fn=None, key=None) -> bool:
        """Enqueues a job. Returns ``False`` if an identical job is already pending."""
        with self._lock:
            if key is not None:
                if key in self._pending_keys:
                    return False
                self._pending_keys.add(key)
            self._queue.put(_FetchTask(job_fn, apply_fn, key))
        return True

    def _is_current(self, generation: int) -> bool:
        return self._running and generation == self._generation

    def _worker_loop(self, generation: int, slot: int, work_queue: "queue.Queue") -> None:
        while True:
            with self._lock:
                if not self._is_current(generation) or slot >= self._max_workers:
                    return
            try:
                task = work_queue.get(timeout=self._IDLE_POLL)
            except queue.Empty:
                continue
            with self._lock:
                if not self._is_current(generation):
                    return
                self._active += 1
            try:
                try:
                    result = task.job_fn()
                except Exception as e:
                    log.warning(f"{PLUGIN_NAME}: background fetch job failed: {type(e).__name__}: {e}")
                    result = None
                with self._lock:
                    current = self._is_current(generation)
                if task.apply_fn is not None and current:
                    invoker = self._invoker
                    if invoker is not None:
                        invoker.post(partial(task.apply_fn, result))
                    else:
                        log.debug(f"{PLUGIN_NAME}: no main-thread invoker; result dropped")
            finally:
                with self._lock:
                    if generation == self._generation:
                        self._active = max(0, self._active - 1)
                        if task.key is not None:
                            self._pending_keys.discard(task.key)


# ---------------------------------------------------------------------------
# Lyrics Backend Providers
# ---------------------------------------------------------------------------
def search_lrcred(
    title: str = "",
    artist: str = "",
    album: str = "",
    isrc: str = "",
    query: str = "",
) -> list[dict]:
    """Uses the documented ISRC resource and the website's text-search API."""
    candidates = []
    seen_ids = set()

    def valid_isrc(value):
        normalized = _normalize_isrc(value)
        return normalized if ISRC_PATTERN.fullmatch(normalized) else ""

    def safe_duration(value):
        if isinstance(value, bool):
            return 0
        try:
            seconds = float(value)
        except (TypeError, ValueError, OverflowError):
            return 0
        if seconds != seconds or seconds in (float("inf"), float("-inf")) or seconds < 0:
            return 0
        return seconds

    def text_field(res, key):
        value = res.get(key, "")
        return value if isinstance(value, str) else ""

    def map_result(res):
        if not isinstance(res, dict):
            return None
        normalized_isrc = valid_isrc(res.get("isrc"))
        if not normalized_isrc:
            return None

        files = res.get("files")
        files = files if isinstance(files, dict) else {}
        ttml_url = files.get("ttml") if isinstance(files.get("ttml"), str) else ""
        lrc_url = files.get("lrc") if isinstance(files.get("lrc"), str) else ""
        if not ttml_url:
            ttml_url = f"{LRC_RED_BASE_URL}/s/{normalized_isrc}.ttml"
        if not lrc_url:
            lrc_url = f"{LRC_RED_BASE_URL}/s/{normalized_isrc}.lrc"

        # Search hits omit sync information; absence does not mean plain text.
        timing_type = res.get("sync") or res.get("timing_type") or "unknown"
        timing_type = timing_type.lower() if isinstance(timing_type, str) else "unknown"

        return {
            "id": normalized_isrc,
            "title": text_field(res, "title"),
            "artist": text_field(res, "artist"),
            "album": text_field(res, "album"),
            "duration": safe_duration(res.get("duration", 0)),
            "isrc": normalized_isrc,
            "timing_type": timing_type,
            "lyrics_url": ttml_url,
            "ttml_url": ttml_url,
            "lrc_url": lrc_url,
            "files": {"ttml": ttml_url, "lrc": lrc_url},
            "source": "LRC.red",
            "format": "ttml",
            "provider_id": "lrcred",
            "metadata_verified": True,
            "raw": res,
        }

    def add_res(res):
        candidate = map_result(res)
        if candidate is None or candidate["isrc"] in seen_ids:
            return
        seen_ids.add(candidate["isrc"])
        candidates.append(candidate)

    # An explicit search is authoritative; it must not return a stale track ISRC.
    search_q = query.strip() if isinstance(query, str) else ""

    # 1. Resolve each validated ISRC through LRC.red's single-object resource.
    # Picard joins multi-valued tags with "; ", so a recording may carry several.
    if not search_q:
        for normalized_isrc in sorted(normalize_isrcs(isrc)):
            data = fetch_json(f"{LRC_RED_BASE_URL}/s/{normalized_isrc}.json")
            if isinstance(data, dict) and valid_isrc(data.get("isrc")) == normalized_isrc:
                add_res(data)
        if candidates:
            return candidates

    # 2. Text search, with bounded cursor pagination and defensive row handling.
    search_q = search_q or f"{artist} {title}".strip()
    next_cursor = None
    seen_cursors = set()
    for _ in range(3):
        if not search_q:
            break
        params = {"q": search_q}
        if next_cursor:
            params["after"] = next_cursor
        data = fetch_json(LRC_RED_SEARCH_URL, params)
        if not isinstance(data, dict):
            break
        hits = data.get("hits")
        if not isinstance(hits, list):
            break
        for res in hits:
            add_res(res)

        cursor = data.get("next")
        if not isinstance(cursor, str) or not cursor or cursor in seen_cursors:
            break
        seen_cursors.add(cursor)
        next_cursor = cursor

    return candidates


def search_unison(
    title: str = "",
    artist: str = "",
    album: str = "",
    duration: int = 0,
    query: str = "",
) -> list[dict]:
    """Searches the Unison crowdsourced database (Better Lyrics community backend)."""
    candidates = []
    seen_ids = set()

    search_q = query or f"{artist} {title}".strip() or title
    if not search_q:
        return candidates
    data = fetch_json(UNISON_SEARCH_URL, {"q": search_q})
    if not isinstance(data, dict) or not data.get("success"):
        return candidates
    rows = data.get("data")
    for res in rows if isinstance(rows, list) else []:
        if not isinstance(res, dict):
            continue
        c_id = res.get("id")
        if c_id is None or isinstance(c_id, (dict, list)) or c_id in seen_ids:
            continue
        seen_ids.add(c_id)
        timing = res.get("syncType")
        direct = res.get("lyrics")
        fmt = res.get("format")
        candidates.append({
            "id": c_id,
            "title": res.get("song") if isinstance(res.get("song"), str) else "",
            "artist": res.get("artist") if isinstance(res.get("artist"), str) else "",
            "album": res.get("album") if isinstance(res.get("album"), str) else "",
            "duration": res.get("duration") if isinstance(res.get("duration"), (int, float)) else 0,
            "isrc": res.get("isrc") if isinstance(res.get("isrc"), str) else "",
            "timing_type": timing.lower() if isinstance(timing, str) and timing else "unknown",
            "format": fmt if isinstance(fmt, str) else "ttml",
            "source": "Unison",
            "provider_id": "unison",
            "metadata_verified": True,
            "direct_lyrics": direct if isinstance(direct, str) else None,
            "raw": res,
        })

    return candidates


def _provider_options() -> dict:
    """Snapshot the provider settings on the GUI thread for use by a worker."""
    return {
        "lyricsplus": bool(config["betterlyrics_enable_lyricsplus"]),
        "boidu": bool(config["betterlyrics_enable_boidu"]),
        "lrclib": bool(config["betterlyrics_enable_lrclib"]),
        "api_key": str(config["betterlyrics_api_key"] or "").strip(),
    }


def _timing_rank(candidate: dict) -> int:
    timing = candidate.get("timing_type")
    if not isinstance(timing, str):
        return _PLAIN_TIMING_RANK
    return _TIMING_RANK.get(timing.lower(), _PLAIN_TIMING_RANK)


def _search_one_provider(provider_id: str, search, *args) -> list[dict]:
    """Run one provider search; a failing provider never aborts the others."""
    try:
        found = search(*args)
    except Exception as e:
        log.warning(f"{PLUGIN_NAME}: provider {provider_id} failed: {type(e).__name__}")
        return []
    found = [item for item in found or [] if isinstance(item, dict)]
    log.info(f"{PLUGIN_NAME}: provider {provider_id} returned {len(found)} candidate(s)")
    return found


def query_all_sources(
    title: str,
    artist: str,
    album: str = "",
    duration: int = 0,
    isrc: str = "",
    query: str = "",
    source_preference: str = "all",
    duration_tolerance: int | None = None,
    prefer_synced: bool | None = None,
    provider_options: dict | None = None,
) -> list[dict]:
    """Queries configured providers and ranks candidates by recording identity.

    Eligible candidates come first, then higher identity scores; synchronized
    lyrics only break ties.  Workers must pass every optional argument, since
    the defaults read Picard's config, which belongs to the GUI thread.
    """
    results = []

    if duration_tolerance is None:
        duration_tolerance = _config_int(get_plugin_api(), "betterlyrics_duration_tolerance", 5)
    if prefer_synced is None:
        prefer_synced = bool(config["betterlyrics_prefer_synced"])
    if provider_options is None:
        provider_options = _provider_options()

    pref = source_preference or "all"
    if pref not in ("all",) + PROVIDER_IDS:
        pref = "all"

    if pref in ("all", "lrcred"):
        results.extend(_search_one_provider("lrcred", search_lrcred, title, artist, album, isrc, query))

    if pref in ("all", "unison"):
        results.extend(_search_one_provider("unison", search_unison, title, artist, album, duration, query))

    target = {
        "title": title,
        "artist": artist,
        "album": album,
        "duration": duration,
        "isrc": isrc,
        "query": query,
    }
    requested = list(OPTIONAL_PROVIDER_IDS) if pref == "all" else ([pref] if pref in OPTIONAL_PROVIDER_IDS else [])
    for provider_id in requested:
        if not provider_options.get(provider_id) and pref == "all":
            log.info(f"{PLUGIN_NAME}: provider {provider_id} skipped: disabled")
            continue
        if _PROVIDER_HTTP is None:
            log.info(f"{PLUGIN_NAME}: provider {provider_id} skipped: client unavailable")
            continue
        try:
            result = search_provider(
                provider_id,
                target,
                _provider_request_json,
                api_key=provider_options.get("api_key", "") if provider_id == "boidu" else "",
            )
        except Exception as e:
            log.warning(f"{PLUGIN_NAME}: provider {provider_id} failed: {type(e).__name__}")
            continue
        status = result.get("status")
        found = result.get("candidates", []) if status == "hit" else []
        results.extend(found)
        # Provider messages may contain response bodies or credential-bearing
        # URLs. Log bounded status codes and counts instead.
        if status not in ("hit", "not_found", "auth_required", "rate_limited",
                          "transport_error", "invalid_response", "cancelled"):
            status = "failed"
        log.info(f"{PLUGIN_NAME}: provider {provider_id}: {status}; {len(found)} candidate(s)")

    ranked = rank_candidates(
        {"title": title, "artist": artist, "album": album, "duration": duration, "isrc": isrc},
        results,
        duration_tolerance,
    )
    ranked.sort(
        key=lambda x: (
            bool(x.get("match_eligible")),
            x.get("score", 0.0),
            1 if prefer_synced and _timing_rank(x) < _PLAIN_TIMING_RANK else 0,
        ),
        reverse=True,
    )
    return ranked


def _lrcred_lyrics_urls(candidate: dict) -> list[str]:
    """Collect LRC.red download URLs, TTML first, without duplicates."""
    urls = []

    def add_url(url):
        if not isinstance(url, str):
            return
        url = url.strip()
        if url and url not in urls:
            urls.append(url)

    # Prefer provider-supplied URLs independently; they need not be derived
    # from one another (for example, a custom TTML URL and a custom LRC URL).
    raw_candidate = candidate.get("raw")
    raw_files = raw_candidate.get("files") if isinstance(raw_candidate, dict) else None
    for files in (candidate.get("files"), raw_files):
        if isinstance(files, dict):
            add_url(files.get("ttml"))
            add_url(files.get("lrc"))
    add_url(candidate.get("ttml_url"))
    add_url(candidate.get("lrc_url"))
    add_url(candidate.get("lyrics_url"))

    isrc_value = _normalize_isrc(candidate.get("isrc"))
    if not ISRC_PATTERN.fullmatch(isrc_value):
        isrc_value = _normalize_isrc(candidate.get("id"))
    if ISRC_PATTERN.fullmatch(isrc_value):
        add_url(f"{LRC_RED_BASE_URL}/s/{isrc_value}.ttml")
        add_url(f"{LRC_RED_BASE_URL}/s/{isrc_value}.lrc")
    return urls


def resolve_lyrics_bundle(candidate: dict, clean_words: bool | None = None) -> dict:
    """Downloads and prepares both TTML and LRC representations for a chosen candidate.

    ``clean_words`` lets background workers pass a config snapshot instead of
    reading plugin config from a non-GUI thread.
    """
    if clean_words is None:
        clean_words = bool(config["betterlyrics_clean_word_timestamps"])
    source = candidate.get("source", "")
    provider_id = candidate.get("provider_id") or candidate.get("source", "")
    empty = {"ttml": None, "lrc": None, "is_synced": False, "source": source}

    if provider_id == "lyricsplus" and isinstance(candidate.get("payload"), dict):
        bundle = make_bundle(parse_kpoe(candidate["payload"]), clean_words=clean_words)
        bundle["source"] = source or "LyricsPlus"
        bundle["provider_id"] = provider_id
        return bundle

    document = None
    direct = candidate.get("direct_lyrics")
    if isinstance(direct, str) and direct.strip():
        document = _parse_lyrics_text(direct)

    if (document is None or not document.get("lines")) and source == "Unison" and candidate.get("id") is not None:
        item_data = fetch_json(f"{UNISON_API_URL}/{candidate['id']}")
        data = item_data.get("data") if isinstance(item_data, dict) and item_data.get("success") else None
        lyrics = data.get("lyrics") if isinstance(data, dict) else None
        if isinstance(lyrics, str) and lyrics.strip():
            document = _parse_lyrics_text(lyrics)

    elif (document is None or not document.get("lines")) and source == "LRC.red":
        # A URL can answer with an empty or unparseable body; keep trying the
        # remaining representations instead of giving up on the candidate.
        for lyrics_url in _lrcred_lyrics_urls(candidate):
            raw_content = fetch_text(lyrics_url)
            if not raw_content:
                continue
            document = _parse_lyrics_text(raw_content)
            if document.get("lines"):
                break

    if document is None or not document.get("lines"):
        return empty

    bundle = make_bundle(document, clean_words=clean_words)
    bundle["source"] = source
    bundle["provider_id"] = provider_id
    return bundle


# ---------------------------------------------------------------------------
# File & Metadata Saving Helpers
# ---------------------------------------------------------------------------
def confirm_replace(parent, title: str, description: str) -> bool:
    """Prompts the user with a confirmation dialog."""
    try:
        parent_widget = QtWidgets.QApplication.activeWindow() if parent is None else parent
        reply = QtWidgets.QMessageBox.question(
            parent_widget,
            title,
            description,
            QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No,
            QtWidgets.QMessageBox.StandardButton.No,
        )
        return reply == QtWidgets.QMessageBox.StandardButton.Yes
    except Exception:
        return False


def _enabled_lyric_tags():
    return [
        tag for tag, option in (
            ("lyrics", "betterlyrics_embed_lyrics"),
            ("ttml", "betterlyrics_embed_ttml_tag"),
            ("syncedlyrics", "betterlyrics_embed_syncedlyrics"),
        ) if config[option]
    ]


def _enabled_sidecars(file):
    filename = getattr(file, "filename", None)
    if not filename:
        return []
    stem = os.path.splitext(filename)[0]
    return [
        (extension, stem + "." + extension)
        for extension, option in (
            ("ttml", "betterlyrics_save_ttml_file"),
            ("lrc", "betterlyrics_save_lrc_file"),
            ("txt", "betterlyrics_plain_as_txt"),
        )
        if config[option]
    ]


def _has_writable_lyrics_destination(files, track):
    """Avoid a lookup when all enabled destinations are already protected."""
    overwrite = bool(config["betterlyrics_auto_overwrite"])
    tags = _enabled_lyric_tags()
    for target in files or ([track] if track is not None else []):
        metadata = getattr(target, "metadata", {})
        if any(overwrite or not metadata.get(tag) for tag in tags):
            return True
    return any(
        overwrite or not os.path.exists(path)
        for file in files or [] for _, path in _enabled_sidecars(file)
    )


def _bundle_destination_values(bundle: dict) -> tuple[dict, dict]:
    """Return the tag values and sidecar contents a bundle can fill.

    Unsynchronized lyrics are written as plain text: a TTML document or an
    ``.lrc`` file without timestamps would only mislead players.
    """
    ttml_content = bundle.get("ttml")
    lrc_content = bundle.get("lrc")
    if bundle.get("quality") == "plain":
        plain = bundle.get("plain") or lrc_content
        tags = {"lyrics": plain, "ttml": None, "syncedlyrics": None}
        return tags, {"txt": plain}

    if config["betterlyrics_format"] in ("ttml", "both") and ttml_content:
        primary = ttml_content
    else:
        primary = lrc_content or ttml_content
    tags = {"lyrics": primary, "ttml": ttml_content, "syncedlyrics": lrc_content or ttml_content}
    return tags, {"ttml": ttml_content, "lrc": lrc_content}


def _write_sidecar(path: str, content: str, overwrite: bool) -> None:
    """Write a sidecar so readers never observe a truncated file.

    Raises ``FileExistsError`` when the file exists and may not be replaced,
    which also covers a sidecar created while the lookup was in flight.
    """
    if not overwrite or not os.path.exists(path):
        with open(path, "x", encoding="utf-8") as handle:
            handle.write(content)
        return
    directory = os.path.dirname(path) or "."
    descriptor, temporary = tempfile.mkstemp(prefix=".betterlyrics-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
        # mkstemp creates the file private to the user; keep the replaced
        # sidecar's permissions so media servers can still read it.
        shutil.copymode(path, temporary)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def save_lyrics_to_files(
    file_list: list[File],
    bundle: dict,
    track: Track | None = None,
    interactive: bool = False,
    changed_files: list | None = None,
) -> bool:
    """Fill enabled destinations, protecting each existing destination separately.

    Return whether anything changed; optionally collect only files whose
    embedded metadata changed, so sidecar writes never trigger audio saves.
    """
    if not bundle:
        return False

    values, sidecar_contents = _bundle_destination_values(bundle)
    primary_tag_content = values["lyrics"]
    if not primary_tag_content:
        return False

    tags = {tag: values[tag] for tag in _enabled_lyric_tags() if values[tag]}
    counts = {"tags": 0, "sidecars": 0, "protected": 0, "unchanged": 0}

    def same_value(metadata, tag, value):
        getall = getattr(metadata, "getall", None)
        return getall(tag) == [value] if callable(getall) else metadata.get(tag) == value

    def apply_target(target, sidecars=()):
        metadata = target.metadata
        overwrite = bool(config["betterlyrics_auto_overwrite"])
        sidecars = [(ext, path) for ext, path in sidecars if sidecar_contents.get(ext)]
        existing = any(metadata.get(tag) and not same_value(metadata, tag, value)
                       for tag, value in tags.items())
        existing = existing or any(os.path.exists(path) for _, path in sidecars)
        if interactive and existing and not overwrite:
            window = getattr(getattr(target, "tagger", None), "window", None)
            overwrite = confirm_replace(
                window, "Overwrite existing lyrics?",
                f"Lyrics already exist for \"{metadata.get('title', '')}\".\n\n"
                f"Preview of new lyrics:\n{truncate_text(primary_tag_content, 5, 45)}\n\n"
                "Do you want to overwrite? Missing destinations will still be filled.",
            )
        changed = False
        for tag, value in tags.items():
            if same_value(metadata, tag, value):
                counts["unchanged"] += 1
            elif metadata.get(tag) and not overwrite:
                counts["protected"] += 1
            else:
                metadata[tag] = value
                counts["tags"] += 1
                changed = True
        if changed and hasattr(target, "update"):
            target.update()
        for extension, path in sidecars:
            content = sidecar_contents[extension]
            exists = os.path.exists(path)
            if exists and not overwrite:
                counts["protected"] += 1
                continue
            try:
                if exists:
                    with open(path, "rb") as handle:
                        if handle.read() == content.encode("utf-8"):
                            counts["unchanged"] += 1
                            continue
                _write_sidecar(path, content, overwrite)
                counts["sidecars"] += 1
                _record_written_sidecar(target, extension, path)
                log.info(f"{PLUGIN_NAME}: Saved {extension.upper()} file to {path}")
            except FileExistsError:
                counts["protected"] += 1
            except Exception as err:
                log.error(f"{PLUGIN_NAME}: Failed to write {extension.upper()} file {path}: {err}")
        return changed

    embedded = False
    for file in file_list:
        if apply_target(file, _enabled_sidecars(file)):
            embedded = True
            if changed_files is not None:
                changed_files.append(file)
    if track is not None and hasattr(track, "metadata") and (embedded or not file_list):
        apply_target(track)

    changed = bool(counts["tags"] or counts["sidecars"])
    log.info(
        f"{PLUGIN_NAME}: Lyrics apply {'staged' if changed else 'no-op'}: "
        f"{counts['tags']} tag(s) changed, {counts['sidecars']} sidecar(s) written, "
        f"{counts['protected']} destination(s) protected, {counts['unchanged']} unchanged"
    )
    return changed


def _record_written_sidecar(file, extension: str, path: str) -> None:
    try:
        _WRITTEN_SIDECARS.setdefault(file, {})[extension] = path
    except TypeError:
        pass  # Objects that cannot be weakly referenced are simply not tracked.


def _move_sidecars_with_file(file) -> None:
    """Move sidecars this plugin wrote when Picard saved the audio elsewhere.

    Sidecars are written next to the file as it was when the lyrics arrived.
    If Picard's rename/move options relocate the audio on save, the sidecars
    follow it instead of being left behind as orphans.  Anything that already
    exists at the destination is left untouched.
    """
    try:
        written = _WRITTEN_SIDECARS.pop(file, None)
    except TypeError:
        return
    filename = getattr(file, "filename", None)
    if not written or not filename:
        return
    stem = os.path.splitext(filename)[0]
    for extension, old_path in written.items():
        new_path = f"{stem}.{extension}"
        if os.path.normcase(os.path.abspath(old_path)) == os.path.normcase(os.path.abspath(new_path)):
            continue
        if not os.path.exists(old_path):
            continue  # Already moved, e.g. by Picard's "move additional files".
        if os.path.exists(new_path):
            log.info(f"{PLUGIN_NAME}: Kept {old_path}; {new_path} already exists")
            continue
        try:
            os.makedirs(os.path.dirname(new_path) or ".", exist_ok=True)
            shutil.move(old_path, new_path)
            log.info(f"{PLUGIN_NAME}: Moved {extension.upper()} sidecar to {new_path}")
        except OSError as err:
            log.error(f"{PLUGIN_NAME}: Failed to move {extension.upper()} sidecar {old_path}: {err}")


# ---------------------------------------------------------------------------
# Interactive Search Table Dialog
# ---------------------------------------------------------------------------
def run_with_busy_dialog(parent, label: str, fn):
    """Run blocking network work on a thread while Picard's UI stays responsive.

    A nested event loop waits for the result behind a window-modal progress
    dialog.  Returns ``fn()``'s result, or ``None`` when the user cancels; a
    cancelled worker finishes in the background and its result is discarded.
    Exceptions raised by ``fn`` propagate to the caller.
    """
    outcome: dict = {}
    done = threading.Event()

    def work():
        try:
            outcome["value"] = fn()
        except BaseException as error:
            outcome["error"] = error
        finally:
            done.set()

    threading.Thread(target=work, name="BetterLyricsSearch", daemon=True).start()
    # Cached lookups finish almost at once; skip the dialog for them.
    if not done.wait(0.15):
        loop = QtCore.QEventLoop()
        progress = QtWidgets.QProgressDialog(label, "Cancel", 0, 0, parent)
        progress.setWindowTitle(PLUGIN_NAME)
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.canceled.connect(loop.quit)
        # A busy indicator never receives setValue(), which is what would
        # otherwise trigger QProgressDialog's delayed automatic show.
        progress.show()
        timer = QtCore.QTimer()
        timer.setInterval(30)
        timer.timeout.connect(lambda: loop.quit() if done.is_set() or progress.wasCanceled() else None)
        timer.start()
        try:
            if not done.is_set():
                loop.exec()
        finally:
            timer.stop()
            progress.close()
            progress.deleteLater()
        if not done.is_set():
            log.info(f"{PLUGIN_NAME}: {label.rstrip('…')} cancelled")
            return None
    if "error" in outcome:
        raise outcome["error"]
    return outcome.get("value")


def show_search_dialog(
    parent,
    title: str,
    artist: str,
    album: str,
    duration: int,
    isrc: str,
    initial_results: list[dict],
) -> dict | None:
    """Renders a PyQt6 dialog with live search, result table, and TTML/LRC preview."""
    parent = QtWidgets.QApplication.activeWindow() if parent is None else parent
    dialog = QtWidgets.QDialog(parent)
    dialog.setWindowTitle("Search Lyrics (TTML / LRC) — Better Lyrics")
    dialog.resize(840, 500)

    layout = QtWidgets.QVBoxLayout(dialog)

    # Search bar layout
    search_layout = QtWidgets.QHBoxLayout()
    search_input = QtWidgets.QLineEdit()
    default_q = f"{artist} {title}".strip() or title
    search_input.setText(default_q)
    search_input.setPlaceholderText("Enter song title and artist...")
    search_button = QtWidgets.QPushButton("Search")
    search_button.setDefault(True)

    search_layout.addWidget(search_input)
    search_layout.addWidget(search_button)
    layout.addLayout(search_layout)

    # Results Table
    table = QtWidgets.QTableWidget(dialog)
    table.setColumnCount(7)
    table.setHorizontalHeaderLabels([
        "#", "Title", "Artist", "Album", "Length", "Format / Sync", "Source"
    ])

    vheader = table.verticalHeader()
    if vheader is not None:
        vheader.setVisible(False)

    hheader = table.horizontalHeader()
    if hheader is not None:
        hheader.setDefaultAlignment(ALIGN_CENTER)
        hheader.setSectionResizeMode(0, RESIZE_TO_CONTENTS)
        hheader.setSectionResizeMode(1, RESIZE_STRETCH)
        hheader.setSectionResizeMode(2, RESIZE_INTERACTIVE)
        hheader.setSectionResizeMode(3, RESIZE_INTERACTIVE)
        hheader.setSectionResizeMode(4, RESIZE_TO_CONTENTS)
        hheader.setSectionResizeMode(5, RESIZE_TO_CONTENTS)
        hheader.setSectionResizeMode(6, RESIZE_TO_CONTENTS)

    table.setEditTriggers(NO_EDIT_TRIGGERS)
    table.setSelectionBehavior(SELECT_ROWS)
    table.setSelectionMode(SINGLE_SELECTION)
    layout.addWidget(table)

    # Bottom layout: Preview button + OK / Cancel
    bottom_layout = QtWidgets.QHBoxLayout()
    preview_button = QtWidgets.QPushButton("Preview Selected (TTML/LRC)")
    preview_button.setEnabled(False)
    bottom_layout.addWidget(preview_button)

    button_box = QtWidgets.QDialogButtonBox(
        QtWidgets.QDialogButtonBox.StandardButton.Ok | QtWidgets.QDialogButtonBox.StandardButton.Cancel
    )
    bottom_layout.addWidget(button_box)
    layout.addLayout(bottom_layout)

    current_results = list(initial_results)

    def candidate_for_row(row: int) -> dict | None:
        """Returns the candidate attached to a table row after sorting."""
        if row < 0:
            return None
        item = table.item(row, 0)
        if item is None:
            return None
        # The row stores an index: PyQt would convert a dict into a lossy copy.
        index = item.data(Qt.ItemDataRole.UserRole)
        if not isinstance(index, int) or not 0 <= index < len(current_results):
            return None
        return current_results[index]

    def populate_table(items: list[dict]):
        table.setSortingEnabled(False)
        table.setRowCount(0)
        table.setRowCount(len(items))

        for row, item in enumerate(items):
            idx_item = QtWidgets.QTableWidgetItem()
            idx_item.setTextAlignment(ALIGN_CENTER)
            idx_item.setData(Qt.ItemDataRole.EditRole, row + 1)
            idx_item.setData(Qt.ItemDataRole.UserRole, row)
            table.setItem(row, 0, idx_item)

            rank = _timing_rank(item)
            is_synced = rank < _TIMING_RANK["unknown"]
            sync_label = ("Word-synced", "Line-synced", "Unknown (preview to check)", "Plain")[rank]

            values = [
                item.get("title") or "?",
                item.get("artist") or "?",
                item.get("album") or "-",
                format_duration(item.get("duration", 0)),
                sync_label,
                item.get("display") or item.get("source") or "Better Lyrics",
            ]

            for col, val in enumerate(values, start=1):
                cell = QtWidgets.QTableWidgetItem(str(val))
                if col in (4, 5, 6):
                    cell.setTextAlignment(ALIGN_CENTER)
                if col == 5:
                    color = QtGui.QColor("#27ae60" if is_synced else "#d35400")
                    cell.setForeground(color)
                table.setItem(row, col, cell)

        table.setSortingEnabled(True)
        if items:
            table.selectRow(0)
            preview_button.setEnabled(True)
        else:
            preview_button.setEnabled(False)

    populate_table(current_results)

    def on_search():
        nonlocal current_results
        query_text = search_input.text().strip()
        if not query_text:
            return
        search = partial(
            query_all_sources,
            title=title,
            artist=artist,
            album=album,
            duration=duration,
            isrc=isrc,
            query=query_text,
            source_preference=config["betterlyrics_source"],
            duration_tolerance=_config_int(get_plugin_api(), "betterlyrics_duration_tolerance", 5),
            prefer_synced=bool(config["betterlyrics_prefer_synced"]),
            provider_options=_provider_options(),
        )
        found = run_with_busy_dialog(dialog, "Searching lyrics providers…", search)
        if found is None:
            return
        current_results = found
        populate_table(current_results)

    search_button.clicked.connect(on_search)
    search_input.returnPressed.connect(on_search)

    def on_selection_change():
        preview_button.setEnabled(len(table.selectedItems()) > 0)

    table.itemSelectionChanged.connect(on_selection_change)

    def on_preview():
        row = table.currentRow()
        candidate = candidate_for_row(row)
        if candidate is None:
            return
        resolve = partial(
            resolve_lyrics_bundle,
            candidate,
            clean_words=bool(config["betterlyrics_clean_word_timestamps"]),
        )
        try:
            bundle = run_with_busy_dialog(dialog, "Downloading lyrics preview…", resolve)
        except Exception as error:
            log.error(f"{PLUGIN_NAME}: lyrics preview failed: {type(error).__name__}: {error}")
            bundle = {}
        if bundle is None:
            return
        ttml_data = bundle.get("ttml")
        lrc_data = bundle.get("lrc") or bundle.get("plain")

        if not ttml_data and not lrc_data:
            QtWidgets.QMessageBox.information(
                dialog, "Preview Lyrics", "Could not load lyrics preview for this item."
            )
            return

        preview_dialog = QtWidgets.QDialog(dialog)
        preview_dialog.setWindowTitle(f"Lyrics Preview (TTML & LRC) — {candidate.get('title')}")
        preview_dialog.resize(680, 480)
        p_layout = QtWidgets.QVBoxLayout(preview_dialog)

        # Tab widget to switch between TTML and LRC preview
        tabs = QtWidgets.QTabWidget(preview_dialog)

        # TTML Tab
        ttml_edit = QtWidgets.QPlainTextEdit(preview_dialog)
        ttml_edit.setReadOnly(True)
        ttml_edit.setPlainText(ttml_data or "No TTML content")
        tabs.addTab(ttml_edit, "TTML (XML)")

        # LRC Tab
        lrc_edit = QtWidgets.QPlainTextEdit(preview_dialog)
        lrc_edit.setReadOnly(True)
        lrc_edit.setPlainText(lrc_data or "No LRC content")
        tabs.addTab(lrc_edit, "LRC (Synced)")

        p_layout.addWidget(tabs)

        p_buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Close)
        p_buttons.rejected.connect(preview_dialog.reject)
        p_layout.addWidget(p_buttons)
        preview_dialog.exec()

    preview_button.clicked.connect(on_preview)

    def on_double_click(index):
        if index.isValid():
            dialog.accept()

    table.doubleClicked.connect(on_double_click)
    button_box.accepted.connect(dialog.accept)
    button_box.rejected.connect(dialog.reject)

    result = dialog.exec()
    if result == QtWidgets.QDialog.DialogCode.Accepted:
        selected_row = table.currentRow()
        return candidate_for_row(selected_row)
    return None


# ---------------------------------------------------------------------------
# Core Lyrics Matcher & Fetcher
# ---------------------------------------------------------------------------
def _extract_track_query(metadata, track) -> tuple[str, str, str, str, int | float]:
    """Reads (title, artist, album, isrc, length) from Picard metadata/track."""
    title = str(metadata.get("title", "") or "").strip()
    artist = str(metadata.get("artist", "") or "").strip()
    album_name = str(metadata.get("album", "") or "").strip()
    isrc = str(metadata.get("isrc", "") or "").strip()

    length = _metadata_seconds(metadata)
    if not length and track:
        length = get_track_duration(track)
    return title, artist, album_name, isrc, length


def _dedup_key(track, files, method):
    """Builds a key so identical in-flight jobs are not queued twice."""
    if method == "save" and files:
        return (method, "files", tuple(sorted(id(file) for file in files)))
    if track is not None:
        return (method, "track", id(track))
    if files:
        names = tuple(sorted(str(getattr(f, "filename", "")) for f in files))
        return (method, "files", names)
    return None


def _auto_cache_key(target: dict, source_pref: str) -> str:
    # Different source settings can legitimately pick different lyrics.
    return f"{track_key(target)}|{source_pref or 'all'}"


def _manual_cache_key(target: dict) -> str:
    return f"manual|{track_key(target)}"


def _with_lrc_style(bundle: dict, clean_words: bool) -> dict:
    """Re-select the LRC flavour, since a cached bundle may predate a settings change."""
    preferred = bundle.get("line_lrc" if clean_words else "enhanced_lrc")
    return dict(bundle, lrc=preferred) if preferred else bundle


def _cached_bundle(key: str, clean_words: bool) -> dict | None:
    store = _LYRICS_STORE
    if store is None:
        return None
    try:
        cached = store.get(key)
    except Exception as e:
        log.debug(f"{PLUGIN_NAME}: lyrics cache read failed: {e}")
        return None
    return _with_lrc_style(cached, clean_words) if cached else None


def _store_bundle(key: str, bundle: dict, ttl: float | None = None) -> None:
    store = _LYRICS_STORE
    if store is None:
        return
    try:
        store.put(key, bundle, ttl=ttl)
    except Exception as e:
        log.debug(f"{PLUGIN_NAME}: lyrics cache write failed: {e}")


def _find_best_bundle(
    title: str,
    artist: str,
    album_name: str,
    length: int | float,
    isrc: str,
    source_pref: str,
    clean_words: bool,
    tolerance: int,
    prefer_synced: bool,
    provider_options: dict | None = None,
) -> dict | None:
    """Worker-thread body: query providers, pick the best match, resolve lyrics.

    Pure network work only - never touches Qt/Picard objects, so it is safe to
    run off the GUI thread.  ``provider_options`` is the GUI-thread snapshot
    from :func:`_provider_options`.
    """
    target = {
        "title": title,
        "artist": artist,
        "album": album_name,
        "duration": length,
        "isrc": isrc,
    }
    if _LYRICS_STORE is not None:
        cached = _cached_bundle(_manual_cache_key(target), clean_words)
        if cached:
            log.info(f"{PLUGIN_NAME}: Automatic lookup: using your earlier manual choice")
            return cached
        cached = _cached_bundle(_auto_cache_key(target, source_pref), clean_words)
        if cached:
            log.info(f"{PLUGIN_NAME}: Automatic lookup: cached lyrics bundle available")
            return cached

    try:
        candidates = query_all_sources(
            title=title,
            artist=artist,
            album=album_name,
            duration=length,
            isrc=isrc,
            source_preference=source_pref,
            duration_tolerance=tolerance,
            prefer_synced=prefer_synced,
            provider_options=provider_options,
        )
    except Exception as e:
        log.warning(f"{PLUGIN_NAME}: search failed for \"{title}\": {type(e).__name__}")
        return None

    if not candidates:
        log.info(f"{PLUGIN_NAME}: Automatic lookup: providers returned no candidates")
        return None

    eligible = 0
    for index, candidate in enumerate(candidates, 1):
        assessment = assess_match(target, candidate, tolerance)
        accepted = candidate.get("match_eligible", assessment["eligible"])
        reason = candidate.get("match_reason", assessment["reason"])
        eligible += bool(accepted)
        provider = candidate.get("provider_id")
        if provider not in PROVIDER_IDS:
            provider = "legacy/unknown"
        log.info(
            f"{PLUGIN_NAME}: Candidate {index} ({provider}) for \"{title}\" decision: "
            f"{'eligible' if accepted else 'rejected'}; reason={reason}"
        )
    log.info(f"{PLUGIN_NAME}: Automatic lookup: {len(candidates)} candidate(s), {eligible} eligible")
    identity_group = choose_identity_group(candidates=candidates, target=target, tolerance=tolerance)
    if not identity_group:
        log.warning(f"{PLUGIN_NAME}: ambiguous or unverified lyrics matches for \"{title}\"")
        return None
    log.info(
        f"{PLUGIN_NAME}: Selected identity group for \"{title}\": "
        f"{len(identity_group)} candidate(s); reason={identity_group[0].get('match_reason')}"
    )

    # Every group member is the same recording, so the timing quality decides
    # the order.  An empty or failing lyrics endpoint falls through to the next
    # member, and plain text is kept only as a fallback when synced is preferred.
    ordered = sorted(identity_group, key=_timing_rank) if prefer_synced else identity_group
    fallback = None
    for candidate in ordered:
        try:
            bundle = resolve_lyrics_bundle(candidate, clean_words=clean_words)
        except Exception as e:
            log.warning(
                f"{PLUGIN_NAME}: failed to resolve lyrics candidate "
                f"\"{candidate.get('title')}\" for \"{title}\": {type(e).__name__}"
            )
            continue

        if not (bundle.get("ttml") or bundle.get("lrc")):
            log.info(f"{PLUGIN_NAME}: Candidate skipped: empty lyrics bundle")
            continue
        if prefer_synced and bundle.get("quality") == "plain":
            log.info(f"{PLUGIN_NAME}: Candidate has unsynchronized lyrics; looking for synced lyrics")
            fallback = fallback or bundle
            continue
        log.info(f"{PLUGIN_NAME}: Candidate resolved: usable lyrics bundle")
        _store_bundle(_auto_cache_key(target, source_pref), bundle)
        return bundle

    if fallback is not None:
        log.info(f"{PLUGIN_NAME}: Using unsynchronized lyrics for \"{title}\"; no synced lyrics found")
        _store_bundle(_auto_cache_key(target, source_pref), fallback)
        return fallback
    log.warning(f"{PLUGIN_NAME}: no compatible lyrics candidate resolved for \"{title}\"")
    return None


def _apply_auto_bundle(method: str, title: str, files, track, bundle: dict | None) -> None:
    """Main-thread consumer: writes a resolved bundle into Picard's data model."""
    try:
        if not bundle:
            log.info(f"{PLUGIN_NAME}: No usable lyrics found for \"{title}\"; nothing staged")
            return
        changed_files = []
        changed = save_lyrics_to_files(
            files, bundle, track=track, interactive=False, changed_files=changed_files
        )
        if method == "save":
            for file in changed_files:
                save = getattr(file, "save", None)
                if not callable(save):
                    continue
                files_processing[file] = "saving"
                try:
                    if save() is False:
                        files_processing.pop(file, None)
                        log.info(f"{PLUGIN_NAME}: Audio save not submitted; lyrics remain staged")
                    else:
                        log.info(f"{PLUGIN_NAME}: Requested audio save for changed lyrics metadata")
                except Exception as err:
                    files_processing.pop(file, None)
                    log.error(f"{PLUGIN_NAME}: Could not save changed lyrics metadata: {err}")
        if changed:
            log.info(f"{PLUGIN_NAME}: Matched and staged lyrics for \"{title}\"")
        else:
            log.info(f"{PLUGIN_NAME}: Matched lyrics for \"{title}\"; no-op (protected or unchanged destinations)")
    except Exception as e:
        log.error(f"{PLUGIN_NAME}: Failed to apply lyrics for \"{title}\": {e}", exc_info=True)
    finally:
        if method == "save":
            for file in files or []:
                if files_processing.get(file) != "saving":
                    files_processing.pop(file, None)


def _manual_search(title, artist, album_name, isrc, length, track, files, interactive) -> bool:
    """Interactive flow: the modal dialogs run on the GUI thread, network work does not."""
    log.info(f"{PLUGIN_NAME}: Searching lyrics for \"{title}\" by \"{artist}\" ({format_duration(length)})")
    parent_win = QtWidgets.QApplication.activeWindow()
    clean_words = bool(config["betterlyrics_clean_word_timestamps"])
    search = partial(
        query_all_sources,
        title=title,
        artist=artist,
        album=album_name,
        duration=length,
        isrc=isrc,
        source_preference=config["betterlyrics_source"],
        duration_tolerance=_config_int(get_plugin_api(), "betterlyrics_duration_tolerance", 5),
        prefer_synced=bool(config["betterlyrics_prefer_synced"]),
        provider_options=_provider_options(),
    )
    try:
        candidates = run_with_busy_dialog(parent_win, "Searching lyrics providers…", search)
    except Exception as error:
        log.error(f"{PLUGIN_NAME}: lyrics search failed: {type(error).__name__}: {error}")
        candidates = []
    if candidates is None:
        return False

    selected_candidate = show_search_dialog(
        parent_win, title, artist, album_name, length, isrc, candidates
    )
    if not selected_candidate:
        log.info(f"{PLUGIN_NAME}: User cancelled lyrics selection")
        return False

    resolve = partial(resolve_lyrics_bundle, selected_candidate, clean_words=clean_words)
    try:
        bundle = run_with_busy_dialog(parent_win, "Downloading lyrics…", resolve)
    except Exception as error:
        log.error(f"{PLUGIN_NAME}: lyrics download failed: {type(error).__name__}: {error}")
        bundle = {}
    if bundle is None:
        return False
    if not bundle.get("ttml") and not bundle.get("lrc"):
        log.warning(f"{PLUGIN_NAME}: Empty lyrics received for \"{title}\"")
        QtWidgets.QMessageBox.information(
            parent_win, PLUGIN_NAME, f"The selected result has no usable lyrics for \"{title}\"."
        )
        return False

    # Later automatic lookups of this recording reuse the user's choice.
    target = {"title": title, "artist": artist, "album": album_name, "duration": length, "isrc": isrc}
    _store_bundle(_manual_cache_key(target), bundle, ttl=MANUAL_CHOICE_TTL)
    save_lyrics_to_files(files, bundle, track=track, interactive=interactive)
    return True


def fetch_and_apply_lyrics(
    method: str,
    track: Track | None,
    files: list[File],
    metadata: Metadata,
    interactive: bool = False,
) -> bool:
    """Main entry point for finding, scoring, and saving TTML/LRC lyrics.

    ``method == "search"`` drives modal dialogs on the GUI thread while its
    network requests run on a helper thread.  All automatic paths
    (``"get"``/``"load"``/``"save"``) are handed to the background
    :class:`FetchQueue`, keeping the UI responsive and pacing requests.
    Returns whether lyrics were applied (search) or a job was queued.
    """
    title, artist, album_name, isrc, length = _extract_track_query(metadata, track)

    if not title:
        log.warning(f"{PLUGIN_NAME}: Cannot fetch lyrics without a track title")
        return False

    if config["betterlyrics_ignore_instrumental"]:
        if "(instrumental)" in title.lower() or "[instrumental]" in title.lower():
            log.info(f"{PLUGIN_NAME}: Skipping instrumental track: {title}")
            return False

    if method == "search":
        return _manual_search(title, artist, album_name, isrc, length, track, files, interactive)

    # Automatic flow: enqueue on the background fetch queue (never blocks the UI).
    if not _has_writable_lyrics_destination(files, track):
        log.info(
            f"{PLUGIN_NAME}: Skipped lyrics lookup for \"{title}\": "
            "all enabled destinations are protected or no destinations are enabled"
        )
        return False
    _ensure_queue_ready()
    # Snapshot every setting here: workers must not read Picard's config.
    source_pref = config["betterlyrics_source"]
    clean_words = bool(config["betterlyrics_clean_word_timestamps"])
    tolerance = _config_int(get_plugin_api(), "betterlyrics_duration_tolerance", 5)
    prefer_synced = bool(config["betterlyrics_prefer_synced"])

    submitted = FETCH_QUEUE.submit(
        job_fn=partial(
            _find_best_bundle,
            title,
            artist,
            album_name,
            length,
            isrc,
            source_pref,
            clean_words,
            tolerance,
            prefer_synced,
            _provider_options(),
        ),
        apply_fn=partial(_apply_auto_bundle, method, title, files, track),
        key=_dedup_key(track, files, method),
    )
    if submitted:
        log.info(
            f"{PLUGIN_NAME}: Queued lyrics search for \"{title}\" by \"{artist}\" "
            f"({format_duration(length)}); {FETCH_QUEUE.pending} job(s) pending"
        )
    else:
        log.debug(f"{PLUGIN_NAME}: duplicate lyrics job for \"{title}\" ignored")
    return submitted


# ---------------------------------------------------------------------------
# Picard 3.0 Processor Hooks
# ---------------------------------------------------------------------------
def on_file_added(api, track, file=None) -> None:
    """Processor hook: runs automatically when a file is added to a track.

    Picard's ``PluginApi`` may invoke registered processors as either
    ``fn(api, track, file)`` or ``fn(track, file)`` depending on the API
    version, so both forms are accepted here.
    """
    if file is None:  # called as (track, file)
        api, track, file = None, api, track

    if not config.get("betterlyrics_get_on_load", True):
        return
    try:
        files = getattr(track, "files", []) or [file]
        metadata = getattr(track, "metadata", None) or getattr(file, "metadata", None)
        if metadata:
            fetch_and_apply_lyrics("load", track, files, metadata, interactive=False)
    except Exception as err:
        log.error(f"{PLUGIN_NAME}: Error in on_file_added: {err}", exc_info=True)


def on_file_saved(api, file=None) -> None:
    """Processor hook: runs automatically when an audio file is saved.

    Accepts both ``fn(api, file)`` and ``fn(file)`` invocation forms.
    """
    if file is None:  # called as (file,)
        api, file = None, api

    _move_sidecars_with_file(file)
    state = files_processing.get(file)
    if state is not None:
        if state == "saving":
            files_processing.pop(file, None)
            log.info(f"{PLUGIN_NAME}: Plugin audio save completed; suppressing recursive lookup")
        else:
            log.debug(f"{PLUGIN_NAME}: Lyrics lookup already pending for saved file")
        return
    if not config.get("betterlyrics_get_on_save", False):
        return

    try:
        files_processing[file] = "fetching"
        parent = _parent_item(file)
        track = parent if isinstance(parent, Track) else None
        metadata = getattr(track, "metadata", None) or file.metadata
        submitted = fetch_and_apply_lyrics("save", track, [file], metadata, interactive=False)
        if not submitted:
            files_processing.pop(file, None)
    except Exception as err:
        log.error(f"{PLUGIN_NAME}: Error in on_file_saved: {err}", exc_info=True)
        files_processing.pop(file, None)


# ---------------------------------------------------------------------------
# Context Menu Actions Helper & Classes
# ---------------------------------------------------------------------------
def _parent_item(item):
    parent = getattr(item, "parent_item", None)
    return parent if parent is not None else getattr(item, "parent", None)


def extract_items(objs):
    """Normalizes selection objects from Picard context menu into tracks and files."""
    tracks = set()
    files = []
    for item in objs:
        if isinstance(item, Track):
            tracks.add(item)
        elif isinstance(item, Album):
            for t in getattr(item, "tracks", []):
                tracks.add(t)
        elif isinstance(item, File):
            parent = _parent_item(item)
            if isinstance(parent, Track):
                tracks.add(parent)
            else:
                files.append(item)
        elif hasattr(item, "files"):
            for f in getattr(item, "files", []):
                parent = _parent_item(f)
                if isinstance(parent, Track):
                    tracks.add(parent)
                else:
                    files.append(f)
    return list(tracks), files


class BetterLyricsGetAction(BaseAction):
    TITLE = "Get lyrics (TTML / LRC) automatically with Better Lyrics"

    def callback(self, objs):
        tracks, files = extract_items(objs)
        for track in tracks:
            track_files = getattr(track, "files", [])
            metadata = getattr(track, "metadata", None)
            if metadata:
                fetch_and_apply_lyrics("get", track, track_files, metadata, interactive=False)

        for file in files:
            parent = _parent_item(file)
            track = parent if isinstance(parent, Track) else None
            fetch_and_apply_lyrics("get", track, [file], file.metadata, interactive=False)


class BetterLyricsSearchAction(BaseAction):
    TITLE = "Search lyrics (TTML / LRC) manually with Better Lyrics..."

    def callback(self, objs):
        tracks, files = extract_items(objs)
        if tracks:
            track = tracks[0]
            track_files = getattr(track, "files", [])
            metadata = getattr(track, "metadata", None)
            if metadata:
                fetch_and_apply_lyrics("search", track, track_files, metadata, interactive=True)
        elif files:
            file = files[0]
            parent = _parent_item(file)
            track = parent if isinstance(parent, Track) else None
            fetch_and_apply_lyrics("search", track, [file], file.metadata, interactive=True)


# ---------------------------------------------------------------------------
# Orphaned .lrc / .ttml Files Cleaner
# ---------------------------------------------------------------------------
def find_orphaned_lyrics_files(root_dir: str) -> list[str]:
    """Return ``.ttml``/``.lrc`` sidecars under ``root_dir`` with no audio file of the same stem.

    Audio extensions match case-insensitively, so ``Song.FLAC`` still owns
    ``Song.lrc`` on case-sensitive file systems.
    """
    orphans = []
    for dirpath, _, filenames in os.walk(root_dir):
        audio_stems = set()
        lyric_files = []
        for name in filenames:
            stem, extension = os.path.splitext(name)
            extension = extension.lower()
            if extension in AUDIO_EXTENSIONS:
                audio_stems.add(stem)
            elif extension in (".lrc", ".ttml"):
                lyric_files.append((stem, name))
        orphans.extend(
            os.path.join(dirpath, name) for stem, name in lyric_files if stem not in audio_stems
        )
    return orphans


def clean_orphaned_lyrics_files():
    """Lets the user pick a folder, then deletes orphaned sidecars after confirmation."""
    try:
        parent = QtWidgets.QApplication.activeWindow()
        root_dir = QtWidgets.QFileDialog.getExistingDirectory(
            parent,
            "Select Music Library Root Directory",
            "",
            QtWidgets.QFileDialog.Option.ShowDirsOnly,
        )

        if not root_dir:
            return

        orphans = find_orphaned_lyrics_files(root_dir)
        if not orphans:
            QtWidgets.QMessageBox.information(parent, "Cleanup Complete", "No orphaned lyrics files found.")
            return
        examples = "\n".join(os.path.relpath(path, root_dir) for path in orphans[:10])
        if len(orphans) > 10:
            examples += f"\n… and {len(orphans) - 10} more"
        if not confirm_replace(
            parent,
            "Delete orphaned lyrics files?",
            f"Found {len(orphans)} .ttml / .lrc file(s) without a matching audio file:\n\n"
            f"{examples}\n\nDelete them permanently?",
        ):
            return

        removed = 0
        for path in orphans:
            try:
                os.remove(path)
                removed += 1
            except OSError as e:
                log.error(f"{PLUGIN_NAME}: Failed to remove orphaned {path}: {e}")

        QtWidgets.QMessageBox.information(
            parent,
            "Cleanup Complete",
            f"Removed {removed} orphaned lyrics file{'s' if removed != 1 else ''}.",
        )
    except Exception as err:
        log.error(f"{PLUGIN_NAME}: Error during orphaned file cleanup: {err}", exc_info=True)


# ---------------------------------------------------------------------------
# Options Page (Picard Preferences)
# ---------------------------------------------------------------------------
class BetterLyricsOptionsPage(OptionsPage):
    NAME = "better_lyrics"
    TITLE = "Better Lyrics"
    PARENT = "plugins"

    def __init__(self, parent=None):
        super().__init__(parent)
        self.box = QtWidgets.QVBoxLayout(self)

        # Description Header
        header = QtWidgets.QLabel(
            "<b>Better Lyrics for MusicBrainz Picard</b><br/>"
            "Fetches rich TTML (Timed Text Markup Language) and LRC lyrics from LRC.red, Unison, "
            "LyricsPlus, Better Lyrics / Boidu and LRCLIB.<br/>"
            "<a href='https://github.com/better-lyrics/better-lyrics'>Better Lyrics Project</a> | "
            "<a href='https://unison.betterlyrics.org'>Unison Database</a>",
            self
        )
        header.setOpenExternalLinks(True)
        self.box.addWidget(header)

        # Format Selection Group
        fmt_group = QtWidgets.QGroupBox("Lyrics Format & Output Mode", self)
        fmt_layout = QtWidgets.QVBoxLayout(fmt_group)

        fmt_h = QtWidgets.QHBoxLayout()
        fmt_label = QtWidgets.QLabel("Primary Lyrics Format:", self)
        self.format_combo = QtWidgets.QComboBox(self)
        self.format_combo.addItem("TTML (Apple Music / Better Lyrics XML)", "ttml")
        self.format_combo.addItem("LRC (Standard Synced)", "lrc")
        self.format_combo.addItem("Both TTML and LRC (Dual Export)", "both")
        fmt_h.addWidget(fmt_label)
        fmt_h.addWidget(self.format_combo)
        fmt_layout.addLayout(fmt_h)

        self.clean_word_timestamps = QtWidgets.QCheckBox(
            "Clean word timestamps in LRC format (convert to standard line timestamps for media players)",
            self
        )
        self.prefer_synced = QtWidgets.QCheckBox("Prefer synchronized / TTML lyrics over plain text", self)
        self.ignore_instrumental = QtWidgets.QCheckBox("Ignore instrumental tracks", self)
        fmt_layout.addWidget(self.clean_word_timestamps)
        fmt_layout.addWidget(self.prefer_synced)
        fmt_layout.addWidget(self.ignore_instrumental)
        self.box.addWidget(fmt_group)

        # Storage & Tag Embedding Group
        storage_group = QtWidgets.QGroupBox("Storage & Metadata Tag Embedding", self)
        storage_layout = QtWidgets.QVBoxLayout(storage_group)

        self.embed_lyrics = QtWidgets.QCheckBox(
            "Embed into standard 'lyrics' tag (written to ©lyr in M4A, USLT in MP3, LYRICS in FLAC)",
            self
        )
        self.embed_ttml_tag = QtWidgets.QCheckBox(
            "Embed raw TTML into dedicated 'ttml' tag",
            self
        )
        self.embed_syncedlyrics = QtWidgets.QCheckBox("Embed into 'syncedlyrics' tag", self)

        self.save_ttml = QtWidgets.QCheckBox("Save external .ttml sidecar file alongside audio files", self)
        self.save_lrc = QtWidgets.QCheckBox("Save external .lrc sidecar file alongside audio files", self)
        self.plain_as_txt = QtWidgets.QCheckBox(
            "Save unsynchronized lyrics as a .txt sidecar (they never go into .ttml / .lrc)", self
        )

        storage_layout.addWidget(self.embed_lyrics)
        storage_layout.addWidget(self.embed_ttml_tag)
        storage_layout.addWidget(self.embed_syncedlyrics)
        storage_layout.addWidget(self.save_ttml)
        storage_layout.addWidget(self.save_lrc)
        storage_layout.addWidget(self.plain_as_txt)
        self.box.addWidget(storage_group)

        # Automation Group
        auto_group = QtWidgets.QGroupBox("Automation", self)
        auto_layout = QtWidgets.QVBoxLayout(auto_group)
        self.get_on_load = QtWidgets.QCheckBox("Search for lyrics when loading tracks", self)
        self.get_on_save = QtWidgets.QCheckBox("Search for lyrics when saving files", self)
        self.auto_overwrite = QtWidgets.QCheckBox("Auto overwrite existing lyrics without confirmation", self)
        auto_layout.addWidget(self.get_on_load)
        auto_layout.addWidget(self.get_on_save)
        auto_layout.addWidget(self.auto_overwrite)
        self.box.addWidget(auto_group)

        # Sources & Advanced
        adv_group = QtWidgets.QGroupBox("Sources & Advanced Settings", self)
        adv_layout = QtWidgets.QVBoxLayout(adv_group)

        src_h = QtWidgets.QHBoxLayout()
        src_label = QtWidgets.QLabel("Primary Source:", self)
        self.source_combo = QtWidgets.QComboBox(self)
        self.source_combo.addItem("All enabled sources (best verified match)", "all")
        self.source_combo.addItem("LRC.red (Better Lyrics Primary)", "lrcred")
        self.source_combo.addItem("Unison (Crowdsourced Community)", "unison")
        self.source_combo.addItem("LyricsPlus (KPOE / Apple-compatible)", "lyricsplus")
        self.source_combo.addItem("Better Lyrics API / Boidu", "boidu")
        self.source_combo.addItem("LRCLIB (line-synced fallback)", "lrclib")
        src_h.addWidget(src_label)
        src_h.addWidget(self.source_combo)
        adv_layout.addLayout(src_h)

        self.enable_lyricsplus = QtWidgets.QCheckBox(
            "Enable LyricsPlus word / syllable-timed fallback", self
        )
        self.enable_boidu = QtWidgets.QCheckBox(
            "Enable Better Lyrics API / Boidu fallback (may require your own API key)", self
        )
        self.enable_lrclib = QtWidgets.QCheckBox(
            "Enable LRCLIB line-synced fallback", self
        )
        adv_layout.addWidget(self.enable_lyricsplus)
        adv_layout.addWidget(self.enable_boidu)
        adv_layout.addWidget(self.enable_lrclib)

        tol_h = QtWidgets.QHBoxLayout()
        tol_label = QtWidgets.QLabel("Duration tolerance (seconds):", self)
        self.duration_spin = QtWidgets.QSpinBox(self)
        self.duration_spin.setRange(1, 30)
        self.duration_spin.setValue(5)
        tol_h.addWidget(tol_label)
        tol_h.addWidget(self.duration_spin)
        adv_layout.addLayout(tol_h)

        # Background fetch queue tuning (stability / anti-abuse)
        perf_group = QtWidgets.QGroupBox("Performance & Stability (Background Queue)", self)
        perf_layout = QtWidgets.QVBoxLayout(perf_group)

        workers_h = QtWidgets.QHBoxLayout()
        workers_label = QtWidgets.QLabel("Max parallel requests:", self)
        self.workers_spin = QtWidgets.QSpinBox(self)
        self.workers_spin.setRange(1, 8)
        self.workers_spin.setValue(3)
        self.workers_spin.setToolTip(
            "Number of background worker threads used to fetch lyrics.\n"
            "Higher values finish larger libraries faster but use more bandwidth."
        )
        workers_h.addWidget(workers_label)
        workers_h.addWidget(self.workers_spin)
        workers_h.addStretch()
        perf_layout.addLayout(workers_h)

        rate_h = QtWidgets.QHBoxLayout()
        rate_label = QtWidgets.QLabel("Minimum delay between requests to a service (ms):", self)
        self.rate_spin = QtWidgets.QSpinBox(self)
        self.rate_spin.setRange(0, 5000)
        self.rate_spin.setSingleStep(50)
        self.rate_spin.setValue(200)
        self.rate_spin.setToolTip(
            "Minimum spacing between HTTP requests to the same service, across all workers.\n"
            "Prevents API abuse and rate-limit (HTTP 429) responses. 0 disables pacing."
        )
        rate_h.addWidget(rate_label)
        rate_h.addWidget(self.rate_spin)
        rate_h.addStretch()
        perf_layout.addLayout(rate_h)

        adv_layout.addWidget(perf_group)

        api_h = QtWidgets.QHBoxLayout()
        api_label = QtWidgets.QLabel("Better Lyrics API Key (Optional):", self)
        self.api_key_input = QtWidgets.QLineEdit(self)
        self.api_key_input.setPlaceholderText("Optional API key for api.betterlyrics.org")
        self.api_key_input.setEchoMode(QtWidgets.QLineEdit.EchoMode.PasswordEchoOnEdit)
        api_h.addWidget(api_label)
        api_h.addWidget(self.api_key_input)
        adv_layout.addLayout(api_h)

        self.clean_orphaned_btn = QtWidgets.QPushButton("Clean Orphaned .ttml / .lrc Files in Library...", self)
        self.clean_orphaned_btn.clicked.connect(clean_orphaned_lyrics_files)
        adv_layout.addWidget(self.clean_orphaned_btn)

        self.box.addWidget(adv_group)
        self.box.addStretch()

    def load(self):
        # ``config`` reads through api.plugin_config using item access
        # (ConfigSection has no ``.get()``) and falls back to registered defaults.
        cfg = config

        self.get_on_load.setChecked(bool(cfg.get("betterlyrics_get_on_load", True)))
        self.get_on_save.setChecked(bool(cfg.get("betterlyrics_get_on_save", False)))
        self.auto_overwrite.setChecked(bool(cfg.get("betterlyrics_auto_overwrite", False)))

        fmt = str(cfg.get("betterlyrics_format", "ttml"))
        f_idx = self.format_combo.findData(fmt)
        if f_idx >= 0:
            self.format_combo.setCurrentIndex(f_idx)

        self.save_ttml.setChecked(bool(cfg.get("betterlyrics_save_ttml_file", True)))
        self.save_lrc.setChecked(bool(cfg.get("betterlyrics_save_lrc_file", False)))
        self.embed_lyrics.setChecked(bool(cfg.get("betterlyrics_embed_lyrics", True)))
        self.embed_ttml_tag.setChecked(bool(cfg.get("betterlyrics_embed_ttml_tag", True)))
        self.embed_syncedlyrics.setChecked(bool(cfg.get("betterlyrics_embed_syncedlyrics", False)))

        self.clean_word_timestamps.setChecked(bool(cfg.get("betterlyrics_clean_word_timestamps", True)))
        self.prefer_synced.setChecked(bool(cfg.get("betterlyrics_prefer_synced", True)))
        self.ignore_instrumental.setChecked(bool(cfg.get("betterlyrics_ignore_instrumental", True)))
        self.plain_as_txt.setChecked(bool(cfg.get("betterlyrics_plain_as_txt", False)))

        src = str(cfg.get("betterlyrics_source", "all"))
        idx = self.source_combo.findData(src)
        if idx >= 0:
            self.source_combo.setCurrentIndex(idx)

        self.enable_lyricsplus.setChecked(bool(cfg.get("betterlyrics_enable_lyricsplus", True)))
        self.enable_boidu.setChecked(bool(cfg.get("betterlyrics_enable_boidu", True)))
        self.enable_lrclib.setChecked(bool(cfg.get("betterlyrics_enable_lrclib", True)))

        api = get_plugin_api()
        self.duration_spin.setValue(_config_int(api, "betterlyrics_duration_tolerance", 5))
        self.api_key_input.setText(str(cfg.get("betterlyrics_api_key", "") or ""))
        self.workers_spin.setValue(_config_int(api, "betterlyrics_max_workers", 3))
        self.rate_spin.setValue(_config_int(api, "betterlyrics_rate_limit_ms", 200))

    def save(self):
        # ``config`` writes through to api.plugin_config (persisted) and the
        # local defaults, so changes take effect immediately.
        cfg = config

        cfg["betterlyrics_get_on_load"] = self.get_on_load.isChecked()
        cfg["betterlyrics_get_on_save"] = self.get_on_save.isChecked()
        cfg["betterlyrics_auto_overwrite"] = self.auto_overwrite.isChecked()

        fmt_data = self.format_combo.currentData()
        cfg["betterlyrics_format"] = fmt_data or "ttml"

        cfg["betterlyrics_save_ttml_file"] = self.save_ttml.isChecked()
        cfg["betterlyrics_save_lrc_file"] = self.save_lrc.isChecked()
        cfg["betterlyrics_embed_lyrics"] = self.embed_lyrics.isChecked()
        cfg["betterlyrics_embed_ttml_tag"] = self.embed_ttml_tag.isChecked()
        cfg["betterlyrics_embed_syncedlyrics"] = self.embed_syncedlyrics.isChecked()

        cfg["betterlyrics_clean_word_timestamps"] = self.clean_word_timestamps.isChecked()
        cfg["betterlyrics_prefer_synced"] = self.prefer_synced.isChecked()
        cfg["betterlyrics_ignore_instrumental"] = self.ignore_instrumental.isChecked()
        cfg["betterlyrics_plain_as_txt"] = self.plain_as_txt.isChecked()

        src_data = self.source_combo.currentData()
        cfg["betterlyrics_source"] = src_data or "all"
        cfg["betterlyrics_enable_lyricsplus"] = self.enable_lyricsplus.isChecked()
        cfg["betterlyrics_enable_boidu"] = self.enable_boidu.isChecked()
        cfg["betterlyrics_enable_lrclib"] = self.enable_lrclib.isChecked()
        cfg["betterlyrics_duration_tolerance"] = self.duration_spin.value()
        cfg["betterlyrics_api_key"] = self.api_key_input.text().strip()
        cfg["betterlyrics_max_workers"] = self.workers_spin.value()
        cfg["betterlyrics_rate_limit_ms"] = self.rate_spin.value()

        # Apply the request pacing and worker count immediately.
        try:
            if _PROVIDER_HTTP is not None:
                _PROVIDER_HTTP.min_interval = self.rate_spin.value() / 1000.0
            if FETCH_QUEUE is not None:
                FETCH_QUEUE.configure(self.workers_spin.value())
        except Exception as error:
            log.warning(f"{PLUGIN_NAME}: could not apply queue settings: {type(error).__name__}")


# ---------------------------------------------------------------------------
# Plugin v3 Registration Hooks
# ---------------------------------------------------------------------------
def enable(api: PluginApi) -> None:
    """Entry point called when the plugin is enabled in MusicBrainz Picard 3.0+."""
    global CURRENT_PLUGIN_API, _MAIN_INVOKER, _PROVIDER_HTTP, _LYRICS_STORE
    CURRENT_PLUGIN_API = api

    # Picard invokes enable on the GUI thread, before menus can use our actions.
    try:
        if not install_menu_lifetime_workaround(log=log):
            log.warning(f"{PLUGIN_NAME}: Qt menu lifetime protection is not active")
    except Exception as error:
        log.warning(
            f"{PLUGIN_NAME}: Qt menu lifetime protection is not active (%s)",
            type(error).__name__,
        )

    # Register options with their defaults
    for key, default in PLUGIN_OPTIONS.items():
        api.plugin_config.register_option(key, default)

    # Prepare the stability layer: HTTP client, persistent cache and queue.
    # A repeated enable without disable must not leak the previous instances.
    _init_caches()
    if _PROVIDER_HTTP is not None:
        _PROVIDER_HTTP.close()
    if _LYRICS_STORE is not None:
        try:
            _LYRICS_STORE.close()
        except Exception:
            pass
    _PROVIDER_HTTP = HttpClient(
        timeout=12.0,
        retries=2,
        min_interval=_config_int(api, "betterlyrics_rate_limit_ms", 200) / 1000.0,
        ssl_context=create_ssl_context(),
        user_agent=USER_AGENT,
    )
    try:
        _LYRICS_STORE = LyricsStore(_lyrics_store_path())
    except Exception as e:
        _LYRICS_STORE = None
        log.warning(f"{PLUGIN_NAME}: persistent lyrics cache unavailable: {e}")
    FETCH_QUEUE.configure(_config_int(api, "betterlyrics_max_workers", 3))
    if _MAIN_INVOKER is not None:
        _MAIN_INVOKER.stop()
    _MAIN_INVOKER = MainThreadDispatcher()
    FETCH_QUEUE.set_invoker(_MAIN_INVOKER)
    FETCH_QUEUE.start()

    # Register file processors
    api.register_file_post_addition_to_track_processor(on_file_added)
    api.register_file_post_save_processor(on_file_saved)

    # Register context menu actions for tracks, albums, files and clusters
    api.register_track_action(BetterLyricsSearchAction)
    api.register_album_action(BetterLyricsSearchAction)
    api.register_file_action(BetterLyricsSearchAction)
    api.register_cluster_action(BetterLyricsSearchAction)

    api.register_track_action(BetterLyricsGetAction)
    api.register_album_action(BetterLyricsGetAction)
    api.register_file_action(BetterLyricsGetAction)
    api.register_cluster_action(BetterLyricsGetAction)

    # Register options page in preferences
    api.register_options_page(BetterLyricsOptionsPage)


def disable() -> None:
    """Cleanup hook called when the plugin is disabled in Picard 3.0+."""
    global CURRENT_PLUGIN_API, _MAIN_INVOKER, _PROVIDER_HTTP, _LYRICS_STORE
    try:
        uninstall_menu_lifetime_workaround()
    except Exception as error:
        log.warning(
            f"{PLUGIN_NAME}: Qt menu lifetime workaround cleanup failed (%s)",
            type(error).__name__,
        )
    if _PROVIDER_HTTP is not None:
        _PROVIDER_HTTP.close()
        _PROVIDER_HTTP = None
    if _LYRICS_STORE is not None:
        try:
            _LYRICS_STORE.close()
        except Exception:
            pass
        _LYRICS_STORE = None
    if FETCH_QUEUE is not None:
        FETCH_QUEUE.stop()
    if _MAIN_INVOKER is not None:
        _MAIN_INVOKER.stop()
        _MAIN_INVOKER = None
    if FETCH_QUEUE is not None:
        FETCH_QUEUE.set_invoker(None)
    CURRENT_PLUGIN_API = None
