"""Schema validation and link formatting for editable study sheets."""
from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path, PurePosixPath
from typing import Any, Mapping
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


def _object(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": required,
            "additionalProperties": False}


_TEXT = {"type": "string", "minLength": 1}
_NUMBER = {"type": "number"}
_LABELS = {key: dict(_TEXT) for key in ("source", "introduction", "summary", "glossary", "figure")}
SHEET_JSON_SCHEMA_V1: dict[str, Any] = _object({
    "schema_version": {"type": "integer", "const": 1},
    "source": _object({"bundle_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                        "title": dict(_TEXT), "url": {"oneOf": [dict(_TEXT), {"type": "null"}]}},
                       ["bundle_sha256", "title", "url"]),
    "language": dict(_TEXT), "title": dict(_TEXT), "introduction": dict(_TEXT),
    "summary": dict(_TEXT), "labels": _object(_LABELS, list(_LABELS)),
    "chapters": {"type": "array", "minItems": 1, "items": _object({
        "title": dict(_TEXT), "text": dict(_TEXT), "start": _NUMBER, "end": _NUMBER,
        "key_points": {"type": "array", "items": dict(_TEXT)},
        "frame": {"oneOf": [{"type": "null"}, _object(
            {"file": dict(_TEXT), "time": _NUMBER}, ["file", "time"])]}},
        ["title", "text", "start", "end", "key_points", "frame"])},
    "glossary": {"type": "array", "items": _object(
        {"term": dict(_TEXT), "definition": dict(_TEXT)}, ["term", "definition"])},
    "writer": _object({"host": dict(_TEXT), "model": dict(_TEXT),
                        "role": {"type": "string", "const": "default"}}, ["host", "model", "role"])
}, ["schema_version", "source", "language", "title", "introduction", "summary", "labels",
    "chapters", "glossary", "writer"])


class SheetValidationError(ValueError):
    def __init__(self, field: str, reason: str) -> None:
        self.field = field
        self.reason = reason
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
    value = int(seconds)
    hours, remainder = divmod(value, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def build_seek_link(source_url: str | None, seconds: float) -> str | None:
    if (not isinstance(source_url, str) or not source_url.strip()
            or isinstance(seconds, bool) or not isinstance(seconds, (int, float))
            or not math.isfinite(seconds) or seconds < 0):
        return None
    try:
        parts = urlsplit(source_url.strip())
        host = (parts.hostname or "").lower()
        if parts.scheme not in {"http", "https"} or parts.username or parts.password or parts.port is not None:
            return None
        stamp = f"{int(seconds)}s"
        if host in {"youtube.com", "www.youtube.com"} and parts.path == "/watch":
            query = [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True) if key != "t"]
            query.append(("t", stamp))
            return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))
        if host in {"youtu.be", "www.youtu.be"} and parts.path.strip("/") and parts.path.count("/") <= 1:
            query = [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True) if key != "t"]
            query.append(("t", stamp))
            return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))
        if host in {"vimeo.com", "www.vimeo.com"} and parts.path.strip("/"):
            return urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, f"t={stamp}"))
    except (TypeError, ValueError, OverflowError):
        return None
    return None


def _fail(field: str, reason: str) -> None:
    raise SheetValidationError(field, reason)


def _keys(value: Any, required: tuple[str, ...], field: str) -> None:
    if not isinstance(value, dict):
        _fail(field, "must be an object")
    if any(key not in value for key in required):
        missing = next(key for key in required if key not in value)
        _fail(missing if field == "$" else f"{field}.{missing}", "is required")
    if set(value) != set(required):
        _fail(field, "must contain only schema fields")


def _text(value: Any, field: str) -> None:
    if not isinstance(value, str) or not value.strip():
        _fail(field, "must be a non-empty string")


def _number(value: Any, field: str) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        _fail(field, "must be a finite number")


def _frame_path(value: Any, analysis_dir: Path, field: str) -> Path:
    if (not isinstance(value, str) or not value or "\\" in value or value.startswith("/")
            or value.startswith("//") or re.match(r"^[A-Za-z]:", value)):
        _fail(field, "must be a normalized relative path inside the analysis folder")
    path = PurePosixPath(value)
    if path.as_posix() != value or any(part in {".", ".."} for part in value.split("/")):
        _fail(field, "must be a normalized relative path inside the analysis folder")
    resolved = (analysis_dir / Path(*path.parts)).resolve()
    try:
        resolved.relative_to(analysis_dir.resolve())
    except ValueError:
        _fail(field, "must be a normalized relative path inside the analysis folder")
    if not resolved.is_file():
        _fail(field, "file does not exist")
    return resolved


