"""Build a chapter-aligned JSON bundle from an existing analysis folder."""
from __future__ import annotations

import argparse
import importlib.metadata
import json
import math
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bundle import (SpeechSegment, SpeechSource, align_frames, build_bundle, discover_chapters,
                    dedupe_rolling_cues, parse_sponsors, parse_srt, parse_vtt,
                    select_speech_source)


@dataclass(frozen=True)
class SpeechRead:
    segments: list[SpeechSegment]
    language: str | None
    file: str | None
    source: str


@dataclass(frozen=True)
class BundleInputs:
    video: Path
    work: Path
    metadata: dict[str, Any]
    raw_info: dict[str, Any]
    transcript: Any
    frames: dict[str, Any]
    keyframes: list[str]
    sponsors: Any
    manifest: dict[str, Any]
    identity: dict[str, Any]
    duration: float
    interval: float


@dataclass(frozen=True)
class BundleResult:
    chapters: list[Any]
    assignments: list[Any]
    speech: SpeechRead
    ocr_texts: dict[str, str]
    ocr_frames: list[Any]
    ocr_skipped: dict[str, str]


class BundleInputError(ValueError):
    """An invalid analysis folder with its public CLI exit code."""

    def __init__(self, message: str, code: int = 2):
        super().__init__(message)
        self.code = code


def _json(path: Path, default: Any) -> Any:
    if not path.is_file():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ValueError(f"invalid JSON in {path.name}: {error}") from error


def _ocr_version() -> str:
    try:
        return f"rapidocr {importlib.metadata.version('rapidocr')}"
    except importlib.metadata.PackageNotFoundError:
        from rapidocr import __version__
        return f"rapidocr {__version__}"


def _finite(value: Any) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _duration(metadata: dict[str, Any], manifest: dict[str, Any]) -> float:
    metadata_duration = _finite(metadata.get("duration")) and float(metadata["duration"])
    manifest_duration = _finite(manifest.get("duration_s")) and float(manifest["duration_s"])
    return metadata_duration or manifest_duration or 0.0


def _frames(work: Path, interval: float) -> tuple[dict[str, Any], list[str]]:
    values = _json(work / "frames.json", {})
    if not isinstance(values, dict):
        raise ValueError("frames.json must contain an object")
    keys_path = work / "keyframes.txt"
    keys = []
    if keys_path.is_file():
        for line in keys_path.read_text(encoding="utf-8").splitlines():
            parts = line.split()
            if len(parts) >= 2:
                keys.append(parts[-1])
    if not values:
        frame_dir = work / "frames"
        if frame_dir.is_dir():
            candidates = sorted([*frame_dir.glob("f*.jpg"), *frame_dir.glob("t*.jpg")])
            values = {}
            for path in candidates:
                match = re.fullmatch(r"t(\d{6})\.(\d{3})", path.stem)
                pts = (int(match.group(1)) + int(match.group(2)) / 1000 if match
                       else (int(path.stem[1:]) - 1) * interval)
                values[f"frames/{path.name}"] = pts
    else:
        values = {name if "/" in name or "\\" in name else f"frames/{name}": pts
                  for name, pts in values.items()}
    return values, keys


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=("Build a chapter-aligned JSON bundle from an existing analysis folder.\n"
                     "Reads metadata, transcript, captions, sampled frame timestamps, and\n"
                     "SponsorBlock data from <video folder>/<video name>.analysis/. It\n"
                     "never reads the video stream or deletes existing files.\n"
                     "Run extract_video.py first, then read bundle.json before raw files."),
        epilog=("Example: python build_bundle.py lecture.mp4 --language fr\n"
                "Exit codes: 0 success or resumed; 1 processing or OCR failure;\n"
                "2 invalid input or missing extraction outputs; 3 manifest conflict."))
    parser.add_argument("video", type=Path, help="video file whose analysis folder to bundle")
    parser.add_argument("--language", default="auto", help="caption language or auto (default: %(default)s)")
    parser.add_argument("--ocr-min-score", type=float, default=0.5, help="minimum OCR confidence from 0 to 1 (default: %(default)s)")
    parser.add_argument("--min-chapter", type=float, default=30, help="minimum fallback chapter length in seconds (default: %(default)s)")
    parser.add_argument("--max-chapter", type=float, default=600, help="maximum chapter length in seconds (default: %(default)s)")
    parser.add_argument("--target-chapter", type=float, default=300, help="target fallback chapter length in seconds (default: %(default)s)")
    parser.add_argument("--skip-ocr", action="store_true", help="omit OCR and record it as skipped")
    return parser


