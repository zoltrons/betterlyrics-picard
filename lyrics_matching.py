"""Conservative recording identity matching for lyrics providers.

The score in this module is an identity-evidence score, not a probability and
not a measure of lyric quality.  Eligibility is decided by explicit metadata
gates before callers use a provider result.
"""

from __future__ import annotations

import math
import re
import unicodedata
from difflib import SequenceMatcher
from typing import Any, Iterable, Mapping


_ISRC_RE = re.compile(r"^[A-Z]{2}[A-Z0-9]{3}[0-9]{7}$")
_ISRC_SPLIT_RE = re.compile(r"[;,]")
_FEATURE_GROUP_RE = re.compile(r"[\[(]\s*(?:feat\.?|ft\.?|featuring)\s+([^\[\]()]+)[\])]", re.IGNORECASE)
_FEATURE_SUFFIX_RE = re.compile(r"\s+(?:feat\.?|ft\.?|featuring)\s+(.+)$", re.IGNORECASE)

# These are deliberately retained by normalize_text.  They are extracted
# separately so that "Title", "Title (Live)", and "Title (Remix)" do not
# accidentally become the same recording.
_VERSION_PATTERNS = (
    ("radio_edit", re.compile(r"\bradio\s+edit\b")),
    ("instrumental", re.compile(r"\binstrumental\b")),
    ("acoustic", re.compile(r"\bacoustic(?:\s+version)?\b")),
    ("remix", re.compile(r"\bremix(?:ed)?\b")),
    ("live", re.compile(r"\blive\b")),
    ("unplugged", re.compile(r"\bunplugged\b")),
    ("karaoke", re.compile(r"\bkaraoke\b")),
    ("demo", re.compile(r"\bdemo\b")),
    ("extended", re.compile(r"\bextended\b")),
    ("club", re.compile(r"\bclub\b")),
    ("edit", re.compile(r"\bedit\b")),
    ("remaster", re.compile(r"\b(?:re[- ]?master(?:ed)?|remaster(?:ed)?)\b")),
    ("mono", re.compile(r"\bmono\b")),
    ("stereo", re.compile(r"\bstereo\b")),
    ("sped_up", re.compile(r"\bsped\s+up\b")),
    ("slowed", re.compile(r"\bslowed(?:\s+down)?\b")),
    ("nightcore", re.compile(r"\bnightcore\b")),
    ("re_recorded", re.compile(r"\bre[- ]?recorded\b")),
)

_WEIGHTS = {
    "isrc": 0.50,
    "title": 0.25,
    "artist": 0.18,
    "album": 0.04,
    "duration": 0.03,
}


def normalize_text(value: Any) -> str:
    """Return punctuation/case-insensitive text while preserving version words."""

    if value is None:
        return ""
    text = unicodedata.normalize("NFKC", str(value)).casefold()
    # Fold accents without dropping non-Latin scripts.  Punctuation is a
    # separator, rather than something that joins words together.
    decomposed = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    text = "".join(" " if unicodedata.category(ch).startswith("P") else ch for ch in text)
    return " ".join(text.split())


def _values(value: Any) -> Iterable[Any]:
    if value is None:
        return ()
    if isinstance(value, (list, tuple, set, frozenset)):
        return value
    if isinstance(value, str):
        return _ISRC_SPLIT_RE.split(value)
    return (value,)


def normalize_isrcs(value: Any) -> frozenset[str]:
    """Return only valid ISRCs, accepting lists and semicolon-separated text."""

    result: set[str] = set()
    for item in _values(value):
        # Split each list member too; provider payloads sometimes contain a
        # mixed list such as ["US-...", "GB...;JP..."].
        for part in _ISRC_SPLIT_RE.split(str(item or "")):
            compact = re.sub(r"[\s-]", "", part).upper()
            if _ISRC_RE.fullmatch(compact):
                result.add(compact)
    return frozenset(result)


def parse_duration(value: Any) -> float | None:
    """Parse a duration into seconds; invalid, negative, and absent values are unknown."""

    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        if isinstance(value, str) and ":" in value:
            parts = value.strip().split(":")
            if len(parts) == 2:
                result = float(parts[0]) * 60.0 + float(parts[1])
            elif len(parts) == 3:
                result = float(parts[0]) * 3600.0 + float(parts[1]) * 60.0 + float(parts[2])
            else:
                return None
        else:
            result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    # Existing provider mappings use zero as an absent-duration sentinel.
    return result if math.isfinite(result) and result > 0 else None


def _verified(value: Any) -> bool:
    if value is None or value is False or value == 0:
        return False
    if isinstance(value, str) and value.strip().casefold() in {"false", "0", "no", "unverified"}:
        return False
    return True


