"""Small, dependency-free model and codecs for timed lyrics.

The public functions in this module deliberately use ordinary dictionaries.  A
document is safe to copy, serialise, and pass between the provider and Picard
layers without importing the GUI plugin.
"""

from __future__ import annotations

import copy
import json
import math
import re
import xml.etree.ElementTree as ET
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any


_TTML = "http://www.w3.org/ns/ttml"
_TTML_META = "http://www.w3.org/ns/ttml#metadata"
_XML = "http://www.w3.org/XML/1998/namespace"
_TIMING_RE = re.compile(r"^\s*(?:(\d+):)?(\d+):(\d{2})(?:\.(\d+))?\s*$")
_LRC_TIME_RE = re.compile(r"\[(\d{1,3}:\d{2}(?:\.\d+)?)\]")
_LRC_WORD_RE = re.compile(r"<(?P<time>\d{1,3}:\d{2}(?:\.\d+)?)>")


def _doc(lines: list[dict] | None = None, metadata: dict | None = None) -> dict:
    return {
        "lines": lines or [],
        "metadata": metadata or {},
        "quality": "none",
    }


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


def _round_ms(number: Any) -> int | None:
    try:
        decimal = Decimal(str(number))
        if not decimal.is_finite() or decimal < 0:
            return None
        return int(decimal.to_integral_value(rounding=ROUND_HALF_UP))
    except (InvalidOperation, ValueError, OverflowError):
        return None


def parse_time_ms(value: str) -> int | None:
    """Parse common lyric/TTML time strings into milliseconds.

    Accepted values are ``MM:SS.frac``, ``HH:MM:SS.frac``, and non-negative
    numeric offsets with ``ms``, ``s``, ``m`` or ``h`` suffixes.  Invalid,
    negative, and non-finite values return ``None`` rather than becoming a
    misleading timestamp.
    """
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    if not text or text.casefold() in {"nan", "+nan", "-nan", "inf", "+inf", "-inf"}:
        return None
    if text.casefold() == "zero":
        return 0
    match = _TIMING_RE.fullmatch(text)
    if match:
        hours, minutes, seconds, fraction = match.groups()
        try:
            h = int(hours or 0)
            m = int(minutes)
            s = int(seconds)
            if (hours is not None and m >= 60) or s >= 60:
                return None
            frac = Decimal("0." + (fraction or "0"))
            return _round_ms((Decimal(h * 3600 + m * 60 + s) + frac) * 1000)
        except (ValueError, InvalidOperation):
            return None
    unit_match = re.fullmatch(r"([+]?(?:\d+(?:\.\d*)?|\.\d+))\s*(ms|s|m|h)?", text, re.I)
    if not unit_match:
        return None
    try:
        number = float(unit_match.group(1))
    except (ValueError, OverflowError):
        return None
    if not math.isfinite(number) or number < 0:
        return None
    multiplier = {None: 1000.0, "ms": 1.0, "s": 1000.0, "m": 60000.0, "h": 3600000.0}
    return _round_ms(number * multiplier[unit_match.group(2).lower() if unit_match.group(2) else None])


def _refresh_quality(document: dict) -> dict:
    has_line = any(line.get("start_ms") is not None for line in document.get("lines", []))
    has_word = any(
        word.get("start_ms") is not None
        for line in document.get("lines", [])
        for word in line.get("words", [])
    )
    if has_word:
        document["quality"] = "word"
    elif has_line:
        document["quality"] = "line"
    elif document.get("lines"):
        document["quality"] = "plain"
    else:
        document["quality"] = "none"
    return document


def _tag(element: ET.Element) -> str:
    return element.tag.rsplit("}", 1)[-1] if isinstance(element.tag, str) else ""


def _attr(element: ET.Element, *names: str) -> str | None:
    wanted = set(names)
    for key, value in element.attrib.items():
        local = key.rsplit("}", 1)[-1]
        if key in wanted or local in wanted:
            return value
    return None


