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

from collections import OrderedDict
from functools import partial
import json
import os
import queue
import random
import re
import ssl
import threading
import time
from urllib.parse import quote, urlencode
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from weakref import WeakKeyDictionary
import xml.etree.ElementTree as ET
from difflib import SequenceMatcher

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
    from .lyrics_matching import assess_match, choose_identity_group, rank_candidates
    from .lyrics_model import make_bundle, parse_kpoe, parse_lrc, parse_ttml
    from .lyrics_providers import search_provider
    from .lyrics_qt_compat import (
        install_menu_lifetime_workaround,
        uninstall_menu_lifetime_workaround,
    )
except ImportError:  # Picard loads a plugin directory as a top-level module.
    from lyrics_cache import LyricsStore, track_key
    from lyrics_http import HttpClient, create_ssl_context
    from lyrics_matching import assess_match, choose_identity_group, rank_candidates
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
BETTER_LYRICS_API_URL = "https://api.betterlyrics.org/getLyrics"

# Matching weights deliberately sum to 1.0.  ISRC is the strongest identity
# signal, while title/artist remain the primary fallback when an API result has
# no ISRC.  Duration is useful for disambiguation, but must not overpower an
# otherwise exact title/artist match.
MATCH_WEIGHTS = {
    "isrc": 0.35,
    "title": 0.30,
    "artist": 0.15,
    "album": 0.05,
    "duration": 0.15,
}
MATCH_MIN_SCORE_WITHOUT_ISRC = 0.40
SYNCED_TIMING_TYPES = frozenset(("word", "line", "richsync", "linesync"))

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36 MusicBrainzPicard-BetterLyrics/1.2"
)

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

# Common audio extensions for orphaned file scanning
AUDIO_EXTENSIONS = {
    ".aac", ".ac3", ".aif", ".aifc", ".aiff", ".ape", ".asf", ".dff",
    ".dsf", ".eac3", ".flac", ".m2a", ".m4a", ".m4b", ".m4p", ".mp2",
    ".mp3", ".mp4", ".mpc", ".ofr", ".ofs", ".oga", ".ogg", ".opus",
    ".spx", ".tak", ".tta", ".wav", ".wma", ".wv"
}

# Global reference to PluginApi instance
CURRENT_PLUGIN_API: PluginApi | None = None

# Picard calls ``disable()`` before its automatic registration cleanup.  These
# flags make our own asynchronous hooks inert immediately, so a late processor
# callback cannot restart the worker pool or touch a closed client after the
# plugin has been disabled.  ``_DISABLED`` starts as False to keep the module
# usable from direct tests and smoke scripts that never call ``enable()``.
_ENABLED = False
_DISABLED = False

# Set on disable, cleared on enable.  Background HTTP work observes it between
# attempts and during backoff so shutdown does not wait for a retry to expire.
_SHUTDOWN_EVENT = threading.Event()


def is_plugin_disabled() -> bool:
    """Return whether Picard disabled the plugin and background work must stop."""
    return _DISABLED


def get_plugin_api() -> PluginApi | None:
    """Retrieves the active PluginApi instance."""
    global CURRENT_PLUGIN_API
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
    """Routes configuration lookups and updates to api.plugin_config."""

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
            except Exception:
                pass
        PLUGIN_OPTIONS[key] = value


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


def _bounded_int(value, default: int, minimum: int, maximum: int) -> int:
    """Coerces a possibly corrupt stored option into a usable widget value.

    Picard's docs recommend bounded numeric options because a hand-edited or
    legacy config can contain an out-of-range value.  The options page must load
    instead of raising in that case.
    """
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        number = default
    return max(minimum, min(maximum, number))


def _ensure_queue_ready() -> bool:
    """Lazily (idempotently) starts the background fetch queue.

    Called from the GUI thread on the automatic paths, guaranteeing that a
    submitted job always has workers and a main-thread invoker available.
    Returns ``False`` once Picard has disabled the plugin: processors may still
    deliver a queued callback, and resurrecting the pool then would start
    network work behind a disabled plugin.
    """
    global _MAIN_INVOKER
    if is_plugin_disabled():
        log.debug(f"{PLUGIN_NAME}: background queue not started (plugin disabled)")
        return False
    _init_caches()
    if _MAIN_INVOKER is None:
        _MAIN_INVOKER = MainThreadDispatcher()
    FETCH_QUEUE.set_invoker(_MAIN_INVOKER)
    if not FETCH_QUEUE.is_running:
        FETCH_QUEUE.start()
    return True


# ---------------------------------------------------------------------------
# Duration & Text Formatting Utilities
# ---------------------------------------------------------------------------
def format_duration(seconds: int | float | None) -> str:
    """Formats duration in seconds to mm:ss or hh:mm:ss."""
    if seconds is None:
        return "0:00"
    total_sec = max(0, int(round(float(seconds))))
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


def get_track_duration(track: Track) -> int:
    """Extracts duration in seconds from track or file metadata."""
    metadata = getattr(track, "metadata", None)
    if metadata and metadata.get("~length"):
        return parse_duration(str(metadata["~length"]))
    files = getattr(track, "files", [])
    if files:
        tr_meta = getattr(files[0], "metadata", None)
        if tr_meta and tr_meta.get("~length"):
            return parse_duration(str(tr_meta["~length"]))
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


