# -*- coding: utf-8 -*-
#
# Better Lyrics Plugin for MusicBrainz Picard 3.0+
#
# Fetches synchronized (word-level and line-level) TTML and LRC lyrics from the
# Better Lyrics ecosystem (LRC.red, Unison crowdsourced database & Better Lyrics API).
#
# Designed specifically for MusicBrainz Picard 3.0 Plugin v3 Architecture.
#

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
from urllib.request import Request, urlopen
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
    "betterlyrics_source": "all",  # "all", "lrcred", "unison"
    "betterlyrics_duration_tolerance": 5,  # seconds
    "betterlyrics_api_key": "",
    # Stability / anti-abuse tuning (background fetch queue)
    "betterlyrics_max_workers": 3,  # concurrent background fetch jobs
    "betterlyrics_rate_limit_ms": 200,  # min delay between outbound HTTP requests
}

# Service URLs
LRC_RED_API_URL = "https://lrc.red/api/v1"
UNISON_API_URL = "https://unison.betterlyrics.org/lyrics"
UNISON_SEARCH_URL = "https://unison.betterlyrics.org/lyrics/search"
BETTER_LYRICS_API_URL = "https://api.betterlyrics.org/getLyrics"

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

# Set to track files currently being saved to avoid re-triggering hooks
files_processing: set = set()

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
        if api and hasattr(api, "plugin_config"):
            try:
                val = api.plugin_config.get(key)
                if val is not None and val != "":
                    return val
            except Exception:
                pass
        return PLUGIN_OPTIONS.get(key, default)

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
            value = api.plugin_config.get(key)
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
        _MAIN_INVOKER = MainThreadInvoker()
    FETCH_QUEUE.set_invoker(_MAIN_INVOKER)
    if not FETCH_QUEUE.is_running:
        FETCH_QUEUE.start()


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
    """Parses TTML timestamp format (mm:ss.xxx, hh:mm:ss.xxx, or xxx.xxxs) into (minutes, seconds)."""
    clean_str = time_str.strip().rstrip("s")
    parts = clean_str.split(":")
    if len(parts) == 2:
        mm = int(parts[0])
        ss = float(parts[1])
    elif len(parts) == 3:
        hh = int(parts[0])
        mm = int(parts[1]) + hh * 60
        ss = float(parts[2])
    else:
        sec_val = float(clean_str)
        mm = int(sec_val // 60)
        ss = sec_val % 60
    return mm, ss


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
            mm, ss = parse_ttml_time(begin)
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
                    s_mm, s_ss = parse_ttml_time(s_begin)
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

    def acquire(self) -> None:
        """Blocks until the next request slot is available, then claims it."""
        if self._min_interval <= 0:
            return
        with self._lock:
            now = time.monotonic()
            wait = self._next_allowed - now
            if wait > 0:
                time.sleep(wait)
                now = time.monotonic()
            self._next_allowed = now + self._min_interval


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
# HTTP Networking Helpers with SSL Fallback
# ---------------------------------------------------------------------------
def _get_ssl_context():
    """Returns an SSL context, falling back to unverified if system certs are missing."""
    try:
        return ssl.create_default_context()
    except Exception:
        return ssl._create_unverified_context()


def _build_url(url: str, params: dict | None) -> str:
    """Appends query parameters to a URL when present."""
    if not params:
        return url
    query = urlencode(params)
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}{query}"