def _parse_validate(argv: list[str] | None) -> tuple[argparse.Namespace | None, int | None]:
    try:
        args = _parser().parse_args(argv)
    except SystemExit as error:
        return None, 0 if error.code == 0 else 2
    if not args.video.is_file():
        print(f"no such video: {args.video}")
        return None, 2
    if (not 0 <= args.ocr_min_score <= 1 or args.min_chapter <= 0
            or args.max_chapter <= 0 or args.target_chapter <= 0):
        print("chapter lengths must be positive and OCR score must be from 0 to 1")
        return None, 2
    return args, None


def _load_inputs(video: Path) -> BundleInputs:
    video = video.resolve()
    work = video.parent / f"{video.stem}.analysis"
    if not work.is_dir():
        raise BundleInputError(f"analysis folder not found: {work}; run extract_video.py first")
    metadata, raw_info, transcript = _load_analysis_json(work)
    identity = _video_identity(video)
    manifest = _load_manifest(work, identity)
    interval = _extract_interval(manifest.get("params", {}).get("extract", {}))
    frames, keyframes = _frames(work, interval)
    _validate_extraction_outputs(work, video, frames, transcript)
    _validate_manifest_source(manifest, identity)
    duration = _duration(metadata, manifest)
    if duration <= 0:
        raise BundleInputError("duration missing from analysis metadata")
    metadata.setdefault("duration", duration)
    metadata.update(video=str(video), video_stem=video.stem)
    return BundleInputs(video, work, metadata, raw_info, transcript, frames, keyframes,
                        _json(work / "sponsorblock.json", []), manifest, identity,
                        duration, interval)


def _load_analysis_json(work: Path) -> tuple[dict[str, Any], dict[str, Any], Any]:
    return (_json(work / "metadata.json", {}), _json(work / "raw_info.json", {}),
            _json(work / "transcript.json", {}))


def _video_identity(video: Path) -> dict[str, Any]:
    stat = video.stat()
    return {"path": str(video), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}


def _load_manifest(work: Path, identity: dict[str, Any]) -> dict[str, Any]:
    default = {"schema_version": 2, "source": identity, "params": {}, "stages": {}}
    return _json(work / "manifest.json", default)


def _validate_extraction_outputs(work: Path, video: Path, frames: dict[str, Any],
                                 transcript: Any) -> None:
    subtitles = list(work.glob(f"{video.stem}.*.vtt")) + list(work.glob(f"{video.stem}.*.srt"))
    has_transcript = isinstance(transcript, dict) and isinstance(transcript.get("segments"), list)
    if not frames and not has_transcript and not subtitles:
        raise BundleInputError("no extraction outputs found; run extract_video.py first")


def _validate_manifest_source(manifest: dict[str, Any], identity: dict[str, Any]) -> None:
    if manifest.get("source") != identity:
        raise BundleInputError(
            f"bundle manifest conflict:\n  recorded: {manifest.get('source')}\n  now:      {identity}", 3)


def _extract_interval(params: dict[str, Any]) -> float:
    return float(_finite(params.get("interval")) or 5.0)


def _read_speech(inputs: BundleInputs, language: str) -> SpeechRead:
    transcript = inputs.transcript
    transcript_language = transcript.get("language") if isinstance(transcript, dict) else None
    raw_info = dict(inputs.raw_info)
    if not raw_info.get("language"):
        raw_info["language"] = inputs.metadata.get("language")
    source = select_speech_source(language, raw_info,
                                  [path for path in inputs.work.iterdir() if path.is_file()],
                                  transcript_language=transcript_language,
                                  transcript_available=isinstance(transcript, dict)
                                  and isinstance(transcript.get("segments"), list),
                                  video_stem=str(inputs.metadata.get("video_stem") or ""))
    segments = _speech_segments(inputs, source)
    return SpeechRead(segments, source.language, source.file, source.source)


