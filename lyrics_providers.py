"""Independent, conservative adapters for public lyrics services.

Adapters return a common candidate shape and leave identity scoring and lyrics
parsing to the main plugin.  They do not rotate mirrors, manufacture
credentials, or retry a provider-specific query beyond the documented small
fallbacks.
"""

from __future__ import annotations

import copy
import re
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit, urlunsplit


RequestJSON = Callable[..., Any]
_ISRC_RE = re.compile(r"^[A-Z]{2}[A-Z0-9]{3}\d{7}$")
_VALID_TIMING = {"word", "line", "none"}


def search_provider(
    provider_id: str,
    target: dict,
    request_json: RequestJSON,
    *,
    endpoint: str = "",
    api_key: str = "",
) -> dict:
    """Search one provider, returning ``status``, ``candidates`` and ``message``.

    ``request_json`` is deliberately injected so the Picard queue can supply its
    own cancellable :class:`lyrics_http.HttpClient` method and tests can remain
    offline.
    """
    provider = str(provider_id or "").strip().lower().replace("-", "_")
    if provider in ("lyrics_plus", "lyricsplus"):
        return _lyricsplus(target, request_json, endpoint, api_key)
    if provider in ("kpoe", "kpoe_v2"):
        # KPOE is the response protocol used by LyricsPlus; accepting the alias
        # is useful to callers that classify sources by protocol.
        return _lyricsplus(target, request_json, endpoint, api_key)
    if provider in ("boidu", "betterlyrics", "better_lyrics"):
        return _boidu(target, request_json, endpoint, api_key)
    if provider in ("lrclib", "lrc_lib"):
        return _lrclib(target, request_json, endpoint, api_key)
    return _result("invalid_response", [], f"unknown lyrics provider: {provider_id}")


def _lyricsplus(target: Mapping[str, Any], request_json: RequestJSON, endpoint: str, api_key: str) -> dict:
    query = _query_target(target, require_split=True)
    if query is None:
        return _result("not_found", [], "manual query needs a reliable 'artist - title' split")
    title, artist = query
    if not title or not artist:
        return _result("not_found", [], "title and artist are required")
    url = _endpoint(endpoint, "https://lyricsplus.binimum.org", "/v2/lyrics/get")
    if not url:
        return _result("invalid_response", [], "provider endpoint must be HTTPS")
    params: dict[str, Any] = {"title": title, "artist": artist}
    manual_override = _query_override(target)
    album = "" if manual_override else _text(target.get("album"))
    duration = _number(target.get("duration"))
    if album:
        params["album"] = album
    if duration is not None and duration >= 0:
        params["duration"] = _decimal(duration)
    isrc = None if manual_override else _valid_isrc(target.get("isrc"))
    if isrc:
        params["isrc"] = isrc
    headers = _api_headers(api_key)
    response = _call(request_json, url, params, headers)
    if response[0] != "hit":
        return _result(response[0], [], response[2])
    body = response[1]
    candidate, error = _parse_kpoe(body, target, title, artist, "lyricsplus")
    if error:
        return _result("invalid_response", [], error)
    return _result("hit", [candidate], "LyricsPlus result")