def _text(element: ET.Element) -> str:
    # ElementTree's itertext emits each text/tail once, including whitespace;
    # unlike repeatedly calling itertext on nested spans this cannot duplicate it.
    return "".join(element.itertext())


def _metadata_from_ttml(root: ET.Element) -> dict:
    metadata: dict[str, Any] = {}
    agents: dict[str, Any] = {}
    for element in root.iter():
        tag = _tag(element)
        value = (_text(element) or "").strip()
        if not value:
            continue
        if tag in {"title", "name", "artist", "album", "song"} and tag not in metadata:
            metadata[tag] = value
        if tag == "agent":
            ident = element.get("{" + _XML + "}id") or element.get("id")
            if ident:
                agents[ident] = value
    if agents:
        metadata["agents"] = agents
    return metadata


def _timing(
    element: ET.Element,
    parent_start: int = 0,
    parent_end: int | None = None,
    relative: bool = False,
) -> tuple[int | None, int | None]:
    begin_raw = _attr(element, "begin")
    end_raw = _attr(element, "end")
    dur_raw = _attr(element, "dur")
    begin = 0 if begin_raw is None else parse_time_ms(begin_raw)
    if begin is None:
        return None, None
    # Apple/LRC.red exports use media-absolute explicit timestamps, including
    # span timestamps.  A ``seq`` timing container is the explicit exception:
    # its child offsets are relative to the preceding child/container.
    start = parent_start if begin_raw is None else (parent_start + begin if relative else begin)
    end: int | None = parent_end
    if end_raw is not None:
        parsed_end = parse_time_ms(end_raw)
        end = None if parsed_end is None else (parent_start + parsed_end if relative else parsed_end)
    if dur_raw is not None:
        duration = parse_time_ms(dur_raw)
        if duration is None:
            return None, None
        end = start + duration
    if end is not None and end < start:
        return None, None
    return start, end


def _span_words(
    line_element: ET.Element,
    line_start: int | None,
    line_end: int | None,
    metadata: dict | None = None,
) -> list[dict]:
    words: list[dict] = []

    def add(text: str | None, start: int | None, end: int | None, attrs: dict) -> None:
        if text:
            if start is None and text.isspace() and words:
                words[-1]["text"] += text
                return
            words.append({"start_ms": start, "end_ms": end, "text": text, **attrs})

    def visit(element: ET.Element, parent_start: int, parent_end: int | None, inherited: dict) -> None:
        has_timing = any(_attr(element, key) is not None for key in ("begin", "end", "dur"))
        # Explicit span times are absolute media times.  Untimed spans inherit
        # the line bounds; nested ``seq`` spans are uncommon and intentionally
        # remain conservative rather than inventing timing.
        start, end = _timing(element, parent_start, parent_end, relative=False)
        attrs = {**inherited, **_line_attrs(element, metadata or {})}
        descendants_timed = any(
            _tag(descendant) == "span" and any(_attr(descendant, key) is not None for key in ("begin", "end", "dur"))
            for descendant in element.iter()
            if descendant is not element
        )
        if has_timing and start is not None:
            # A wrapper span around explicitly timed children is a container,
            # not an additional word.  This is the key anti-duplication rule.
            if not descendants_timed:
                explicit_end = end if _attr(element, "end", "dur") is not None else None
                add(_text(element), start, explicit_end, attrs)
                return
        add(element.text, None, None, attrs)
        for child in list(element):
            visit(child, start if start is not None else parent_start, end, attrs)
            add(child.tail, None, None, attrs)

    add(line_element.text, None, None, {})
    for child in list(line_element):
        visit(child, line_start or 0, line_end, {})
        add(child.tail, None, None, {})
    return words if any(word["start_ms"] is not None for word in words) else []