def _http_get(req, timeout: int):
    """Performs a single HTTP GET. Returns (status, body); raises on network error."""
    try:
        resp = urlopen(req, timeout=timeout, context=_get_ssl_context())
    except Exception:
        # Retry once with an unverified context to tolerate missing CA bundles.
        resp = urlopen(req, timeout=timeout, context=ssl._create_unverified_context())
    with resp:
        return resp.status, resp.read().decode("utf-8", errors="replace")


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
        _RATE_LIMITER.acquire()
        try:
            status, body = _http_get(req, timeout)
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
        time.sleep(delay)

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
class MainThreadInvoker(QtCore.QObject):
    """Marshals callables from worker threads onto the Qt main thread.

    A queued signal/slot connection guarantees the slot runs in the thread that
    owns this object (the GUI thread), where Picard's data model (tracks, files,
    metadata) must be mutated.
    """

    _invoke = QtCore.pyqtSignal(object)

    def __init__(self):
        super().__init__()
        self._invoke.connect(self._run, Qt.ConnectionType.QueuedConnection)

    def _run(self, fn):
        try:
            fn()
        except Exception as e:
            log.error(f"{PLUGIN_NAME}: error applying queued result: {e}", exc_info=True)

    def post(self, fn) -> None:
        self._invoke.emit(fn)


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
      * graceful start/stop,
      * results delivered back to the GUI thread via :class:`MainThreadInvoker`.
    """

    def __init__(self, max_workers: int = 3):
        self._max_workers = max(1, int(max_workers))
        self._queue: "queue.Queue" = queue.Queue()
        self._workers: list = []
        self._lock = threading.Lock()
        self._pending_keys: set = set()
        self._running = False
        self._invoker = None

    def configure(self, max_workers: int | None = None) -> None:
        if max_workers is not None:
            self._max_workers = max(1, int(max_workers))

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

    def start(self) -> None:
        with self._lock:
            if self._running:
                return
            self._running = True
            self._workers = [
                threading.Thread(
                    target=self._worker_loop,
                    name=f"BetterLyricsFetch-{i}",
                    daemon=True,
                )
                for i in range(self._max_workers)
            ]
        for worker in self._workers:
            worker.start()
        log.debug(f"{PLUGIN_NAME}: fetch queue started ({self._max_workers} worker(s))")

    def stop(self) -> None:
        """Signals workers to drain the queue and exit, then joins them."""
        with self._lock:
            if not self._running:
                return
            self._running = False
            workers = list(self._workers)
            self._workers = []
        for _ in workers:
            self._queue.put(None)  # poison pill enqueued after pending work
        for worker in workers:
            worker.join(timeout=5.0)
        with self._lock:
            self._pending_keys.clear()
        log.debug(f"{PLUGIN_NAME}: fetch queue stopped")

    def submit(self, job_fn, apply_fn=None, key=None) -> bool:
        """Enqueues a job. Returns ``False`` if an identical job is already pending."""
        if key is not None:
            with self._lock:
                if key in self._pending_keys:
                    return False
                self._pending_keys.add(key)
        self._queue.put(_FetchTask(job_fn, apply_fn, key))
        return True

    def _release_key(self, key) -> None:
        if key is None:
            return
        with self._lock:
            self._pending_keys.discard(key)

    def _worker_loop(self) -> None:
        while True:
            task = self._queue.get()
            try:
                if task is None:
                    return
                try:
                    result = task.job_fn()
                except Exception as e:
                    log.warning(f"{PLUGIN_NAME}: background fetch job failed: {e}")
                    result = None
                if task.apply_fn is not None:
                    invoker = self._invoker
                    if invoker is not None:
                        invoker.post(partial(task.apply_fn, result))
                    else:
                        log.debug(f"{PLUGIN_NAME}: no main-thread invoker; result dropped")
            finally:
                self._release_key(getattr(task, "key", None))
                self._queue.task_done()


# ---------------------------------------------------------------------------
# Match Scoring Algorithm
# ---------------------------------------------------------------------------
def calculate_match_score(
    target_title: str,
    target_artist: str,
    target_album: str | None,
    target_duration: int,
    target_isrc: str | None,
    item: dict,
) -> float:
    """Calculates a match confidence score between 0.0 and 1.0."""
    item_title = item.get("title", "")
    item_artist = item.get("artist", "")
    item_album = item.get("album", "")
    item_duration = item.get("duration", 0)
    item_isrc = item.get("isrc")
    item_timing = item.get("timing_type", "none")

    score = 0.0

    # 1. ISRC Exact Match (Massive confidence boost)
    if target_isrc and item_isrc and target_isrc.strip().upper() == item_isrc.strip().upper():
        score += 0.50

    # 2. Title Similarity
    if target_title and item_title:
        t_ratio = SequenceMatcher(
            None, target_title.lower().strip(), item_title.lower().strip()
        ).ratio()
        score += t_ratio * 0.40
    else:
        score += 0.20

    # 3. Artist Similarity
    if target_artist and item_artist:
        a_ratio = SequenceMatcher(
            None, target_artist.lower().strip(), item_artist.lower().strip()
        ).ratio()
        score += a_ratio * 0.30
    else:
        score += 0.15

    # 4. Album Similarity
    if target_album and item_album:
        al_ratio = SequenceMatcher(
            None, target_album.lower().strip(), item_album.lower().strip()
        ).ratio()
        score += al_ratio * 0.10

    # 5. Duration Difference Match
    if target_duration > 0 and item_duration > 0:
        diff = abs(target_duration - item_duration)
        if diff <= 2:
            score += 0.20
        elif diff <= 5:
            score += 0.15
        elif diff <= 10:
            score += 0.05
        elif diff > 30:
            score -= 0.15

    # 6. Prefer Synced / TTML Lyrics
    if item_timing in ("word", "line", "richsync", "linesync"):
        score += 0.05

    return max(0.0, min(1.0, score))


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
    """Searches the LRC.red API (Better Lyrics primary catalog)."""
    candidates = []
    seen_ids = set()

    def add_res(res):
        c_id = res.get("id") or res.get("isrc") or f"{res.get('track_name')}_{res.get('artist_name')}"
        if c_id in seen_ids:
            return
        seen_ids.add(c_id)
        candidates.append({
            "id": res.get("id"),
            "title": res.get("track_name", ""),
            "artist": res.get("artist_name", ""),
            "album": res.get("album_name", ""),
            "duration": res.get("duration", 0),
            "isrc": res.get("isrc", ""),
            "timing_type": res.get("timing_type", "none"),
            "lyrics_url": res.get("lyricsUrl", ""),
            "source": "LRC.red",
            "format": "ttml",
            "raw": res,
        })

    # 1. Search by ISRC
    if isrc:
        data = fetch_json(LRC_RED_API_URL, {"isrc": isrc.strip()})
        if data and isinstance(data, dict):
            for res in data.get("results", []):
                add_res(res)
        if candidates:
            return candidates

    # 2. Search by Track + Artist
    if title and artist:
        data = fetch_json(LRC_RED_API_URL, {"track": title, "artist": artist})
        if data and isinstance(data, dict):
            for res in data.get("results", []):
                add_res(res)

    # 3. Search by Free-form Query
    search_q = query or f"{artist} {title}".strip()
    if search_q:
        data = fetch_json(LRC_RED_API_URL, {"q": search_q})
        if data and isinstance(data, dict):
            for res in data.get("results", []):
                add_res(res)

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
        if data and isinstance(data, dict) and data.get("success"):
            for res in data.get("data", []):
                c_id = res.get("id")
                if c_id in seen_ids:
                    continue
                seen_ids.add(c_id)
                candidates.append({
                    "id": c_id,
                    "title": res.get("song", ""),
                    "artist": res.get("artist", ""),
                    "album": res.get("album", ""),
                    "duration": res.get("duration", 0),
                    "isrc": res.get("isrc", ""),
                    "timing_type": res.get("syncType", "plain"),
                    "format": res.get("format", "ttml"),
                    "source": "Unison",
                    "direct_lyrics": res.get("lyrics"),
                    "raw": res,
                })

    return candidates


def query_all_sources(
    title: str,
    artist: str,
    album: str = "",
    duration: int = 0,
    isrc: str = "",
    query: str = "",
    source_preference: str = "all",
) -> list[dict]:
    """Queries LRC.red, Unison, or both according to user preferences."""
    results = []

    pref = source_preference or "all"
    if pref not in ("all", "lrcred", "unison"):
        pref = "all"

    if pref in ("all", "lrcred"):
        results.extend(search_lrcred(title, artist, album, isrc, query))

    if pref in ("all", "unison"):
        results.extend(search_unison(title, artist, album, duration, query))

    for item in results:
        item["score"] = calculate_match_score(title, artist, album, duration, isrc, item)

    results.sort(key=lambda x: x.get("score", 0.0), reverse=True)
    return results


def resolve_lyrics_bundle(candidate: dict, clean_words: bool | None = None) -> dict:
    """Downloads and prepares both TTML and LRC representations for a chosen candidate.

    ``clean_words`` lets background workers pass a config snapshot instead of
    reading plugin config from a non-GUI thread.
    """
    source = candidate.get("source", "")
    raw_content = None

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
        lyrics_url = candidate.get("lyrics_url", "")
        if not lyrics_url and candidate.get("id"):
            lyrics_url = f"https://lrc.red/s/{candidate['id']}.ttml"

        if lyrics_url:
            raw_content = fetch_text(lyrics_url)
            if not raw_content and lyrics_url.endswith(".ttml"):
                lrc_url = re.sub(r"\.ttml$", ".lrc", lyrics_url)
                raw_content = fetch_text(lrc_url)

    if not raw_content:
        return {"ttml": None, "lrc": None, "is_synced": False, "source": source}

    if clean_words is None:
        clean_words = config["betterlyrics_clean_word_timestamps"]

    if is_ttml_content(raw_content):
        ttml_str = raw_content
        lrc_str = ttml_to_lrc(ttml_str, word_synced=not clean_words)
        if clean_words:
            lrc_str = clean_word_timestamps(lrc_str)
        is_synced = is_lrc_synced(lrc_str)
        return {
            "ttml": ttml_str,
            "lrc": lrc_str,
            "is_synced": is_synced,
            "source": source,
        }
    else:
        lrc_str = clean_word_timestamps(raw_content) if clean_words else raw_content
        is_synced = is_lrc_synced(lrc_str)
        ttml_str = lrc_to_ttml(
            lrc_str,
            title=candidate.get("title", ""),
            artist=candidate.get("artist", "")
        )
        return {
            "ttml": ttml_str,
            "lrc": lrc_str,
            "is_synced": is_synced,
            "source": source,
        }


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


def save_lyrics_to_files(
    file_list: list[File],
    bundle: dict,
    track: Track | None = None,
    interactive: bool = False,
) -> bool:
    """Saves lyrics to the given Picard File objects and sidecar files."""
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

    # Also update track metadata if track is available
    if track and hasattr(track, "metadata"):
        if config["betterlyrics_embed_lyrics"]:
            track.metadata["lyrics"] = primary_tag_content
        if config["betterlyrics_embed_ttml_tag"] and ttml_content:
            track.metadata["ttml"] = ttml_content
        if config["betterlyrics_embed_syncedlyrics"]:
            track.metadata["syncedlyrics"] = lrc_content or ttml_content
        if hasattr(track, "update"):
            track.update()

    if not file_list:
        return True

    for file in file_list:
        full_path = getattr(file, "filename", None)
        if not full_path:
            continue

        dirname = os.path.dirname(full_path)
        base_name = os.path.splitext(os.path.basename(full_path))[0]

        ttml_sidecar = os.path.join(dirname, base_name + ".ttml")
        lrc_sidecar = os.path.join(dirname, base_name + ".lrc")

        has_tag_lyrics = bool(file.metadata.get("lyrics")) or bool(file.metadata.get("ttml"))
        has_sidecar = os.path.exists(ttml_sidecar) or os.path.exists(lrc_sidecar)

        if (
            (has_tag_lyrics or has_sidecar)
            and not config["betterlyrics_auto_overwrite"]
            and interactive
        ):
            title = "Overwrite existing lyrics?"
            preview_snippet = truncate_text(primary_tag_content, 5, 45)
            desc = (
                f"Lyrics already exist for \"{file.metadata.get('title', base_name)}\".\n\n"
                f"Preview of new lyrics:\n{preview_snippet}\n\n"
                "Do you want to overwrite?"
            )
            parent = getattr(file, "tagger", None)
            parent_window = getattr(parent, "window", None) if parent else None
            if not confirm_replace(parent_window, title, desc):
                return False

        # 1. Embed Metadata Tags
        if config["betterlyrics_embed_lyrics"]:
            file.metadata["lyrics"] = primary_tag_content

        if config["betterlyrics_embed_ttml_tag"] and ttml_content:
            file.metadata["ttml"] = ttml_content

        if config["betterlyrics_embed_syncedlyrics"]:
            file.metadata["syncedlyrics"] = lrc_content or ttml_content

        # Crucial for Picard: Notify that file has pending metadata changes
        if hasattr(file, "update"):
            file.update()

        # 2. Save Sidecar Files (.ttml / .lrc)
        save_ttml = config["betterlyrics_save_ttml_file"] or pref_format in ("ttml", "both")
        save_lrc = config["betterlyrics_save_lrc_file"] or pref_format in ("lrc", "both")

        if save_ttml and ttml_content:
            try:
                with open(ttml_sidecar, "w", encoding="utf-8") as f:
                    f.write(ttml_content)
                log.info(f"{PLUGIN_NAME}: Saved TTML file to {ttml_sidecar}")
            except Exception as err:
                log.error(f"{PLUGIN_NAME}: Failed to write TTML file {ttml_sidecar}: {err}")

        if save_lrc and lrc_content:
            try:
                with open(lrc_sidecar, "w", encoding="utf-8") as f:
                    f.write(lrc_content)
                log.info(f"{PLUGIN_NAME}: Saved LRC file to {lrc_sidecar}")
            except Exception as err:
                log.error(f"{PLUGIN_NAME}: Failed to write LRC file {lrc_sidecar}: {err}")

    return True


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

    def populate_table(items: list[dict]):
        table.setSortingEnabled(False)
        table.setRowCount(0)
        table.setRowCount(len(items))

        for row, item in enumerate(items):
            idx_item = QtWidgets.QTableWidgetItem()
            idx_item.setTextAlignment(ALIGN_CENTER)
            idx_item.setData(Qt.ItemDataRole.EditRole, row + 1)
            table.setItem(row, 0, idx_item)

            timing = item.get("timing_type", "none").lower()
            is_synced = timing in ("word", "line", "richsync", "linesync")
            sync_label = "TTML (Word)" if timing in ("word", "richsync") else ("TTML (Line)" if is_synced else "Plain")

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
            title=query_text,
            artist="",
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
        if row < 0 or row >= len(current_results):
            return
        candidate = current_results[row]
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
        if 0 <= selected_row < len(current_results):
            return current_results[selected_row]
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
) -> dict | None:
    """Worker-thread body: query providers, pick the best match, resolve lyrics.

    Pure network work only - never touches Qt/Picard objects, so it is safe to
    run off the GUI thread.
    """
    try:
        candidates = query_all_sources(
            title=title,
            artist=artist,
            album=album_name,
            duration=length,
            isrc=isrc,
            source_preference=source_pref,
        )
    except Exception as e:
        log.warning(f"{PLUGIN_NAME}: search failed for \"{title}\": {e}")
        return None

    if not candidates:
        return None

    best = candidates[0]
    if length > 0 and best.get("duration", 0) > 0:
        diff = abs(length - best["duration"])
        # If duration mismatch is large and title/artist score is weak, skip.
        if diff > (tolerance + 5) and best.get("score", 0.0) < 0.50:
            log.warning(
                f"{PLUGIN_NAME}: Best match \"{best.get('title')}\" duration difference "
                f"({diff}s) exceeds tolerance ({tolerance}s) with low score "
                f"({best.get('score', 0):.2f})"
            )
            return None

    try:
        bundle = resolve_lyrics_bundle(best, clean_words=clean_words)
    except Exception as e:
        log.warning(f"{PLUGIN_NAME}: failed to resolve lyrics for \"{title}\": {e}")
        return None

    if not bundle.get("ttml") and not bundle.get("lrc"):
        return None
    return bundle


def _apply_auto_bundle(title: str, files, track, bundle: dict | None) -> None:
    """Main-thread consumer: writes a resolved bundle into Picard's data model."""
    if not bundle:
        log.warning(f"{PLUGIN_NAME}: No lyrics found for \"{title}\"")
        return
    try:
        saved = save_lyrics_to_files(files, bundle, track=track, interactive=False)
        if saved:
            log.info(f"{PLUGIN_NAME}: Successfully matched and saved TTML/LRC lyrics for \"{title}\"")
    except Exception as e:
        log.error(f"{PLUGIN_NAME}: Failed to apply lyrics for \"{title}\": {e}", exc_info=True)


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
        return

    if config["betterlyrics_ignore_instrumental"]:
        if "(instrumental)" in title.lower() or "[instrumental]" in title.lower():
            log.info(f"{PLUGIN_NAME}: Skipping instrumental track: {title}")
            return

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
        return

    # Automatic flow: enqueue on the background fetch queue (never blocks the UI).
    _ensure_queue_ready()
    source_pref = config["betterlyrics_source"]
    clean_words = bool(config["betterlyrics_clean_word_timestamps"])
    tolerance = int(config["betterlyrics_duration_tolerance"])

    submitted = FETCH_QUEUE.submit(
        job_fn=partial(
            _find_best_bundle, title, artist, album_name, length, isrc, source_pref, clean_words, tolerance
        ),
        apply_fn=partial(_apply_auto_bundle, title, files, track),
        key=_dedup_key(track, files, method),
    )
    if submitted:
        log.info(
            f"{PLUGIN_NAME}: Queued lyrics search for \"{title}\" by \"{artist}\" "
            f"({format_duration(length)}); {FETCH_QUEUE.pending} job(s) pending"
        )
    else:
        log.debug(f"{PLUGIN_NAME}: duplicate lyrics job for \"{title}\" ignored")