def _boidu(target: Mapping[str, Any], request_json: RequestJSON, endpoint: str, api_key: str) -> dict:
    query = _query_target(target, require_split=True)
    if query is None:
        return _result("not_found", [], "manual query needs a reliable 'artist - title' split")
    title, artist = query
    duration = _number(target.get("duration"))
    if not title or not artist:
        return _result("not_found", [], "title and artist are required")
    if duration is None or duration <= 0:
        return _result("not_found", [], "boidu requires track duration")
    url = _endpoint(endpoint, "https://api.betterlyrics.org", "/getLyrics")
    if not url:
        return _result("invalid_response", [], "provider endpoint must be HTTPS")
    manual_override = _query_override(target)
    album = "" if manual_override else _text(target.get("album"))
    base_params: dict[str, Any] = {"s": title, "a": artist, "d": int(round(duration))}
    if album:
        base_params["al"] = album
    # A key is only sent to the endpoint explicitly selected by this adapter;
    # it is never copied to another provider or put in query parameters.
    headers = _api_headers(api_key)
    response = _call(request_json, url, base_params, headers)
    if response[0] == "auth_required" and not api_key and album:
        # Better Lyrics permits a keyless album-less cache lookup.  It is only
        # attempted after the album query explicitly says authentication is
        # required, avoiding an extra upstream request in normal operation.
        second = {"s": title, "a": artist, "d": int(round(duration))}
        response = _call(request_json, url, second, {})
    if response[0] != "hit":
        return _result(response[0], [], response[2])
    candidate, error = _parse_boidu(response[1], target, title, artist)
    if error:
        return _result("invalid_response", [], error)
    return _result("hit", [candidate], "Better Lyrics result")


def _lrclib(target: Mapping[str, Any], request_json: RequestJSON, endpoint: str, api_key: str) -> dict:
    base = _endpoint(endpoint, "https://lrclib.net", "")
    if not base:
        return _result("invalid_response", [], "provider endpoint must be HTTPS")
    # An explicit q is honored by search below.  If it cannot be split, do not
    # silently replace it with stale title/artist metadata.
    explicit = _text(target.get("query"))
    query = _query_target(target, require_split=False)
    title = artist = ""
    if query:
        title, artist = query
    headers = _api_headers(api_key)
    if not explicit and title and artist:
        params: dict[str, Any] = {"track_name": title, "artist_name": artist}
        album = _text(target.get("album"))
        duration = _number(target.get("duration"))
        if album:
            params["album_name"] = album
        if duration is not None and duration >= 0:
            params["duration"] = _decimal(duration)
        exact = _call(request_json, _lrclib_path(base, "get"), params, headers)
        if exact[0] == "hit":
            candidates, error = _parse_lrclib_body(exact[1], target, title, artist)
            if error:
                return _result("invalid_response", [], error)
            if candidates:
                return _result("hit", candidates, "LRCLIB exact result")
        elif exact[0] not in ("not_found",):
            return _result(exact[0], [], exact[2])
    if explicit:
        search_params = {"q": explicit}
    elif title and artist:
        search_params = {"q": f"{artist} {title}"}
    else:
        return _result("not_found", [], "title and artist are required")
    searched = _call(request_json, _lrclib_path(base, "search"), search_params, headers)
    if searched[0] != "hit":
        return _result(searched[0], [], searched[2])
    fallback_title, fallback_artist = ("", "") if _query_override(target) else (title, artist)
    candidates, error = _parse_lrclib_body(searched[1], target, fallback_title, fallback_artist)
    if error:
        return _result("invalid_response", [], error)
    if not candidates:
        return _result("not_found", [], "LRCLIB returned no lyrics")
    return _result("hit", candidates, "LRCLIB search result")


def _parse_kpoe(body: Any, target: Mapping[str, Any], title: str, artist: str, provider: str) -> tuple[dict | None, str | None]:
    if not isinstance(body, dict):
        return None, "provider returned a non-object payload"
    lyrics = body.get("lyrics")
    if not isinstance(lyrics, list) or not lyrics:
        return None, "KPOE response has no lyrics list"
    timing = _text(body.get("type")).lower()
    if timing not in _VALID_TIMING:
        return None, "KPOE response has an unsupported timing type"
    valid_items = [item for item in lyrics if isinstance(item, dict) and _kpoe_item_has_text(item)]
    if not valid_items:
        return None, "KPOE lyrics list is malformed"
    selected = _selected_metadata(body)
    actual_title = _metadata_text(selected, ("title", "trackName", "songName", "name"))
    actual_artist = _metadata_text(selected, ("artist", "artistName", "artistNames", "performer"))
    actual_album = _metadata_text(selected, ("album", "albumName"))
    actual_isrc = _valid_isrc(_metadata_value(selected, ("isrc", "songISRC", "songIsrc", "trackIsrc")))
    actual_duration = _metadata_duration(selected)
    if actual_duration is None:
        actual_duration = _metadata_duration(body.get("metadata"))
    candidate = _candidate(
        title=actual_title or title,
        artist=actual_artist or artist,
        album=actual_album or None,
        duration=actual_duration,
        isrc=actual_isrc,
        source=_metadata_text(selected, ("source", "provider")) or provider,
        display=_metadata_text(selected, ("source", "provider")) or provider,
        provider_id=provider,
        format="kpoe",
        direct_lyrics=None,
        payload=body,
        raw=body,
        timing_type=timing,
        metadata_verified=bool(actual_title and actual_artist),
    )
    return candidate, None