def _validate_source(source: Any, expected_digest: str) -> None:
    _keys(source, ("bundle_sha256", "title", "url"), "source")
    if not isinstance(source["bundle_sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", source["bundle_sha256"]):
        _fail("source.bundle_sha256", "must be a non-empty string")
    _text(source["title"], "source.title")
    if source["url"] is not None and (not isinstance(source["url"], str) or not source["url"].strip()):
        _fail("source.url", "must be null or a non-empty string")
    if source["bundle_sha256"] != expected_digest:
        _fail("source.bundle_sha256", "does not match current bundle.json")


def _validate_labels(labels: Any) -> None:
    names = ("source", "introduction", "summary", "glossary", "figure")
    _keys(labels, names, "labels")
    for label, text in labels.items():
        _text(text, f"labels.{label}")


def _validate_writer(writer: Any, expected_writer: Mapping[str, Any]) -> None:
    _keys(writer, ("host", "model", "role"), "writer")
    _text(writer["host"], "writer.host")
    _text(writer["model"], "writer.model")
    if dict(writer) != dict(expected_writer):
        _fail("writer", "does not match sheet preparation")


def _validate_chapters(chapters: Any, analysis_dir: Path) -> None:
    if not isinstance(chapters, list):
        _fail("chapters", "must be a list")
    if not chapters:
        _fail("chapters", "must be a list")
    previous_end = -math.inf
    for index, chapter in enumerate(chapters):
        field = f"chapters[{index}]"
        _keys(chapter, ("title", "text", "start", "end", "key_points", "frame"), field)
        for key in ("title", "text"):
            _text(chapter[key], f"{field}.{key}")
        start, end = chapter["start"], chapter["end"]
        _number(start, f"{field}.start")
        _number(end, f"{field}.end")
        if not 0 <= start < end:
            _fail(field, "must satisfy 0 <= start < end")
        if start < previous_end:
            _fail(field, "must be ordered and non-overlapping")
        previous_end = end
        points = chapter["key_points"]
        if not isinstance(points, list):
            _fail(f"{field}.key_points", "must be a list")
        for point_index, point in enumerate(points):
            _text(point, f"{field}.key_points[{point_index}]")
        frame = chapter["frame"]
        if frame is not None:
            _keys(frame, ("file", "time"), f"{field}.frame")
            _number(frame["time"], f"{field}.frame.time")
            if not start <= frame["time"] <= end:
                _fail(f"{field}.frame.time", "must be within chapter interval")
            _frame_path(frame["file"], analysis_dir, f"{field}.frame.file")


def _validate_glossary(glossary: Any) -> None:
    if not isinstance(glossary, list):
        _fail("glossary", "must be a list")
    for index, entry in enumerate(glossary):
        field = f"glossary[{index}]"
        _keys(entry, ("term", "definition"), field)
        _text(entry["term"], f"{field}.term")
        _text(entry["definition"], f"{field}.definition")


def validate_sheet(value: Any, *, analysis_dir: Path, expected_bundle_sha256: str,
                   expected_writer: Mapping[str, Any]) -> dict[str, Any]:
    _keys(value, tuple(SHEET_JSON_SCHEMA_V1["required"]), "$")
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        _fail("schema_version", "must be schema version 1")
    _validate_source(value["source"], expected_bundle_sha256)
    for field in ("language", "title", "introduction", "summary"):
        _text(value[field], field)
    _validate_labels(value["labels"])
    _validate_writer(value["writer"], expected_writer)
    _validate_chapters(value["chapters"], analysis_dir)
    _validate_glossary(value["glossary"])
    return value


def load_sheet(path: Path, *, analysis_dir: Path, expected_bundle_sha256: str,
               expected_writer: Mapping[str, Any]) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        _fail("$", "invalid JSON")
    return validate_sheet(value, analysis_dir=analysis_dir,
                          expected_bundle_sha256=expected_bundle_sha256, expected_writer=expected_writer)