# ---------------------------------------------------------------------------
# Picard 3.0 Processor Hooks
# ---------------------------------------------------------------------------
def on_file_added(api: PluginApi, track: Track, file: File) -> None:
    """Processor hook: runs automatically when a file is added to a track."""
    cfg = api.plugin_config
    if not cfg.get("betterlyrics_get_on_load", True):
        return
    try:
        files = getattr(track, "files", []) or [file]
        metadata = getattr(track, "metadata", None) or getattr(file, "metadata", None)
        if metadata:
            fetch_and_apply_lyrics("load", track, files, metadata, interactive=False)
    except Exception as err:
        api.logger.error("Error in on_file_added: %s", err, exc_info=True)


def on_file_saved(api: PluginApi, file: File) -> None:
    """Processor hook: runs automatically when an audio file is saved."""
    cfg = api.plugin_config
    if not cfg.get("betterlyrics_get_on_save", False):
        return
    if file.filename in files_processing:
        files_processing.discard(file.filename)
        return

    try:
        files_processing.add(file.filename)
        parent = getattr(file, "parent", None)
        track = parent if isinstance(parent, Track) else None
        metadata = getattr(track, "metadata", None) or file.metadata
        fetch_and_apply_lyrics("save", track, [file], metadata, interactive=False)
    except Exception as err:
        api.logger.error("Error in on_file_saved: %s", err, exc_info=True)
    finally:
        files_processing.discard(file.filename)