def _line_attrs(element: ET.Element, metadata: dict) -> dict:
    result: dict[str, Any] = {}
    agent = _attr(element, "agent", "singer")
    role = _attr(element, "role")
    if agent:
        agents = metadata.get("agents", {}) if isinstance(metadata, dict) else {}
        result["singer"] = agents.get(agent, agent) if isinstance(agents, dict) else agent
    if role:
        result["role"] = role
    for key, aliases in {
        "translation": ("translation", "data-translation"),
        "romanized": ("romanized", "transliteration", "data-romanized"),
    }.items():
        value = _attr(element, *aliases)
        if value is not None:
            result[key] = value
    return result


def parse_ttml(text: str) -> dict:
    """Parse TTML while retaining the exact source as ``native_ttml``.

    For interoperability with Apple/LRC.red files, explicit ``begin``/``end``
    values (clock forms and unit forms alike) are treated as media-absolute.
    Timing is accumulated only for an explicit ``timeContainer="seq"``;
    omitted child times inherit the enclosing bound.  This avoids doubling a
    line's timestamp when its spans carry the same absolute media timestamps.
    """
    document = _doc()
    if not isinstance(text, str) or not text.strip():
        return document
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        document["metadata"]["parse_error"] = str(exc)
        return document
    if _tag(root) != "tt":
        document["metadata"]["parse_error"] = "not TTML"
        return document
    document["native_ttml"] = text
    document["metadata"].update(_metadata_from_ttml(root))

    def walk(container: ET.Element, container_start: int, container_end: int | None, inherited: dict) -> None:
        current = container_start
        attrs = {**inherited, **_line_attrs(container, document["metadata"])}
        sequential = (_attr(container, "timeContainer") or "").casefold() == "seq"
        for child in list(container):
            child_start_base = current if sequential else container_start
            child_start, child_end = _timing(child, child_start_base, container_end, relative=sequential)
            if child_start is None:
                continue
            if _tag(child) == "p":
                explicitly_timed = any(_attr(child, key) is not None for key in ("begin", "end", "dur"))
                words = _span_words(child, child_start, child_end, document["metadata"])
                line = {
                    "start_ms": child_start if explicitly_timed else (min((w["start_ms"] for w in words if w["start_ms"] is not None), default=None)),
                    "end_ms": child_end,
                    "text": _text(child),
                    "words": words,
                }
                line.update({**attrs, **_line_attrs(child, document["metadata"])})
                if line["text"] or line["words"]:
                    document["lines"].append(line)
            else:
                walk(child, child_start, child_end, attrs)
            if sequential:
                current = child_end if child_end is not None else child_start

    root_start, root_end = _timing(root, 0, None)
    if root_start is not None:
        walk(root, root_start, root_end, {})
    if document["lines"]:
        document["native_ttml"] = text
        document["source_native_ttml"] = text
    return _refresh_quality(document)


def _lrc_timestamp(value: str) -> int | None:
    return parse_time_ms(value)


def _parse_lrc_words(value: str) -> tuple[str, list[dict]]:
    matches = list(_LRC_WORD_RE.finditer(value))
    if not matches:
        return value, []
    words: list[dict] = []
    plain_parts: list[str] = []
    first_text_end = matches[0].start()
    plain_parts.append(value[:first_text_end])
    for index, match in enumerate(matches):
        start = _lrc_timestamp(match.group("time"))
        if start is None:
            continue
        chunk_start = match.end()
        chunk_end = matches[index + 1].start() if index + 1 < len(matches) else len(value)
        chunk = value[chunk_start:chunk_end]
        plain_parts.append(chunk)
        if chunk:
            # Enhanced LRC markers denote starts; a following marker is not
            # converted into a fabricated end time.
            words.append({"start_ms": start, "end_ms": None, "text": chunk})
    return "".join(plain_parts), words


