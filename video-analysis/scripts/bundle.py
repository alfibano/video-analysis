"""Pure normalization and alignment helpers for per-chapter bundles."""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


@dataclass(frozen=True, slots=True)
class Chapter:
    start: float
    end: float
    title: str
    source: str


@dataclass(frozen=True, slots=True)
class SpeechSegment:
    start: float
    end: float
    text: str


@dataclass(frozen=True, slots=True)
class SpeechSource:
    source: str
    language: str | None
    file: str | None


@dataclass(frozen=True, slots=True)
class FrameAssignment:
    file: str
    pts: float
    chapter_index: int
    is_key: bool


@dataclass(frozen=True, slots=True)
class Sponsor:
    start: float
    end: float
    category: str | None = None
    title: str | None = None


def parse_sponsors(values: list[dict[str, Any]]) -> list[Sponsor]:
    """Normalize SponsorBlock spans into immutable sponsor records."""
    sponsors = []
    for value in values:
        segment = value.get("segment")
        start = _finite(value.get("start_time", value.get("start")))
        end = _finite(value.get("end_time", value.get("end")))
        if isinstance(segment, (list, tuple)) and len(segment) >= 2:
            start = _finite(segment[0]) if start is None else start
            end = _finite(segment[1]) if end is None else end
        if start is not None and end is not None and 0 <= start < end:
            sponsors.append(Sponsor(start, end, value.get("category"), value.get("title")))
    return sponsors


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _chapter_parts(start: float, end: float, title: str, source: str,
                   maximum: float = 600) -> list[Chapter]:
    count = max(1, math.ceil((end - start) / maximum))
    width = (end - start) / count
    return [Chapter(start + index * width, end if index == count - 1 else start + (index + 1) * width,
                    f"{title} ({index + 1}/{count})" if count > 1 else title, source)
            for index in range(count)]


def _metadata_chapters(values: Iterable[Mapping[str, Any]], duration: float) -> list[Chapter]:
    valid = []
    for value in values:
        start = _finite(value.get("start_time", value.get("start")))
        end = _finite(value.get("end_time", value.get("end")))
        if start is not None and end is not None and 0 <= start < end:
            valid.append((start, end, str(value.get("title") or "Chapter")))
    valid.sort(key=lambda item: (item[0], item[1]))
    chapters: list[Chapter] = []
    previous_end = 0.0
    for start, end, title in valid:
        start = max(start, previous_end)
        end = min(end, duration)
        if end <= start:
            continue
        chapters.extend(_chapter_parts(start, end, title, "metadata"))
        previous_end = end
    return chapters


def _pause_cuts(segments: Sequence[Mapping[str, Any]], target: float) -> float | None:
    pauses = []
    ordered = sorted(((a, b) for item in segments
                      if (a := _finite(item.get("start"))) is not None
                      and (b := _finite(item.get("end"))) is not None), key=lambda pair: pair[0])
    for (_, left_end), (right_start, _) in zip(ordered, ordered[1:]):
        gap = right_start - left_end
        midpoint = (right_start + left_end) / 2
        if gap >= 1.5 and abs(midpoint - target) <= 60:
            pauses.append((gap, midpoint))
    return max(pauses, key=lambda item: (item[0], -abs(item[1] - target)))[1] if pauses else None


def discover_chapters(metadata_chapters: Iterable[Mapping[str, Any]] | None,
                      duration: float, *, transcript_segments: Sequence[Mapping[str, Any]] = (),
                      max_chapter: float = 600, target: float = 300,
                      min_chapter: float = 30) -> list[Chapter]:
    """Normalize authored chapters or create deterministic pause-aware segments."""
    duration = _finite(duration) or 0.0
    if duration <= 0:
        return []
    chapters = _metadata_chapters(metadata_chapters or (), duration)
    if chapters:
        result = []
        for chapter in chapters:
            result.extend(_chapter_parts(chapter.start, chapter.end, chapter.title, chapter.source,
                                         max_chapter))
        return result
    if duration < min_chapter:
        return [Chapter(0.0, duration, "Chapter 1", "fallback")]
    cuts = [0.0]
    while duration - cuts[-1] > target:
        ideal = min(cuts[-1] + target, duration - min_chapter)
        pause = _pause_cuts(transcript_segments, ideal)
        cut = ideal if pause is None else pause
        if cut <= cuts[-1] or cut >= duration:
            cut = ideal
        cuts.append(cut)
    cuts.append(duration)
    if len(cuts) > 2 and cuts[-1] - cuts[-2] < min_chapter:
        cuts.pop(-2)
    return [Chapter(start, end, f"Chapter {index + 1}", "fallback")
            for index, (start, end) in enumerate(zip(cuts, cuts[1:])) if end > start]


