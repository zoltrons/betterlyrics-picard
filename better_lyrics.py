# -*- coding: utf-8 -*-
#
# Better Lyrics Plugin for MusicBrainz Picard 3.0+ & 2.x
#
# Fetches synchronized (word-level and line-level) TTML and LRC lyrics from the
# Better Lyrics ecosystem (LRC.red, Unison crowdsourced database & Better Lyrics API).
#
# Features:
# - Full compatibility with MusicBrainz Picard 3.0 (PyQt6) and Picard 2.x (PyQt5)
# - First-class TTML Support:
#     * Writes native TTML XML directly into audio file tags (lyrics, ttml, etc.)
#     * Saves standalone .ttml sidecar files alongside audio files
#     * Converts LRC to TTML or TTML to LRC as needed
# - Dual / Hybrid Lyric Sources:
#     * LRC.red API (Primary Better Lyrics database by w4v)
#     * Unison API (Official Better Lyrics crowdsourced community database)
#     * Optional Better Lyrics API key support
# - Word-synced cleaning option: converts <mm:ss.xx> word-by-word timestamps into standard [mm:ss.xx]
#   when exporting LRC, while keeping raw word-level timing intact in TTML
# - Smart track matching with fuzzy title/artist similarity, duration difference checks, and ISRC matching
# - Context menu actions for automated and manual interactive search
# - Interactive search dialog with live search, result list, and lyric preview
# - Automatic fetch on track load and/or file save
# - Metadata tag embedding (lyrics, ttml, and syncedlyrics)
# - Orphaned .lrc and .ttml file cleaner tool
#
# Based on concepts from picard-lrclib and better-lyrics.

from functools import partial
import json
import os
import re
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET
from difflib import SequenceMatcher

# ---------------------------------------------------------------------------
# Qt Compatibility Layer (PyQt6 for Picard 3.0, PyQt5 for Picard 2.x)
# ---------------------------------------------------------------------------
try:
    from PyQt6 import QtCore, QtGui, QtWidgets
    from PyQt6.QtCore import Qt
    from PyQt6.QtNetwork import QNetworkRequest
    IS_QT6 = True
except ImportError:
    from PyQt5 import QtCore, QtGui, QtWidgets
    from PyQt5.QtCore import Qt
    from PyQt5.QtNetwork import QNetworkRequest
    IS_QT6 = False

if IS_QT6:
    ALIGN_CENTER = Qt.AlignmentFlag.AlignCenter
    ALIGN_LEFT = Qt.AlignmentFlag.AlignLeft
    ALIGN_RIGHT = Qt.AlignmentFlag.AlignRight
    ALIGN_VCENTER = Qt.AlignmentFlag.AlignVCenter
    ALIGN_HCENTER = Qt.AlignmentFlag.AlignHCenter
    RESIZE_TO_CONTENTS = QtWidgets.QHeaderView.ResizeMode.ResizeToContents
    RESIZE_STRETCH = QtWidgets.QHeaderView.ResizeMode.Stretch
    RESIZE_INTERACTIVE = QtWidgets.QHeaderView.ResizeMode.Interactive
    SELECT_ROWS = QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows
    SINGLE_SELECTION = QtWidgets.QAbstractItemView.SelectionMode.SingleSelection
    NO_EDIT_TRIGGERS = QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers
    PREFER_NETWORK = QNetworkRequest.CacheLoadControl.PreferNetwork
else:
    ALIGN_CENTER = QtCore.Qt.AlignCenter
    ALIGN_LEFT = QtCore.Qt.AlignLeft
    ALIGN_RIGHT = QtCore.Qt.AlignRight
    ALIGN_VCENTER = QtCore.Qt.AlignVCenter
    ALIGN_HCENTER = QtCore.Qt.AlignHCenter
    RESIZE_TO_CONTENTS = QtWidgets.QHeaderView.ResizeToContents
    RESIZE_STRETCH = QtWidgets.QHeaderView.Stretch
    RESIZE_INTERACTIVE = QtWidgets.QHeaderView.Interactive
    SELECT_ROWS = QtWidgets.QAbstractItemView.SelectRows
    SINGLE_SELECTION = QtWidgets.QAbstractItemView.SingleSelection
    NO_EDIT_TRIGGERS = QtWidgets.QAbstractItemView.NoEditTriggers
    PREFER_NETWORK = QNetworkRequest.PreferNetwork