def _similarity(left: Any, right: Any) -> float:
    a, b = normalize_text(left), normalize_text(right)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    return SequenceMatcher(None, a, b).ratio()


def _strong_text(left: Any, right: Any) -> bool:
    """Allow small spelling differences, not changed words or numbered parts."""

    a, b = normalize_text(left), normalize_text(right)
    if not a or not b:
        return False
    if a == b:
        return True
    if SequenceMatcher(None, a, b).ratio() < 0.90:
        return False
    aa, bb = a.split(), b.split()
    if len(aa) != len(bb):
        return False
    return all(
        x == y or (
            len(x) >= 5 and len(y) >= 5
            and not any(ch.isdigit() for ch in x + y)
            and SequenceMatcher(None, x, y).ratio() >= 0.85
        )
        for x, y in zip(aa, bb)
    )


def _recording_text(item: Mapping[str, Any]) -> tuple[str, str, frozenset[str]]:
    """Move explicit feature credits into shared evidence; never discard guests.

    Only explicit feat/ft/featuring annotations are movable. Primary artists
    and unlabelled collaborations remain intact, as do all version labels.
    """

    guests: set[str] = set()
    fields = []
    for field in ("title", "artist"):
        text = unicodedata.normalize("NFKC", str(item.get(field) or ""))

        def take_credit(match: re.Match) -> str:
            guests.add(normalize_text(match.group(1)))
            return " "

        text = _FEATURE_GROUP_RE.sub(take_credit, text)
        text = _FEATURE_SUFFIX_RE.sub(take_credit, text)
        fields.append(normalize_text(text))
    return fields[0], fields[1], frozenset(guests)


def _version_signature(item: Mapping[str, Any]) -> frozenset[str]:
    text = " ".join((_recording_text(item)[0], normalize_text(item.get("album"))))
    return frozenset(name for name, pattern in _VERSION_PATTERNS if pattern.search(text))