def _parse_boidu(body: Any, target: Mapping[str, Any], title: str, artist: str) -> tuple[dict | None, str | None]:
    if not isinstance(body, dict):
        return None, "Better Lyrics returned a non-object payload"
    text = None
    fmt = None
    for key, kind in (("ttml", "ttml"), ("syncedLyrics", "lrc"), ("lrc", "lrc"), ("lyrics", "plain")):
        value = body.get(key)
        if isinstance(value, str) and value.strip():
            text, fmt = value, kind
            break
    if text is None:
        return None, "Better Lyrics response has no supported lyrics field"
    metadata = _selected_metadata(body)
    actual_title = _metadata_text(metadata, ("title", "trackName", "songName", "name"))
    actual_artist = _metadata_text(metadata, ("artist", "artistName", "performer"))
    actual_album = _metadata_text(metadata, ("album", "albumName"))
    actual_duration = _metadata_duration(metadata)
    actual_isrc = _valid_isrc(_metadata_value(metadata, ("isrc", "songISRC", "trackIsrc")))
    return _candidate(
        title=actual_title or title,
        artist=actual_artist or artist,
        album=actual_album or None,
        duration=actual_duration,
        isrc=actual_isrc,
        source="boidu",
        display="Better Lyrics",
        provider_id="boidu",
        format=fmt,
        direct_lyrics=text,
        payload=body,
        raw=body,
        timing_type="line" if fmt in ("ttml", "lrc") else "none",
        metadata_verified=bool(actual_title and actual_artist),
    ), None


def _parse_lrclib_body(body: Any, target: Mapping[str, Any], fallback_title: str = "", fallback_artist: str = "") -> tuple[list[dict], str | None]:
    if isinstance(body, dict):
        records = [body]
    elif isinstance(body, list):
        records = body
    else:
        return [], "LRCLIB returned an unexpected payload"
    output: list[dict] = []
    for record in records:
        if not isinstance(record, dict):
            continue
        synced = record.get("syncedLyrics")
        plain = record.get("plainLyrics")
        if not isinstance(synced, str) or not synced.strip():
            synced = None
        if not isinstance(plain, str) or not plain.strip():
            plain = None
        instrumental = bool(record.get("instrumental"))
        if not synced and not plain and not instrumental:
            continue
        actual_title = _metadata_text(record, ("trackName", "title", "name"))
        actual_artist = _metadata_text(record, ("artistName", "artist", "performer"))
        actual_album = _metadata_text(record, ("albumName", "album"))
        actual_isrc = _valid_isrc(_metadata_value(record, ("isrc", "trackIsrc")))
        actual_duration = _metadata_duration(record)
        text = synced or plain or ""
        fmt = "lrc" if synced else "plain"
        output.append(_candidate(
            title=actual_title or fallback_title or None,
            artist=actual_artist or fallback_artist or None,
            album=actual_album or None,
            duration=actual_duration,
            isrc=actual_isrc,
            source="lrclib",
            display="LRCLIB",
            provider_id="lrclib",
            format=fmt,
            direct_lyrics=text,
            payload=record,
            raw=record,
            timing_type="line" if synced else "none",
            metadata_verified=bool(actual_title and actual_artist),
        ))
    return output, None