def exec_dialog(dialog):
    """Executes a Qt dialog in a way that works across PyQt5 and PyQt6."""
    if hasattr(dialog, "exec"):
        return dialog.exec()
    return dialog.exec_()


# ---------------------------------------------------------------------------
# Picard API Imports
# ---------------------------------------------------------------------------
from picard import config, log
from picard.album import Album
from picard.config import BoolOption, IntOption, TextOption
from picard.file import (
    File,
    register_file_post_addition_to_track_processor,
    register_file_post_save_processor,
)
from picard.metadata import Metadata
from picard.track import Track
from picard.ui.itemviews import (
    BaseAction,
    register_album_action,
    register_track_action,
)
from picard.ui.options import (
    OptionsPage,
    register_options_page,
)

# ---------------------------------------------------------------------------
# Plugin Metadata
# ---------------------------------------------------------------------------
PLUGIN_NAME = "Better Lyrics"
PLUGIN_AUTHOR = "zoltrons"
PLUGIN_DESCRIPTION = (
    "Fetch high-quality TTML (Timed Text Markup Language) and LRC lyrics using the "
    "Better Lyrics ecosystem (LRC.red, Unison crowdsourced database & Better Lyrics API). "
    "Embeds rich TTML/LRC directly into audio file tags (lyrics/ttml/syncedlyrics), "
    "saves .ttml and .lrc sidecar files, and provides an interactive search dialog."
)
PLUGIN_VERSION = "1.1.0"
PLUGIN_API_VERSIONS = [
    "2.0", "2.1", "2.2", "2.3", "2.4", "2.5", "2.6", "2.7", "2.8", "2.9",
    "2.10", "2.11", "2.12", "3.0"
]
PLUGIN_LICENSE = "MIT"
PLUGIN_LICENSE_URL = "https://opensource.org/licenses/MIT"
PLUGIN_USER_GUIDE_URL = "https://github.com/zoltrons/betterlyrics-picard"

# ---------------------------------------------------------------------------
# Default Settings
# ---------------------------------------------------------------------------
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
    "betterlyrics_duration_tolerance": 4,  # seconds
    "betterlyrics_api_key": "",
}

# ---------------------------------------------------------------------------
# Service URLs & Constants
# ---------------------------------------------------------------------------
LRC_RED_API_URL = "https://lrc.red/api/v1"
UNISON_API_URL = "https://unison.betterlyrics.org/lyrics"
UNISON_SEARCH_URL = "https://unison.betterlyrics.org/lyrics/search"
BETTER_LYRICS_API_URL = "https://api.betterlyrics.org/getLyrics"

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (compatible; MusicBrainzPicard-BetterLyrics/1.1; "
    "+https://github.com/better-lyrics/better-lyrics)"
)

# Set to track files currently being saved
files_processing: set = set()

