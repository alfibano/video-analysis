"""Strict v2 schema validation and safe link helpers for study sheets."""
from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from pathlib import Path, PurePosixPath
from typing import Any, Mapping
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


def _object(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": required, "additionalProperties": False}


_TEXT = {"type": "string", "minLength": 1}
_NUMBER = {"type": "number"}
FACT_KEYS = ("title", "channel_name", "channel_url", "youtube_publication_date", "duration_s", "file_size_mb", "resolution", "view_count", "like_count", "comment_count", "audio_language", "manual_subtitle_languages", "automatic_caption_language_count", "description_links")
LABEL_KEYS = ("source", "introduction", "summary", "chapters_rationale", "glossary", "references", "figure", "cost", "tokens", "estimated", "measured", "prices_as_of", "series", "comments", "author_email", "recording_date", "correction", "complement", "question")
_LABELS = {key: dict(_TEXT) for key in LABEL_KEYS}
_LABELS["facts"] = _object({key: dict(_TEXT) for key in FACT_KEYS}, list(FACT_KEYS))
_NULL_TEXT = {"oneOf": [dict(_TEXT), {"type": "null"}]}
_FRAME = {"oneOf": [{"type": "null"}, _object({"file": dict(_TEXT), "time": _NUMBER}, ["file", "time"])]}
_BLOCK = _object({"text": dict(_TEXT), "frame": _FRAME}, ["text", "frame"])
_CALLOUT = _object({"kind": {"enum": ["correction", "complement", "question"]}, "text": dict(_TEXT), "sources": {"type": "array", "items": {"type": "integer"}}}, ["kind", "text", "sources"])
_CHAPTER = _object({"title": dict(_TEXT), "start": _NUMBER, "end": _NUMBER, "blocks": {"type": "array", "minItems": 1, "items": _BLOCK}, "callouts": {"type": "array", "items": _CALLOUT}}, ["title", "start", "end", "blocks", "callouts"])
_EVIDENCE = _object({"value": dict(_TEXT), "evidence": dict(_TEXT)}, ["value", "evidence"])
_CONTEXT = _object({"recording_date": {"oneOf": [{"type": "null"}, _EVIDENCE]}, "series": {"oneOf": [{"type": "null"}, _object({"theme": dict(_TEXT), "first_video_url": _NULL_TEXT}, ["theme", "first_video_url"])]}, "comments_summary": _NULL_TEXT, "author_email": {"oneOf": [{"type": "null"}, _EVIDENCE]}}, ["recording_date", "series", "comments_summary", "author_email"])
_REFERENCE = _object({"id": {"type": "integer", "minimum": 1}, "url": dict(_TEXT), "title": dict(_TEXT), "origin": {"enum": ["video", "source"]}}, ["id", "url", "title", "origin"])
SHEET_JSON_SCHEMA_V2: dict[str, Any] = _object({
    "schema_version": {"type": "integer", "const": 2},
    "source": _object({"bundle_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"}, "title": dict(_TEXT), "url": _NULL_TEXT}, ["bundle_sha256", "title", "url"]),
    "language": dict(_TEXT), "title": dict(_TEXT), "introduction": dict(_TEXT), "summary": {"type": "array", "minItems": 1, "items": dict(_TEXT)},
    "chapter_rationale": dict(_TEXT), "chapters": {"type": "array", "minItems": 1, "items": _CHAPTER}, "context": _CONTEXT,
    "glossary": {"type": "array", "items": _object({"term": dict(_TEXT), "definition": dict(_TEXT)}, ["term", "definition"])},
    "references": {"type": "array", "items": _REFERENCE}, "labels": _object(_LABELS, [*LABEL_KEYS, "facts"]),
    "writer": _object({"host": dict(_TEXT), "model": dict(_TEXT), "role": {"type": "string", "const": "default"}}, ["host", "model", "role"])
}, ["schema_version", "source", "language", "title", "introduction", "summary", "chapter_rationale", "chapters", "context", "glossary", "references", "labels", "writer"])


class SheetValidationError(ValueError):
    def __init__(self, field: str, reason: str) -> None:
        self.field, self.reason = field, reason
        super().__init__(f"{field}: {reason}")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def format_hhmmss(seconds: float) -> str:
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not math.isfinite(seconds) or seconds < 0:
        raise ValueError("seconds must be a non-negative finite number")
    hours, remainder = divmod(int(seconds), 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def is_safe_web_url(url: str | None) -> bool:
    if not isinstance(url, str) or not url.strip() or "\\" in url or any(c.isspace() or unicodedata.category(c) == "Cc" for c in url):
        return False
    try:
        parts = urlsplit(url)
        if parts.scheme.lower() not in {"http", "https"} or not parts.hostname or "@" in parts.netloc:
            return False
        _ = parts.port
        return True
    except (TypeError, ValueError):
        return False


def build_seek_link(source_url: str | None, seconds: float) -> str | None:
    if not is_safe_web_url(source_url) or isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not math.isfinite(seconds) or seconds < 0:
        return None
    try:
        parts = urlsplit(source_url.strip())
        host = (parts.hostname or "").lower()
        if parts.username or parts.password or parts.port is not None:
            return None
        stamp = f"{int(seconds)}s"
        if host in {"youtube.com", "www.youtube.com"} and parts.path == "/watch":
            query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k != "t"]
            query.append(("t", stamp)); return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))
        if host in {"youtu.be", "www.youtu.be"} and parts.path.strip("/") and parts.path.count("/") <= 1:
            query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k != "t"]
            query.append(("t", stamp)); return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))
        if host in {"vimeo.com", "www.vimeo.com"} and parts.path.strip("/"):
            return urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, f"t={stamp}"))
    except (TypeError, ValueError, OverflowError):
        return None
    return None