# ---------------------------------------------------------------------------
# Context Menu Actions Helper & Classes
# ---------------------------------------------------------------------------
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
            parent = getattr(item, "parent", None)
            if isinstance(parent, Track):
                tracks.add(parent)
            else:
                files.append(item)
        elif hasattr(item, "files"):
            for f in getattr(item, "files", []):
                parent = getattr(f, "parent", None)
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
            parent = getattr(file, "parent", None)
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
            parent = getattr(file, "parent", None)
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
        src_h.addWidget(src_label)
        src_h.addWidget(self.source_combo)
        adv_layout.addLayout(src_h)

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
        api = getattr(self, "api", None) or get_plugin_api()
        cfg = api.plugin_config if api else {}

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

        self.duration_spin.setValue(int(cfg.get("betterlyrics_duration_tolerance", 5)))
        self.api_key_input.setText(str(cfg.get("betterlyrics_api_key", "")))
        self.workers_spin.setValue(int(cfg.get("betterlyrics_max_workers", 3)))
        self.rate_spin.setValue(int(cfg.get("betterlyrics_rate_limit_ms", 200)))

    def save(self):
        api = getattr(self, "api", None) or get_plugin_api()
        if not api:
            return
        cfg = api.plugin_config

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
    global CURRENT_PLUGIN_API, _MAIN_INVOKER
    CURRENT_PLUGIN_API = api

    # Register options with their defaults
    for key, default in PLUGIN_OPTIONS.items():
        api.plugin_config.register_option(key, default)

    # Prepare the stability layer: caches, request pacer and background queue.
    _init_caches()
    _RATE_LIMITER.set_interval(_config_int(api, "betterlyrics_rate_limit_ms", 200) / 1000.0)
    FETCH_QUEUE.configure(_config_int(api, "betterlyrics_max_workers", 3))
    if _MAIN_INVOKER is None:
        _MAIN_INVOKER = MainThreadInvoker()
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
    global CURRENT_PLUGIN_API
    if FETCH_QUEUE is not None:
        FETCH_QUEUE.stop()
    if _HTTP_TEXT_CACHE is not None:
        _HTTP_TEXT_CACHE.clear()
    if _JSON_CACHE is not None:
        _JSON_CACHE.clear()
    CURRENT_PLUGIN_API = None