def _timestamp(value: str) -> float:
    value = value.strip().replace(",", ".")
    parts = value.split(":")
    if len(parts) == 2:
        parts.insert(0, "0")
    if len(parts) != 3:
        raise ValueError(f"invalid subtitle timestamp: {value}")
    return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])


_TIMING = re.compile(r"^\s*(\d{1,2}:\d{2}:\d{2}[.,]\d+|\d{1,2}:\d{2}[.,]\d+)\s*-->\s*(\d{1,2}:\d{2}:\d{2}[.,]\d+|\d{1,2}:\d{2}[.,]\d+)")
_TAG = re.compile(r"<[^>]*>|\{\\[^}]*\}")


def _parse_subtitles(text: str, *, rolling: bool = False) -> list[SpeechSegment]:
    lines = text.lstrip("\ufeff").splitlines()
    segments: list[SpeechSegment] = []
    index = 0
    while index < len(lines):
        match = _TIMING.match(lines[index])
        index += 1
        if not match:
            continue
        start, end = _timestamp(match.group(1)), _timestamp(match.group(2))
        body = []
        while index < len(lines) and lines[index] != "" and not _TIMING.match(lines[index]):
            body.append(lines[index])
            index += 1
        clean = "\n".join(body) if rolling else " ".join(line.strip() for line in body if line.strip())
        clean = re.sub(r"\s+", " ", _TAG.sub("", clean)).strip() if not rolling else clean
        if end > start and clean:
            segments.append(SpeechSegment(start, end, clean))
    return segments


def parse_vtt(text: str, *, rolling: bool = False) -> list[SpeechSegment]:
    return _parse_subtitles(text, rolling=rolling)


def parse_srt(text: str) -> list[SpeechSegment]:
    return _parse_subtitles(text)


def dedupe_rolling_cues(cues: Iterable[SpeechSegment | tuple[float, float, str]]) -> list[SpeechSegment]:
    result: list[SpeechSegment] = []
    previous = ""
    for cue in cues:
        segment = cue if isinstance(cue, SpeechSegment) else SpeechSegment(float(cue[0]), float(cue[1]), str(cue[2]))
        for line in segment.text.splitlines():
            text = re.sub(r"<[^>]*>|\d{1,2}:\d{2}:\d{2}[.,]\d+", "", line)
            text = re.sub(r"\s+", " ", text).strip()
            if not text or text == previous:
                continue
            result.append(SpeechSegment(segment.start, segment.end, text))
            previous = text
    return result


def _language_match(keys: Iterable[str], language: str) -> str | None:
    normalized = language.casefold()
    candidates = [key for key in keys if key.casefold() == normalized]
    if candidates:
        return candidates[0]
    prefix = normalized.split("-", 1)[0]
    return next((key for key in keys if key.casefold().split("-", 1)[0] == prefix), None)


def select_speech_source(language: str | None, raw_info: Mapping[str, Any],
                         files: Iterable[str | Path] = (), *, transcript_language: str | None = None,
                         transcript_available: bool = True, video_stem: str | None = None) -> SpeechSource:
    """Choose manual, automatic, transcript, or empty speech using provenance and files."""
    selected_language = language if language and language != "auto" else (
        raw_info.get("language") or transcript_language)
    if selected_language:
        paths = [Path(item) for item in files]
        stem = video_stem
        for group, source in ((raw_info.get("subtitles") or {}, "manual"),
                              (raw_info.get("automatic_captions") or {}, "automatic")):
            key = _language_match(group.keys(), str(selected_language))
            if key is None:
                continue
            candidates = [path for path in paths if path.suffix.casefold() in {".vtt", ".srt"}
                          and (stem is None or path.name.startswith(f"{stem}.{key}."))]
            if candidates:
                return SpeechSource(source, key, str(sorted(candidates, key=lambda path: path.name)[0]))
    if transcript_available:
        return SpeechSource("whisper", transcript_language or selected_language, "transcript.json")
    return SpeechSource("none", selected_language, None)


