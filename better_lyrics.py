# -*- coding: utf-8 -*-
#
# Better Lyrics Plugin for MusicBrainz Picard 3.0+ & 2.x
#
# Fetches synchronized (word-level and line-level) and plain lyrics from the
# Better Lyrics ecosystem (LRC.red, Unison crowdsourced database & Better Lyrics API).
#
# Features:
# - Full compatibility with MusicBrainz Picard 3.0 (PyQt6) and Picard 2.x (PyQt5)
# - Dual / Hybrid Lyric Sources:
#     * LRC.red API (Primary Better Lyrics database by w4v)
#     * Unison API (Official Better Lyrics crowdsourced community database)
#     * Optional Better Lyrics API key support
# - TTML to LRC conversion: seamlessly converts rich Apple Music / TTML lyrics to standard LRC
# - Word-synced cleaning option: converts <mm:ss.xx> word-by-word timestamps into standard [mm:ss.xx]
#   line timestamps for maximum media player compatibility (Plex, Apple Music, Foobar2000, Jellyfin, etc.)
# - Smart track matching with fuzzy title/artist similarity, duration difference checks, and ISRC matching
# - Context menu actions for automated and manual interactive search
# - Interactive search dialog with live search, result list, and lyric preview
# - Automatic fetch on track load and/or file save
# - Metadata tag embedding (lyrics and syncedlyrics) and sidecar .lrc/.txt file export
# - Orphaned .lrc file cleaner tool
#
# Based on concepts from picard-lrclib (by izaz4141 / Dylancyclone) and better-lyrics.

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
PLUGIN_AUTHOR = "Better Lyrics Community / Picard Plugin"
PLUGIN_DESCRIPTION = (
    "Fetch high-quality synchronized and plain lyrics using the Better Lyrics "
    "ecosystem (LRC.red, Unison crowdsourced database & Better Lyrics API). "
    "Features word-by-word and line-by-line sync, automatic TTML to LRC conversion, "
    "word timestamp cleaning for player compatibility, ISRC matching, automatic "
    "tag embedding, sidecar .lrc files, and an interactive manual search dialog."
)
PLUGIN_VERSION = "1.0.0"
PLUGIN_API_VERSIONS = [
    "2.0", "2.1", "2.2", "2.3", "2.4", "2.5", "2.6", "2.7", "2.8", "2.9",
    "2.10", "2.11", "2.12", "3.0"
]
PLUGIN_LICENSE = "MIT"
PLUGIN_LICENSE_URL = "https://opensource.org/licenses/MIT"
PLUGIN_USER_GUIDE_URL = "https://github.com/better-lyrics/better-lyrics"

