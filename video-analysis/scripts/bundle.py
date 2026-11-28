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
    corrected_from: str | None = None


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


def iter_caption_candidates(language: str, raw_info: Mapping[str, Any],
                            files: Iterable[str | Path] = (), *,
                            video_stem: str | None = None) -> Iterable[SpeechSource]:
    """Yield manual then automatic caption files in the canonical video-language order."""
    from fetch_video import caption_language_candidates
    paths = sorted((Path(item) for item in files), key=lambda path: path.name.casefold())
    languages = caption_language_candidates({"language": language})
    for source, field in (("manual", "subtitles"), ("automatic", "automatic_captions")):
        group = raw_info.get(field) or {}
        if not isinstance(group, Mapping):
            continue
        for requested in languages:
            key = next((str(item) for item in group if str(item).casefold() == requested.casefold()), None)
            if key is None:
                continue
            prefix = f"{video_stem}.{key}." if video_stem else None
            for path in paths:
                if (path.suffix.casefold() in {".vtt", ".srt"}
                        and (prefix is None or path.name.casefold().startswith(prefix.casefold()))):
                    yield SpeechSource(source, str(key), str(path))


def _usable_caption(source: SpeechSource, work: Path) -> bool:
    try:
        return bool(read_speech_segments(source, work, {}))
    except (OSError, UnicodeError, ValueError, TypeError, OverflowError):
        return False


def select_speech_source(language: str | None, raw_info: Mapping[str, Any],
                         files: Iterable[str | Path] = (), *, transcript_language: str | None = None,
                         transcript_available: bool = True, video_stem: str | None = None,
                         work: Path | None = None) -> SpeechSource:
    """Choose manual, automatic, transcript, or empty speech using provenance and files."""
    selected_language = language if language and language != "auto" else (
        raw_info.get("language") or transcript_language)
    if selected_language:
        for candidate in iter_caption_candidates(str(selected_language), raw_info, files,
                                                 video_stem=video_stem):
            if _usable_caption(candidate, work or Path.cwd()):
                return candidate
    if transcript_available:
        return SpeechSource("whisper", transcript_language or selected_language, "transcript.json")
    return SpeechSource("none", selected_language, None)


def read_speech_segments(source: SpeechSource, work: Path,
                         transcript: Mapping[str, Any]) -> list[SpeechSegment]:
    """Read selected speech with the same Whisper and caption normalization everywhere."""
    if source.source == "whisper":
        raw_segments = transcript.get("segments", [])
        if not isinstance(raw_segments, list):
            return []
        return [SpeechSegment(float(item["start"]), float(item["end"]), str(item["text"]).strip())
                for item in raw_segments if isinstance(item, Mapping)
                and _finite(item.get("start")) is not None and _finite(item.get("end")) is not None
                and float(item["end"]) > float(item["start"]) and str(item.get("text", "")).strip()]
    if source.source not in {"manual", "automatic"} or not source.file:
        return []
    path = Path(source.file)
    if not path.is_absolute():
        path = work / path
    text = path.read_text(encoding="utf-8-sig")
    parsed = parse_vtt(text, rolling=source.source == "automatic") if path.suffix.lower() == ".vtt" else parse_srt(text)
    return dedupe_rolling_cues(parsed) if source.source == "automatic" else parsed


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
                    sponsors: Sequence[Sponsor], ocr_texts: Mapping[str, str],
                    ocr_skipped: Mapping[str, str]) -> list[dict[str, Any]]:
    result = []
    for frame in frames:
        if frame.chapter_index != chapter_index:
            continue
        sponsored = any(item.start <= frame.pts < item.end for item in sponsors)
        frame_file = f"frames/{Path(frame.file.replace(chr(92), '/')).name}"
        item = {"file": frame_file, "pts": frame.pts, "is_key": frame.is_key,
                "is_sponsor": sponsored, "alignment": "in_range",
                "ocr_text": ocr_texts.get(frame.file) if frame.is_key else None}
        if frame.file in ocr_skipped:
            item["ocr_skipped"] = ocr_skipped[frame.file]
        result.append(item)
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
                      sponsors: Sequence[Sponsor], ocr_texts: Mapping[str, str],
                      ocr_skipped: Mapping[str, str]) -> dict[str, Any]:
    speech = _chapter_speech(chapter, index, last_index, segments, sponsors)
    return {"index": index, "title": chapter.title, "start": chapter.start, "end": chapter.end,
            "source": chapter.source,
            "speech_text": " ".join(item["text"] for item in speech if not item["is_sponsor"]),
            "speech_segments": speech, "frames": _chapter_frames(index, frames, sponsors, ocr_texts, ocr_skipped),
            "sponsor_overlap": _sponsor_overlap(chapter, sponsors)}


def build_bundle(metadata: Mapping[str, Any], chapters: Sequence[Chapter],
                 speech_segments: Sequence[SpeechSegment], frames: Sequence[FrameAssignment],
                 sponsors: Sequence[Sponsor], *, speech_source: SpeechSource | None = None,
                 speech_language: str | None = None, speech_file: str | None = None,
                 ocr_texts: Mapping[str, str] | None = None,
                 ocr_skipped: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Build schema-version-2 bundle data with paths relative to the analysis folder."""
    chapters = list(chapters) or discover_chapters([], float(metadata.get("duration") or 0))
    source = speech_source or SpeechSource("none", speech_language, speech_file)
    output = [_assemble_chapter(i, len(chapters) - 1, chapter, speech_segments, frames,
                                sponsors, ocr_texts or {}, ocr_skipped or {}) for i, chapter in enumerate(chapters)]
    bundle_source = {key: metadata.get(key) for key in ("title", "author", "channel", "url", "duration")}
    video = metadata.get("video")
    bundle_source["video"] = f"../{Path(str(video)).name}" if video else None
    speech_file = source.file
    if speech_file:
        speech_path = Path(speech_file.replace(chr(92), "/"))
        speech_file = speech_path.name if speech_path.is_absolute() else speech_path.as_posix()
    speech = {"source": source.source, "language": source.language, "file": speech_file}
    if source.corrected_from:
        speech["corrected_from"] = source.corrected_from
    return {"schema_version": 2, "path_base": "analysis_folder",
            "source": bundle_source, "speech": speech, "chapters": output}