def align_frames(frames: Mapping[str, Any], chapters: Sequence[Chapter],
                 key_frames: Iterable[str] = (), *, interval: float = 5.0) -> list[FrameAssignment]:
    key_set = {Path(name).name for name in key_frames}
    result = []
    for index, (filename, raw_pts) in enumerate(frames.items()):
        pts = _finite(raw_pts)
        if pts is None:
            pts = index * interval
        for chapter_index, chapter in enumerate(chapters):
            if chapter.start <= pts < chapter.end:
                result.append(FrameAssignment(filename, pts, chapter_index,
                                              Path(filename).name in key_set))
                break
    return result


def _chapter_speech(chapter: Chapter, index: int, last_index: int,
                    segments: Sequence[SpeechSegment], sponsors: Sequence[Sponsor]) -> list[dict[str, Any]]:
    result = []
    for segment in segments:
        midpoint = (segment.start + segment.end) / 2
        in_chapter = chapter.start <= midpoint < chapter.end
        if index == last_index and midpoint == chapter.end:
            in_chapter = True
        if in_chapter:
            sponsored = any(item.start <= midpoint < item.end for item in sponsors)
            result.append({"start": segment.start, "end": segment.end,
                           "text": segment.text, "is_sponsor": sponsored})
    return result


def _chapter_frames(chapter_index: int, frames: Sequence[FrameAssignment],
                    sponsors: Sequence[Sponsor], ocr_texts: Mapping[str, str]) -> list[dict[str, Any]]:
    result = []
    for frame in frames:
        if frame.chapter_index != chapter_index:
            continue
        sponsored = any(item.start <= frame.pts < item.end for item in sponsors)
        result.append({"file": frame.file, "pts": frame.pts, "is_key": frame.is_key,
                       "is_sponsor": sponsored, "alignment": "in_range",
                       "ocr_text": ocr_texts.get(frame.file) if frame.is_key else None})
    return result


def _sponsor_overlap(chapter: Chapter, sponsors: Sequence[Sponsor]) -> list[dict[str, Any]]:
    result = []
    for sponsor in sponsors:
        start, end = max(chapter.start, sponsor.start), min(chapter.end, sponsor.end)
        if end > start:
            result.append({"start": start, "end": end, "category": sponsor.category,
                           "title": sponsor.title, "overlap_s": end - start})
    return result


def _assemble_chapter(index: int, last_index: int, chapter: Chapter,
                      segments: Sequence[SpeechSegment], frames: Sequence[FrameAssignment],
                      sponsors: Sequence[Sponsor], ocr_texts: Mapping[str, str]) -> dict[str, Any]:
    speech = _chapter_speech(chapter, index, last_index, segments, sponsors)
    return {"index": index, "title": chapter.title, "start": chapter.start, "end": chapter.end,
            "source": chapter.source,
            "speech_text": " ".join(item["text"] for item in speech if not item["is_sponsor"]),
            "speech_segments": speech, "frames": _chapter_frames(index, frames, sponsors, ocr_texts),
            "sponsor_overlap": _sponsor_overlap(chapter, sponsors)}


def build_bundle(metadata: Mapping[str, Any], chapters: Sequence[Chapter],
                 speech_segments: Sequence[SpeechSegment], frames: Sequence[FrameAssignment],
                 sponsors: Sequence[Sponsor], *, speech_source: SpeechSource | None = None,
                 speech_language: str | None = None, speech_file: str | None = None,
                 ocr_texts: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Build schema-version-1 bundle data from normalized pure inputs."""
    chapters = list(chapters) or discover_chapters([], float(metadata.get("duration") or 0))
    source = speech_source or SpeechSource("none", speech_language, speech_file)
    output = [_assemble_chapter(i, len(chapters) - 1, chapter, speech_segments, frames,
                                sponsors, ocr_texts or {}) for i, chapter in enumerate(chapters)]
    return {"schema_version": 1,
            "source": {key: metadata.get(key) for key in ("title", "author", "channel", "url", "duration", "video")},
            "speech": {"source": source.source, "language": source.language, "file": source.file},
            "chapters": output}
