"""Small, bounded, thread-safe SQLite cache for lyric bundles and choices."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import threading
import time
from typing import Any

try:  # Package import, while retaining direct-file usability for host tests.
    from .lyrics_matching import normalize_isrcs, normalize_text, parse_duration
except ImportError:  # pragma: no cover - only used by direct module loading
    from lyrics_matching import normalize_isrcs, normalize_text, parse_duration


SCHEMA_VERSION = 1
PARSER_SCHEMA_VERSION = 1
DEFAULT_TTL = 30 * 24 * 60 * 60.0
MAX_TTL = 365 * 24 * 60 * 60.0
_SENSITIVE_KEYS = re.compile(
    r"(?:authorization|api[_-]?key|credential|cookie|password|secret|token|private)", re.I
)


def track_key(target: dict) -> str:
    """Return a deterministic versioned hash of normalized recording metadata."""

    target = target if isinstance(target, dict) else {}
    duration = parse_duration(target.get("duration"))
    canonical = {
        "schema_version": SCHEMA_VERSION,
        "parser_schema_version": PARSER_SCHEMA_VERSION,
        "isrc": sorted(normalize_isrcs(target.get("isrc"))),
        "title": normalize_text(target.get("title")),
        "artist": normalize_text(target.get("artist")),
        "album": normalize_text(target.get("album")),
        "duration": None if duration is None else round(duration, 3),
    }
    encoded = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "v{}:".format(SCHEMA_VERSION) + hashlib.sha256(encoded).hexdigest()


def _safe_choice(value: Any, drop_raw: bool = True) -> Any:
    """Make a JSON-safe copy while excluding obvious credentials/private fields."""

    if isinstance(value, dict):
        return {
            str(key): _safe_choice(item, drop_raw)
            for key, item in value.items()
            if not _SENSITIVE_KEYS.search(str(key)) and not (drop_raw and str(key).casefold() == "raw")
        }
    if isinstance(value, (list, tuple)):
        return [_safe_choice(item, drop_raw) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    raise TypeError("choice candidate must be JSON serializable")


def _ttl(value: Any) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("ttl must be finite")
    return min(MAX_TTL, max(0.0, result))


class LyricsStore:
    """A bounded persistent store; cache expiry never expires manual choices."""

    def __init__(self, path: str | os.PathLike[str], max_entries: int = 512, default_ttl: float = DEFAULT_TTL):
        self.path = os.fspath(path)
        self.max_entries = max(1, int(max_entries))
        self.default_ttl = _ttl(default_ttl)
        parent = os.path.dirname(os.path.abspath(self.path))
        if self.path != ":memory:":
            os.makedirs(parent, exist_ok=True)
        self._lock = threading.RLock()
        self._closed = False
        self._db = sqlite3.connect(self.path, timeout=10.0, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.execute("PRAGMA busy_timeout=10000")
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.executescript(
                """
                CREATE TABLE IF NOT EXISTS store_meta (
                    name TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS lyric_cache (
                    key TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    expires_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    outcome TEXT NOT NULL DEFAULT 'bundle'
                );
                CREATE TABLE IF NOT EXISTS lyric_choices (
                    key TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    updated_at REAL NOT NULL
                );
                """
            )
            self._db.execute(
                "INSERT INTO store_meta(name, value) VALUES('schema_version', ?) "
                "ON CONFLICT(name) DO UPDATE SET value=excluded.value",
                (str(SCHEMA_VERSION),),
            )
            self._db.commit()

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("LyricsStore is closed")

    def _prune(self) -> None:
        now = time.time()
        self._db.execute("DELETE FROM lyric_cache WHERE expires_at <= ?", (now,))
        for table in ("lyric_cache", "lyric_choices"):
            self._db.execute(
                f"DELETE FROM {table} WHERE key IN (SELECT key FROM {table} ORDER BY updated_at DESC LIMIT -1 OFFSET ?)",
                (self.max_entries,),
            )

    def get(self, key: str) -> dict | None:
        with self._lock:
            self._ensure_open()
            row = self._db.execute("SELECT payload, expires_at, outcome FROM lyric_cache WHERE key=?", (str(key),)).fetchone()
            if row is None:
                return None
            if row[1] <= time.time():
                self._db.execute("DELETE FROM lyric_cache WHERE key=?", (str(key),))
                self._db.commit()
                return None
            if row[2] == "not_found":
                return None
            try:
                value = json.loads(row[0])
            except (TypeError, ValueError):
                value = None
            if not isinstance(value, dict):
                self._db.execute("DELETE FROM lyric_cache WHERE key=?", (str(key),))
                self._db.commit()
                return None
            return value

    def put(self, key: str, bundle: dict, ttl: float | None = None) -> None:
        if not isinstance(bundle, dict):
            raise TypeError("bundle must be a dictionary")
        if bundle.get("error") or bundle.get("status") in {
            "not_found", "auth_required", "rate_limited", "transport_error",
            "invalid_response", "cancelled", "error",
        }:
            return
        payload = json.dumps(_safe_choice(bundle, drop_raw=False), ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        ttl = self.default_ttl if ttl is None else _ttl(ttl)
        with self._lock:
            self._ensure_open()
            now = time.time()
            self._db.execute(
                "INSERT INTO lyric_cache(key,payload,expires_at,updated_at,outcome) VALUES(?,?,?,?, 'bundle') "
                "ON CONFLICT(key) DO UPDATE SET payload=excluded.payload, expires_at=excluded.expires_at, "
                "updated_at=excluded.updated_at, outcome='bundle'",
                (str(key), payload, now + ttl, now),
            )
            self._prune()
            self._db.commit()

    def cache_not_found(self, key: str, ttl: float = 60.0) -> None:
        """Remember a confirmed miss; get() then returns {"status": "not_found"}.

        Callers must opt into negative caching here after a genuine not_found
        result; transport and provider errors belong outside the persistent cache.
        """

        ttl = _ttl(ttl)
        with self._lock:
            self._ensure_open()
            now = time.time()
            self._db.execute(
                "INSERT INTO lyric_cache(key,payload,expires_at,updated_at,outcome) VALUES(?,?,?,?,'not_found') "
                "ON CONFLICT(key) DO UPDATE SET payload=excluded.payload, expires_at=excluded.expires_at, "
                "updated_at=excluded.updated_at, outcome='not_found'",
                (str(key), "null", now + ttl, now),
            )
            self._prune()
            self._db.commit()

    def get_choice(self, key: str) -> dict | None:
        with self._lock:
            self._ensure_open()
            row = self._db.execute("SELECT payload FROM lyric_choices WHERE key=?", (str(key),)).fetchone()
            if row is None:
                return None
            try:
                value = json.loads(row[0])
            except (TypeError, ValueError):
                return None
            return value if isinstance(value, dict) else None

    def set_choice(self, key: str, provider_id: str, offset_ms: int = 0, candidate: dict | None = None) -> None:
        if not isinstance(provider_id, str) or not provider_id.strip():
            raise ValueError("provider_id must be a non-empty string")
        try:
            offset = int(offset_ms)
        except (TypeError, ValueError, OverflowError):
            raise ValueError("offset_ms must be an integer") from None
        value = {"provider_id": provider_id, "offset_ms": offset, "candidate": _safe_choice(candidate) if candidate is not None else None}
        payload = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        with self._lock:
            self._ensure_open()
            self._db.execute(
                "INSERT INTO lyric_choices(key,payload,updated_at) VALUES(?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET payload=excluded.payload, updated_at=excluded.updated_at",
                (str(key), payload, time.time()),
            )
            self._prune()
            self._db.commit()

    def clear_choice(self, key: str) -> None:
        with self._lock:
            self._ensure_open()
            self._db.execute("DELETE FROM lyric_choices WHERE key=?", (str(key),))
            self._db.commit()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            try:
                self._db.commit()
                self._db.close()
            finally:
                self._closed = True

    def __enter__(self) -> "LyricsStore":
        self._ensure_open()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def __del__(self):  # pragma: no cover - interpreter shutdown safety
        try:
            self.close()
        except Exception:
            pass


__all__ = ["LyricsStore", "track_key", "SCHEMA_VERSION", "PARSER_SCHEMA_VERSION"]