def _fail(field: str, reason: str) -> None:
    raise SheetValidationError(field, reason)


def _keys(value: Any, required: tuple[str, ...], field: str) -> None:
    if not isinstance(value, dict): _fail(field, "must be an object")
    missing = next((key for key in required if key not in value), None)
    if missing: _fail(missing if field == "$" else f"{field}.{missing}", "is required")
    if set(value) != set(required): _fail(field, "must contain only schema fields")


def _text(value: Any, field: str) -> None:
    if not isinstance(value, str) or not value.strip(): _fail(field, "must be a non-empty string")


def _number(value: Any, field: str) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value): _fail(field, "must be a finite number")


def _frame_path(value: Any, analysis_dir: Path, field: str) -> Path:
    if not isinstance(value, str) or not value or "\\" in value or value.startswith("/") or re.match(r"^[A-Za-z]:", value): _fail(field, "must be a normalized relative path inside the analysis folder")
    path = PurePosixPath(value)
    if path.as_posix() != value or any(part in {".", ".."} for part in value.split("/")): _fail(field, "must be a normalized relative path inside the analysis folder")
    resolved = (analysis_dir / Path(*path.parts)).resolve()
    try: resolved.relative_to(analysis_dir.resolve())
    except ValueError: _fail(field, "must be a normalized relative path inside the analysis folder")
    if resolved.suffix.lower() not in {".jpg", ".jpeg", ".png"}: _fail(field, "must be a JPEG or PNG image")
    if not resolved.is_file(): _fail(field, "file does not exist")
    try:
        from PIL import Image
        with Image.open(resolved) as image: image.verify()
    except Exception: _fail(field, "must be a JPEG or PNG image")
    return resolved