def _candidate(**values: Any) -> dict:
    # deepcopy keeps a provider's mutable response from leaking into the main
    # matching pipeline or a caller's later test mutations.
    result = {
        "title": values.get("title"), "artist": values.get("artist"), "album": values.get("album"),
        "duration": values.get("duration"), "isrc": values.get("isrc"), "source": values.get("source"),
        "display": values.get("display"), "provider_id": values.get("provider_id"),
        "format": values.get("format"), "direct_lyrics": values.get("direct_lyrics"),
        "payload": values.get("payload"), "raw": values.get("raw"),
        "timing_type": values.get("timing_type"), "metadata_verified": bool(values.get("metadata_verified")),
    }
    return copy.deepcopy(result)


def _result(status: str, candidates: list[dict], message: str) -> dict:
    if status not in _VALID_STATUSES:
        status = "invalid_response"
    return {"status": status, "candidates": copy.deepcopy(candidates), "message": str(message or "")}


_VALID_STATUSES = {"hit", "not_found", "auth_required", "rate_limited", "transport_error", "invalid_response", "cancelled"}


def _call(request_json: RequestJSON, url: str, params: Mapping[str, Any], headers: Mapping[str, str]) -> tuple[str, Any, str]:
    try:
        response = request_json(url, params=dict(params), headers=dict(headers))
    except Exception as exc:
        return "transport_error", None, f"provider request failed: {type(exc).__name__}"
    if hasattr(response, "status"):
        status = str(getattr(response, "status", "invalid_response"))
        data = getattr(response, "data", None)
        code = getattr(response, "status_code", None)
        if status not in _VALID_STATUSES:
            status = _status_code(code)
        return status, data, _status_message(status, code)
    # A plain mapping is convenient for tiny injected test transports and is a
    # valid successful JSON object, rather than a fake HttpResult.
    return "hit", response, ""


def _status_code(code: Any) -> str:
    try:
        code = int(code)
    except (TypeError, ValueError):
        return "invalid_response"
    if code == 404:
        return "not_found"
    if code in (401, 403):
        return "auth_required"
    if code == 429:
        return "rate_limited"
    if 200 <= code < 300:
        return "hit"
    if code >= 500 or code in (408, 425):
        return "transport_error"
    return "invalid_response"


def _status_message(status: str, code: Any) -> str:
    if code is not None:
        return f"provider HTTP status {code} ({status})"
    return f"provider request {status}"


def _endpoint(value: str, default_base: str, suffix: str) -> str:
    selected = (value or default_base).strip().rstrip("/")
    parsed = urlsplit(selected)
    if (parsed.scheme.lower() != "https" or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment or any(c.isspace() for c in selected)):
        return ""
    path = parsed.path.rstrip("/")
    if suffix and not path.endswith(suffix):
        path += suffix
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def _lrclib_path(base: str, operation: str) -> str:
    """Resolve a root or ``/api`` LRCLIB base without producing ``/api/api``."""
    parsed = urlsplit(base)
    path = parsed.path.rstrip("/")
    if path.endswith(f"/api/{operation}"):
        final_path = path
    elif path.endswith("/api"):
        final_path = f"{path}/{operation}"
    else:
        final_path = f"{path}/api/{operation}"
    return urlunsplit((parsed.scheme, parsed.netloc, final_path, "", ""))


def _api_headers(api_key: str) -> dict[str, str]:
    key = str(api_key or "").strip()
    return {"X-API-Key": key} if key else {}