# Common audio extensions for orphaned file scanning
AUDIO_EXTENSIONS = {
    ".aac", ".ac3", ".aif", ".aifc", ".aiff", ".ape", ".asf", ".dff",
    ".dsf", ".eac3", ".flac", ".m2a", ".m4a", ".m4b", ".m4p", ".mp2",
    ".mp3", ".mp4", ".mpc", ".ofr", ".ofs", ".oga", ".ogg", ".opus",
    ".spx", ".tak", ".tta", ".wav", ".wma", ".wv"
}


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
    metadata = track.metadata
    if metadata.get("~length"):
        return parse_duration(str(metadata["~length"]))
    if track.files:
        tr_meta = track.files[0].metadata
        if tr_meta.get("~length"):
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
    """
    Converts TTML (Timed Text Markup Language) XML into standard or word-synced LRC.
    """
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
    """
    Converts LRC lines into a standard TTML (Timed Text Markup Language) XML document.
    """
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
            # Clean word timestamps if present
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
# HTTP Networking Helpers
# ---------------------------------------------------------------------------
def fetch_json(url: str, params: dict | None = None, headers: dict | None = None, timeout: int = 10):
    """Performs a synchronous HTTP GET request and parses the JSON response."""
    try:
        full_url = url
        if params:
            query = urlencode(params)
            sep = "&" if "?" in url else "?"
            full_url = f"{url}{sep}{query}"

        req_headers = {"User-Agent": DEFAULT_USER_AGENT, "Accept": "application/json"}
        if headers:
            req_headers.update(headers)

        req = Request(full_url, headers=req_headers)
        with urlopen(req, timeout=timeout) as resp:
            if resp.status != 200:
                log.debug(f"{PLUGIN_NAME}: HTTP {resp.status} for {full_url}")
                return None
            body = resp.read().decode("utf-8", errors="replace")
            return json.loads(body)
    except Exception as e:
        log.debug(f"{PLUGIN_NAME}: fetch_json error for {url}: {e}")
        return None