def parse_lrc(text: str) -> dict:
    """Parse standard and enhanced LRC, including multiple line timestamps."""
    document = _doc()
    if not isinstance(text, str) or not text.strip():
        return document
    stripped = text.lstrip()
    if stripped.startswith("<") and re.match(r"<\s*(?:!doctype|html|body|script|head|div|p|tt|\?xml)", stripped, re.I):
        document["metadata"]["rejected"] = "html"
        return document
    if stripped.startswith("{") or (stripped.startswith("[") and not _LRC_TIME_RE.match(stripped)):
        try:
            json.loads(stripped)
        except (TypeError, ValueError):
            pass
        else:
            document["metadata"]["rejected"] = "json"
            return document

    offset = 0
    for offset_value in re.findall(r"\[offset:\s*([+-]?\d+(?:\.\d+)?)\s*\]", text, re.I):
        try:
            number = Decimal(offset_value)
            if number.is_finite():
                offset = int(number.to_integral_value(rounding=ROUND_HALF_UP))
        except (InvalidOperation, ValueError, OverflowError):
            pass
    for raw_line in text.splitlines():
        line = raw_line.rstrip("\r")
        for key, value in re.findall(r"\[([A-Za-z]+):([^\]]*)\]", line):
            if key.casefold() == "offset":
                continue
            elif key.casefold() in {"ar", "al", "by", "ti", "re", "ve"}:
                document["metadata"][key.casefold()] = value.strip()
        timestamps = []
        remainder = line.lstrip()
        while True:
            match = _LRC_TIME_RE.match(remainder)
            if not match:
                break
            timestamps.append(match)
            remainder = remainder[match.end():]
        if timestamps:
            content = remainder
            plain, words = _parse_lrc_words(content)
            for marker in timestamps:
                start = _lrc_timestamp(marker.group(1))
                if start is None:
                    continue
                start += offset
                if start < 0:
                    start = 0
                adjusted_words = copy.deepcopy(words)
                # Repeated line timestamps repeat the inline word pattern at
                # the same relative positions within each repeated line.
                first_start = _lrc_timestamp(timestamps[0].group(1))
                repetition_shift = (_lrc_timestamp(marker.group(1)) or 0) - (first_start or 0)
                for word in adjusted_words:
                    if word.get("start_ms") is not None:
                        word["start_ms"] += offset + repetition_shift
                        if word["start_ms"] < 0:
                            word["start_ms"] = 0
                item = {"start_ms": start, "end_ms": None, "text": plain, "words": adjusted_words}
                document["lines"].append(item)
        elif line.strip() and not re.match(r"^\s*\[[A-Za-z]+:", line):
            plain, words = _parse_lrc_words(line)
            for word in words:
                if word["start_ms"] is not None:
                    word["start_ms"] = max(0, word["start_ms"] + offset)
            start = min((w["start_ms"] for w in words if w["start_ms"] is not None), default=None)
            document["lines"].append({"start_ms": start, "end_ms": None, "text": plain, "words": words})
    if offset:
        document["metadata"]["offset_ms"] = offset
    return _refresh_quality(document)


def _kpoe_value(value: Any, *, duration: bool = False) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return _round_ms(float(value))
    value_text = str(value).strip()
    if not value_text:
        return None
    if re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)", value_text):
        try:
            number = float(value_text)
        except ValueError:
            return None
        return _round_ms(number)
    return parse_time_ms(value_text)