def _versions_compatible(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    a, b = _version_signature(left), _version_signature(right)
    if a != b:
        return False
    left_title = _recording_text(left)[0]
    right_title = _recording_text(right)[0]
    # Named remixes, live venues, and edit labels matter too, not just the
    # generic "remix"/"live" category.  Keep full version-bearing titles.
    has_title_version = any(pattern.search(left_title) or pattern.search(right_title) for _, pattern in _VERSION_PATTERNS)
    if has_title_version and left_title and right_title:
        return left_title == right_title
    return True


def _benign_remaster_annotation(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    # Preserve the established exact-ISRC remaster exception, but never extend
    # it to live/remix/edit versions or arbitrary changed title words.
    if (_version_signature(left) ^ _version_signature(right)) - {"remaster"}:
        return False
    suffix = r"\s+re\s?master(?:ed)?(?:\s+\d{4})?$"
    a, b = _recording_text(left)[0], _recording_text(right)[0]
    return bool(a and b and re.sub(suffix, "", a) == re.sub(suffix, "", b))


def _duration_score(left: float | None, right: float | None, tolerance: float) -> float:
    if left is None or right is None:
        return 0.0
    delta = abs(left - right)
    if tolerance <= 0:
        return 1.0 if delta == 0 else 0.0
    return max(0.0, 1.0 - delta / tolerance)


def _identity(target: Mapping[str, Any], candidate: Mapping[str, Any]) -> str:
    target_isrcs = normalize_isrcs(target.get("isrc"))
    candidate_isrcs = normalize_isrcs(candidate.get("isrc"))
    common = sorted(target_isrcs & candidate_isrcs)
    if common:
        return "isrc:" + ",".join(common)
    if candidate_isrcs:
        return "isrc:" + ",".join(sorted(candidate_isrcs))
    title, artist, guests = _recording_text(candidate)
    return "meta:" + "|".join(
        (
            title,
            artist,
            ",".join(sorted(guests)),
            normalize_text(candidate.get("album", "")),
            ",".join(sorted(_version_signature(candidate))),
        )
    )


def _gross_metadata_conflict(target: Mapping[str, Any], candidate: Mapping[str, Any]) -> str | None:
    title, artist, guests = _recording_text(target)
    other_title, other_artist, other_guests = _recording_text(candidate)
    if guests != other_guests:
        return "artist_conflict"
    if title and other_title and _similarity(title, other_title) < 0.45:
        return "title_conflict"
    if artist and other_artist and _similarity(artist, other_artist) < 0.45:
        return "artist_conflict"
    if not _versions_compatible(target, candidate):
        return "version_conflict"
    return None


def assess_match(target: dict, candidate: dict, tolerance: float = 5) -> dict:
    """Assess whether *candidate* can represent *target*.

    A candidate without duration is eligible only with an exact valid ISRC, or
    with strong title+artist evidence and a matching album. Unverified metadata
    requires a supported Boidu/LRCLIB query contract. Known conflicts override
    those contracts. An exact ISRC still has to pass gross title/artist and
    duration checks; its version exception is limited to remaster annotations.
    """

    target = target if isinstance(target, Mapping) else {}
    candidate = candidate if isinstance(candidate, Mapping) else {}
    tolerance = max(0.0, float(tolerance))
    target_isrcs = normalize_isrcs(target.get("isrc"))
    candidate_isrcs = normalize_isrcs(candidate.get("isrc"))
    common_isrcs = target_isrcs & candidate_isrcs
    exact_isrc = bool(common_isrcs)
    title, artist, _ = _recording_text(target)
    other_title, other_artist, _ = _recording_text(candidate)
    title_score = _similarity(title, other_title)
    artist_score = _similarity(artist, other_artist)
    album_score = _similarity(target.get("album"), candidate.get("album"))
    target_duration = parse_duration(target.get("duration"))
    candidate_duration = parse_duration(candidate.get("duration"))

    # Score is deliberately computed even for rejected items so ranking can
    # explain why a near result lost; missing fields contribute zero.
    score = (
        (_WEIGHTS["isrc"] if exact_isrc else 0.0)
        + (_WEIGHTS["title"] * title_score if target.get("title") and candidate.get("title") else 0.0)
        + (_WEIGHTS["artist"] * artist_score if target.get("artist") and candidate.get("artist") else 0.0)
        + (_WEIGHTS["album"] * album_score if target.get("album") and candidate.get("album") else 0.0)
        + (_WEIGHTS["duration"] * _duration_score(target_duration, candidate_duration, tolerance))
    )
    score = max(0.0, min(1.0, score))
    identity = _identity(target, candidate)

    reason = ""
    eligible = True
    conflict = _gross_metadata_conflict(target, candidate)
    verified = _verified(candidate.get("metadata_verified", True))
    if verified and exact_isrc and conflict == "version_conflict" and _benign_remaster_annotation(target, candidate):
        conflict = None
    # Query-bound metadata may be incomplete, but known contradictions always
    # win over provider trust flags, scores, and exact lookup provenance.
    if target_isrcs and candidate_isrcs and not common_isrcs:
        eligible, reason = False, "isrc_conflict"
    elif conflict:
        eligible, reason = False, conflict
    elif target_duration is not None and candidate_duration is not None and abs(target_duration - candidate_duration) > tolerance:
        eligible, reason = False, "duration_conflict"
    elif not verified:
        # Boidu's public cache endpoint is keyed by the exact title/artist/
        # duration query but often returns only TTML, without metadata. Treat
        # that narrow provider contract as evidence, not as a generic bypass.
        query_bound = bool(candidate.get("query_bound"))
        if (
            query_bound
            and candidate.get("provider_id") == "boidu"
            and _strong_text(title, other_title)
            and _strong_text(artist, other_artist)
        ):
            eligible, reason = True, "provider_query_bound"
        elif (
            candidate.get("exact_lookup")
            and candidate.get("provider_id") == "lrclib"
            and _strong_text(title, other_title)
            and _strong_text(artist, other_artist)
        ):
            eligible, reason = True, "provider_exact_lookup"
        else:
            eligible, reason = False, "metadata_unverified"
    else:
        if exact_isrc:
            eligible, reason = True, "exact_isrc"
        elif not _strong_text(title, other_title):
            eligible, reason = False, "title_insufficient"
        elif not _strong_text(artist, other_artist):
            eligible, reason = False, "artist_insufficient"
        elif target_duration is None or candidate_duration is None:
            if not (target.get("album") and candidate.get("album") and album_score >= 0.90):
                eligible, reason = False, "duration_missing"
            else:
                eligible, reason = True, "strong_metadata_no_duration"
        else:
            eligible, reason = True, "strong_metadata"

    return {"eligible": bool(eligible), "score": float(score), "reason": reason, "identity": identity}


def rank_candidates(target: dict, candidates: Iterable[dict], tolerance: float = 5) -> list[dict]:
    """Return annotated shallow copies, with eligible identity matches first."""

    ranked: list[dict] = []
    for candidate in candidates or ():
        copy = dict(candidate) if isinstance(candidate, Mapping) else {}
        result = assess_match(target, copy, tolerance)
        copy["score"] = result["score"]
        copy["match_eligible"] = result["eligible"]
        copy["match_reason"] = result["reason"]
        copy["identity"] = result["identity"]
        ranked.append(copy)
    ranked.sort(key=lambda item: (bool(item.get("match_eligible")), float(item.get("score", 0.0))), reverse=True)
    return ranked


def same_recording(a: dict, b: dict, tolerance: float = 5) -> bool:
    """Return true only for compatible verified identities and known close lengths."""

    if not isinstance(a, Mapping) or not isinstance(b, Mapping):
        return False
    if not _verified(a.get("metadata_verified", True)) or not _verified(b.get("metadata_verified", True)):
        return False
    da, db = parse_duration(a.get("duration")), parse_duration(b.get("duration"))
    if da is None or db is None or abs(da - db) > max(0.0, float(tolerance)):
        return False
    a_isrcs, b_isrcs = normalize_isrcs(a.get("isrc")), normalize_isrcs(b.get("isrc"))
    if a_isrcs and b_isrcs and not a_isrcs.intersection(b_isrcs):
        return False
    if not _versions_compatible(a, b):
        return False
    if a_isrcs.intersection(b_isrcs):
        return _gross_metadata_conflict(a, b) is None
    # Eligibility permits small provider typos.  Grouping is stricter: fuzzy
    # title matches must not silently group adjacent songs on the same album.
    text = _recording_text(a)
    return bool(text[0] and text[1] and text == _recording_text(b))


def _metadata_tie_choice(target: dict, contenders: list[dict], tolerance: float) -> dict | None:
    """Choose one release identity, without claiming distinct ISRCs equivalent."""

    if normalize_isrcs(target.get("isrc")):
        return None
    text = _recording_text(target)
    album = normalize_text(target.get("album"))
    duration = parse_duration(target.get("duration"))
    if not text[0] or not text[1] or not album or duration is None:
        return None
    durations = [duration]
    for item in contenders:
        other_duration = parse_duration(item.get("duration"))
        if (
            not _verified(item.get("metadata_verified", True))
            or _recording_text(item) != text
            or normalize_text(item.get("album")) != album
            or other_duration is None
            or not _versions_compatible(target, item)
        ):
            return None
        durations.append(other_duration)
    if max(durations) - min(durations) > max(0.0, float(tolerance)):
        return None
    # Scores prefer the closest duration; lexical identity breaks exact ties
    # reproducibly even when provider response order changes.
    best = min(contenders, key=lambda item: (
        -item["score"], item["identity"], str(item.get("provider_id") or ""),
        str(item.get("id") or ""), str(item.get("url") or ""),
    ))
    return dict(best, match_reason="exact_metadata_tie_break")


def choose_identity_group(target: dict, candidates: Iterable[dict], tolerance: float = 5, margin: float = 0.03) -> list[dict]:
    """Return one identity group, or [] on an unresolved identity tie.

    Unlike fuzzy eligibility, group membership requires same_recording.  This
    also keeps candidates of unknown duration separate: there is no duration
    evidence to prove they can safely be used for quality-based fallback. A
    narrowly exact metadata tie may select ONE candidate, never a merged group.
    """

    ranked = rank_candidates(target, candidates, tolerance)
    eligible = [item for item in ranked if item.get("match_eligible")]
    if not eligible:
        return []
    best = eligible[0]
    contenders = [item for item in eligible if best["score"] - item["score"] <= max(0.0, float(margin))]
    # Pairwise checks prevent a metadata-only or multi-ISRC candidate from
    # bridging two records whose explicit ISRCs contradict each other.
    if any(
        not same_recording(item, other, tolerance)
        for index, item in enumerate(contenders)
        for other in contenders[index + 1:]
    ):
        choice = _metadata_tie_choice(target, contenders, tolerance)
        return [choice] if choice is not None else []
    group = [best]
    for item in eligible[1:]:
        if all(same_recording(member, item, tolerance) for member in group):
            group.append(item)
    return group


def ambiguity(target: dict, candidates: Iterable[dict], tolerance: float = 5, margin: float = 0.03) -> bool:
    """Report distinct identities, even if exact metadata permits choosing one."""

    ranked = rank_candidates(target, candidates, tolerance)
    eligible = [item for item in ranked if item.get("match_eligible")]
    if len(eligible) < 2:
        return False
    best = eligible[0]
    contenders = [item for item in eligible if best["score"] - item["score"] <= max(0.0, float(margin))]
    return any(
        not same_recording(item, other, tolerance)
        for index, item in enumerate(contenders)
        for other in contenders[index + 1:]
    )


__all__ = [
    "assess_match",
    "rank_candidates",
    "same_recording",
    "ambiguity",
    "choose_identity_group",
    "normalize_text",
    "normalize_isrcs",
    "parse_duration",
]
