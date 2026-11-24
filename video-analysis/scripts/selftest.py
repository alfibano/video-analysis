"""Self-test of extract_video.py on a synthetic 20 s video (two static shots + tone, cut at 10 s).

Usage:
    python selftest.py [--full] [--work-dir DIR]

  --full       also run Whisper (downloads the model once); default skips the transcript
  --work-dir   where to create the synthetic video (default: a fresh folder in the system temp dir)

Prints PASS/FAIL per check and the folder to send to the Recycle Bin afterwards. Exit 0 if all pass.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

HERE = Path(__file__).resolve().parent


def check_tool_resolution() -> bool:
    try:
        from tools import resolve_ffmpeg
    except ImportError:
        return False
    env = {"VIDEO_ANALYSIS_FFMPEG": "env-ffmpeg"}
    which = lambda command: {"ffmpeg": "path-ffmpeg"}.get(command)
    env_tool = resolve_ffmpeg(env=env, which=which, imageio_lookup=lambda: "bundled-ffmpeg")
    path_tool = resolve_ffmpeg(env={}, which=which, imageio_lookup=lambda: "bundled-ffmpeg")
    bundled_tool = resolve_ffmpeg(env={}, which=lambda _: None, imageio_lookup=lambda: "bundled-ffmpeg")
    return ((env_tool.path, env_tool.origin) == ("env-ffmpeg", "env")
            and (path_tool.path, path_tool.origin) == ("path-ffmpeg", "path")
            and (bundled_tool.path, bundled_tool.origin) == ("bundled-ffmpeg", "bundled"))


def check_js_runtime() -> bool:
    try:
        from tools import resolve_js_runtime
    except ImportError:
        return False
    resolved = resolve_js_runtime(which=lambda command: {"deno": "deno-bin", "node": "node-bin"}.get(command))
    fallback = resolve_js_runtime(which=lambda command: "node-bin" if command == "node" else None)
    return (resolved is not None and resolved.path == "deno-bin"
            and fallback is not None and fallback.path == "node-bin")


def check_ytdlp_module_resolution() -> bool:
    try:
        from tools import resolve_ytdlp
    except ImportError:
        return False
    fake = SimpleNamespace(__file__="fake/yt_dlp/__init__.py",
                           version=SimpleNamespace(__version__="test-version"))
    calls = []
    lookup = lambda name: calls.append(name) or fake
    module = resolve_ytdlp(env={}, which=lambda _: None, module_lookup=lookup)
    overridden = resolve_ytdlp(env={"VIDEO_ANALYSIS_YTDLP": "custom-ytdlp"},
                                which=lambda _: None,
                                module_lookup=lambda _: (_ for _ in ()).throw(AssertionError()))
    return (module is not None and module.path == fake.__file__ and module.origin == "module"
            and module.version == "test-version" and calls == ["yt_dlp"]
            and overridden is not None and overridden.path == "custom-ytdlp"
            and overridden.origin == "env")


def check_setup_plan() -> bool:
    try:
        import setup_tools
        from tools import Tool
    except ImportError:
        return False
    lookup = lambda module: None if module == "PIL" else (_ for _ in ()).throw(ImportError(module))
    missing = setup_tools.missing_packages(module_lookup=lookup, ffmpeg=Tool("ffmpeg", "path"))
    calls = []
    fake_run = lambda command, **kwargs: calls.append(command) or SimpleNamespace(returncode=0)
    with patch.object(setup_tools.subprocess, "run", side_effect=fake_run), \
            patch.object(setup_tools.sys, "executable", "python-test"):
        installed = setup_tools.install(missing)
    return (installed and "Pillow" not in missing and "imageio-ffmpeg" not in missing
            and "yt-dlp[default]" in missing and len(calls) == 1
            and calls[0] == ["python-test", "-m", "pip", "install", "--user", "--quiet", *missing])


def check_metadata_fixture() -> bool:
    try:
        from metadata import normalize_metadata
    except ImportError:
        return False
    info = json.loads((HERE / "fixtures" / "yt_dlp_info.json").read_text(encoding="utf-8"))
    normalized = normalize_metadata(info)
    empty = normalize_metadata({})
    return (normalized.get("upload_date") == "2024-03-07" and normalized.get("chapters") == []
            and normalized.get("tags") == [] and empty.get("title") is None
            and empty.get("upload_date") is None and empty.get("chapters") == [] and empty.get("tags") == [])


def make_offset_video(target: Path) -> None:
    from tools import resolve_ffmpeg
    ffmpeg = resolve_ffmpeg().path
    subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                    "color=c=darkred:size=320x180:rate=10:duration=10", "-c:v", "libx264",
                    "-pix_fmt", "yuv420p", "-output_ts_offset", "2", str(target)], check=True)


def check_offset_pts(folder: Path) -> bool:
    try:
        from extract_video import frame_time, frame_label, load_frame_stamps
        from tools import resolve_ffmpeg
    except ImportError:
        return False
    video = folder / "offset.mp4"
    make_offset_video(video)
    completed = extract(video, "--skip-transcript", "--interval", "2")
    work = folder / "offset.analysis"
    stamps = json.loads((work / "frames.json").read_text(encoding="utf-8"))
    loaded_stamps = load_frame_stamps(work)
    frames = sorted((work / "frames").glob("f*.jpg"))
    labels = (work / "keyframes.txt").read_text(encoding="utf-8")
    return (completed == 0 and frames and abs(frame_time(frames[0], 2, loaded_stamps) - 2.0) <= 0.05
            and abs(stamps[frames[0].name] - 2.0) <= 0.05
            and "00:00:02.000" in labels and frame_label(frames[0], 2, loaded_stamps) == "00:00:02.000"
            and resolve_ffmpeg() is not None)


def make_video(target: Path) -> None:
    import imageio_ffmpeg
    # Two STATIC shots: testsrc animates a counter, which would itself count as key frames.
    source = ("color=c=darkred:size=640x360:rate=10:duration=10[a];"
              "color=c=navy:size=640x360:rate=10:duration=10[b];[a][b]concat=n=2:v=1:a=0[v]")
    subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-y",
                    "-filter_complex", source, "-f", "lavfi", "-i", "sine=frequency=440:duration=20",
                    "-map", "[v]", "-map", "0:a", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
                    str(target)], check=True)


def extract(video: Path, *extra: str) -> int:
    return subprocess.run([sys.executable, str(HERE / "extract_video.py"), str(video), *extra],
                          check=False).returncode


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--full", action="store_true")
    parser.add_argument("--work-dir", type=Path)
    args = parser.parse_args()
    folder = args.work_dir or Path(tempfile.mkdtemp(prefix="video-analysis-selftest-"))
    folder.mkdir(parents=True, exist_ok=True)
    video = folder / "synthetic video é.mp4"
    make_video(video)
    work = folder / "synthetic video é.analysis"
    skip = [] if args.full else ["--skip-transcript"]

    first = extract(video, *skip, "--at", "00:00:12.250")
    manifest = json.loads((work / "manifest.json").read_text(encoding="utf-8"))
    frames_mtime = (work / "frames" / "f00001.jpg").stat().st_mtime
    second = extract(video, *skip)
    conflict = extract(video, *skip, "--interval", "3")
    orphan = folder / "orphan.mp4"
    orphan.write_bytes(video.read_bytes())
    (folder / "orphan.analysis").mkdir()
    (folder / "orphan.analysis" / "stray.txt").write_text("not ours", encoding="utf-8")
    orphan_refused = extract(orphan, *skip)
    checks = {
        "first run exit 0": first == 0,
        "folder created next to the video": work.parent == video.parent,
        "4 frames at 5 s interval": manifest["stages"]["frames"]["frames"] == 4,
        "1 contact sheet": len(list((work / "sheets").glob("*.jpg"))) == 1,
        "scene change detected (2 key frames)": manifest["stages"]["frames"]["keyframes"] == 2,
        "exact frame at 00:00:12.250": (work / "at" / "at_00h00m12s250.jpg").exists(),
        "audio detected": manifest["has_audio"] is True,
        "rerun exit 0 and frames untouched": second == 0
        and (work / "frames" / "f00001.jpg").stat().st_mtime == frames_mtime,
        "changed parameters refused with exit 3": conflict == 3,
        "non-empty folder without manifest refused with exit 3": orphan_refused == 3,
    }
    if args.full:
        with wave.open(str(work / "audio.wav"), "rb") as stream:
            checks["wav is 16 kHz mono PCM16"] = (stream.getframerate(), stream.getnchannels(),
                                                  stream.getsampwidth()) == (16_000, 1, 2)
        checks["transcript files written"] = (work / "transcript.json").exists() and (work / "transcript.txt").exists()
    try:
        checks["ffmpeg resolution precedence"] = check_tool_resolution()
        checks["yt-dlp resolved as module when CLI absent"] = check_ytdlp_module_resolution()
        checks["JS runtime prefers deno then node"] = check_js_runtime()
        checks["setup installs only missing dependencies"] = check_setup_plan()
        checks["metadata normalization fixture"] = check_metadata_fixture()
        checks["offset container PTS preserved"] = check_offset_pts(folder)
    except Exception as error:  # report failures as named checks, not a traceback
        print(f"FAIL  new checks raised {type(error).__name__}: {error}")
        checks["new checks completed"] = False
    for name, passed in checks.items():
        print(f"{'PASS' if passed else 'FAIL'}  {name}")
    print(f"test folder (send to the Recycle Bin when done): {folder}")
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