def parse_kpoe(payload: dict) -> dict:
    """Parse KPOE v2's dictionary representation without trusting bad times."""
    document = _doc()
    if not isinstance(payload, dict):
        return document
    metadata = copy.deepcopy(payload.get("metadata")) if isinstance(payload.get("metadata"), dict) else {}
    known = {"lines", "lyrics", "metadata", "version", "type", "format"}
    for key, value in payload.items():
        if key not in known:
            metadata.setdefault("kpoe_extra", {})[key] = copy.deepcopy(value)
    document["metadata"] = metadata
    metadata["kpoe_raw"] = copy.deepcopy(payload)
    entries = payload.get("lines", payload.get("lyrics", []))
    if not isinstance(entries, list):
        return document
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        kind = str(entry.get("type", payload.get("type", "Line"))).casefold()
        start = _kpoe_value(entry.get("time", entry.get("start", entry.get("startTime"))))
        duration = _kpoe_value(entry.get("duration", entry.get("dur")))
        end = start + duration if start is not None and duration is not None else None
        text = entry.get("text", entry.get("content", entry.get("data", "")))
        text = text if isinstance(text, str) else str(text or "")
        words: list[dict] = []
        syllabus = entry.get("syllabus", entry.get("syllables", entry.get("words", entry.get("elements", []))))
        if isinstance(syllabus, list):
            for raw_word in syllabus:
                if not isinstance(raw_word, dict):
                    continue
                word_start = _kpoe_value(raw_word.get("time", raw_word.get("start", raw_word.get("startTime"))))
                word_duration = _kpoe_value(raw_word.get("duration", raw_word.get("dur")))
                word_end = word_start + word_duration if word_start is not None and word_duration is not None else None
                word_text = raw_word.get("text", raw_word.get("content", raw_word.get("data", "")))
                if not isinstance(word_text, str):
                    word_text = str(word_text or "")
                if word_text or word_start is not None:
                    word = {"start_ms": word_start, "end_ms": word_end, "text": word_text}
                    transliteration = raw_word.get("romanized", raw_word.get("transliteration"))
                    if transliteration is not None:
                        word["romanized"] = str(transliteration)
                    word_element = raw_word.get("element")
                    for word_key in ("singer", "role"):
                        value = raw_word.get(word_key)
                        if value is None and isinstance(word_element, dict):
                            value = word_element.get(word_key)
                        if value is not None:
                            word[word_key] = copy.deepcopy(value)
                    words.append(word)
        if kind == "none":
            start = end = None
            for word in words:
                word["start_ms"] = word["end_ms"] = None
        if not text and words:
            text = "".join(word["text"] for word in words)
        line: dict[str, Any] = {"start_ms": start, "end_ms": end, "text": text, "words": words}
        for key in ("singer", "role", "translation", "romanized", "transliteration"):
            value = entry.get(key)
            if value is not None:
                normalized_key = "romanized" if key == "transliteration" else key
                if normalized_key == "romanized" and isinstance(value, dict):
                    line["romanized"] = str(value.get("text", value.get("content", "")))
                    raw_words = value.get("syllabus", value.get("words", []))
                    if isinstance(raw_words, list):
                        roman_words = []
                        for raw_word in raw_words:
                            if not isinstance(raw_word, dict):
                                continue
                            word_start = _kpoe_value(raw_word.get("time", raw_word.get("start", raw_word.get("startTime"))))
                            word_duration = _kpoe_value(raw_word.get("duration", raw_word.get("dur")))
                            roman_words.append({
                                "start_ms": word_start,
                                "end_ms": word_start + word_duration if word_start is not None and word_duration is not None else None,
                                "text": str(raw_word.get("text", raw_word.get("content", ""))),
                            })
                        line["romanized_words"] = roman_words
                else:
                    line[normalized_key] = value
        romanized_words = entry.get("romanized_words", entry.get("transliterationWords"))
        if isinstance(romanized_words, list):
            line["romanized_words"] = copy.deepcopy(romanized_words)
        element = entry.get("element")
        if isinstance(element, dict):
            if element.get("singer") is not None and "singer" not in line:
                line["singer"] = element["singer"]
            if element.get("role") is not None and "role" not in line:
                line["role"] = element["role"]
        if line["text"] or line["words"] or line["start_ms"] is not None:
            document["lines"].append(line)
    transliteration = payload.get("transliteration")
    if isinstance(transliteration, dict):
        transliteration = transliteration.get("lyrics", transliteration.get("lines", []))
    if isinstance(transliteration, list):
        for index, romanized in enumerate(transliteration):
            if index >= len(document["lines"]):
                break
            if isinstance(romanized, str):
                document["lines"][index]["romanized"] = romanized
            elif isinstance(romanized, dict):
                document["lines"][index]["romanized"] = str(romanized.get("text", ""))
                raw_words = romanized.get("syllabus", romanized.get("words", []))
                if isinstance(raw_words, list):
                    roman_words = []
                    for raw_word in raw_words:
                        if not isinstance(raw_word, dict):
                            continue
                        start = _kpoe_value(raw_word.get("time"))
                        duration = _kpoe_value(raw_word.get("duration"))
                        roman_words.append({
                            "start_ms": start,
                            "end_ms": start + duration if start is not None and duration is not None else None,
                            "text": str(raw_word.get("text", "")),
                        })
                    document["lines"][index]["romanized_words"] = roman_words
    return _refresh_quality(document)