# ---------------------------------------------------------------------------
# Default Settings
# ---------------------------------------------------------------------------
PLUGIN_OPTIONS = {
    "betterlyrics_get_on_load": True,
    "betterlyrics_get_on_save": False,
    "betterlyrics_auto_overwrite": False,
    "betterlyrics_save_lrc_file": True,
    "betterlyrics_embed_lyrics": True,
    "betterlyrics_embed_syncedlyrics": False,
    "betterlyrics_clean_word_timestamps": True,
    "betterlyrics_prefer_synced": True,
    "betterlyrics_ignore_instrumental": True,
    "betterlyrics_plain_as_txt": False,
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
    "Mozilla/5.0 (compatible; MusicBrainzPicard-BetterLyrics/1.0; "
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
        # If greater than 10000, likely milliseconds
        if time_val > 10000:
            return int(round(time_val / 1000.0))
        return int(round(time_val))

    time_str = str(time_val).strip()
    if not time_str:
        return 0

    if time_str.isdigit():
        val = int(time_str)
        if val > 10000:  # milliseconds
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


def truncate_text(text: str, max_lines: int = 5, max_chars_per_line: int = 50) -> str:
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
# Lyrics Processing (TTML Parsing, Word-Sync Cleaning)
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


def ttml_to_lrc(ttml_content: str, word_synced: bool = False) -> str:
    """
    Converts Apple Music / Better Lyrics TTML (Timed Text Markup Language)
    into standard or word-synced LRC text.
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


def clean_word_timestamps(lrc_text: str) -> str:
    """
    Strips word-by-word timestamps <mm:ss.xx> from LRC text to produce clean,
    highly compatible line-synchronized LRC [mm:ss.xx].
    """
    if not lrc_text:
        return ""
    word_pattern = re.compile(r"<\d{1,2}:\d{2}(?:\.\d+)?>")
    cleaned_lines = []
    for line in lrc_text.splitlines():
        # Remove inner word tags but leave leading line timestamp intact
        cleaned_line = word_pattern.sub("", line).rstrip()
        cleaned_lines.append(cleaned_line)
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
    Considers title similarity, artist similarity, duration difference,
    ISRC identity, and sync availability.
    """
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

    # 2. Title Similarity (0.0 to 0.35)
    t_ratio = SequenceMatcher(
        None,
        target_title.lower().strip(),
        item_title.lower().strip(),
    ).ratio()
    score += t_ratio * 0.35

    # 3. Artist Similarity (0.0 to 0.25)
    a_ratio = SequenceMatcher(
        None,
        target_artist.lower().strip(),
        item_artist.lower().strip(),
    ).ratio()
    score += a_ratio * 0.25

    # 4. Album Similarity (0.0 to 0.10)
    if target_album and item_album:
        al_ratio = SequenceMatcher(
            None,
            target_album.lower().strip(),
            item_album.lower().strip(),
        ).ratio()
        score += al_ratio * 0.10

    # 5. Duration Difference Match (0.0 to 0.20)
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

    # 6. Prefer Synced Lyrics (+0.05)
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
    """
    Searches the LRC.red API (Better Lyrics primary catalog).
    Returns standardized candidate dictionaries.
    """
    candidates = []

    # 1. Search by ISRC if available
    if isrc:
        data = fetch_json(LRC_RED_API_URL, {"isrc": isrc.strip()})
        if data and isinstance(data, dict):
            for res in data.get("results", []):
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
                    "raw": res,
                })
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
                    "raw": res,
                })
        if candidates:
            return candidates

    # 3. Search by Free-form Query
    search_q = query or f"{artist} {title}".strip()
    if search_q:
        data = fetch_json(LRC_RED_API_URL, {"q": search_q})
        if data and isinstance(data, dict):
            for res in data.get("results", []):
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
                    "raw": res,
                })

    return candidates


def search_unison(
    title: str = "",
    artist: str = "",
    album: str = "",
    duration: int = 0,
    query: str = "",
) -> list[dict]:
    """
    Searches the Unison crowdsourced database (Better Lyrics community backend).
    Returns standardized candidate dictionaries.
    """
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


