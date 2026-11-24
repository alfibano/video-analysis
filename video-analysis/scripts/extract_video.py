"""Extract what a video SHOWS (sampled frames, contact sheets, key frames) and SAYS (timestamped
transcript), into a working folder created NEXT TO THE VIDEO: <video dir>/<video stem>.analysis/

Usage:
    python extract_video.py VIDEO [--interval 5] [--dedup-threshold 6] [--model small]
                                  [--language auto|fr|en|...] [--skip-frames] [--skip-transcript]
                                  [--at HH:MM:SS[.mmm]]...

Outputs (all inside the .analysis folder, nothing anywhere else except pip/model caches):
    frames/f00001.jpg ...   one frame every --interval seconds, 960 px wide
    sheets/sheet00.jpg ...  3x3 contact sheets, each frame stamped with its mm:ss time
    keyframes.txt           frames that differ visibly from the previous one (slide changes)
    keysheets/              contact sheets of the key frames only
    audio.wav               16 kHz mono PCM16
    transcript.json/.txt    timestamped segments
    at/at_00h01m23s500.jpg  full-resolution frames at exact --at times
    manifest.json           source identity, parameters, durations, stage status

Re-running resumes: finished stages are skipped. If the video or the parameters changed, it stops
(exit 3) rather than mixing outputs - send the old folder to the Recycle Bin first.

Exit codes: 0 success, 1 processing failure, 2 usage or input error, 3 manifest conflict.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import wave
from pathlib import Path

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
    return (int(frame.stem[1:]) - 1) * interval


def timestamp(seconds: float) -> str:
    total_ms = int(round(seconds * 1000))
    whole, millis = divmod(total_ms, 1000)
    return f"{whole // 3600:02d}:{whole % 3600 // 60:02d}:{whole % 60:02d}.{millis:03d}"


def frame_label(frame: Path, interval: float, stamps: dict[str, float]) -> str:
    return timestamp(frame_time(frame, interval, stamps))


def parse_showinfo(log: str) -> list[float]:
    return [float(value) for value in re.findall(r"pts_time:([-+]?\d+(?:\.\d+)?)", log)]


def extract_frames(video: Path, work: Path, interval: float) -> int:
    folder = work / "frames"
    folder.mkdir(exist_ok=True)
    log = run_ffmpeg(["-copyts", "-i", str(video), "-vf",
                      f"fps=1/{interval},scale=960:-2,showinfo", "-q:v", "3",
                      str(folder / "f%05d.jpg")], loglevel="info")
    frames = sorted(folder.glob("f*.jpg"))
    stamps = parse_showinfo(log)
    if len(frames) != len(stamps):
        raise ExtractionError(f"ffmpeg returned {len(frames)} frames but {len(stamps)} PTS values")
    (work / "frames.json").write_text(
        json.dumps({frame.name: pts for frame, pts in zip(frames, stamps)}, indent=1), encoding="utf-8")
    return len(frames)


def build_sheets(frames: list[Path], folder: Path, interval: float, stamps: dict[str, float]) -> int:
    folder.mkdir(exist_ok=True)
    per_sheet = SHEET_COLUMNS * SHEET_ROWS
    for sheet_index, start in enumerate(range(0, len(frames), per_sheet)):
        sheet = Image.new("RGB", (SHEET_COLUMNS * CELL_WIDTH, SHEET_ROWS * CELL_HEIGHT), "white")
        draw = ImageDraw.Draw(sheet)
        for cell, frame in enumerate(frames[start:start + per_sheet]):
            x, y = (cell % SHEET_COLUMNS) * CELL_WIDTH, (cell // SHEET_COLUMNS) * CELL_HEIGHT
            with Image.open(frame) as image:
                sheet.paste(image.convert("RGB").resize((CELL_WIDTH, CELL_HEIGHT)), (x, y))
            draw.rectangle([x, y, x + 74, y + 18], fill="yellow")
            draw.text((x + 3, y + 3), frame_label(frame, interval, stamps), fill="black")
        sheet.save(folder / f"sheet{sheet_index:02d}.jpg", quality=80)
    return -(-len(frames) // per_sheet)


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


def transcribe(audio: Path, work: Path, model_name: str, language: str) -> int:
    """Whisper on an in-memory array: passing a path breaks on recent PyAV ('metadata_errors')."""
    from faster_whisper import WhisperModel
    with wave.open(str(audio), "rb") as stream:
        raw = stream.readframes(stream.getnframes())
    samples = np.frombuffer(raw, np.int16).astype(np.float32) / 32768.0
    model = WhisperModel(model_name, device="cpu", compute_type="int8")
    segments, info = model.transcribe(samples, language=None if language == "auto" else language)
    rows = [{"start": round(s.start, 2), "end": round(s.end, 2), "text": s.text.strip()} for s in segments]
    (work / "transcript.json").write_text(
        json.dumps({"language": info.language, "segments": rows}, ensure_ascii=False, indent=1),
        encoding="utf-8")
    lines = [f"[{clock(row['start'])}] {row['text']}" for row in rows]
    (work / "transcript.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return len(rows)


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
    path = work / "manifest.json"
    if not path.exists():
        if any(work.iterdir()):
            raise FileExistsError(f"{work} is not empty and has no manifest: refusing to mix outputs.\n"
                                  "Send that folder to the Recycle Bin (trash skill), then re-run.")
        return {"schema_version": 2, "source": identity,
                "params": {"fetch": {}, "extract": params}, "stages": {}}
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if manifest.get("schema_version") == 2:
        recorded_params = manifest.get("params", {})
        extract_params = recorded_params.get("extract", {})
        fetch_params = recorded_params.get("fetch", {})
        fetch_only_unconfigured = not extract_params
    else:
        # v0.1 stored extraction settings directly in params.
        fetch_params = {}
        extract_params = manifest.get("params", {})
        fetch_only_unconfigured = False
        manifest = {"schema_version": 2, "source": manifest.get("source"),
                    "params": {"fetch": fetch_params, "extract": extract_params},
                    "stages": manifest.get("stages", {})}
    if fetch_only_unconfigured:
        extract_params = params
    if manifest.get("source") != identity or extract_params != params:
        raise FileExistsError(
            f"{work} was made from another video or other parameters.\n"
            f"  recorded: {manifest.get('source')} {extract_params}\n"
            f"  now:      {identity} {params}\n"
            "Send that folder to the Recycle Bin (trash skill), then re-run.")
    manifest["params"] = {"fetch": fetch_params, "extract": extract_params}
    return manifest


def save_manifest(work: Path, manifest: dict) -> None:
    (work / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")


def folder_megabytes(folder: Path) -> float:
    return sum(f.stat().st_size for f in folder.rglob("*") if f.is_file()) / 1_048_576


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Extract frames and transcript next to a video.")
    parser.add_argument("video", type=Path)
    parser.add_argument("--interval", type=float, default=5.0, help="seconds between frames (default 5)")
    parser.add_argument("--dedup-threshold", type=float, default=6.0,
                        help="mean grey difference 0-255 for a key frame (default 6)")
    parser.add_argument("--model", default="small", help="Whisper model (default small)")
    parser.add_argument("--language", default="auto", help="auto, fr, en, ... (default auto)")
    parser.add_argument("--skip-frames", action="store_true")
    parser.add_argument("--skip-transcript", action="store_true")
    parser.add_argument("--at", type=parse_time, action="append", default=[],
                        help="extra full-resolution frame at HH:MM:SS[.mmm]; repeatable")
    return parser.parse_args()


def run(args: argparse.Namespace) -> None:
    video = args.video.resolve()
    work = video.parent / f"{video.stem}.analysis"
    stat = video.stat()
    identity = {"path": str(video), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}
    params = {"interval": args.interval, "dedup_threshold": args.dedup_threshold,
              "model": args.model, "language": args.language}
    work.mkdir(exist_ok=True)
    manifest = load_manifest(work, identity, params)
    stages = manifest["stages"]
    manifest.update(probe(video))
    save_manifest(work, manifest)

    if not args.skip_frames and not stages.get("frames"):
        count = extract_frames(video, work, args.interval)
        frames = sorted((work / "frames").glob("f*.jpg"))
        stamps = load_frame_stamps(work)
        sheets = build_sheets(frames, work / "sheets", args.interval, stamps)
        keys = key_frames(frames, args.dedup_threshold)
        (work / "keyframes.txt").write_text(
            "".join(f"{frame_label(k, args.interval, stamps)}  frames/{k.name}\n" for k in keys), encoding="utf-8")
        key_sheets = build_sheets(keys, work / "keysheets", args.interval, stamps)
        stages["frames"] = {"frames": count, "sheets": sheets, "keyframes": len(keys), "keysheets": key_sheets}
        save_manifest(work, manifest)
        print(f"frames {count} | sheets {sheets} | key frames {len(keys)}")

    if not args.skip_transcript and not stages.get("transcript"):
        if not manifest["has_audio"]:
            stages["transcript"] = {"segments": 0, "note": "no audio stream"}
            print("no audio stream: transcript skipped")
        else:
            audio = extract_audio(video, work)
            segments = transcribe(audio, work, args.model, args.language)
            stages["transcript"] = {"segments": segments}
            print(f"transcript segments {segments}")
        save_manifest(work, manifest)

    if args.at:
        stages.setdefault("at", [])
        stages["at"] = sorted(set(stages["at"]) | set(exact_frames(video, work, args.at, manifest["duration_s"])))
        save_manifest(work, manifest)

    print(f"WORK FOLDER: {work}  ({folder_megabytes(work):.1f} MB)")
    print("When finished with this video, send that folder to the Recycle Bin (trash skill).")


def main() -> int:
    try:
        args = parse_arguments()
    except SystemExit as exit_signal:
        return 0 if exit_signal.code == 0 else 2
    if not args.video.is_file():
        print(f"no such video: {args.video}")
        return 2
    if args.interval <= 0:
        print("--interval must be > 0")
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