def _shifted(document: dict, offset_ms: int) -> dict:
    result = copy.deepcopy(document)
    result.setdefault("metadata", {})
    if not offset_ms:
        return _refresh_quality(result)
    for line in result.get("lines", []):
        for field in ("start_ms", "end_ms"):
            if line.get(field) is not None:
                line[field] += offset_ms
                if line[field] < 0:
                    line[field] = 0
        for word in line.get("words", []):
            for field in ("start_ms", "end_ms"):
                if word.get(field) is not None:
                    word[field] += offset_ms
                    if word[field] < 0:
                        word[field] = 0
        for word in line.get("romanized_words", []):
            if isinstance(word, dict):
                for field in ("start_ms", "end_ms"):
                    if word.get(field) is not None:
                        word[field] = max(0, word[field] + offset_ms)
    result["metadata"]["offset_ms"] = result["metadata"].get("offset_ms", 0) + offset_ms
    return _refresh_quality(result)


def _format_lrc_ms(value: int) -> str:
    minutes, remainder = divmod(max(0, int(value)), 60000)
    seconds, millis = divmod(remainder, 1000)
    return f"{minutes:02d}:{seconds:02d}.{millis:03d}"


def render_lrc(document: dict, enhanced: bool = True) -> str:
    """Render LRC; enhanced words retain starts and never invent ends."""
    output: list[str] = []
    for line in document.get("lines", []):
        start = line.get("start_ms")
        if start is None:
            if line.get("text"):
                output.append(str(line["text"]))
            continue
        text = line.get("text", "")
        words = line.get("words") or []
        if enhanced and words:
            word_text = "".join(str(word.get("text", "")) for word in words)
            prefix = text[:-len(word_text)] if word_text and str(text).endswith(word_text) else (str(text) if not word_text else "")
            rendered = prefix
            for word in words:
                if word.get("start_ms") is not None:
                    rendered += f"<{_format_lrc_ms(word['start_ms'])}>"
                rendered += str(word.get("text", ""))
            text = rendered
        output.append(f"[{_format_lrc_ms(start)}]{text}")
    return "\n".join(output)


