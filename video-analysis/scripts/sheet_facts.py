"""Deterministic, script-owned facts for study sheets."""
from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit

FACT_KEYS = ("title", "channel_name", "channel_url", "youtube_publication_date", "duration_s",
             "file_size_mb", "resolution", "view_count", "like_count", "comment_count",
             "audio_language", "manual_subtitle_languages", "automatic_caption_language_count",
             "description_links")


def _text(*values: Any) -> str | None:
    return next((v.strip() for v in values if isinstance(v, str) and v.strip()), None)


def _number(*values: Any, positive: bool = False) -> int | float | None:
    for value in values:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            continue
        if value >= 0 and (not positive or value > 0):
            return value
    return None


def _safe_url(value: Any) -> str | None:
    if (not isinstance(value, str) or any(c.isspace() or unicodedata.category(c) == "Cc" for c in value)
            or "\\" in value):
        return None
    try:
        parts = urlsplit(value)
        if parts.scheme.lower() not in {"http", "https"} or not parts.hostname or "@" in parts.netloc:
            return None
        _ = parts.port
        return value
    except ValueError:
        return None


def extract_description_links(description: object) -> list[str]:
    if not isinstance(description, str):
        return []
    found: list[str] = []
    for match in re.findall(r"https?://[^\s<>\"']+", description, flags=re.IGNORECASE):
        candidate = match.rstrip(".,;:!?)]}")
        safe = _safe_url(candidate)
        if safe and safe not in found:
            found.append(safe)
    return found


def _date(metadata: Mapping[str, Any], raw: Mapping[str, Any]) -> str | None:
    candidate = _text(metadata.get("upload_date"), raw.get("upload_date"))
    if candidate and re.fullmatch(r"\d{8}", candidate):
        return f"{candidate[:4]}-{candidate[4:6]}-{candidate[6:]}"
    if candidate and re.fullmatch(r"\d{4}-\d{2}-\d{2}", candidate):
        return candidate
    stamp = _number(raw.get("timestamp"))
    if stamp is not None:
        try:
            return datetime.fromtimestamp(stamp, timezone.utc).date().isoformat()
        except (OverflowError, OSError, ValueError):
            pass
    return None


def _resolution(metadata: Mapping[str, Any], raw: Mapping[str, Any], manifest: Mapping[str, Any]) -> str | None:
    value = _text(metadata.get("resolution"), raw.get("resolution"))
    if value:
        return value
    width = _number(raw.get("width"), manifest.get("width"), positive=True)
    height = _number(raw.get("height"), manifest.get("height"), positive=True)
    return f"{int(width)}x{int(height)}" if width and height else None


def _count(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def build_sheet_facts(video: Path, *, metadata: Mapping[str, Any], raw_info: Mapping[str, Any],
                      manifest: Mapping[str, Any], bundle: Mapping[str, Any]) -> dict[str, Any]:
    source, speech = bundle.get("source", {}), bundle.get("speech", {})
    source = source if isinstance(source, Mapping) else {}
    speech = speech if isinstance(speech, Mapping) else {}
    title = _text(metadata.get("title"), raw_info.get("title"), source.get("title"))
    channel = _text(raw_info.get("channel"), metadata.get("channel"), raw_info.get("creator"),
                    raw_info.get("uploader"), raw_info.get("author"), metadata.get("author"),
                    source.get("channel"), source.get("author"))
    channel_url = _safe_url(raw_info.get("channel_url")) or _safe_url(raw_info.get("uploader_url"))
    duration = _number(source.get("duration"), metadata.get("duration"), raw_info.get("duration"),
                       manifest.get("duration_s"), positive=True)
    try:
        size = video.stat().st_size / 1_048_576
    except OSError:
        size = None
    subtitles, automatic = raw_info.get("subtitles"), raw_info.get("automatic_captions")
    subtitles = subtitles if isinstance(subtitles, Mapping) else {}
    automatic = automatic if isinstance(automatic, Mapping) else {}
    facts: dict[str, Any] = {"title": title, "channel_name": channel, "channel_url": channel_url,
        "youtube_publication_date": _date(metadata, raw_info), "duration_s": duration, "file_size_mb": size,
        "resolution": _resolution(metadata, raw_info, manifest),
        "view_count": _count(raw_info.get("view_count")), "like_count": _count(raw_info.get("like_count")),
        "comment_count": _count(raw_info.get("comment_count")), "audio_language": _text(speech.get("language")),
        "manual_subtitle_languages": ([k for k in subtitles if isinstance(k, str) and k.strip()]
                                       if isinstance(raw_info.get("subtitles"), Mapping) else None),
        "automatic_caption_language_count": (sum(bool(k.strip()) for k in automatic if isinstance(k, str))
                                              if isinstance(raw_info.get("automatic_captions"), Mapping) else None),
        "description_links": extract_description_links(raw_info.get("description") or metadata.get("description"))}
    return {key: value for key, value in facts.items() if value is not None}


def canonical_sha256(value: Mapping[str, Any] | Sequence[Any]) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