def fetch_text(url: str, timeout: int = 10) -> str | None:
    """Performs a synchronous HTTP GET request and returns raw string response."""
    try:
        req = Request(url, headers={"User-Agent": DEFAULT_USER_AGENT})
        with urlopen(req, timeout=timeout) as resp:
            if resp.status != 200:
                log.debug(f"{PLUGIN_NAME}: HTTP {resp.status} for {url}")
                return None
            return resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        log.debug(f"{PLUGIN_NAME}: fetch_text error for {url}: {e}")
        return None


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
    """
    Calculates a match confidence score between 0.0 and 1.0.
    """
    item_title = item.get("title", "")
    item_artist = item.get("artist", "")
    item_album = item.get("album", "")
    item_duration = item.get("duration", 0)
    item_isrc = item.get("isrc")
    item_timing = item.get("timing_type", "none")

    score = 0.0

    # 1. ISRC Exact Match
    if target_isrc and item_isrc and target_isrc.strip().upper() == item_isrc.strip().upper():
        score += 0.50

    # 2. Title Similarity
    t_ratio = SequenceMatcher(
        None, target_title.lower().strip(), item_title.lower().strip()
    ).ratio()
    score += t_ratio * 0.35

    # 3. Artist Similarity
    a_ratio = SequenceMatcher(
        None, target_artist.lower().strip(), item_artist.lower().strip()
    ).ratio()
    score += a_ratio * 0.25

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
        elif diff > 25:
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

    def make_candidate(res):
        return {
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
        }

    # 1. Search by ISRC
    if isrc:
        data = fetch_json(LRC_RED_API_URL, {"isrc": isrc.strip()})
        if data and isinstance(data, dict):
            for res in data.get("results", []):
                candidates.append(make_candidate(res))
        if candidates:
            return candidates

    # 2. Search by Track + Artist + Album
    if title and artist:
        params = {"track": title, "artist": artist}
        if album:
            params["album"] = album
        data = fetch_json(LRC_RED_API_URL, params)
        if data and isinstance(data, dict):
            for res in data.get("results", []):
                candidates.append(make_candidate(res))
        if candidates:
            return candidates

    # 3. Search by Free-form Query
    search_q = query or f"{artist} {title}".strip()
    if search_q:
        data = fetch_json(LRC_RED_API_URL, {"q": search_q})
        if data and isinstance(data, dict):
            for res in data.get("results", []):
                candidates.append(make_candidate(res))

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

    # 1. Direct song + artist lookup
    if title and artist:
        params = {"song": title, "artist": artist}
        if album:
            params["album"] = album
        if duration > 0:
            params["duration"] = str(duration)
        data = fetch_json(UNISON_API_URL, params)
        if data and isinstance(data, dict) and data.get("success") and data.get("data"):
            res = data["data"]
            candidates.append({
                "id": res.get("id"),
                "title": res.get("song", title),
                "artist": res.get("artist", artist),
                "album": res.get("album", album),
                "duration": res.get("duration", duration),
                "isrc": res.get("isrc", ""),
                "timing_type": res.get("syncType", "plain"),
                "format": res.get("format", "ttml"),
                "source": "Unison",
                "direct_lyrics": res.get("lyrics"),
                "raw": res,
            })
            return candidates

    # 2. Free-form Search
    search_q = query or f"{artist} {title}".strip()
    if search_q:
        data = fetch_json(UNISON_SEARCH_URL, {"q": search_q})
        if data and isinstance(data, dict) and data.get("success"):
            for res in data.get("data", []):
                candidates.append({
                    "id": res.get("id"),
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

    if source_preference in ("all", "lrcred"):
        results.extend(search_lrcred(title, artist, album, isrc, query))

    if source_preference in ("all", "unison"):
        results.extend(search_unison(title, artist, album, duration, query))

    for item in results:
        item["score"] = calculate_match_score(title, artist, album, duration, isrc, item)

    results.sort(key=lambda x: x.get("score", 0.0), reverse=True)
    return results


def resolve_lyrics_bundle(candidate: dict) -> dict:
    """
    Downloads and prepares both TTML and LRC representations for a chosen candidate.
    Returns a dict with:
      - 'ttml': raw or generated TTML string
      - 'lrc': line-synced or word-synced LRC string
      - 'is_synced': bool
      - 'source': str
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
            # First fetch the native TTML
            raw_content = fetch_text(lyrics_url)
            # If ttml fetch failed, attempt .lrc
            if not raw_content and lyrics_url.endswith(".ttml"):
                lrc_url = re.sub(r"\.ttml$", ".lrc", lyrics_url)
                raw_content = fetch_text(lrc_url)

    if not raw_content:
        return {"ttml": None, "lrc": None, "is_synced": False, "source": source}

    # Determine whether raw_content is TTML or LRC
    clean_words = config.setting["betterlyrics_clean_word_timestamps"]

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
        # Raw content is LRC or plain text
        lrc_str = clean_word_timestamps(raw_content) if clean_words else raw_content
        is_synced = is_lrc_synced(lrc_str)
        # Convert LRC into TTML
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
            QtWidgets.QMessageBox.StandardButton.Yes if IS_QT6 else QtWidgets.QMessageBox.Yes
            | (QtWidgets.QMessageBox.StandardButton.No if IS_QT6 else QtWidgets.QMessageBox.No),
            QtWidgets.QMessageBox.StandardButton.No if IS_QT6 else QtWidgets.QMessageBox.No,
        )
        yes_btn = QtWidgets.QMessageBox.StandardButton.Yes if IS_QT6 else QtWidgets.QMessageBox.Yes
        return reply == yes_btn
    except Exception:
        return False


def save_lyrics_to_files(
    file_list: list[File],
    bundle: dict,
    interactive: bool = False,
) -> bool:
    """
    Saves lyrics to the given Picard File objects:
    - Writes TTML to 'lyrics' and 'ttml' tags (or LRC depending on format setting)
    - Saves sidecar .ttml and/or .lrc files
    """
    if not file_list or not bundle:
        return False

    pref_format = config.setting["betterlyrics_format"]  # "ttml", "lrc", "both"
    ttml_content = bundle.get("ttml")
    lrc_content = bundle.get("lrc")
    is_synced = bundle.get("is_synced", False)

    # Primary text to write to 'lyrics' tag
    if pref_format in ("ttml", "both") and ttml_content:
        primary_tag_content = ttml_content
    else:
        primary_tag_content = lrc_content or ttml_content

    if not primary_tag_content:
        return False

    for file in file_list:
        full_path = file.filename
        if not full_path:
            continue

        dirname = os.path.dirname(full_path)
        base_name = os.path.splitext(os.path.basename(full_path))[0]

        ttml_sidecar = os.path.join(dirname, base_name + ".ttml")
        lrc_sidecar = os.path.join(dirname, base_name + ".lrc")

        has_tag_lyrics = bool(file.metadata.get("lyrics")) or bool(file.metadata.get("ttml"))
        has_sidecar = os.path.exists(ttml_sidecar) or os.path.exists(lrc_sidecar)

        # Overwrite prompt for interactive/manual mode
        if (
            (has_tag_lyrics or has_sidecar)
            and not config.setting["betterlyrics_auto_overwrite"]
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

        # -------------------------------------------------------------
        # 1. Embed Metadata Tags
        # -------------------------------------------------------------
        # Embed into standard 'lyrics' tag (Picard maps to ©lyr in MP4/M4A, USLT in MP3, LYRICS in FLAC)
        if config.setting["betterlyrics_embed_lyrics"]:
            file.metadata["lyrics"] = primary_tag_content

        # Embed into dedicated 'ttml' tag if TTML content exists
        if config.setting["betterlyrics_embed_ttml_tag"] and ttml_content:
            file.metadata["ttml"] = ttml_content

        # Embed into 'syncedlyrics' tag if enabled
        if config.setting["betterlyrics_embed_syncedlyrics"]:
            file.metadata["syncedlyrics"] = lrc_content or ttml_content

        # -------------------------------------------------------------
        # 2. Save Sidecar Files (.ttml / .lrc)
        # -------------------------------------------------------------
        save_ttml = config.setting["betterlyrics_save_ttml_file"] or pref_format in ("ttml", "both")
        save_lrc = config.setting["betterlyrics_save_lrc_file"] or pref_format in ("lrc", "both")

        # Save .ttml sidecar
        if save_ttml and ttml_content:
            try:
                with open(ttml_sidecar, "w", encoding="utf-8") as f:
                    f.write(ttml_content)
                log.info(f"{PLUGIN_NAME}: Saved TTML file to {ttml_sidecar}")
            except Exception as err:
                log.error(f"{PLUGIN_NAME}: Failed to write TTML file {ttml_sidecar}: {err}")

        # Save .lrc sidecar
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
    """Renders a PyQt dialog with live search, result table, and TTML/LRC preview."""
    parent = QtWidgets.QApplication.activeWindow() if parent is None else parent
    dialog = QtWidgets.QDialog(parent)
    dialog.setWindowTitle("Search Lyrics (TTML / LRC) — Better Lyrics")
    dialog.resize(840, 500)

    layout = QtWidgets.QVBoxLayout(dialog)

    # Search bar layout
    search_layout = QtWidgets.QHBoxLayout()
    search_input = QtWidgets.QLineEdit()
    default_q = f"{artist} {title}".strip()
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
        if IS_QT6 else QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel
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
            idx_item.setData(Qt.ItemDataRole.EditRole if IS_QT6 else QtCore.Qt.EditRole, row + 1)
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
            title="",
            artist="",
            query=query_text,
            source_preference=config.setting["betterlyrics_source"],
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

        p_buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Close if IS_QT6 else QtWidgets.QDialogButtonBox.Close
        )
        p_buttons.rejected.connect(preview_dialog.reject)
        p_layout.addWidget(p_buttons)
        exec_dialog(preview_dialog)

    preview_button.clicked.connect(on_preview)

    def on_double_click(index):
        if index.isValid():
            dialog.accept()

    table.doubleClicked.connect(on_double_click)
    button_box.accepted.connect(dialog.accept)
    button_box.rejected.connect(dialog.reject)

    result = exec_dialog(dialog)
    ok_val = QtWidgets.QDialog.DialogCode.Accepted if IS_QT6 else QtWidgets.QDialog.Accepted
    if result == ok_val:
        selected_row = table.currentRow()
        if 0 <= selected_row < len(current_results):
            return current_results[selected_row]
    return None


# ---------------------------------------------------------------------------
# Core Lyrics Matcher & Fetcher
# ---------------------------------------------------------------------------
def fetch_and_apply_lyrics(
    method: str,
    track: Track | None,
    files: list[File],
    metadata: Metadata,
    interactive: bool = False,
):
    """
    Main entry point for finding, scoring, and saving TTML/LRC lyrics for a track.
    """
    title = metadata.get("title", "").strip()
    artist = metadata.get("artist", "").strip()
    album_name = metadata.get("album", "").strip()
    isrc = metadata.get("isrc", "").strip()

    length = 0
    if metadata.get("~length"):
        length = parse_duration(str(metadata["~length"]))
    elif track:
        length = get_track_duration(track)

    if not title:
        log.warning(f"{PLUGIN_NAME}: Cannot fetch lyrics without a track title")
        return

    # Check instrumental
    if config.setting["betterlyrics_ignore_instrumental"]:
        if "(instrumental)" in title.lower() or "[instrumental]" in title.lower():
            log.info(f"{PLUGIN_NAME}: Skipping instrumental track: {title}")
            return

    log.info(f"{PLUGIN_NAME}: Searching lyrics for \"{title}\" by \"{artist}\" ({format_duration(length)})")

    selected_candidate = None

    if method == "search":
        candidates = query_all_sources(
            title=title,
            artist=artist,
            album=album_name,
            duration=length,
            isrc=isrc,
            source_preference=config.setting["betterlyrics_source"],
        )
        parent_win = QtWidgets.QApplication.activeWindow()
        selected_candidate = show_search_dialog(
            parent_win, title, artist, album_name, length, isrc, candidates
        )
        if not selected_candidate:
            log.info(f"{PLUGIN_NAME}: User cancelled lyrics selection")
            return
    else:
        candidates = query_all_sources(
            title=title,
            artist=artist,
            album=album_name,
            duration=length,
            isrc=isrc,
            source_preference=config.setting["betterlyrics_source"],
        )
        if not candidates:
            log.warning(f"{PLUGIN_NAME}: No lyrics found for \"{title}\" by \"{artist}\"")
            return

        best = candidates[0]
        tolerance = config.setting["betterlyrics_duration_tolerance"]

        if length > 0 and best.get("duration", 0) > 0:
            diff = abs(length - best["duration"])
            if diff > tolerance and best.get("score", 0.0) < 0.60:
                log.warning(
                    f"{PLUGIN_NAME}: Best match \"{best.get('title')}\" duration difference "
                    f"({diff}s) exceeds tolerance ({tolerance}s) with low score ({best.get('score', 0):.2f})"
                )
                return

        selected_candidate = best

    if not selected_candidate:
        return

    # Resolve full TTML and LRC bundle
    bundle = resolve_lyrics_bundle(selected_candidate)
    if not bundle.get("ttml") and not bundle.get("lrc"):
        log.warning(f"{PLUGIN_NAME}: Empty lyrics received for \"{title}\"")
        return

    save_lyrics_to_files(files, bundle, interactive=interactive)
    log.info(f"{PLUGIN_NAME}: Successfully matched and saved TTML/LRC lyrics for \"{title}\"")


# ---------------------------------------------------------------------------
# Picard Hooks & Processors
# ---------------------------------------------------------------------------
def get_on_load(track: Track, file: File) -> None:
    """Processor hook: runs automatically when a file is added to a track."""
    if not config.setting["betterlyrics_get_on_load"]:
        return
    try:
        album = track.album
        if not isinstance(album, Album):
            return
        files = track.files or [file]
        fetch_and_apply_lyrics("load", track, files, track.metadata, interactive=False)
    except Exception as err:
        log.error(f"{PLUGIN_NAME}: Error in get_on_load: {err}", exc_info=True)


def get_on_save(file: File) -> None:
    """Processor hook: runs automatically when an audio file is saved."""
    if not config.setting["betterlyrics_get_on_save"]:
        return
    if file.filename in files_processing:
        files_processing.discard(file.filename)
        return

    try:
        files_processing.add(file.filename)
        parent = getattr(file, "parent", None)
        track = parent if isinstance(parent, Track) else None
        fetch_and_apply_lyrics("save", track, [file], file.metadata, interactive=False)
    except Exception as err:
        log.error(f"{PLUGIN_NAME}: Error in get_on_save: {err}", exc_info=True)
    finally:
        files_processing.discard(file.filename)


# ---------------------------------------------------------------------------
# Context Menu Actions
# ---------------------------------------------------------------------------
class BetterLyricsGetAction(BaseAction):
    NAME = "Get lyrics (TTML / LRC) automatically with Better Lyrics"

    def execute_on_track(self, track: Track):
        if not track.linked_files and not track.files:
            return
        files = track.files or list(track.linked_files)
        fetch_and_apply_lyrics("get", track, files, track.metadata, interactive=False)

    def callback(self, objs):
        for item in objs:
            if isinstance(item, Track):
                self.execute_on_track(item)
            elif isinstance(item, Album):
                for track in item.tracks:
                    self.execute_on_track(track)


class BetterLyricsSearchAction(BaseAction):
    NAME = "Search lyrics (TTML / LRC) manually with Better Lyrics"

    def execute_on_track(self, track: Track):
        files = track.files or list(track.linked_files)
        if not files:
            return
        fetch_and_apply_lyrics("search", track, files, track.metadata, interactive=True)

    def callback(self, objs):
        for item in objs:
            if isinstance(item, Track):
                self.execute_on_track(item)
                break
            elif isinstance(item, Album) and item.tracks:
                self.execute_on_track(item.tracks[0])
                break


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
            QtWidgets.QFileDialog.Option.ShowDirsOnly if IS_QT6 else QtWidgets.QFileDialog.ShowDirsOnly,
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

    options = [
        BoolOption("setting", "betterlyrics_get_on_load", PLUGIN_OPTIONS["betterlyrics_get_on_load"]),
        BoolOption("setting", "betterlyrics_get_on_save", PLUGIN_OPTIONS["betterlyrics_get_on_save"]),
        BoolOption("setting", "betterlyrics_auto_overwrite", PLUGIN_OPTIONS["betterlyrics_auto_overwrite"]),
        TextOption("setting", "betterlyrics_format", PLUGIN_OPTIONS["betterlyrics_format"]),
        BoolOption("setting", "betterlyrics_save_ttml_file", PLUGIN_OPTIONS["betterlyrics_save_ttml_file"]),
        BoolOption("setting", "betterlyrics_save_lrc_file", PLUGIN_OPTIONS["betterlyrics_save_lrc_file"]),
        BoolOption("setting", "betterlyrics_embed_lyrics", PLUGIN_OPTIONS["betterlyrics_embed_lyrics"]),
        BoolOption("setting", "betterlyrics_embed_ttml_tag", PLUGIN_OPTIONS["betterlyrics_embed_ttml_tag"]),
        BoolOption("setting", "betterlyrics_embed_syncedlyrics", PLUGIN_OPTIONS["betterlyrics_embed_syncedlyrics"]),
        BoolOption("setting", "betterlyrics_clean_word_timestamps", PLUGIN_OPTIONS["betterlyrics_clean_word_timestamps"]),
        BoolOption("setting", "betterlyrics_prefer_synced", PLUGIN_OPTIONS["betterlyrics_prefer_synced"]),
        BoolOption("setting", "betterlyrics_ignore_instrumental", PLUGIN_OPTIONS["betterlyrics_ignore_instrumental"]),
        BoolOption("setting", "betterlyrics_plain_as_txt", PLUGIN_OPTIONS["betterlyrics_plain_as_txt"]),
        TextOption("setting", "betterlyrics_source", PLUGIN_OPTIONS["betterlyrics_source"]),
        IntOption("setting", "betterlyrics_duration_tolerance", PLUGIN_OPTIONS["betterlyrics_duration_tolerance"]),
        TextOption("setting", "betterlyrics_api_key", PLUGIN_OPTIONS["betterlyrics_api_key"]),
    ]

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
        self.duration_spin.setValue(4)
        tol_h.addWidget(tol_label)
        tol_h.addWidget(self.duration_spin)
        adv_layout.addLayout(tol_h)

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
        self.get_on_load.setChecked(bool(config.setting["betterlyrics_get_on_load"]))
        self.get_on_save.setChecked(bool(config.setting["betterlyrics_get_on_save"]))
        self.auto_overwrite.setChecked(bool(config.setting["betterlyrics_auto_overwrite"]))

        fmt = str(config.setting["betterlyrics_format"])
        f_idx = self.format_combo.findData(fmt)
        if f_idx >= 0:
            self.format_combo.setCurrentIndex(f_idx)

        self.save_ttml.setChecked(bool(config.setting["betterlyrics_save_ttml_file"]))
        self.save_lrc.setChecked(bool(config.setting["betterlyrics_save_lrc_file"]))
        self.embed_lyrics.setChecked(bool(config.setting["betterlyrics_embed_lyrics"]))
        self.embed_ttml_tag.setChecked(bool(config.setting["betterlyrics_embed_ttml_tag"]))
        self.embed_syncedlyrics.setChecked(bool(config.setting["betterlyrics_embed_syncedlyrics"]))

        self.clean_word_timestamps.setChecked(bool(config.setting["betterlyrics_clean_word_timestamps"]))
        self.prefer_synced.setChecked(bool(config.setting["betterlyrics_prefer_synced"]))
        self.ignore_instrumental.setChecked(bool(config.setting["betterlyrics_ignore_instrumental"]))
        self.plain_as_txt.setChecked(bool(config.setting["betterlyrics_plain_as_txt"]))

        src = str(config.setting["betterlyrics_source"])
        idx = self.source_combo.findData(src)
        if idx >= 0:
            self.source_combo.setCurrentIndex(idx)

        self.duration_spin.setValue(int(config.setting["betterlyrics_duration_tolerance"]))
        self.api_key_input.setText(str(config.setting["betterlyrics_api_key"]))

    def save(self):
        config.setting["betterlyrics_get_on_load"] = self.get_on_load.isChecked()
        config.setting["betterlyrics_get_on_save"] = self.get_on_save.isChecked()
        config.setting["betterlyrics_auto_overwrite"] = self.auto_overwrite.isChecked()

        fmt_data = self.format_combo.currentData()
        config.setting["betterlyrics_format"] = fmt_data or "ttml"

        config.setting["betterlyrics_save_ttml_file"] = self.save_ttml.isChecked()
        config.setting["betterlyrics_save_lrc_file"] = self.save_lrc.isChecked()
        config.setting["betterlyrics_embed_lyrics"] = self.embed_lyrics.isChecked()
        config.setting["betterlyrics_embed_ttml_tag"] = self.embed_ttml_tag.isChecked()
        config.setting["betterlyrics_embed_syncedlyrics"] = self.embed_syncedlyrics.isChecked()

        config.setting["betterlyrics_clean_word_timestamps"] = self.clean_word_timestamps.isChecked()
        config.setting["betterlyrics_prefer_synced"] = self.prefer_synced.isChecked()
        config.setting["betterlyrics_ignore_instrumental"] = self.ignore_instrumental.isChecked()
        config.setting["betterlyrics_plain_as_txt"] = self.plain_as_txt.isChecked()

        src_data = self.source_combo.currentData()
        config.setting["betterlyrics_source"] = src_data or "all"
        config.setting["betterlyrics_duration_tolerance"] = self.duration_spin.value()
        config.setting["betterlyrics_api_key"] = self.api_key_input.text().strip()


# ---------------------------------------------------------------------------
# Plugin Registration
# ---------------------------------------------------------------------------
register_file_post_addition_to_track_processor(get_on_load)
register_file_post_save_processor(get_on_save)

register_track_action(BetterLyricsSearchAction())
register_album_action(BetterLyricsSearchAction())

register_track_action(BetterLyricsGetAction())
register_album_action(BetterLyricsGetAction())

register_options_page(BetterLyricsOptionsPage)