# ---------------------------------------------------------------------------
# TTML & LRC Format Handlers
# ---------------------------------------------------------------------------
def parse_ttml_time(time_str: str) -> tuple[int, float]:
    """Parses a TTML timestamp into ``(minutes, seconds)``.

    Supported forms are clock time (``mm:ss.mmm`` / ``hh:mm:ss.mmm``), offset
    time with an explicit unit (``1.5s``, ``1500ms``) and a bare seconds value.
    Decimal commas are accepted because some TTML producers emit them.  Invalid
    values raise ``ValueError``; callers decide whether one bad line may be
    skipped instead of failing the whole document.
    """
    if time_str is None:
        raise ValueError("empty timestamp")
    clean_str = str(time_str).strip()
    if not clean_str:
        raise ValueError("empty timestamp")
    lowered = clean_str.casefold()

    if lowered.endswith("ms"):
        total_seconds = float(lowered[:-2].replace(",", ".")) / 1000.0
        if total_seconds < 0:
            raise ValueError("negative timestamp")
        return int(total_seconds // 60), total_seconds % 60
    if lowered.endswith("s"):
        lowered = lowered[:-1]

    parts = lowered.split(":")
    if len(parts) == 1:
        total_seconds = float(parts[0].replace(",", "."))
        if total_seconds < 0:
            raise ValueError("negative timestamp")
        return int(total_seconds // 60), total_seconds % 60

    if len(parts) not in (2, 3):
        raise ValueError(f"unsupported timestamp: {time_str!r}")
    seconds_value = float(parts[-1].replace(",", "."))
    if seconds_value < 0:
        raise ValueError("negative timestamp")
    if len(parts) == 2:
        minutes = int(parts[0])
    else:
        minutes = int(parts[0]) * 60 + int(parts[1])
    if minutes < 0:
        raise ValueError("negative timestamp")
    return minutes + int(seconds_value // 60), seconds_value % 60


def is_ttml_content(text: str) -> bool:
    """Checks whether the given string is TTML XML."""
    if not text:
        return False
    stripped = text.strip()
    return stripped.startswith("<tt") or stripped.startswith("<?xml") or "<tt xmlns=" in text


def ttml_to_lrc(ttml_content: str, word_synced: bool = False) -> str:
    """Converts TTML (Timed Text Markup Language) XML into standard or word-synced LRC."""
    try:
        root = ET.fromstring(ttml_content)
    except Exception as e:
        log.warning(f"{PLUGIN_NAME}: Failed to parse TTML XML: {e}")
        return ""

    lines = []
    for elem in root.iter():
        tag = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
        if tag == "p":
            begin = elem.get("begin")
            if not begin:
                continue
            try:
                mm, ss = parse_ttml_time(begin)
            except (TypeError, ValueError):
                # One malformed cue must not discard the rest of the document.
                log.debug(f"{PLUGIN_NAME}: skipping TTML cue with invalid begin={begin!r}")
                continue
            line_ts = f"[{mm:02d}:{ss:05.2f}]"

            spans = [
                e for e in elem.iter()
                if (e.tag.split("}")[-1] if "}" in e.tag else e.tag) == "span"
                and e.get("begin")
            ]

            if word_synced and spans:
                words = []
                for s in spans:
                    s_begin = s.get("begin")
                    try:
                        s_mm, s_ss = parse_ttml_time(s_begin)
                    except (TypeError, ValueError):
                        continue
                    word_text = "".join(s.itertext())
                    words.append(f"<{s_mm:02d}:{s_ss:05.2f}>{word_text}")
                text = "".join(words).strip()
            else:
                text = "".join(elem.itertext()).strip()
                text = re.sub(r"\s+", " ", text)

            if text:
                lines.append(f"{line_ts}{text}")

    return "\n".join(lines)


def lrc_to_ttml(lrc_text: str, title: str = "", artist: str = "") -> str:
    """Converts LRC lines into a standard TTML (Timed Text Markup Language) XML document."""
    line_regex = re.compile(r"\[(\d{1,2}):(\d{2})(?:\.(\d+))?\](.*)")
    body_lines = []

    for line in lrc_text.splitlines():
        match = line_regex.match(line.strip())
        if match:
            mm = int(match.group(1))
            ss = int(match.group(2))
            ms_str = match.group(3) or "0"
            ms = float(f"0.{ms_str}") if ms_str else 0.0
            text = match.group(4).strip()
            text = re.sub(r"<\d{1,2}:\d{2}(?:\.\d+)?>", "", text).strip()
            if text:
                time_fmt = f"{mm:02d}:{ss:02d}.{int(ms * 1000):03d}"
                escaped = (
                    text.replace("&", "&amp;")
                    .replace("<", "&lt;")
                    .replace(">", "&gt;")
                )
                body_lines.append(f'      <p begin="{time_fmt}">{escaped}</p>')

    escaped_title = (title or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    escaped_artist = (artist or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    ttml = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<tt xmlns="http://www.w3.org/ns/ttml" xmlns:ttm="http://www.w3.org/ns/ttml#metadata">\n'
        '  <head>\n'
        '    <metadata>\n'
        f'      <ttm:title>{escaped_title}</ttm:title>\n'
        f'      <ttm:agent type="person" xml:id="v1">{escaped_artist}</ttm:agent>\n'
        '    </metadata>\n'
        '  </head>\n'
        '  <body>\n'
        '    <div>\n'
        f"{chr(10).join(body_lines)}\n"
        '    </div>\n'
        '  </body>\n'
        '</tt>'
    )
    return ttml


def clean_word_timestamps(lrc_text: str) -> str:
    """Strips word-by-word timestamps <mm:ss.xx> from LRC text."""
    if not lrc_text:
        return ""
    word_pattern = re.compile(r"<\d{1,2}:\d{2}(?:\.\d+)?>")
    cleaned_lines = []
    for line in lrc_text.splitlines():
        cleaned_lines.append(word_pattern.sub("", line).rstrip())
    return "\n".join(cleaned_lines)


def is_lrc_synced(lrc_text: str) -> bool:
    """Checks whether the lyrics contain line or word timestamps."""
    if not lrc_text:
        return False
    return bool(re.search(r"\[\d{1,2}:\d{2}(?:\.\d+)?\]", lrc_text))


# ---------------------------------------------------------------------------
# Concurrency Primitives: Rate Limiter, TTL Cache & Queue Globals
# ---------------------------------------------------------------------------
# Sentinel used to distinguish a genuine cache miss from a cached ``None``.
_CACHE_MISS = object()

# Retry policy for transient HTTP failures (429 / 5xx / connection errors).
HTTP_RETRIES = 2
RETRY_STATUS_CODES = frozenset({408, 425, 429, 500, 502, 503, 504})
_BACKOFF_BASE = 0.5  # seconds
_BACKOFF_MAX = 4.0  # seconds cap
_NEGATIVE_CACHE_TTL = 30.0  # seconds to remember a failed lookup (anti-hammering)

# Response caches (TTL, bounded LRU). One for raw text, one for parsed JSON.
_HTTP_TEXT_CACHE = None  # initialised in _init_caches() to avoid import-time work
_JSON_CACHE = None

# Global outbound-request pacer, shared by worker threads and the main thread.
_RATE_LIMITER = None

# Foreground/background fetch queue (created lazily, started from ``enable``).
FETCH_QUEUE = None

# Main-thread dispatcher used by worker threads to apply results safely.
_MAIN_INVOKER = None

# Optional provider client and persistent bundle cache.  They are created only
# after Picard enables the plugin, so importing the module stays side-effect free.
_PROVIDER_HTTP = None
_LYRICS_STORE = None

# A verified TLS context is expensive to build and safe to reuse for every
# request; per-request construction only adds latency and failure surface.
_SSL_CONTEXT = None


def _lyrics_store_path() -> str:
    """Return a user-data path, never a path inside the installed plugin."""
    api = get_plugin_api()
    app_data = None
    try:
        app_data = QtCore.QStandardPaths.writableLocation(
            QtCore.QStandardPaths.StandardLocation.AppDataLocation
        )
    except Exception:
        pass
    if not app_data:
        app_data = os.path.join(
            os.path.expanduser("~"), "Library", "Application Support", "MusicBrainz", "Picard"
        )
    return os.path.join(app_data, "betterlyrics_cache.sqlite3")


def _provider_request_json(url: str, params=None, headers=None):
    if _PROVIDER_HTTP is None:
        return None
    return _PROVIDER_HTTP.request_json(url, params=params, headers=headers)


class RateLimiter:
    """Thread-safe minimum-interval limiter that paces outbound requests.

    Only one caller is ever allowed to pass per ``min_interval`` window, so the
    plugin can never burst-hammer an API regardless of how many jobs run in
    parallel.  Callers block briefly instead of failing.
    """

    def __init__(self, min_interval: float = 0.0):
        self._min_interval = max(0.0, float(min_interval))
        self._lock = threading.Lock()
        self._next_allowed = 0.0

    def set_interval(self, min_interval: float) -> None:
        with self._lock:
            self._min_interval = max(0.0, float(min_interval))

    @property
    def interval(self) -> float:
        return self._min_interval

    def acquire(self, cancel_event: threading.Event | None = None) -> bool:
        """Waits until the next request slot is available, then claims it.

        The slot is reserved before waiting, so concurrent workers queue behind
        the reservation instead of sleeping while holding the lock.  When
        ``cancel_event`` is supplied, a paced wait is abandoned as soon as the
        event is set; the caller then skips the request entirely.
        """
        if self._min_interval <= 0:
            return not (cancel_event is not None and cancel_event.is_set())

        with self._lock:
            now = time.monotonic()
            slot = max(now, self._next_allowed)
            self._next_allowed = slot + self._min_interval

        wait = slot - time.monotonic()
        if wait > 0:
            if cancel_event is not None:
                if cancel_event.wait(wait):
                    return False
            else:
                time.sleep(wait)
        return not (cancel_event is not None and cancel_event.is_set())


class TTLCache:
    """Small thread-safe LRU cache with per-entry time-to-live.

    Values may legitimately be ``None`` (negative caching); use :meth:`get`
    together with the ``_CACHE_MISS`` sentinel to tell a miss from a cached
    ``None``.
    """

    def __init__(self, max_entries: int = 512, ttl: float = 1800.0):
        self._lock = threading.Lock()
        self._store: "OrderedDict[object, tuple]" = OrderedDict()
        self._max_entries = max(1, int(max_entries))
        self._ttl = float(ttl)

    def get(self, key, default=None):
        with self._lock:
            item = self._store.get(key)
            if item is None:
                return default
            value, expires = item
            if expires < time.monotonic():
                self._store.pop(key, None)
                return default
            self._store.move_to_end(key)
            return value

    def set(self, key, value, ttl: float | None = None) -> None:
        expiry = time.monotonic() + (self._ttl if ttl is None else float(ttl))
        with self._lock:
            self._store[key] = (value, expiry)
            self._store.move_to_end(key)
            while len(self._store) > self._max_entries:
                self._store.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._store.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._store)


def _init_caches() -> None:
    """Creates the module-level caches/limiter/queue singletons exactly once."""
    global _HTTP_TEXT_CACHE, _JSON_CACHE, _RATE_LIMITER, FETCH_QUEUE
    if _HTTP_TEXT_CACHE is None:
        _HTTP_TEXT_CACHE = TTLCache(max_entries=256, ttl=1800.0)
    if _JSON_CACHE is None:
        _JSON_CACHE = TTLCache(max_entries=512, ttl=900.0)
    if _RATE_LIMITER is None:
        _RATE_LIMITER = RateLimiter(0.2)
    if FETCH_QUEUE is None:
        FETCH_QUEUE = FetchQueue()


# ---------------------------------------------------------------------------
# HTTP Networking Helpers
# ---------------------------------------------------------------------------
def _get_ssl_context():
    """Returns the verified default SSL context used for HTTPS requests."""
    global _SSL_CONTEXT
    if _SSL_CONTEXT is not None:
        return _SSL_CONTEXT
    try:
        _SSL_CONTEXT = create_ssl_context()
        return _SSL_CONTEXT
    except Exception as e:
        log.error(f"{PLUGIN_NAME}: Failed to create a verified SSL context: {e}", exc_info=True)
        raise


def _build_url(url: str, params: dict | None) -> str:
    """Appends query parameters to a URL when present."""
    if not params:
        return url
    query = urlencode(params)
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}{query}"


def _http_get(req, timeout: int):
    """Performs a single HTTP GET. Returns (status, body); raises on network error."""
    resp = urlopen(req, timeout=timeout, context=_get_ssl_context())
    with resp:
        return resp.status, resp.read().decode("utf-8", errors="replace")


def _wait_for_retry(seconds: float) -> bool:
    """Waits for a retry backoff, returning ``True`` when shutdown interrupts it."""
    return _SHUTDOWN_EVENT.wait(max(0.0, float(seconds)))


def _http_get_with_retry(req, timeout: int, retries: int = HTTP_RETRIES) -> str | None:
    """Throttled HTTP GET with exponential backoff for transient failures.

    Every outbound attempt first passes through the shared :class:`RateLimiter`,
    so parallel workers never burst-hammer an API. Transient failures
    (connection errors, 429 and 5xx responses) are retried with jittered
    exponential backoff. Returns the body on success, else ``None``.
    """
    _init_caches()
    last_error = None
    last_status = None

    for attempt in range(retries + 1):
        if _SHUTDOWN_EVENT.is_set():
            log.debug(f"{PLUGIN_NAME}: request cancelled by plugin shutdown")
            return None
        if not _RATE_LIMITER.acquire(_SHUTDOWN_EVENT):
            log.debug(f"{PLUGIN_NAME}: paced request cancelled by plugin shutdown")
            return None
        try:
            status, body = _http_get(req, timeout)
        except HTTPError as e:
            status = e.code
            last_status = status
            last_error = None
            try:
                e.close()
            except Exception:
                pass
        except Exception as e:
            last_error = e
            status = None
        else:
            last_error = None
            if status == 200:
                return body
            last_status = status

        retryable = status is None or status in RETRY_STATUS_CODES
        if not retryable or attempt >= retries:
            break

        delay = min(_BACKOFF_MAX, _BACKOFF_BASE * (2 ** attempt)) + random.uniform(0.0, 0.25)
        log.debug(
            f"{PLUGIN_NAME}: retrying {req.full_url} after "
            f"{'network error' if status is None else f'HTTP {status}'} "
            f"(attempt {attempt + 1}/{retries}, waiting {delay:.2f}s)"
        )
        if _wait_for_retry(delay):
            log.debug(f"{PLUGIN_NAME}: retry backoff cancelled by plugin shutdown")
            return None

    if last_error is not None:
        log.warning(f"{PLUGIN_NAME}: request failed for {req.full_url}: {last_error}")
    elif last_status is not None and last_status != 200:
        log.warning(f"{PLUGIN_NAME}: HTTP {last_status} for {req.full_url}")
    return None


def fetch_json(
    url: str,
    params: dict | None = None,
    headers: dict | None = None,
    timeout: int = 10,
    use_cache: bool = True,
):
    """Throttled, cached, retrying HTTP GET that parses a JSON response.

    Successful responses are cached for 15 minutes; failures are cached briefly
    (negative caching) so a broken endpoint is not hammered.
    """
    _init_caches()
    full_url = _build_url(url, params)

    if use_cache:
        cached = _JSON_CACHE.get(full_url, _CACHE_MISS)
        if cached is not _CACHE_MISS:
            return cached

    req_headers = {"User-Agent": DEFAULT_USER_AGENT, "Accept": "application/json"}
    if headers:
        req_headers.update(headers)

    body = _http_get_with_retry(Request(full_url, headers=req_headers), timeout)

    data = None
    if body is not None:
        try:
            data = json.loads(body)
        except Exception as e:
            log.warning(f"{PLUGIN_NAME}: invalid JSON from {full_url}: {e}")
            data = None

    if use_cache:
        ttl = None if data is not None else _NEGATIVE_CACHE_TTL
        _JSON_CACHE.set(full_url, data, ttl=ttl)
    return data


def fetch_text(
    url: str,
    timeout: int = 10,
    use_cache: bool = True,
    cache_ttl: float | None = None,
) -> str | None:
    """Throttled, cached, retrying HTTP GET returning the raw text response."""
    _init_caches()

    if use_cache:
        cached = _HTTP_TEXT_CACHE.get(url, _CACHE_MISS)
        if cached is not _CACHE_MISS:
            return cached

    body = _http_get_with_retry(Request(url, headers={"User-Agent": DEFAULT_USER_AGENT}), timeout)

    if use_cache:
        ttl = cache_ttl if body is not None else _NEGATIVE_CACHE_TTL
        _HTTP_TEXT_CACHE.set(url, body, ttl=ttl)
    return body


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

    def __init__(self, poll_ms: int = 100, max_callbacks_per_tick: int = 25):
        super().__init__()
        self._callbacks: "queue.Queue" = queue.Queue()
        self._post_lock = threading.Lock()
        self._stopped = False
        self._last_status: str | None = None
        self._max_callbacks_per_tick = max(1, int(max_callbacks_per_tick))
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
        """Stops the timer, drops pending results and clears the status line."""
        with self._post_lock:
            if self._stopped:
                return
            self._stopped = True
            while True:
                try:
                    self._callbacks.get_nowait()
                except queue.Empty:
                    break
        timer = self._timer
        self._timer = None
        if timer is not None:
            try:
                timer.stop()
            except Exception:
                pass
            try:
                timer.deleteLater()
            except Exception:
                pass
        try:
            if self._last_status is not None:
                _clear_status_message()
        except Exception:
            pass
        self._last_status = None

    def poll(self) -> None:
        """Runs a bounded batch of pending callbacks, then refreshes status.

        The batch is capped so a burst of thousands of completed lookups cannot
        monopolise the GUI thread in a single timer tick.  Remaining callbacks
        are applied on subsequent ticks.
        """
        handled = 0
        while handled < self._max_callbacks_per_tick:
            try:
                fn = self._callbacks.get_nowait()
            except queue.Empty:
                break
            handled += 1
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
      * a configurable, bounded worker pool instead of an unbounded burst,
      * deduplication of identical in-flight jobs (``key``),
      * graceful, fast start/stop: pending jobs are discarded on stop instead
        of being drained behind the user's back,
      * an epoch per start: a worker left over from an earlier enable cycle can
        never run a job or deliver a result into the current session,
      * results delivered back to the GUI thread via :class:`MainThreadDispatcher`.
    """

    _POLL_INTERVAL = 0.1

    def __init__(self, max_workers: int = 3):
        self._max_workers = max(1, int(max_workers))
        self._queue: "queue.Queue" = queue.Queue()
        self._workers: list = []
        self._lock = threading.Lock()
        self._pending_keys: set = set()
        self._running = False
        self._invoker = None
        self._active = 0
        self._epoch = 0
        self._stop_event = threading.Event()
        self._stop_event.set()

    def configure(self, max_workers: int | None = None) -> None:
        if max_workers is not None:
            self._max_workers = max(1, int(max_workers))

    def set_invoker(self, invoker) -> None:
        with self._lock:
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
            self._stop_event.clear()
            self._epoch += 1
            epoch = self._epoch
            self._running = True
            self._workers = [
                threading.Thread(
                    target=self._worker_loop,
                    args=(epoch,),
                    name=f"BetterLyricsFetch-{i}",
                    daemon=True,
                )
                for i in range(self._max_workers)
            ]
        started = 0
        for worker in self._workers:
            try:
                worker.start()
                started += 1
            except RuntimeError as error:
                log.warning(f"{PLUGIN_NAME}: could not start fetch worker: {error}")
        if started == 0:
            with self._lock:
                self._running = False
                self._stop_event.set()
                self._workers = []
            log.warning(f"{PLUGIN_NAME}: fetch queue has no usable workers")
            return
        log.debug(f"{PLUGIN_NAME}: fetch queue started ({started} worker(s))")

    def stop(self, timeout: float = 1.0) -> None:
        """Stop workers, discard queued jobs and detach stale results.

        Pending work is intentionally *not* drained: on disable the user has
        already left the session, and finishing hundreds of lookups would keep
        network traffic and metadata writes alive behind a disabled plugin.
        Workers finish the request they are already executing (bounded by the
        HTTP timeout), then exit when they observe the new epoch.
        """
        with self._lock:
            if not self._running:
                self._stop_event.set()
                self._pending_keys.clear()
                return
            self._running = False
            self._epoch += 1
            self._stop_event.set()
            workers = list(self._workers)
            self._workers = []

        dropped = 0
        while True:
            try:
                task = self._queue.get_nowait()
            except queue.Empty:
                break
            dropped += 1
            self._release_key(getattr(task, "key", None))
            self._queue.task_done()

        # One shared deadline: three stuck workers must not cost 3 × timeout on
        # the GUI thread.  Stragglers are daemon threads and epoch-guarded.
        deadline = time.monotonic() + max(0.0, float(timeout))
        for worker in workers:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            worker.join(remaining)
        with self._lock:
            self._pending_keys.clear()
            self._active = 0
        log.debug(
            f"{PLUGIN_NAME}: fetch queue stopped ({dropped} pending job(s) discarded)"
        )

    def submit(self, job_fn, apply_fn=None, key=None) -> bool:
        """Enqueues a job. Returns ``False`` if the queue is stopped or duplicating."""
        task = _FetchTask(job_fn, apply_fn, key)
        with self._lock:
            if not self._running or self._stop_event.is_set():
                return False
            if key is not None:
                if key in self._pending_keys:
                    return False
                self._pending_keys.add(key)
            # Enqueue while holding the lock so a concurrent stop() cannot miss
            # this task and leave it for the next enable cycle.
            self._queue.put(task)
        return True

    def _release_key(self, key, epoch: int | None = None) -> None:
        if key is None:
            return
        with self._lock:
            if epoch is None or epoch == self._epoch:
                self._pending_keys.discard(key)

    def _worker_loop(self, epoch: int) -> None:
        while True:
            try:
                task = self._queue.get(timeout=self._POLL_INTERVAL)
            except queue.Empty:
                with self._lock:
                    if self._stop_event.is_set() or epoch != self._epoch or not self._running:
                        return
                continue

            counted = False
            try:
                if task is None:
                    return
                with self._lock:
                    current = (
                        self._running
                        and epoch == self._epoch
                        and not self._stop_event.is_set()
                    )
                    if current:
                        self._active += 1
                        counted = True
                if not current:
                    # This worker belongs to a previous enable cycle.  Never run
                    # or apply its job against the current Picard session.
                    return

                try:
                    result = task.job_fn()
                except Exception as e:
                    log.warning(
                        f"{PLUGIN_NAME}: background fetch job failed: {type(e).__name__}"
                    )
                    result = None
                if task.apply_fn is not None:
                    with self._lock:
                        invoker = self._invoker
                        current = epoch == self._epoch and self._running
                    if current and invoker is not None and not self._stop_event.is_set():
                        invoker.post(partial(task.apply_fn, result))
                    else:
                        log.debug(f"{PLUGIN_NAME}: result dropped (plugin stopped)")
            finally:
                if counted:
                    with self._lock:
                        self._active = max(0, self._active - 1)
                self._release_key(getattr(task, "key", None), epoch)
                self._queue.task_done()


# ---------------------------------------------------------------------------
# Match Scoring Algorithm
# ---------------------------------------------------------------------------
def _normalize_match_text(value) -> str:
    """Normalizes metadata enough for stable, punctuation-insensitive matching."""
    text = str(value or "").casefold()
    text = re.sub(r"[^\w\s]", " ", text, flags=re.UNICODE)
    return " ".join(text.split())


def _text_similarity(left, right) -> float:
    left_normalized = _normalize_match_text(left)
    right_normalized = _normalize_match_text(right)
    if not left_normalized or not right_normalized:
        return 0.0
    return SequenceMatcher(None, left_normalized, right_normalized).ratio()


def _normalize_isrc(value) -> str:
    """Normalizes ISRC formatting without changing its identity."""
    return re.sub(r"[^A-Z0-9]", "", str(value or "").upper())


def _duration_similarity(target_duration: int, item_duration: int, tolerance: int) -> float:
    """Returns a duration score that reaches zero outside twice the tolerance."""
    if target_duration <= 0 or item_duration <= 0:
        return 0.0
    diff = abs(target_duration - item_duration)
    tolerance = max(0, int(tolerance))
    if tolerance == 0:
        return 1.0 if diff == 0 else 0.0
    if diff <= tolerance:
        # A small duration drift is expected, but an exact duration still wins.
        return 1.0 - 0.4 * (diff / tolerance)
    return max(0.0, 0.6 * (1.0 - ((diff - tolerance) / tolerance)))


def _has_exact_isrc(target_isrc: str | None, item_isrc: str | None) -> bool:
    target = _normalize_isrc(target_isrc)
    item = _normalize_isrc(item_isrc)
    return bool(target and item and target == item)


def calculate_match_score(
    target_title: str,
    target_artist: str,
    target_album: str | None,
    target_duration: int,
    target_isrc: str | None,
    item: dict,
    duration_tolerance: int = 5,
) -> float:
    """Calculates a weighted match confidence score between 0.0 and 1.0.

    Missing metadata contributes zero rather than receiving a false bonus.  A
    synchronized result is intentionally not part of this identity score; it
    is a preference-only tie breaker in :func:`query_all_sources`.
    """
    item_title = item.get("title", "")
    item_artist = item.get("artist", "")
    item_album = item.get("album", "")
    item_duration = parse_duration(item.get("duration", 0))
    item_isrc = item.get("isrc")

    score = 0.0

    if _has_exact_isrc(target_isrc, item_isrc):
        score += MATCH_WEIGHTS["isrc"]

    if target_title and item_title:
        score += _text_similarity(target_title, item_title) * MATCH_WEIGHTS["title"]

    if target_artist and item_artist:
        score += _text_similarity(target_artist, item_artist) * MATCH_WEIGHTS["artist"]

    if target_album and item_album:
        score += _text_similarity(target_album, item_album) * MATCH_WEIGHTS["album"]

    if target_duration and item_duration:
        score += _duration_similarity(
            parse_duration(target_duration), item_duration, duration_tolerance
        ) * MATCH_WEIGHTS["duration"]

    return max(0.0, min(1.0, score))


def _candidate_is_compatible(
    target_title: str,
    target_artist: str,
    target_duration: int,
    target_isrc: str,
    item: dict,
    tolerance: int,
) -> bool:
    """Rejects identity conflicts before lyrics content is downloaded."""
    # ``query_all_sources`` already annotates provider results with the
    # standalone matcher. Do not apply the legacy score floor a second time:
    # exact provider lookups (notably Boidu and LRCLIB /api/get) can be valid
    # even when the response has sparse metadata and therefore a low score.
    if "match_eligible" in item:
        return bool(item.get("match_eligible"))
    item_isrc = item.get("isrc")
    exact_isrc = _has_exact_isrc(target_isrc, item_isrc)
    normalized_target_isrc = _normalize_isrc(target_isrc)
    normalized_item_isrc = _normalize_isrc(item_isrc)

    if normalized_target_isrc and normalized_item_isrc and not exact_isrc:
        return False

    # An exact ISRC is authoritative. Providers may format title/artist
    # metadata differently, so text gates are only applied when ISRC is absent.
    item_title = item.get("title", "")
    item_artist = item.get("artist", "")
    if not exact_isrc:
        if target_title and not item_title:
            return False
        if target_artist and not item_artist:
            return False
        if target_title and _text_similarity(target_title, item_title) < 0.55:
            return False
        if target_artist and _text_similarity(target_artist, item_artist) < 0.45:
            return False
        if item.get("score", 0.0) < MATCH_MIN_SCORE_WITHOUT_ISRC:
            return False

    item_duration = parse_duration(item.get("duration", 0))
    if target_duration > 0 and item_duration > 0:
        if abs(target_duration - item_duration) > max(0, int(tolerance)) and not exact_isrc:
            return False
    return True


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
    isrc_pattern = re.compile(r"^[A-Z]{2}[A-Z0-9]{3}[0-9]{7}$")

    def valid_isrc(value):
        normalized = _normalize_isrc(value)
        return normalized if isrc_pattern.fullmatch(normalized) else ""

    def safe_duration(value):
        try:
            seconds = float(value)
        except (TypeError, ValueError, OverflowError):
            return 0
        if seconds != seconds or seconds in (float("inf"), float("-inf")) or seconds < 0:
            return 0
        return seconds

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
        if not isinstance(timing_type, str):
            timing_type = "unknown"

        return {
            "id": normalized_isrc,
            "title": res.get("title", "") if isinstance(res.get("title", ""), str) else "",
            "artist": res.get("artist", "") if isinstance(res.get("artist", ""), str) else "",
            "album": res.get("album", "") if isinstance(res.get("album", ""), str) else "",
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

    # 1. Resolve a validated ISRC through LRC.red's single-object resource.
    normalized_isrc = valid_isrc(isrc) if not search_q else ""
    if normalized_isrc:
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
    if search_q:
        data = fetch_json(UNISON_SEARCH_URL, {"q": search_q})
        rows = data.get("data") if isinstance(data, dict) and data.get("success") else None
        if isinstance(rows, list):
            for res in rows:
                if not isinstance(res, dict):
                    continue
                c_id = res.get("id")
                if c_id is None or isinstance(c_id, (dict, list, set)):
                    continue
                if c_id in seen_ids:
                    continue
                seen_ids.add(c_id)
                sync_type = res.get("syncType", "plain")
                file_format = res.get("format", "ttml")
                candidates.append({
                    "id": c_id,
                    "title": res.get("song", "") if isinstance(res.get("song", ""), str) else "",
                    "artist": res.get("artist", "") if isinstance(res.get("artist", ""), str) else "",
                    "album": res.get("album", "") if isinstance(res.get("album", ""), str) else "",
                    "duration": res.get("duration", 0),
                    "isrc": res.get("isrc", "") if isinstance(res.get("isrc", ""), str) else "",
                    "timing_type": sync_type if isinstance(sync_type, str) else "plain",
                    "format": file_format if isinstance(file_format, str) else "ttml",
                    "source": "Unison",
                    "provider_id": "unison",
                    "metadata_verified": True,
                    "direct_lyrics": res.get("lyrics"),
                    "raw": res,
                })

    return candidates


def _call_provider(provider_id: str, callback) -> list[dict]:
    """Runs one provider without letting its failure cancel the fallback chain.

    A single malformed provider response used to abort the whole search, which
    also suppressed Unison/LRCLIB fallbacks.  Provider failures are now logged
    with a bounded type name and treated as an empty result set.
    """
    try:
        found = callback()
    except Exception as error:
        log.warning(
            f"{PLUGIN_NAME}: provider {provider_id} failed: {type(error).__name__}"
        )
        return []
    if found is None:
        return []
    if not isinstance(found, list):
        log.warning(f"{PLUGIN_NAME}: provider {provider_id} returned an invalid result")
        return []
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
    options: dict | None = None,
) -> list[dict]:
    """Queries configured providers and ranks candidates by recording identity.

    ``options`` is an optional settings snapshot for background workers.  When
    omitted (interactive GUI search) each value is read from the plugin config.
    """
    results = []

    if duration_tolerance is None:
        if options and options.get("duration_tolerance") is not None:
            duration_tolerance = int(options["duration_tolerance"])
        else:
            duration_tolerance = _config_int(
                get_plugin_api(), "betterlyrics_duration_tolerance", 5
            )
    if prefer_synced is None:
        if options and "prefer_synced" in options:
            prefer_synced = bool(options["prefer_synced"])
        else:
            prefer_synced = bool(config["betterlyrics_prefer_synced"])

    pref = source_preference or "all"
    if pref not in ("all", "lrcred", "unison", "lyricsplus", "boidu", "lrclib"):
        pref = "all"

    if pref in ("all", "lrcred"):
        found = _call_provider(
            "lrcred", lambda: search_lrcred(title, artist, album, isrc, query)
        )
        results.extend(found)
        log.info(f"{PLUGIN_NAME}: provider lrcred returned {len(found)} candidate(s)")

    if pref in ("all", "unison"):
        found = _call_provider(
            "unison", lambda: search_unison(title, artist, album, duration, query)
        )
        results.extend(found)
        log.info(f"{PLUGIN_NAME}: provider unison returned {len(found)} candidate(s)")

    target = {
        "title": title,
        "artist": artist,
        "album": album,
        "duration": duration,
        "isrc": isrc,
        "query": query,
    }

    def option(name: str) -> bool:
        if options is not None and name in options:
            return bool(options[name])
        return bool(config[f"betterlyrics_{name}"])

    optional = {
        "lyricsplus": option("enable_lyricsplus"),
        "boidu": option("enable_boidu"),
        "lrclib": option("enable_lrclib"),
    }
    if options is not None and "api_key" in options:
        api_key = options["api_key"]
    else:
        api_key = config["betterlyrics_api_key"]
    requested = list(optional) if pref == "all" else ([pref] if pref in optional else [])
    for provider_id in requested:
        if not optional[provider_id] and pref == "all":
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
                api_key=api_key if provider_id == "boidu" else "",
            )
        except Exception as error:
            log.warning(
                f"{PLUGIN_NAME}: provider {provider_id} failed: {type(error).__name__}"
            )
            result = {}
        if not isinstance(result, dict):
            log.warning(f"{PLUGIN_NAME}: provider {provider_id} returned an invalid result")
            result = {}
        status = result.get("status")
        found = result.get("candidates", []) if status == "hit" else []
        if not isinstance(found, list):
            found = []
        results.extend(found)
        # Provider messages may contain response bodies or credential-bearing
        # URLs. Log bounded status codes and counts instead.
        if status not in ("hit", "not_found", "auth_required", "error", "rate_limited", "unavailable"):
            status = "failed"
        log.info(f"{PLUGIN_NAME}: provider {provider_id}: {status}; {len(found)} candidate(s)")

    for item in results:
        item["score"] = calculate_match_score(
            title,
            artist,
            album,
            duration,
            isrc,
            item,
            duration_tolerance=duration_tolerance,
        )

    # The old providers predate the standalone matcher and may not carry an
    # explicit verification flag. Their result metadata is still provider data;
    # optional providers are stricter and set this flag themselves.
    ranked = rank_candidates(
        {"title": title, "artist": artist, "album": album, "duration": duration, "isrc": isrc},
        results,
        duration_tolerance,
    )

    ranked.sort(
        key=lambda x: (
            x.get("score", 0.0),
            1 if prefer_synced and x.get("timing_type") in SYNCED_TIMING_TYPES else 0,
        ),
        reverse=True,
    )
    return ranked


def resolve_lyrics_bundle(candidate: dict, clean_words: bool | None = None) -> dict:
    """Downloads and prepares both TTML and LRC representations for a chosen candidate.

    ``clean_words`` lets background workers pass a config snapshot instead of
    reading plugin config from a non-GUI thread.
    """
    source = candidate.get("source", "")
    raw_content = None

    provider_id = candidate.get("provider_id") or candidate.get("source", "")
    if provider_id == "lyricsplus" and isinstance(candidate.get("payload"), dict):
        document = parse_kpoe(candidate["payload"])
        bundle = make_bundle(document, clean_words=True if clean_words is None else clean_words)
        bundle["source"] = source or "LyricsPlus"
        bundle["provider_id"] = provider_id
        return bundle

    if provider_id in {"boidu", "lrclib"} and isinstance(candidate.get("direct_lyrics"), str):
        raw_content = candidate["direct_lyrics"]

    # Handle Unison Source
    if source == "Unison":
        raw_content = candidate.get("direct_lyrics")
        if not raw_content and candidate.get("id"):
            item_data = fetch_json(f"{UNISON_API_URL}/{candidate['id']}")
            if item_data and item_data.get("success") and item_data.get("data"):
                raw_content = item_data["data"].get("lyrics")
                candidate["format"] = item_data["data"].get("format", candidate.get("format", "ttml"))

    # Handle LRC.red Source
    elif source == "LRC.red":
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
        if not re.fullmatch(r"[A-Z]{2}[A-Z0-9]{3}[0-9]{7}", isrc_value):
            isrc_value = _normalize_isrc(candidate.get("id"))
        if re.fullmatch(r"[A-Z]{2}[A-Z0-9]{3}[0-9]{7}", isrc_value):
            add_url(f"{LRC_RED_BASE_URL}/s/{isrc_value}.ttml")
            add_url(f"{LRC_RED_BASE_URL}/s/{isrc_value}.lrc")

        for lyrics_url in urls:
            raw_content = fetch_text(lyrics_url)
            if raw_content:
                break

    if not raw_content:
        return {"ttml": None, "lrc": None, "is_synced": False, "source": source}

    if clean_words is None:
        clean_words = config["betterlyrics_clean_word_timestamps"]

    document = parse_ttml(raw_content) if is_ttml_content(raw_content) else parse_lrc(raw_content)
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
        for extension in ("ttml", "lrc")
        if config[f"betterlyrics_save_{extension}_file"]
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

    pref_format = config["betterlyrics_format"]  # "ttml", "lrc", "both"
    ttml_content = bundle.get("ttml")
    lrc_content = bundle.get("lrc")

    if pref_format in ("ttml", "both") and ttml_content:
        primary_tag_content = ttml_content
    else:
        primary_tag_content = lrc_content or ttml_content

    if not primary_tag_content:
        return False

    values = {"lyrics": primary_tag_content, "ttml": ttml_content,
              "syncedlyrics": lrc_content or ttml_content}
    tags = {tag: values[tag] for tag in _enabled_lyric_tags() if values[tag]}
    counts = {"tags": 0, "sidecars": 0, "protected": 0, "unchanged": 0}

    def same_value(metadata, tag, value):
        getall = getattr(metadata, "getall", None)
        return getall(tag) == [value] if callable(getall) else metadata.get(tag) == value

    def apply_target(target, sidecars=()):
        metadata = target.metadata
        overwrite = bool(config["betterlyrics_auto_overwrite"])
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
            content = bundle.get(extension)
            if not content:
                continue
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
                # Exclusive creation also protects a sidecar created while the
                # lookup was in flight, without copying it into metadata.
                with open(path, "w" if overwrite else "x", encoding="utf-8") as handle:
                    handle.write(content)
                counts["sidecars"] += 1
                log.info(f"{PLUGIN_NAME}: Saved {extension.upper()} file to {path}")
            except FileExistsError:
                counts["protected"] += 1
            except Exception as err:
                log.error(f"{PLUGIN_NAME}: Failed to write {extension.upper()} file {path}: {err}")
        return changed

    embedded = False
    for file in file_list:
        try:
            if apply_target(file, _enabled_sidecars(file)):
                embedded = True
                if changed_files is not None:
                    changed_files.append(file)
        except Exception as err:
            # One unreadable/locked file must not discard lyrics for the rest of
            # the selection or leave the save guard stuck.
            log.error(f"{PLUGIN_NAME}: Failed to apply lyrics to a file: {err}")
    if track is not None and hasattr(track, "metadata") and (embedded or not file_list):
        try:
            apply_target(track)
        except Exception as err:
            log.error(f"{PLUGIN_NAME}: Failed to apply lyrics to track metadata: {err}")

    changed = bool(counts["tags"] or counts["sidecars"])
    log.info(
        f"{PLUGIN_NAME}: Lyrics apply {'staged' if changed else 'no-op'}: "
        f"{counts['tags']} tag(s) changed, {counts['sidecars']} sidecar(s) written, "
        f"{counts['protected']} destination(s) protected, {counts['unchanged']} unchanged"
    )
    return changed


# ---------------------------------------------------------------------------
# Interactive Search Table Dialog
# ---------------------------------------------------------------------------
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
        candidate = item.data(Qt.ItemDataRole.UserRole)
        return candidate if isinstance(candidate, dict) else None

    def populate_table(items: list[dict]):
        table.setSortingEnabled(False)
        table.setRowCount(0)
        table.setRowCount(len(items))

        for row, item in enumerate(items):
            idx_item = QtWidgets.QTableWidgetItem()
            idx_item.setTextAlignment(ALIGN_CENTER)
            idx_item.setData(Qt.ItemDataRole.EditRole, row + 1)
            idx_item.setData(Qt.ItemDataRole.UserRole, item)
            table.setItem(row, 0, idx_item)

            timing = item.get("timing_type", "none").lower()
            is_synced = timing in ("word", "line", "richsync", "linesync")
            sync_label = "TTML (Word)" if timing in ("word", "richsync") else ("TTML (Line)" if is_synced else "Plain")
            if timing == "unknown":
                sync_label = "Unknown (preview to check)"

            values = [
                item.get("title") or "?",
                item.get("artist") or "?",
                item.get("album") or "-",
                format_duration(item.get("duration", 0)),
                sync_label,
                item.get("source", "Better Lyrics"),
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
        current_results = query_all_sources(
            title=title,
            artist=artist,
            album=album,
            duration=duration,
            isrc=isrc,
            query=query_text,
            source_preference=config["betterlyrics_source"],
        )
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
        bundle = resolve_lyrics_bundle(candidate)
        ttml_data = bundle.get("ttml")
        lrc_data = bundle.get("lrc")

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
def _extract_track_query(metadata, track) -> tuple[str, str, str, str, int]:
    """Reads (title, artist, album, isrc, length) from Picard metadata/track."""
    title = str(metadata.get("title", "") or "").strip()
    artist = str(metadata.get("artist", "") or "").strip()
    album_name = str(metadata.get("album", "") or "").strip()
    isrc = str(metadata.get("isrc", "") or "").strip()

    length = 0
    if metadata.get("~length"):
        length = parse_duration(str(metadata["~length"]))
    elif track:
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


def _find_best_bundle(
    title: str,
    artist: str,
    album_name: str,
    length: int,
    isrc: str,
    source_pref: str,
    clean_words: bool,
    tolerance: int,
    prefer_synced: bool,
    options: dict | None = None,
) -> dict | None:
    """Worker-thread body: query providers, pick the best match, resolve lyrics.

    Pure network work only - never touches Qt/Picard objects, so it is safe to
    run off the GUI thread.  ``options`` is a snapshot captured on the GUI
    thread, so provider toggles and credentials are never read from Picard's
    configuration object off-thread.
    """
    target = {
        "title": title,
        "artist": artist,
        "album": album_name,
        "duration": length,
        "isrc": isrc,
    }
    cache_key = track_key(target) if _LYRICS_STORE is not None else None
    if cache_key:
        try:
            cached = _LYRICS_STORE.get(cache_key)
        except Exception as e:
            log.debug(f"{PLUGIN_NAME}: lyrics cache read failed: {e}")
            cached = None
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
            options=options,
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
        if provider not in ("lrcred", "unison", "lyricsplus", "boidu", "lrclib"):
            provider = "legacy/unknown"
        log.info(
            f"{PLUGIN_NAME}: Candidate {index} ({provider}) for \"{title}\" decision: "
            f"{'eligible' if accepted else 'rejected'}; reason={reason}"
        )
    log.info(f"{PLUGIN_NAME}: Automatic lookup: {len(candidates)} candidate(s), {eligible} eligible")
    identity_group = choose_identity_group(candidates= candidates, target=target, tolerance=tolerance)
    if not identity_group:
        log.warning(f"{PLUGIN_NAME}: ambiguous or unverified lyrics matches for \"{title}\"")
        return None
    log.info(
        f"{PLUGIN_NAME}: Selected identity group for \"{title}\": "
        f"{len(identity_group)} candidate(s); reason={identity_group[0].get('match_reason')}"
    )

    # A high-scoring result is not useful if its lyrics endpoint is empty or
    # unavailable.  Try the next compatible candidate instead of failing the
    # whole track on the first provider result.
    for candidate in identity_group:
        if not _candidate_is_compatible(title, artist, length, isrc, candidate, tolerance):
            log.info(f"{PLUGIN_NAME}: Candidate skipped: incompatible recording")
            continue
        try:
            bundle = resolve_lyrics_bundle(candidate, clean_words=clean_words)
        except Exception as e:
            log.warning(
                f"{PLUGIN_NAME}: failed to resolve lyrics candidate "
                f"\"{candidate.get('title')}\" for \"{title}\": {type(e).__name__}"
            )
            continue

        if bundle.get("ttml") or bundle.get("lrc"):
            log.info(f"{PLUGIN_NAME}: Candidate resolved: usable lyrics bundle")
            if cache_key:
                try:
                    _LYRICS_STORE.put(cache_key, bundle)
                except Exception as e:
                    log.debug(f"{PLUGIN_NAME}: lyrics cache write failed: {e}")
            return bundle
        log.info(f"{PLUGIN_NAME}: Candidate skipped: empty lyrics bundle")

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


def fetch_and_apply_lyrics(
    method: str,
    track: Track | None,
    files: list[File],
    metadata: Metadata,
    interactive: bool = False,
):
    """Main entry point for finding, scoring, and saving TTML/LRC lyrics.

    ``method == "search"`` runs synchronously on the GUI thread (it drives a
    modal dialog). All automatic paths (``"get"``/``"load"``/``"save"``) are
    handed to the background :class:`FetchQueue`, keeping the UI responsive and
    pacing outbound requests.
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
        # Interactive flow: the modal dialog must run on the GUI thread.
        log.info(f"{PLUGIN_NAME}: Searching lyrics for \"{title}\" by \"{artist}\" ({format_duration(length)})")
        candidates = query_all_sources(
            title=title,
            artist=artist,
            album=album_name,
            duration=length,
            isrc=isrc,
            source_preference=config["betterlyrics_source"],
            duration_tolerance=int(config["betterlyrics_duration_tolerance"]),
            prefer_synced=bool(config["betterlyrics_prefer_synced"]),
        )
        parent_win = QtWidgets.QApplication.activeWindow()
        selected_candidate = show_search_dialog(
            parent_win, title, artist, album_name, length, isrc, candidates
        )
        if not selected_candidate:
            log.info(f"{PLUGIN_NAME}: User cancelled lyrics selection")
            return

        bundle = resolve_lyrics_bundle(selected_candidate)
        if not bundle.get("ttml") and not bundle.get("lrc"):
            log.warning(f"{PLUGIN_NAME}: Empty lyrics received for \"{title}\"")
            return
        save_lyrics_to_files(files, bundle, track=track, interactive=interactive)
        return True

    # Automatic flow: enqueue on the background fetch queue (never blocks the UI).
    if is_plugin_disabled():
        log.debug(
            f"{PLUGIN_NAME}: ignored automatic lyrics lookup for \"{title}\": plugin disabled"
        )
        return False
    if not _has_writable_lyrics_destination(files, track):
        log.info(
            f"{PLUGIN_NAME}: Skipped lyrics lookup for \"{title}\": "
            "all enabled destinations are protected or no destinations are enabled"
        )
        return False
    if not _ensure_queue_ready():
        return False
    source_pref = config["betterlyrics_source"]
    clean_words = bool(config["betterlyrics_clean_word_timestamps"])
    tolerance = int(config["betterlyrics_duration_tolerance"])
    prefer_synced = bool(config["betterlyrics_prefer_synced"])
    # Capture every setting the worker needs here, on the GUI thread.  The
    # worker must never read Picard's config object while it is being edited.
    worker_options = {
        "duration_tolerance": tolerance,
        "prefer_synced": prefer_synced,
        "enable_lyricsplus": bool(config["betterlyrics_enable_lyricsplus"]),
        "enable_boidu": bool(config["betterlyrics_enable_boidu"]),
        "enable_lrclib": bool(config["betterlyrics_enable_lrclib"]),
        "api_key": config["betterlyrics_api_key"],
    }

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
            worker_options,
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

    if is_plugin_disabled():
        return
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

    if is_plugin_disabled():
        return
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
def clean_orphaned_lyrics_files():
    """Recursively scans a user-selected folder and deletes .ttml/.lrc files without matching audio."""
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

        orphaned_count = 0
        for dirpath, _, filenames in os.walk(root_dir):
            lyric_files = [f for f in filenames if f.lower().endswith((".lrc", ".ttml"))]
            for lfile in lyric_files:
                base_name = os.path.splitext(lfile)[0]
                audio_exists = any(
                    os.path.exists(os.path.join(dirpath, base_name + ext))
                    for ext in AUDIO_EXTENSIONS
                )
                if not audio_exists:
                    try:
                        os.remove(os.path.join(dirpath, lfile))
                        orphaned_count += 1
                    except Exception as e:
                        log.error(f"{PLUGIN_NAME}: Failed to remove orphaned {lfile}: {e}")

        QtWidgets.QMessageBox.information(
            parent,
            "Cleanup Complete",
            f"Removed {orphaned_count} orphaned lyrics file{'s' if orphaned_count != 1 else ''}.",
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
            "Fetches rich TTML (Timed Text Markup Language) and LRC lyrics from LRC.red and Unison.<br/>"
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
        self.plain_as_txt = QtWidgets.QCheckBox("Save plain unsynchronized lyrics as .txt", self)

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
        self.source_combo.addItem("All Sources (Smart Hybrid: LRC.red + Unison)", "all")
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
        rate_label = QtWidgets.QLabel("Minimum delay between requests (ms):", self)
        self.rate_spin = QtWidgets.QSpinBox(self)
        self.rate_spin.setRange(0, 5000)
        self.rate_spin.setSingleStep(50)
        self.rate_spin.setValue(200)
        self.rate_spin.setToolTip(
            "Minimum spacing between outbound HTTP requests across all workers.\n"
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

        self.duration_spin.setValue(
            _bounded_int(cfg.get("betterlyrics_duration_tolerance", 5), 5, 1, 30)
        )
        self.api_key_input.setText(str(cfg.get("betterlyrics_api_key", "")))
        self.workers_spin.setValue(
            _bounded_int(cfg.get("betterlyrics_max_workers", 3), 3, 1, 8)
        )
        self.rate_spin.setValue(
            _bounded_int(cfg.get("betterlyrics_rate_limit_ms", 200), 200, 0, 5000)
        )

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

        # Apply the request pacer immediately; worker count applies on next reload.
        try:
            if _RATE_LIMITER is not None:
                _RATE_LIMITER.set_interval(self.rate_spin.value() / 1000.0)
            if FETCH_QUEUE is not None:
                FETCH_QUEUE.configure(self.workers_spin.value())
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Plugin v3 Registration Hooks
# ---------------------------------------------------------------------------
def enable(api: PluginApi) -> None:
    """Entry point called when the plugin is enabled in MusicBrainz Picard 3.0+."""
    global CURRENT_PLUGIN_API, _MAIN_INVOKER, _PROVIDER_HTTP, _LYRICS_STORE
    global _ENABLED, _DISABLED
    if _ENABLED and CURRENT_PLUGIN_API is not None:
        # Picard normally disables before re-enabling.  Ignoring a duplicate
        # enable keeps threads, timers and context-menu registrations unique.
        log.warning(f"{PLUGIN_NAME}: enable() called while already enabled; ignoring")
        return
    CURRENT_PLUGIN_API = api
    _DISABLED = False
    _SHUTDOWN_EVENT.clear()

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

    # Prepare the stability layer: caches, request pacer and background queue.
    _init_caches()
    rate_limit = _config_int(api, "betterlyrics_rate_limit_ms", 200) / 1000.0
    _RATE_LIMITER.set_interval(rate_limit)
    try:
        ssl_context = _get_ssl_context()
    except Exception:
        # A rejected/missing CA bundle must not prevent the plugin from loading;
        # the provider client can still attempt its own verified context.
        ssl_context = None
    _PROVIDER_HTTP = HttpClient(
        timeout=12.0,
        retries=2,
        min_interval=rate_limit,
        ssl_context=ssl_context,
    )
    try:
        _LYRICS_STORE = LyricsStore(_lyrics_store_path())
    except Exception as e:
        _LYRICS_STORE = None
        log.warning(f"{PLUGIN_NAME}: persistent lyrics cache unavailable: {e}")
    FETCH_QUEUE.configure(_config_int(api, "betterlyrics_max_workers", 3))
    if _MAIN_INVOKER is not None:
        _MAIN_INVOKER.stop()
    # Picard may call enable() from its install/update worker.  The dispatcher
    # moves itself to the GUI thread before creating its QTimer, so results are
    # still delivered immediately after an install or update.
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
    _ENABLED = True


def disable() -> None:
    """Cleanup hook called when the plugin is disabled in Picard 3.0+.

    Picard removes ``api.register_*`` registrations automatically; this method
    only tears down what Picard does not own: worker threads, the GUI callback
    timer, HTTP clients and the persistent cache.  It must stay fast, because it
    runs on the GUI thread while Picard is closing the plugin.
    """
    global CURRENT_PLUGIN_API, _MAIN_INVOKER, _PROVIDER_HTTP, _LYRICS_STORE
    global _ENABLED, _DISABLED
    _DISABLED = True
    _ENABLED = False
    # Wake any paced/backoff wait in the legacy HTTP path before joining.
    _SHUTDOWN_EVENT.set()

    try:
        uninstall_menu_lifetime_workaround()
    except Exception as error:
        log.warning(
            f"{PLUGIN_NAME}: Qt menu lifetime workaround cleanup failed (%s)",
            type(error).__name__,
        )

    # Closing the provider client cancels in-flight provider requests and makes
    # the queue join below return quickly.
    if _PROVIDER_HTTP is not None:
        try:
            _PROVIDER_HTTP.close()
        except Exception as error:
            log.warning(f"{PLUGIN_NAME}: provider client cleanup failed: {error}")
        _PROVIDER_HTTP = None
    if FETCH_QUEUE is not None:
        FETCH_QUEUE.stop()
    if _MAIN_INVOKER is not None:
        _MAIN_INVOKER.stop()
        _MAIN_INVOKER = None
    if FETCH_QUEUE is not None:
        FETCH_QUEUE.set_invoker(None)
    if _LYRICS_STORE is not None:
        try:
            _LYRICS_STORE.close()
        except Exception:
            pass
        _LYRICS_STORE = None
    if _HTTP_TEXT_CACHE is not None:
        _HTTP_TEXT_CACHE.clear()
    if _JSON_CACHE is not None:
        _JSON_CACHE.clear()
    CURRENT_PLUGIN_API = None