def fetch_candidate_lyrics(candidate: dict, clean_words: bool = True) -> tuple[str | None, bool]:
    """
    Downloads and converts lyrics for a chosen candidate.
    Returns (lyrics_string, is_synced_boolean).
    """
    source = candidate.get("source", "")
    timing_type = candidate.get("timing_type", "none")

    # Handle Unison Source
    if source == "Unison":
        raw_lyrics = candidate.get("direct_lyrics")
        if not raw_lyrics and candidate.get("id"):
            item_data = fetch_json(f"{UNISON_API_URL}/{candidate['id']}")
            if item_data and item_data.get("success") and item_data.get("data"):
                raw_lyrics = item_data["data"].get("lyrics")
                candidate["format"] = item_data["data"].get("format", candidate.get("format"))

        if not raw_lyrics:
            return None, False

        lyrics_format = candidate.get("format", "ttml")
        if lyrics_format == "ttml":
            converted = ttml_to_lrc(raw_lyrics, word_synced=not clean_words)
            if not converted:
                return None, False
            if clean_words:
                converted = clean_word_timestamps(converted)
            return converted, is_lrc_synced(converted)
        else:
            final_text = clean_word_timestamps(raw_lyrics) if clean_words else raw_lyrics
            return final_text, is_lrc_synced(final_text)

    # Handle LRC.red Source
    lyrics_url = candidate.get("lyrics_url", "")
    if not lyrics_url and candidate.get("id"):
        lyrics_url = f"https://lrc.red/s/{candidate['id']}.lrc"

    if lyrics_url:
        # Prefer direct .lrc download
        lrc_download_url = re.sub(r"\.ttml$", ".lrc", lyrics_url)
        content = fetch_text(lrc_download_url)

        # Fallback to .ttml if .lrc failed
        if not content and lyrics_url.endswith(".ttml"):
            ttml_content = fetch_text(lyrics_url)
            if ttml_content:
                content = ttml_to_lrc(ttml_content, word_synced=not clean_words)

        if not content:
            return None, False

        if clean_words:
            content = clean_word_timestamps(content)

        return content, is_lrc_synced(content)

    return None, False


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
        lrcred_results = search_lrcred(title, artist, album, isrc, query)
        results.extend(lrcred_results)

    if source_preference in ("all", "unison"):
        unison_results = search_unison(title, artist, album, duration, query)
        results.extend(unison_results)

    # Calculate match scores
    for item in results:
        item["score"] = calculate_match_score(title, artist, album, duration, isrc, item)

    # Sort descending by match score
    results.sort(key=lambda x: x.get("score", 0.0), reverse=True)
    return results


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
    lyrics: str,
    is_synced: bool,
    interactive: bool = False,
) -> bool:
    """
    Saves lyrics to the given Picard File objects:
    - Embeds in metadata tags ('lyrics' and optionally 'syncedlyrics')
    - Writes sidecar .lrc or .txt file if enabled
    """
    if not file_list or not lyrics:
        return False

    clean_words = config.setting["betterlyrics_clean_word_timestamps"]
    if clean_words:
        lyrics = clean_word_timestamps(lyrics)

    plain_as_txt = config.setting["betterlyrics_plain_as_txt"]
    ext = ".txt" if (not is_synced and plain_as_txt) else ".lrc"

    for file in file_list:
        full_path = file.filename
        if not full_path:
            continue

        dirname = os.path.dirname(full_path)
        base_name = os.path.splitext(os.path.basename(full_path))[0]
        sidecar_path = os.path.join(dirname, base_name + ext)

        has_tag_lyrics = bool(file.metadata.get("lyrics"))
        has_sidecar = os.path.exists(sidecar_path)

        # Overwrite prompt for interactive/manual mode
        if (
            (has_tag_lyrics or has_sidecar)
            and not config.setting["betterlyrics_auto_overwrite"]
            and interactive
        ):
            title = "Overwrite existing lyrics?"
            desc = (
                f"Lyrics already exist for \"{file.metadata.get('title', base_name)}\".\n\n"
                f"Preview of new lyrics:\n{truncate_text(lyrics, 5, 45)}\n\n"
                "Do you want to overwrite?"
            )
            parent = getattr(file, "tagger", None)
            parent_window = getattr(parent, "window", None) if parent else None
            if not confirm_replace(parent_window, title, desc):
                return False

        # 1. Embed tags
        if config.setting["betterlyrics_embed_lyrics"]:
            file.metadata["lyrics"] = lyrics

        if config.setting["betterlyrics_embed_syncedlyrics"] and is_synced:
            file.metadata["syncedlyrics"] = lyrics

        # 2. Save sidecar file
        if config.setting["betterlyrics_save_lrc_file"]:
            # Remove any conflicting previous sidecar extension
            for old_ext in (".txt", ".lrc"):
                old_path = os.path.join(dirname, base_name + old_ext)
                if old_path != sidecar_path and os.path.exists(old_path):
                    try:
                        os.remove(old_path)
                    except Exception as err:
                        log.debug(f"{PLUGIN_NAME}: Could not remove old sidecar {old_path}: {err}")

            try:
                with open(sidecar_path, "w", encoding="utf-8") as f:
                    f.write(lyrics)
                log.info(f"{PLUGIN_NAME}: Saved lyrics to {sidecar_path}")
            except Exception as err:
                log.error(f"{PLUGIN_NAME}: Failed to write sidecar file {sidecar_path}: {err}")
                if interactive:
                    parent_win = QtWidgets.QApplication.activeWindow()
                    QtWidgets.QMessageBox.critical(
                        parent_win,
                        "Failed to Save Lyrics File",
                        f"Could not write lyrics file:\n\n{sidecar_path}\n\nError: {err}",
                    )

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
    """Renders a PyQt dialog with live search, result table, and lyric preview."""
    parent = QtWidgets.QApplication.activeWindow() if parent is None else parent
    dialog = QtWidgets.QDialog(parent)
    dialog.setWindowTitle("Search Lyrics — Better Lyrics")
    dialog.resize(800, 480)

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
        "#", "Title", "Artist", "Album", "Length", "Sync", "Source"
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
    preview_button = QtWidgets.QPushButton("Preview Selected")
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
            # Index
            idx_item = QtWidgets.QTableWidgetItem()
            idx_item.setTextAlignment(ALIGN_CENTER)
            idx_item.setData(Qt.ItemDataRole.EditRole if IS_QT6 else QtCore.Qt.EditRole, row + 1)
            table.setItem(row, 0, idx_item)

            timing = item.get("timing_type", "none").lower()
            is_synced = timing in ("word", "line", "richsync", "linesync")
            sync_label = "Word" if timing in ("word", "richsync") else ("Line" if is_synced else "Plain")

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
        lyrics, _ = fetch_candidate_lyrics(
            candidate,
            clean_words=config.setting["betterlyrics_clean_word_timestamps"],
        )
        if not lyrics:
            QtWidgets.QMessageBox.information(
                dialog,
                "Preview Lyrics",
                "Could not load lyrics preview for this item.",
            )
            return

        preview_dialog = QtWidgets.QDialog(dialog)
        preview_dialog.setWindowTitle(f"Lyrics Preview — {candidate.get('title')}")
        preview_dialog.resize(600, 420)
        p_layout = QtWidgets.QVBoxLayout(preview_dialog)

        text_edit = QtWidgets.QPlainTextEdit(preview_dialog)
        text_edit.setReadOnly(True)
        text_edit.setPlainText(lyrics)
        p_layout.addWidget(text_edit)

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
    Main entry point for finding, scoring, and saving lyrics for a track.
    Supports automated matching and interactive search dialog.
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
        # Interactive Manual Search Dialog
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
        # Automated Matching Mode
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

        # If best match has duration and track has duration, check tolerance
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

    # Download lyrics content
    clean_words = config.setting["betterlyrics_clean_word_timestamps"]
    lyrics_text, is_synced = fetch_candidate_lyrics(selected_candidate, clean_words=clean_words)

    if not lyrics_text:
        log.warning(f"{PLUGIN_NAME}: Empty lyrics received for \"{title}\"")
        return

    save_lyrics_to_files(files, lyrics_text, is_synced, interactive=interactive)
    log.info(f"{PLUGIN_NAME}: Successfully matched and saved lyrics for \"{title}\"")


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
    NAME = "Get lyrics automatically with Better Lyrics"

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
    NAME = "Search lyrics manually with Better Lyrics"

    def execute_on_track(self, track: Track):
        files = track.files or list(track.linked_files)
        if not files:
            return
        fetch_and_apply_lyrics("search", track, files, track.metadata, interactive=True)

    def callback(self, objs):
        for item in objs:
            if isinstance(item, Track):
                self.execute_on_track(item)
                break  # Manual search works on one track at a time
            elif isinstance(item, Album) and item.tracks:
                self.execute_on_track(item.tracks[0])
                break