def _validate_source(source: Any, digest: str, source_url: str | None) -> None:
    _keys(source, ("bundle_sha256", "title", "url"), "source")
    if not isinstance(source["bundle_sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", source["bundle_sha256"]): _fail("source.bundle_sha256", "must be a non-empty string")
    _text(source["title"], "source.title")
    if source["url"] is not None and not isinstance(source["url"], str): _fail("source.url", "must be null or a non-empty string")
    if source["bundle_sha256"] != digest: _fail("source.bundle_sha256", "does not match current bundle.json")
    if source["url"] != source_url: _fail("source.url", "does not match current bundle.json")


def _validate_context(value: Any) -> None:
    _keys(value, ("recording_date", "series", "comments_summary", "author_email"), "context")
    for field in ("recording_date", "author_email"):
        entry = value[field]
        if entry is not None:
            _keys(entry, ("value", "evidence"), f"context.{field}"); _text(entry["value"], f"context.{field}.value"); _text(entry["evidence"], f"context.{field}.evidence")
            if field == "author_email" and not re.fullmatch(r"[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+", entry["value"]): _fail("context.author_email.value", "must be a single email address")
    if value["comments_summary"] is not None: _text(value["comments_summary"], "context.comments_summary")
    series = value["series"]
    if series is not None:
        _keys(series, ("theme", "first_video_url"), "context.series"); _text(series["theme"], "context.series.theme")
        if series["first_video_url"] is not None and not is_safe_web_url(series["first_video_url"]): _fail("context.series.first_video_url", "must be a safe HTTP(S) URL")


def _validate_labels(value: Any) -> None:
    _keys(value, (*LABEL_KEYS, "facts"), "labels")
    for key in LABEL_KEYS: _text(value[key], f"labels.{key}")
    _keys(value["facts"], FACT_KEYS, "labels.facts")
    for key, label in value["facts"].items(): _text(label, f"labels.facts.{key}")


def _validate_writer(value: Any, expected: Mapping[str, Any]) -> None:
    _keys(value, ("host", "model", "role"), "writer"); _text(value["host"], "writer.host"); _text(value["model"], "writer.model")
    if value["role"] != "default" or dict(value) != dict(expected): _fail("writer", "does not match sheet preparation")


def validate_sheet(value: Any, *, analysis_dir: Path, expected_bundle_sha256: str, expected_writer: Mapping[str, Any], expected_source_url: str | None, expected_duration_s: float) -> dict[str, Any]:
    _keys(value, tuple(SHEET_JSON_SCHEMA_V2["required"]), "$ ".strip())
    if type(value["schema_version"]) is not int or value["schema_version"] != 2: _fail("schema_version", "must be schema version 2")
    _validate_source(value["source"], expected_bundle_sha256, expected_source_url)
    if value["source"]["url"] is not None and not is_safe_web_url(value["source"]["url"]): _fail("source.url", "must be a safe HTTP(S) URL")
    for field in ("language", "title", "introduction", "chapter_rationale"): _text(value[field], field)
    _number(expected_duration_s, "expected_duration_s")
    minimum = max(1, math.ceil(expected_duration_s / 600))
    if not isinstance(value["summary"], list) or len(value["summary"]) < minimum: _fail("summary", f"requires at least {minimum} paragraphs")
    for i, item in enumerate(value["summary"]): _text(item, f"summary[{i}]")
    _validate_context(value["context"]); _validate_labels(value["labels"]); _validate_writer(value["writer"], expected_writer)
    references = value["references"]
    if not isinstance(references, list): _fail("references", "must be a list")
    ids: set[int] = set()
    for i, ref in enumerate(references):
        field = f"references[{i}]"; _keys(ref, ("id", "url", "title", "origin"), field)
        if type(ref["id"]) is not int or ref["id"] != i + 1: _fail(f"{field}.id", "must be sequential starting at 1")
        ids.add(ref["id"]); _text(ref["title"], f"{field}.title")
        if not is_safe_web_url(ref["url"]): _fail(f"{field}.url", "must be a safe HTTP(S) URL")
        if ref["origin"] not in {"video", "source"}: _fail(f"{field}.origin", "is invalid")
    previous_end = -math.inf; block_chars = callout_chars = 0
    chapters = value["chapters"]
    if not isinstance(chapters, list) or not chapters: _fail("chapters", "must be a non-empty list")
    for ci, chapter in enumerate(chapters):
        field = f"chapters[{ci}]"; _keys(chapter, ("title", "start", "end", "blocks", "callouts"), field); _text(chapter["title"], f"{field}.title")
        start, end = chapter["start"], chapter["end"]; _number(start, f"{field}.start"); _number(end, f"{field}.end")
        if not 0 <= start < end: _fail(field, "must satisfy 0 <= start < end")
        if start < previous_end: _fail(field, "must be ordered and non-overlapping")
        previous_end = end
        blocks = chapter["blocks"]
        if not isinstance(blocks, list) or not blocks: _fail(f"{field}.blocks", "must be a non-empty list")
        for bi, block in enumerate(blocks):
            bf = f"{field}.blocks[{bi}]"; _keys(block, ("text", "frame"), bf); _text(block["text"], f"{bf}.text"); block_chars += len(block["text"].strip())
            if block["frame"] is not None:
                frame = block["frame"]; _keys(frame, ("file", "time"), f"{bf}.frame"); _number(frame["time"], f"{bf}.frame.time")
                if not start <= frame["time"] <= end: _fail(f"{bf}.frame.time", "must be within chapter interval")
                _frame_path(frame["file"], analysis_dir, f"{bf}.frame.file")
        if not isinstance(chapter["callouts"], list): _fail(f"{field}.callouts", "must be a list")
        for oi, callout in enumerate(chapter["callouts"]):
            cf = f"{field}.callouts[{oi}]"; _keys(callout, ("kind", "text", "sources"), cf)
            if callout["kind"] not in {"correction", "complement", "question"}: _fail(f"{cf}.kind", "is invalid")
            _text(callout["text"], f"{cf}.text"); callout_chars += len(callout["text"].strip())
            sources = callout["sources"]
            if not isinstance(sources, list) or any(type(i) is not int or i not in ids for i in sources): _fail(f"{cf}.sources", "must resolve to references")
            if callout["kind"] in {"correction", "complement"} and not sources: _fail(f"{cf}.sources", "correction and complement require a source")
    if callout_chars > block_chars: _fail("chapters", "callout text exceeds block text budget")
    glossary = value["glossary"]
    if not isinstance(glossary, list): _fail("glossary", "must be a list")
    for i, item in enumerate(glossary):
        _keys(item, ("term", "definition"), f"glossary[{i}]"); _text(item["term"], f"glossary[{i}].term"); _text(item["definition"], f"glossary[{i}].definition")
    return value


def load_sheet(path: Path, *, analysis_dir: Path, expected_bundle_sha256: str, expected_writer: Mapping[str, Any], expected_source_url: str | None, expected_duration_s: float) -> dict[str, Any]:
    try: value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError): _fail("$", "invalid JSON")
    return validate_sheet(value, analysis_dir=analysis_dir, expected_bundle_sha256=expected_bundle_sha256, expected_writer=expected_writer, expected_source_url=expected_source_url, expected_duration_s=expected_duration_s)