def _speech_segments(inputs: BundleInputs, source: Any) -> list[SpeechSegment]:
    if source.source == "whisper":
        return [SpeechSegment(float(item["start"]), float(item["end"]), str(item["text"]).strip())
                for item in inputs.transcript.get("segments", []) if isinstance(item, dict)
                and _finite(item.get("start")) and _finite(item.get("end"))
                and float(item["end"]) > float(item["start"]) and str(item.get("text", "")).strip()]
    if source.source not in {"manual", "automatic"} or not source.file:
        return []
    path = Path(source.file)
    text = path.read_text(encoding="utf-8-sig")
    parsed = parse_vtt(text, rolling=source.source == "automatic") if path.suffix.lower() == ".vtt" else parse_srt(text)
    return dedupe_rolling_cues(parsed) if source.source == "automatic" else parsed


def transient_key_frames(frames: dict[str, Any], keyframes: list[str],
                         extract: dict[str, Any]) -> tuple[set[str], float | None]:
    """Find adaptive key frames followed quickly by another key frame."""
    if extract.get("sampling_mode") != "adaptive":
        return set(), None
    try:
        fps = float(extract.get("sampling_fps"))
    except (TypeError, ValueError):
        return set(), None
    if not math.isfinite(fps) or fps <= 0:
        return set(), None
    window = 1 / fps + 0.05
    key_names = {Path(name).name for name in keyframes}
    ordered = sorted(((float(pts), Path(name).name) for name, pts in frames.items()
                      if Path(name).name in key_names and _finite(pts)), key=lambda item: item[0])
    skipped = {name for (pts, name), (next_pts, _) in zip(ordered, ordered[1:])
               if next_pts <= pts + window}
    return skipped, window


def _bundle_params(args: argparse.Namespace, extract: dict[str, Any] | None = None) -> dict[str, Any]:
    engine = "skipped" if args.skip_ocr else _ocr_version()
    if args.skip_ocr:
        ocr_threads = None
    else:
        import os
        from ocr import default_ocr_threads
        ocr_threads = default_ocr_threads(os.cpu_count())
    return {"language": args.language, "ocr_min_score": args.ocr_min_score,
            "min_chapter": args.min_chapter, "max_chapter": args.max_chapter,
            "target_chapter": args.target_chapter, "skip_ocr": args.skip_ocr,
            "ocr_engine": engine, "ocr_intra_op_threads": ocr_threads,
            "ocr_skip_transient_s": transient_key_frames({}, [], extract or {})[1]}


def resume_outcome(inputs: BundleInputs, params: dict[str, Any]) -> int | None:
    manifest = inputs.manifest
    if manifest.get("source") != inputs.identity:
        print(f"bundle manifest conflict:\n  recorded: {manifest.get('source')}\n  now:      {inputs.identity}")
        return 3
    previous = manifest.get("params", {}).get("bundle")
    stage = manifest.get("stages", {}).get("bundle")
    bundle_path = inputs.work / "bundle.json"
    if not bundle_path.exists():
        return None
    try:
        existing_bundle = _json(bundle_path, {})
    except ValueError as error:
        print(f"bundle manifest conflict: {error}")
        return 3
    if existing_bundle.get("schema_version") == 1:
        print("bundle manifest conflict: existing schema-1 bundle.json; send it to the Recycle Bin "
              "and run build_bundle.py again")
        return 3
    if stage is None:
        print("bundle.json exists without a bundle manifest stage")
        return 3
    previous_signature = dict(previous or {})
    current_signature = dict(params)
    if current_signature.get("ocr_skip_transient_s") is None:
        current_signature.setdefault("ocr_skip_transient_s", None)
        previous_signature.setdefault("ocr_skip_transient_s", None)
    previous_signature.pop("ocr_intra_op_threads", None)
    current_signature.pop("ocr_intra_op_threads", None)
    if previous_signature != current_signature:
        print(f"bundle manifest conflict: recorded {previous}; now {params}")
        return 3
    print(f"BUNDLE: resumed | chapters {stage.get('chapters', 0)} | frames {stage.get('frames', 0)}")
    print(f"WORK FOLDER: {inputs.work}")
    return 0