def _query_target(target: Mapping[str, Any], *, require_split: bool) -> tuple[str, str] | None:
    title = _text(target.get("title"))
    artist = _text(target.get("artist"))
    query = _text(target.get("query"))
    if query:
        # Explicit manual input is authoritative.  Never silently replace it
        # with stale metadata from the selected Picard track.
        match = re.match(r"^\s*(.*?)\s+[-–—]\s+(.*?)\s*$", query)
        if match and match.group(1).strip() and match.group(2).strip():
            artist, title = match.group(1).strip(), match.group(2).strip()
        elif title and artist and _same_query(query, f"{artist} {title}"):
            # Picard's default search label is often just "artist title".  It
            # is safe to retain the selected target only when it is identical.
            pass
        elif require_split:
            return None
    return title, artist


def _query_override(target: Mapping[str, Any]) -> bool:
    query = _text(target.get("query"))
    title = _text(target.get("title"))
    artist = _text(target.get("artist"))
    return bool(query and not (title and artist and _same_query(query, f"{artist} {title}")))


def _same_query(left: str, right: str) -> bool:
    return " ".join(left.split()).casefold() == " ".join(right.split()).casefold()


def _text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
        return number if number == number and number not in (float("inf"), float("-inf")) else None
    except (TypeError, ValueError):
        return None


def _decimal(value: float) -> str:
    return format(value, ".6f").rstrip("0").rstrip(".") or "0"


def _valid_isrc(value: Any) -> str | None:
    text = re.sub(r"[\s-]", "", _text(value)).upper()
    return text if _ISRC_RE.fullmatch(text) else None


def _selected_metadata(body: Mapping[str, Any]) -> Mapping[str, Any]:
    # Keep both layers: KPOE commonly puts source/title in the selected-song
    # record while duration or album is in the top-level metadata object.
    selected = body.get("processingTime", {}).get("selectedSongMetadata") if isinstance(body.get("processingTime"), dict) else None
    metadata = body.get("metadata")
    direct = body.get("selectedSongMetadata")
    merged: dict[str, Any] = {}
    for obj in (metadata, selected, direct):
        if isinstance(obj, dict):
            merged.update(obj)
    return merged


def _metadata_value(obj: Mapping[str, Any], names: tuple[str, ...]) -> Any:
    for name in names:
        if name in obj and obj[name] not in (None, ""):
            return obj[name]
    return None


def _metadata_text(obj: Mapping[str, Any], names: tuple[str, ...]) -> str:
    value = _metadata_value(obj, names)
    return _text(value) if value is not None and not isinstance(value, (dict, list)) else ""


def _metadata_duration(obj: Any) -> float | None:
    if not isinstance(obj, dict):
        return None
    for name in ("duration", "durationSeconds", "durationSec", "totalDuration", "songDuration"):
        if name in obj and obj[name] not in (None, ""):
            return _duration_seconds(obj[name])
    if "durationMs" in obj and obj["durationMs"] not in (None, ""):
        milliseconds = _number(obj["durationMs"])
        return milliseconds / 1000.0 if milliseconds is not None and milliseconds >= 0 else None
    return None


def _duration_seconds(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, str) and ":" in value:
        parts = value.strip().split(":")
        try:
            if len(parts) == 2:
                minutes, seconds = float(parts[0]), float(parts[1])
                if minutes < 0 or seconds < 0 or seconds >= 60:
                    return None
                number = minutes * 60.0 + seconds
            elif len(parts) == 3:
                hours, minutes, seconds = (float(part) for part in parts)
                if hours < 0 or minutes < 0 or minutes >= 60 or seconds < 0 or seconds >= 60:
                    return None
                number = hours * 3600.0 + minutes * 60.0 + seconds
            else:
                return None
        except (TypeError, ValueError):
            return None
    else:
        number = _number(value)
    return number if number is not None and number >= 0 else None


def _kpoe_item_has_text(item: Mapping[str, Any]) -> bool:
    for key in ("text", "content", "line", "lyrics"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return True
    syllabus = item.get("syllabus")
    return isinstance(syllabus, list) and any(isinstance(x, dict) and _text(x.get("text") or x.get("content")) for x in syllabus)
