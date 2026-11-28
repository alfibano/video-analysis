"""Extract frames and a timestamped transcript; run with -h for details."""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import queue
import subprocess
import sys
import threading
import wave
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO, Iterator

import numpy as np
from PIL import Image, ImageDraw
from tools import resolve_ffmpeg

SHEET_COLUMNS, SHEET_ROWS, CELL_WIDTH, CELL_HEIGHT = 3, 3, 640, 360
THUMB_SIZE = (64, 36)
SAMPLE_RATE = 16_000


class ExtractionError(Exception):
    """A processing step failed; the message says which and why."""


def ffmpeg_exe() -> str:
    tool = resolve_ffmpeg()
    if tool is None:
        raise ExtractionError("ffmpeg unavailable: install ffmpeg or imageio-ffmpeg")
    return tool.path


def run_ffmpeg(arguments: list[str], loglevel: str = "error") -> str:
    result = subprocess.run([ffmpeg_exe(), "-hide_banner", "-loglevel", loglevel, "-y", *arguments],
                            capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise ExtractionError(f"ffmpeg failed: {result.stderr.strip()[-500:]}")
    return result.stderr


def probe(video: Path) -> dict[str, object]:
    """Duration and audio presence, read from ffmpeg's own banner (no ffprobe in imageio-ffmpeg)."""
    result = subprocess.run([ffmpeg_exe(), "-hide_banner", "-i", str(video)],
                            capture_output=True, text=True, check=False)
    match = re.search(r"Duration: (\d+):(\d+):(\d+\.\d+)", result.stderr)
    if match is None:
        raise ExtractionError(f"not a readable video: {video}")
    hours, minutes, seconds = match.groups()
    return {"duration_s": int(hours) * 3600 + int(minutes) * 60 + float(seconds),
            "has_audio": "Audio:" in result.stderr}


def clock(seconds: float) -> str:
    whole = int(seconds)
    if whole >= 3600:
        return f"{whole // 3600}:{whole % 3600 // 60:02d}:{whole % 60:02d}"
    return f"{whole // 60:02d}:{whole % 60:02d}"


def parse_time(text: str) -> float:
    parts = text.split(":")
    if not 1 <= len(parts) <= 3:
        raise argparse.ArgumentTypeError(f"bad time '{text}', expected HH:MM:SS[.mmm]")
    try:
        values = [float(p) for p in parts]
    except ValueError as error:
        raise argparse.ArgumentTypeError(f"bad time '{text}'") from error
    seconds = 0.0
    for value in values:
        seconds = seconds * 60 + value
    return seconds


def load_frame_stamps(work: Path) -> dict[str, float]:
    frames_path = work / "frames.json"
    if not frames_path.exists():
        return {}
    try:
        raw = json.loads(frames_path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("expected an object")
        return {str(name): float(value) for name, value in raw.items()}
    except (OSError, ValueError, TypeError) as error:
        raise ExtractionError(f"invalid frame timestamp file {frames_path}: {error}") from error


def frame_time(frame: Path, interval: float, stamps: dict[str, float]) -> float:
    if frame.name in stamps:
        return stamps[frame.name]
    match = re.fullmatch(r"t(\d{6})\.(\d{3})", frame.stem)
    if match:
        return int(match.group(1)) + int(match.group(2)) / 1000
    return (int(frame.stem[1:]) - 1) * interval


def timestamp(seconds: float) -> str:
    total_ms = int(round(seconds * 1000))
    whole, millis = divmod(total_ms, 1000)
    return f"{whole // 3600:02d}:{whole % 3600 // 60:02d}:{whole % 60:02d}.{millis:03d}"


def frame_label(frame: Path, interval: float, stamps: dict[str, float]) -> str:
    return timestamp(frame_time(frame, interval, stamps))


def frame_filename(pts: float) -> str:
    """Return a lexically sortable timestamp name derived from rounded PTS milliseconds."""
    if not 0 <= pts <= 999_999.999:
        raise ExtractionError(f"frame PTS is outside the supported range: {pts}")
    milliseconds = int(round(pts * 1000))
    seconds, millis = divmod(milliseconds, 1000)
    return f"t{seconds:06d}.{millis:03d}.jpg"


def _showinfo_reader(stream: BinaryIO, records: queue.Queue[tuple[float, int, int] | None]) -> None:
    pattern = re.compile(r"pts_time:([-+]?\d+(?:\.\d+)?).*?\bs:(\d+)x(\d+)")
    try:
        for line in stream:
            match = pattern.search(line.decode("utf-8", errors="replace"))
            if match:
                records.put((float(match.group(1)), int(match.group(2)), int(match.group(3))))
    finally:
        records.put(None)


def _read_exact(stream: BinaryIO, size: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < size:
        part = stream.read(size - len(chunks))
        if not part:
            break
        chunks.extend(part)
    if len(chunks) != size:
        raise ExtractionError(f"ffmpeg returned an incomplete raw frame ({len(chunks)} of {size} bytes)")
    return bytes(chunks)


def _write_frame(folder: Path, pts: float, raw: bytes, size: tuple[int, int],
                 stamps: dict[str, float]) -> None:
    name = frame_filename(pts)
    if name in stamps:
        if stamps[name] == pts:
            return
        raise ExtractionError(f"frame PTS collision at rounded millisecond: {name}")
    if (folder / name).exists():
        raise ExtractionError(f"refusing to overwrite an existing frame: {name}")
    image = Image.frombytes("RGB", size, raw)
    image.save(folder / name, format="JPEG", quality=90)
    stamps[name] = pts


def _raw_frame_process(
    video: Path, interval: float, adaptive: bool
) -> tuple[subprocess.Popen[bytes], queue.Queue[tuple[float, int, int] | None], threading.Thread]:
    rate = "1" if adaptive else f"1/{interval}"
    command = [ffmpeg_exe(), "-hide_banner", "-loglevel", "info", "-copyts", "-i", str(video),
               "-an", "-vf", f"fps={rate},scale=960:-2,format=rgb24,showinfo",
               "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"]
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    records: queue.Queue = queue.Queue()
    thread = threading.Thread(target=_showinfo_reader, args=(process.stderr, records), daemon=True)
    thread.start()
    return process, records, thread


def _raw_frames(video: Path, interval: float, adaptive: bool) -> Iterator[tuple[float, bytes, tuple[int, int]]]:
    process, records, thread = _raw_frame_process(video, interval, adaptive)
    try:
        while (record := records.get()) is not None:
            pts, width, height = record
            yield pts, _read_exact(process.stdout, width * height * 3), (width, height)
        if process.stdout.read(1):
            raise ExtractionError("ffmpeg returned raw frames without matching showinfo PTS records")
        process.stdout.close()
        return_code = process.wait()
        thread.join()
        if return_code:
            raise ExtractionError("ffmpeg frame pass failed")
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        if thread.is_alive():
            thread.join(timeout=2)
        process.stdout.close()
        process.stderr.close()


def _thumbnail(raw: bytes, size: tuple[int, int]) -> np.ndarray:
    image = Image.frombytes("RGB", size, raw).convert("L").resize(THUMB_SIZE, Image.Resampling.BOX)
    return np.asarray(image, dtype=np.float32)


@dataclass
class FrameSelector:
    folder: Path
    interval: float
    adaptive: bool
    threshold: float
    margin: float
    stamps: dict[str, float] = field(default_factory=dict)
    ring: deque[tuple[float, bytes, tuple[int, int], np.ndarray]] = field(default_factory=deque)
    previous: np.ndarray | None = None
    start_pts: float | None = None
    next_grid: float = 0.0
    active_until: float = float("-inf")

    def add(self, pts: float, raw: bytes, size: tuple[int, int]) -> None:
        if not self.adaptive:
            _write_frame(self.folder, pts, raw, size, self.stamps)
            return
        thumb = _thumbnail(raw, size)
        self.start_pts = pts if self.start_pts is None else self.start_pts
        self._select_adaptive(pts, raw, size, thumb)

    def _select_adaptive(self, pts: float, raw: bytes, size: tuple[int, int],
                         thumb: np.ndarray) -> None:
        relative = pts - self.start_pts
        keep = relative + 1e-6 >= self.next_grid
        if keep:
            self.next_grid += max(1, int((relative - self.next_grid) // self.interval) + 1) * self.interval
        changed = self.previous is not None and float(np.abs(thumb - self.previous).mean()) > self.threshold
        if changed:
            self.active_until = max(self.active_until, pts + self.margin)
            for old_pts, old_raw, old_size, _ in self.ring:
                if old_pts >= pts - self.margin:
                    _write_frame(self.folder, old_pts, old_raw, old_size, self.stamps)
        if keep or pts <= self.active_until:
            _write_frame(self.folder, pts, raw, size, self.stamps)
        self.previous = thumb
        self.ring.append((pts, raw, size, thumb))
        while self.ring and self.ring[0][0] < pts - self.margin:
            self.ring.popleft()


def extract_frames(video: Path, work: Path, interval: float, *, adaptive: bool = True,
                   motion_threshold: float = 6.0, motion_margin: float = 1.0) -> int:
    folder = work / "frames"
    folder.mkdir(exist_ok=True)
    selector = FrameSelector(folder, interval, adaptive, motion_threshold, motion_margin)
    for pts, raw, size in _raw_frames(video, interval, adaptive):
        selector.add(pts, raw, size)
    ordered = dict(sorted(selector.stamps.items(), key=lambda item: item[1]))
    (work / "frames.json").write_text(
        json.dumps(ordered, indent=1), encoding="utf-8")
    return len(ordered)


def build_sheets(frames: list[Path], folder: Path, interval: float, stamps: dict[str, float]) -> int:
    folder.mkdir(exist_ok=True)
    per_sheet = SHEET_COLUMNS * SHEET_ROWS
    for sheet_index, start in enumerate(range(0, len(frames), per_sheet)):
        _write_sheet_page(frames[start:start + per_sheet], folder / f"sheet{sheet_index:02d}.jpg",
                          interval, stamps)
    return -(-len(frames) // per_sheet)


def _write_sheet_page(frames: list[Path], target: Path, interval: float,
                      stamps: dict[str, float]) -> None:
    sheet = Image.new("RGB", (SHEET_COLUMNS * CELL_WIDTH, SHEET_ROWS * CELL_HEIGHT), "white")
    draw = ImageDraw.Draw(sheet)
    for cell, frame in enumerate(frames):
        x, y = (cell % SHEET_COLUMNS) * CELL_WIDTH, (cell // SHEET_COLUMNS) * CELL_HEIGHT
        with Image.open(frame) as image:
            sheet.paste(image.convert("RGB").resize((CELL_WIDTH, CELL_HEIGHT)), (x, y))
        draw.rectangle([x, y, x + 74, y + 18], fill="yellow")
        draw.text((x + 3, y + 3), frame_label(frame, interval, stamps), fill="black")
    sheet.save(target, quality=80)


def build_key_sheets(keys: list[Path], frames: list[Path], key_folder: Path,
                     contact_folder: Path, interval: float,
                     stamps: dict[str, float]) -> list[dict[str, object]]:
    """Write unique key pages and index pages reused from the contact sheets."""
    per_sheet = SHEET_COLUMNS * SHEET_ROWS
    contact_pages = [frames[start:start + per_sheet] for start in range(0, len(frames), per_sheet)]
    index: list[dict[str, object]] = []
    for page_number, start in enumerate(range(0, len(keys), per_sheet)):
        page = keys[start:start + per_sheet]
        reused = next((number for number, contact in enumerate(contact_pages)
                       if [frame.name for frame in page] == [frame.name for frame in contact]
                       and (contact_folder / f"sheet{number:02d}.jpg").is_file()), None)
        if reused is None:
            key_folder.mkdir(exist_ok=True)
            path = key_folder / f"sheet{page_number:02d}.jpg"
            _write_sheet_page(page, path, interval, stamps)
            relative = f"keysheets/{path.name}"
        else:
            relative = f"sheets/sheet{reused:02d}.jpg"
        index.append({"page": page_number, "path": relative,
                      "frames": [frame.name for frame in page], "reused_contact_sheet": reused is not None})
    return index


def key_frames(frames: list[Path], threshold: float) -> list[Path]:
    """Frames whose grey 64x36 thumbnail differs from the previous KEPT one by > threshold (0-255)."""
    kept: list[Path] = []
    reference: np.ndarray | None = None
    for frame in frames:
        with Image.open(frame) as image:
            thumb = np.asarray(image.convert("L").resize(THUMB_SIZE), dtype=np.float32)
        if reference is None or float(np.abs(thumb - reference).mean()) > threshold:
            kept.append(frame)
            reference = thumb
    return kept


def extract_audio(video: Path, work: Path) -> Path:
    target = work / "audio.wav"
    # async resample pads a delayed audio start, so transcript times stay on the video clock.
    run_ffmpeg(["-i", str(video), "-vn", "-af", "aresample=async=1:first_pts=0",
                "-ac", "1", "-ar", str(SAMPLE_RATE), "-c:a", "pcm_s16le",
                str(target)])
    return target


@dataclass(frozen=True, slots=True)
class TranscriptionResult:
    rows: list[dict[str, object]]
    language: str
    requested_device: str
    actual_device: str | None
    compute_type: str | None
    fallback_reason: str | None

    def to_stage(self, transcribe: str = "whisper") -> dict[str, object]:
        return {"segments": len(self.rows), "source": "whisper", "transcribe": transcribe,
                "requested_device": self.requested_device, "actual_device": self.actual_device,
                "compute_type": self.compute_type, "fallback_reason": self.fallback_reason}


def prepend_nvidia_bin_paths(current_path: str, candidate_dirs: list[Path]) -> str:
    """Prepend existing NVIDIA wheel bin folders once, preserving current PATH."""
    parts = current_path.split(os.pathsep) if current_path else []
    known = {os.path.normcase(os.path.abspath(part)) for part in parts if part}
    prefix = []
    for candidate in candidate_dirs:
        if candidate.is_dir():
            value = str(candidate.resolve())
            key = os.path.normcase(os.path.abspath(value))
            if key not in known:
                known.add(key)
                prefix.append(value)
    return os.pathsep.join([*prefix, *parts])


def nvidia_bin_directories() -> list[Path]:
    try:
        spec = importlib.util.find_spec("nvidia")
        roots = list(spec.submodule_search_locations or []) if spec else []
    except (ImportError, ValueError):
        roots = []
    return [folder for root in roots for folder in Path(root).glob("*/bin") if folder.is_dir()]


def _whisper_factory(model_name: str, device: str, compute_type: str):
    if device == "cuda" and os.name == "nt":
        os.environ["PATH"] = prepend_nvidia_bin_paths(os.environ.get("PATH", ""), nvidia_bin_directories())
    from faster_whisper import WhisperModel
    return WhisperModel(model_name, device=device, compute_type=compute_type)


def _fallback_text(error: Exception) -> str:
    return f"{type(error).__name__}: {' '.join(str(error).split())}"[:160]


def transcribe(audio: Path, work: Path, model_name: str, language: str,
               requested_device: str = "auto", model_factory=None) -> TranscriptionResult:
    """Materialize complete Whisper attempts before output, restarting cleanly on CPU."""
    with wave.open(str(audio), "rb") as stream:
        raw = stream.readframes(stream.getnframes())
    samples = np.frombuffer(raw, np.int16).astype(np.float32) / 32768.0
    factory = model_factory or _whisper_factory
    attempts = [("cpu", "int8")] if requested_device == "cpu" else [("cuda", "int8_float16"), ("cpu", "int8")]
    fallback_reason = None
    for index, (device, compute_type) in enumerate(attempts):
        try:
            model = factory(model_name, device=device, compute_type=compute_type)
            segments, info = model.transcribe(samples, language=None if language == "auto" else language)
            rows = [{"start": round(segment.start, 2), "end": round(segment.end, 2),
                     "text": segment.text.strip()} for segment in segments]
            result = TranscriptionResult(rows, info.language, requested_device, device, compute_type,
                                         fallback_reason)
            break
        except Exception as error:
            if index == len(attempts) - 1:
                raise
            fallback_reason = _fallback_text(error)
    (work / "transcript.json").write_text(
        json.dumps({"language": result.language, "segments": result.rows}, ensure_ascii=False, indent=1),
        encoding="utf-8")
    lines = [f"[{clock(float(row['start']))}] {row['text']}" for row in result.rows]
    (work / "transcript.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return result


def caption_transcript(work: Path, video_stem: str, language: str) -> tuple[str, list[dict[str, object]]] | None:
    """Return normalized video-language caption segments when a usable file exists."""
    from bundle import read_speech_segments, select_speech_source
    try:
        raw_info = json.loads((work / "raw_info.json").read_text(encoding="utf-8"))
        metadata_path = work / "metadata.json"
        metadata = json.loads(metadata_path.read_text(encoding="utf-8")) if metadata_path.is_file() else {}
    except (OSError, json.JSONDecodeError, UnicodeError):
        raw_info, metadata = {}, {}
    raw_info = raw_info if isinstance(raw_info, dict) else {}
    raw_info.setdefault("language", metadata.get("language"))
    selected = language if language != "auto" else raw_info.get("language")
    if not isinstance(selected, str) or not selected:
        return None
    files = [path for path in work.iterdir() if path.is_file()]
    source = select_speech_source(selected, raw_info, files, transcript_available=False,
                                  video_stem=video_stem, work=work)
    if source.source not in {"manual", "automatic"}:
        return None
    segments = read_speech_segments(source, work, {})
    if not segments:
        return None
    rows = [{"start": segment.start, "end": segment.end, "text": segment.text} for segment in segments]
    return f"captions:{source.language}" + (":auto" if source.source == "automatic" else ""), rows


def write_caption_transcript(work: Path, language: str, rows: list[dict[str, object]]) -> None:
    """Write caption segments in the existing Whisper transcript file format."""
    (work / "transcript.json").write_text(
        json.dumps({"language": language, "segments": rows}, ensure_ascii=False, indent=1), encoding="utf-8")
    lines = [f"[{clock(float(row['start']))}] {row['text']}" for row in rows]
    (work / "transcript.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")


def exact_frames(video: Path, work: Path, times: list[float], duration: float) -> list[str]:
    folder = work / "at"
    folder.mkdir(exist_ok=True)
    written = []
    for seconds in times:
        if not 0 <= seconds < duration:
            print(f"skip --at {seconds:.3f}s: outside the video (0..{duration:.1f}s)")
            continue
        whole = int(seconds)
        name = f"at_{whole // 3600:02d}h{whole % 3600 // 60:02d}m{whole % 60:02d}s{int(round((seconds - whole) * 1000)):03d}.jpg"
        run_ffmpeg(["-ss", f"{seconds:.3f}", "-i", str(video), "-frames:v", "1", "-q:v", "2",
                    str(folder / name)])
        written.append(f"at/{name}")
    return written


def load_manifest(work: Path, identity: dict[str, object], params: dict[str, object]) -> dict:
    from tools import media_source_kind, same_video
    params = {key: value for key, value in params.items() if key not in {"transcribe", "device"}}
    path = work / "manifest.json"
    if not path.exists():
        if any(work.iterdir()):
            raise FileExistsError(f"{work} is not empty and has no manifest: refusing to mix outputs.\n"
                                  "Send that folder to the Recycle Bin (trash skill), then re-run.")
        return {"schema_version": 2, "source": {**identity, "kind": "local"},
                "params": {"fetch": {}, "extract": params}, "stages": {}}
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if manifest.get("schema_version") == 2:
        recorded_params = manifest.get("params", {})
        other_params = {key: value for key, value in recorded_params.items()
                        if key not in {"fetch", "extract"}}
        extract_params = {key: value for key, value in recorded_params.get("extract", {}).items()
                          if key not in {"transcribe", "device"}}
        fetch_params = recorded_params.get("fetch", {})
        fetch_only_unconfigured = not extract_params
    else:
        # v0.1 stored extraction settings directly in params.
        fetch_params = {}
        extract_params = manifest.get("params", {})
        other_params = {}
        fetch_only_unconfigured = False
        manifest = {"schema_version": 2, "source": manifest.get("source"),
                    "params": {"fetch": fetch_params, "extract": extract_params},
                    "stages": manifest.get("stages", {})}
    recorded_source = manifest.get("source") or {}
    source_kind = media_source_kind({"source": recorded_source,
                                     "params": {"fetch": fetch_params}})
    if recorded_source.get("kind") != source_kind:
        recorded_source = {**recorded_source,
                           "kind": source_kind}
    source_matches = same_video(recorded_source, identity)
    if fetch_only_unconfigured:
        extract_params = params
    if not source_matches or extract_params != params:
        raise FileExistsError(
            f"{work} was made from another video or other parameters.\n"
            f"  recorded: {recorded_source} {extract_params}\n"
            f"  now:      {identity} {params}\n"
            "Send that folder to the Recycle Bin (trash skill), then re-run.")
    manifest["source"] = recorded_source
    manifest["params"] = {"fetch": fetch_params, "extract": extract_params, **other_params}
    return manifest


def save_manifest(work: Path, manifest: dict) -> None:
    (work / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")


def folder_megabytes(folder: Path) -> float:
    return sum(f.stat().st_size for f in folder.rglob("*") if f.is_file()) / 1_048_576


def parse_arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=("Extract sampled frames, optional contact sheets, visible scene changes,\n"
                     "and an offline timestamped transcript from one local video.\n"
                     "Reads the video and a cached Whisper model, then writes all results into\n"
                     "<video folder>/<video name>.analysis/; it never deletes files.\n"
                     "Run setup_tools.py and selftest.py first when preparing a machine;\n"
                     "afterward read transcript.txt and the key frames. Use --contact-sheets\n"
                     "to create optional contact sheets for free analysis."),
        epilog=("Example: python extract_video.py \"lecture.mp4\" --interval 5 --language fr\n"
                "Exit codes: 0 success; 1 processing failure; 2 usage or input error;\n"
                "3 manifest conflict."))
    parser.add_argument("video", type=Path, help="local video file to analyze")
    parser.add_argument("--interval", type=float, default=5.0,
                        help="seconds between sampled frames (default: %(default)s)")
    parser.add_argument("--fixed-interval", action="store_true",
                        help="sample only at --interval instead of adaptive scene-aware sampling")
    parser.add_argument("--motion-threshold", type=float, default=6.0,
                        help="adaptive grayscale change threshold, 0-255 (default: %(default)s)")
    parser.add_argument("--motion-margin", type=float, default=1.0,
                        help="seconds retained before and after a change, 0-30 (default: %(default)s)")
    parser.add_argument("--dedup-threshold", type=float, default=6.0,
                        help="mean grey difference 0-255 for a key frame (default: %(default)s)")
    parser.add_argument("--model", default="small", help="Whisper model name (default: %(default)s)")
    parser.add_argument("--language", default="auto", help="transcript language, such as auto, fr, or en (default: %(default)s)")
    parser.add_argument("--transcribe", choices=("auto", "whisper"), default="auto",
                        help="use usable captions first or force Whisper (default: %(default)s)")
    parser.add_argument("--device", choices=("auto", "cpu", "cuda"), default="auto",
                        help="try CUDA then restart on CPU, or select CPU only (default: %(default)s)")
    parser.add_argument("--skip-frames", action="store_true", help="skip frame and optional contact-sheet extraction")
    parser.add_argument("--contact-sheets", action="store_true",
                        help="also create contact and key-frame sheets (default: off)")
    parser.add_argument("--skip-transcript", action="store_true", help="skip audio transcription")
    parser.add_argument("--at", type=parse_time, action="append", default=[],
                        help="also save a full-resolution frame at HH:MM:SS[.mmm]; repeatable")
    return parser.parse_args(argv)


def run(args: argparse.Namespace) -> None:
    video = args.video.resolve()
    work = video.parent / f"{video.stem}.analysis"
    stat = video.stat()
    identity = {"path": str(video), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}
    adaptive = not args.fixed_interval
    params = {"interval": args.interval, "dedup_threshold": args.dedup_threshold,
              "model": args.model, "language": args.language,
              "sampling_mode": "adaptive" if adaptive else "fixed",
              "sampling_fps": 1 if adaptive else 1 / args.interval,
              "motion_threshold": args.motion_threshold,
              "motion_margin_s": args.motion_margin,
              "sampler_version": 1, "frame_naming": "pts_ms_v1"}
    work.mkdir(exist_ok=True)
    manifest = load_manifest(work, identity, params)
    stages = manifest["stages"]
    manifest.update(probe(video))
    save_manifest(work, manifest)

    if not args.skip_frames:
        new_frames = not stages.get("frames")
        if new_frames:
            count = extract_frames(video, work, args.interval, adaptive=adaptive,
                                   motion_threshold=args.motion_threshold,
                                   motion_margin=args.motion_margin)
        if new_frames or args.contact_sheets:
            frames = sorted((work / "frames").glob("*.jpg"))
            stamps = load_frame_stamps(work)
            keys = key_frames(frames, args.dedup_threshold)
            if new_frames:
                (work / "keyframes.txt").write_text(
                    "".join(f"{frame_label(k, args.interval, stamps)}  frames/{k.name}\n"
                            for k in keys), encoding="utf-8")
                stages["frames"] = {"frames": count, "keyframes": len(keys)}
                save_manifest(work, manifest)
            if args.contact_sheets and not stages.get("sheets"):
                sheets = build_sheets(frames, work / "sheets", args.interval, stamps)
                key_sheet_index = build_key_sheets(keys, frames, work / "keysheets", work / "sheets",
                                                   args.interval, stamps)
                key_sheets = sum(not entry["reused_contact_sheet"] for entry in key_sheet_index)
                stages["sheets"] = {"sheets": sheets, "keysheets": key_sheets,
                                    "key_sheet_index": key_sheet_index}
                save_manifest(work, manifest)
            frame_count = stages["frames"]["frames"]
            key_count = stages["frames"]["keyframes"]
            if args.contact_sheets:
                sheet_stage = stages["sheets"]
                print(f"frames {frame_count} | sheets {sheet_stage['sheets']} | key frames {key_count}")
            else:
                print(f"frames {frame_count} | key frames {key_count}")

    if not args.skip_transcript and not stages.get("transcript"):
        caption_result = caption_transcript(work, video.stem, args.language) if args.transcribe == "auto" else None
        if caption_result:
            source, rows = caption_result
            language = source.split(":")[1]
            write_caption_transcript(work, language, rows)
            stages["transcript"] = {"segments": len(rows), "source": source, "transcribe": args.transcribe,
                                     "requested_device": args.device, "actual_device": None,
                                     "compute_type": None, "fallback_reason": None}
            print(f"transcript segments {len(rows)} from {source}")
        elif not manifest["has_audio"]:
            stages["transcript"] = {"segments": 0, "source": "none", "transcribe": args.transcribe,
                                     "requested_device": args.device, "actual_device": None,
                                     "compute_type": None, "fallback_reason": None,
                                     "note": "no audio stream"}
            print("no audio stream: transcript skipped")
        else:
            audio = extract_audio(video, work)
            result = transcribe(audio, work, args.model, args.language, args.device)
            stages["transcript"] = result.to_stage(args.transcribe)
            print(f"transcript segments {len(result.rows)} on {result.actual_device}/{result.compute_type}")
        save_manifest(work, manifest)

    if args.at:
        stages.setdefault("at", [])
        stages["at"] = sorted(set(stages["at"]) | set(exact_frames(video, work, args.at, manifest["duration_s"])))
        save_manifest(work, manifest)

    print(f"WORK FOLDER: {work}  ({folder_megabytes(work):.1f} MB)")
    print("When finished with this video, send that folder to the Recycle Bin (trash skill).")


def main(argv: list[str] | None = None) -> int:
    try:
        args = parse_arguments(argv)
    except SystemExit as exit_signal:
        return 0 if exit_signal.code == 0 else 2
    if not args.video.is_file():
        print(f"no such video: {args.video}")
        return 2
    if args.interval <= 0:
        print("--interval must be > 0")
        return 2
    if not args.fixed_interval and args.interval < 1:
        print("--interval must be at least 1 in adaptive mode")
        return 2
    if not 0 <= args.motion_threshold <= 255:
        print("--motion-threshold must be between 0 and 255")
        return 2
    if not 0 <= args.motion_margin <= 30:
        print("--motion-margin must be between 0 and 30")
        return 2
    if not 0 < args.dedup_threshold < 255:
        print("--dedup-threshold must be between 0 and 255")
        return 2
    try:
        run(args)
    except FileExistsError as conflict:
        print(conflict)
        return 3
    except (ExtractionError, OSError, wave.Error) as error:
        print(f"FAILED: {error}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