def _make_chapters(inputs: BundleInputs, args: argparse.Namespace,
                   speech: SpeechRead) -> tuple[list[Any], list[Any]]:
    chapters = discover_chapters(inputs.metadata.get("chapters", []), inputs.duration,
                                 transcript_segments=[{"start": s.start, "end": s.end}
                                                      for s in speech.segments],
                                 min_chapter=args.min_chapter, max_chapter=args.max_chapter,
                                 target=args.target_chapter)
    assignments = align_frames(inputs.frames, chapters, inputs.keyframes, interval=inputs.interval)
    return chapters, assignments


def run_ocr(inputs: BundleInputs, assignments: list[Any],
            args: argparse.Namespace) -> tuple[dict[str, str], list[Any], dict[str, str]]:
    if args.skip_ocr:
        return {}, [], {}
    from ocr import ocr_key_frames
    extract = inputs.manifest.get("params", {}).get("extract", {})
    skipped_names, _ = transient_key_frames(inputs.frames, inputs.keyframes, extract)
    skipped = {item.file: "transient" for item in assignments
               if item.is_key and Path(item.file).name in skipped_names}
    frames = [(item.file, inputs.work / item.file) for item in assignments
              if item.is_key and item.file not in skipped]
    return ocr_key_frames(frames, min_score=args.ocr_min_score), frames, skipped


def _prepare(inputs: BundleInputs, args: argparse.Namespace) -> BundleResult:
    speech = _read_speech(inputs, args.language)
    chapters, assignments = _make_chapters(inputs, args, speech)
    return BundleResult(chapters, assignments, speech, {}, [], {})


def _with_ocr(inputs: BundleInputs, result: BundleResult,
              args: argparse.Namespace) -> BundleResult:
    texts, frames, skipped = run_ocr(inputs, result.assignments, args)
    return BundleResult(result.chapters, result.assignments, result.speech, texts, frames, skipped)


def write_outputs(inputs: BundleInputs, result: BundleResult,
                  params: dict[str, Any]) -> int:
    speech = result.speech
    bundle = build_bundle(inputs.metadata, result.chapters, speech.segments,
                          result.assignments, inputs.sponsors,
                          speech_language=speech.language, speech_file=speech.file,
                          speech_source=SpeechSource(speech.source, speech.language, speech.file),
                          ocr_texts=result.ocr_texts, ocr_skipped=result.ocr_skipped)
    _write_bundle(inputs, result, params, bundle)
    _report_bundle(inputs, result, speech.source)
    return 0


def _write_bundle(inputs: BundleInputs, result: BundleResult,
                  params: dict[str, Any], bundle: dict[str, Any]) -> None:
    (inputs.work / "bundle.json").write_text(json.dumps(bundle, ensure_ascii=False, indent=1) + "\n",
                                             encoding="utf-8")
    inputs.manifest.setdefault("params", {})["bundle"] = params
    inputs.manifest.setdefault("stages", {})["bundle"] = {
        "status": "done", "chapters": len(result.chapters), "frames": len(result.assignments),
        "ocr_frames": len(result.ocr_frames), "ocr_transient_skipped": len(result.ocr_skipped),
        "speech_source": result.speech.source}
    (inputs.work / "manifest.json").write_text(
        json.dumps(inputs.manifest, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def _report_bundle(inputs: BundleInputs, result: BundleResult, source: str) -> None:
    print(f"BUNDLE: chapters {len(result.chapters)} | frames {len(result.assignments)} | "
          f"OCR frames {len(result.ocr_frames)} | speech {source}")
    print(f"WORK FOLDER: {inputs.work}")


def main(argv: list[str] | None = None) -> int:
    args, exit_code = _parse_validate(argv)
    if exit_code is not None:
        return exit_code
    try:
        inputs = _load_inputs(args.video)
        result = _prepare(inputs, args)
        params = _bundle_params(args, inputs.manifest.get("params", {}).get("extract", {}))
        outcome = resume_outcome(inputs, params)
        if outcome is not None:
            return outcome
        return write_outputs(inputs, _with_ocr(inputs, result, args), params)
    except BundleInputError as error:
        print(str(error))
        return error.code
    except ImportError as error:
        print(f"OCR unavailable ({error}); run setup_tools.py or use --skip-ocr")
        return 1
    except (OSError, ValueError, TypeError, KeyError) as error:
        print(f"FAILED: {error}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
