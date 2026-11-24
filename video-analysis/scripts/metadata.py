"""Normalize yt-dlp info dictionaries into stable metadata fields."""
from __future__ import annotations

from typing import Any


def _date(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    if len(value) == 8 and value.isdigit():
        return f"{value[:4]}-{value[4:6]}-{value[6:]}"
    if len(value) == 10 and value[4] == "-" and value[7] == "-":
        return value
    return None


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def normalize_metadata(info: dict[str, Any]) -> dict[str, Any]:
    width, height = info.get("width"), info.get("height")
    resolution = info.get("resolution")
    if resolution is None and width and height:
        resolution = f"{width}x{height}"
    return {
        "title": info.get("title"),
        "author": info.get("creator") or info.get("uploader"),
        "channel": info.get("channel"),
        "upload_date": _date(info.get("upload_date")),
        "duration": info.get("duration"),
        "resolution": resolution,
        "format": info.get("format") or info.get("format_id"),
        "description": info.get("description"),
        "chapters": _list(info.get("chapters")),
        "tags": _list(info.get("tags")),
        "url": info.get("webpage_url") or info.get("original_url"),
        "extractor": info.get("extractor_key") or info.get("extractor"),
        "id": info.get("id"),
    }