def render_ttml(document: dict) -> str:
    """Render a generated TTML document from the neutral model."""
    ET.register_namespace("", _TTML)
    ET.register_namespace("ttm", _TTML_META)
    root = ET.Element("{" + _TTML + "}tt")
    metadata = document.get("metadata", {})
    agents = metadata.get("agents", {})
    if metadata.get("title") or agents:
        head = ET.SubElement(root, "{" + _TTML + "}head")
        meta = ET.SubElement(head, "{" + _TTML + "}metadata")
        if metadata.get("title"):
            ET.SubElement(meta, "{" + _TTML_META + "}title").text = str(metadata["title"])
        if isinstance(agents, dict):
            for ident, name in agents.items():
                if isinstance(name, dict):
                    agent_id = name.get("id", ident)
                    agent_type = name.get("type", "person")
                    agent_name = name.get("name", name.get("text", ""))
                else:
                    agent_id, agent_type, agent_name = ident, "person", name
                agent = ET.SubElement(meta, "{" + _TTML_META + "}agent", {"{" + _XML + "}id": str(agent_id), "type": str(agent_type)})
                ET.SubElement(agent, "{" + _TTML_META + "}name").text = str(agent_name)
    body = ET.SubElement(root, "{" + _TTML + "}body")
    div = ET.SubElement(body, "{" + _TTML + "}div")
    for line in document.get("lines", []):
        p = ET.SubElement(div, "{" + _TTML + "}p")
        if line.get("start_ms") is not None:
            p.set("begin", _format_lrc_ms(line["start_ms"]))
        if line.get("end_ms") is not None:
            p.set("end", _format_lrc_ms(line["end_ms"]))
        for key in ("singer", "role"):
            if line.get(key):
                value = line[key]
                if isinstance(value, dict):
                    value = value.get("id", value.get("name", value.get("type", "")))
                p.set("{" + _TTML_META + "}" + ("agent" if key == "singer" else "role"), str(value))
        for key, attr in (("translation", "data-translation"), ("romanized", "data-romanized")):
            if line.get(key) is not None:
                value = line[key]
                if isinstance(value, dict):
                    value = value.get("text", value.get("content", ""))
                p.set(attr, str(value))
        if line.get("romanized_words") is not None:
            p.set("data-romanized-words", json.dumps(line["romanized_words"], ensure_ascii=False, separators=(",", ":")))
        words = line.get("words") or []
        if words:
            for word in words:
                span = ET.SubElement(p, "{" + _TTML + "}span")
                if word.get("start_ms") is not None:
                    span.set("begin", _format_lrc_ms(word["start_ms"]))
                if word.get("end_ms") is not None:
                    span.set("end", _format_lrc_ms(word["end_ms"]))
                for key in ("singer", "role"):
                    if word.get(key):
                        value = word[key]
                        if isinstance(value, dict):
                            value = value.get("id", value.get("name", value.get("type", "")))
                        span.set("{" + _TTML_META + "}" + ("agent" if key == "singer" else "role"), str(value))
                if word.get("romanized") is not None:
                    span.set("data-romanized", str(word["romanized"]))
                span.text = str(word.get("text", ""))
        else:
            p.text = str(line.get("text", ""))
    return ET.tostring(root, encoding="unicode")


def make_bundle(doc: dict, clean_words: bool = True, offset_ms: int = 0) -> dict:
    """Create compatible TTML/LRC/plain representations from a document."""
    source_doc = doc if isinstance(doc, dict) else _doc()
    try:
        offset = int(offset_ms)
    except (TypeError, ValueError):
        offset = 0
    document = _shifted(source_doc, offset)
    native = source_doc.get("native_ttml") if isinstance(source_doc.get("native_ttml"), str) else None
    already_shifted = bool(source_doc.get("metadata", {}).get("offset_ms"))
    if native and offset == 0 and not already_shifted and document.get("lines"):
        ttml = native
        origin = "native"
    else:
        ttml = render_ttml(document) if document.get("lines") else None
        origin = "generated"
    enhanced_lrc = render_lrc(document, enhanced=True)
    line_lrc = render_lrc(document, enhanced=False)
    selected_lrc = line_lrc if clean_words else enhanced_lrc
    plain = "\n".join(str(line.get("text", "")) for line in document.get("lines", []) if line.get("text") is not None)
    return {
        "ttml": ttml,
        "lrc": selected_lrc or None,
        "enhanced_lrc": enhanced_lrc or None,
        "line_lrc": line_lrc or None,
        "plain": plain or None,
        "is_synced": document.get("quality") in {"word", "line"},
        "quality": document.get("quality", "none"),
        "ttml_origin": origin,
        "source_native_ttml": native,
        "document": document,
        "source": document.get("source", document.get("metadata", {}).get("source")),
    }


__all__ = [
    "parse_time_ms", "parse_ttml", "parse_lrc", "parse_kpoe", "make_bundle",
    "render_ttml", "render_lrc",
]