# ---------------------------------------------------------------------------
# Orphaned .lrc Files Cleaner
# ---------------------------------------------------------------------------
def clean_orphaned_lrc_files():
    """Recursively scans a user-selected folder and deletes .lrc files without matching audio."""
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
            lrc_files = [f for f in filenames if f.lower().endswith(".lrc")]
            for lrc_file in lrc_files:
                base_name = os.path.splitext(lrc_file)[0]
                audio_exists = any(
                    os.path.exists(os.path.join(dirpath, base_name + ext))
                    for ext in AUDIO_EXTENSIONS
                )
                if not audio_exists:
                    try:
                        os.remove(os.path.join(dirpath, lrc_file))
                        orphaned_count += 1
                    except Exception as e:
                        log.error(f"{PLUGIN_NAME}: Failed to remove orphaned {lrc_file}: {e}")

        QtWidgets.QMessageBox.information(
            parent,
            "Cleanup Complete",
            f"Removed {orphaned_count} orphaned .lrc file{'s' if orphaned_count != 1 else ''}.",
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
        BoolOption("setting", "betterlyrics_save_lrc_file", PLUGIN_OPTIONS["betterlyrics_save_lrc_file"]),
        BoolOption("setting", "betterlyrics_embed_lyrics", PLUGIN_OPTIONS["betterlyrics_embed_lyrics"]),
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
            "Fetches synchronized and plain lyrics from LRC.red and Unison databases.<br/>"
            "<a href='https://github.com/better-lyrics/better-lyrics'>Better Lyrics Project</a> | "
            "<a href='https://unison.betterlyrics.org'>Unison Crowdsourced Database</a>",
            self
        )
        header.setOpenExternalLinks(True)
        self.box.addWidget(header)

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

        # Lyric Processing & Sync Format
        format_group = QtWidgets.QGroupBox("Lyric Format & Sync Options", self)
        format_layout = QtWidgets.QVBoxLayout(format_group)
        self.clean_word_timestamps = QtWidgets.QCheckBox(
            "Convert word timestamps to standard line-synced LRC (Recommended for player compatibility)",
            self
        )
        self.prefer_synced = QtWidgets.QCheckBox("Prefer synchronized lyrics over plain lyrics", self)
        self.ignore_instrumental = QtWidgets.QCheckBox("Ignore instrumental tracks", self)
        format_layout.addWidget(self.clean_word_timestamps)
        format_layout.addWidget(self.prefer_synced)
        format_layout.addWidget(self.ignore_instrumental)
        self.box.addWidget(format_group)

        # File & Tag Storage Group
        storage_group = QtWidgets.QGroupBox("Storage & Metadata Embedding", self)
        storage_layout = QtWidgets.QVBoxLayout(storage_group)
        self.embed_lyrics = QtWidgets.QCheckBox("Embed lyrics into 'lyrics' tag", self)
        self.embed_syncedlyrics = QtWidgets.QCheckBox("Embed synced lyrics into 'syncedlyrics' tag", self)
        self.save_lrc = QtWidgets.QCheckBox("Save external .lrc file alongside audio files", self)
        self.plain_as_txt = QtWidgets.QCheckBox("Save plain unsynchronized lyrics as .txt instead of .lrc", self)
        storage_layout.addWidget(self.embed_lyrics)
        storage_layout.addWidget(self.embed_syncedlyrics)
        storage_layout.addWidget(self.save_lrc)
        storage_layout.addWidget(self.plain_as_txt)
        self.box.addWidget(storage_group)

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

        self.clean_orphaned_btn = QtWidgets.QPushButton("Clean Orphaned .lrc Files in Library...", self)
        self.clean_orphaned_btn.clicked.connect(clean_orphaned_lrc_files)
        adv_layout.addWidget(self.clean_orphaned_btn)

        self.box.addWidget(adv_group)
        self.box.addStretch()

    def load(self):
        self.get_on_load.setChecked(bool(config.setting["betterlyrics_get_on_load"]))
        self.get_on_save.setChecked(bool(config.setting["betterlyrics_get_on_save"]))
        self.auto_overwrite.setChecked(bool(config.setting["betterlyrics_auto_overwrite"]))
        self.clean_word_timestamps.setChecked(bool(config.setting["betterlyrics_clean_word_timestamps"]))
        self.prefer_synced.setChecked(bool(config.setting["betterlyrics_prefer_synced"]))
        self.ignore_instrumental.setChecked(bool(config.setting["betterlyrics_ignore_instrumental"]))
        self.embed_lyrics.setChecked(bool(config.setting["betterlyrics_embed_lyrics"]))
        self.embed_syncedlyrics.setChecked(bool(config.setting["betterlyrics_embed_syncedlyrics"]))
        self.save_lrc.setChecked(bool(config.setting["betterlyrics_save_lrc_file"]))
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
        config.setting["betterlyrics_clean_word_timestamps"] = self.clean_word_timestamps.isChecked()
        config.setting["betterlyrics_prefer_synced"] = self.prefer_synced.isChecked()
        config.setting["betterlyrics_ignore_instrumental"] = self.ignore_instrumental.isChecked()
        config.setting["betterlyrics_embed_lyrics"] = self.embed_lyrics.isChecked()
        config.setting["betterlyrics_embed_syncedlyrics"] = self.embed_syncedlyrics.isChecked()
        config.setting["betterlyrics_save_lrc_file"] = self.save_lrc.isChecked()
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
