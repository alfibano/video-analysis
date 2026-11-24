"""Check the video-analysis tools and extraction flow; run with -h for details."""
from __future__ import annotations

import argparse
import ast
import contextlib
import io
import json
import re
import subprocess
import sys
import tempfile
import wave
from pathlib import Path
from typing import Callable
from types import SimpleNamespace
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
CHILD_OUTPUT: list[str] = []
LAST_EXTRACT_OUTPUT = ""
CHECK_OUTPUTS: dict[str, str] = {}


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


def check_fetch_manifest() -> bool:
    try:
        import fetch_video
        from extract_video import load_manifest
    except ImportError:
        return False
    dest = Path(tempfile.mkdtemp(prefix="video-analysis-manifest-"))
    info = {"_type": "video", "id": "test-id", "title": "Sample video", "language": "fr",
            "webpage_url": "https://example.invalid/video", "duration": 12, "requested_downloads": []}
    calls: list[bool] = []
    option_sets: list[dict] = []

    class FakeYDL:
        def __init__(self, options: dict) -> None:
            self.options = options
            option_sets.append(options)

        def __enter__(self):
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def extract_info(self, url: str, *, download: bool):
            calls.append(download)
            if download:
                video = dest / "Sample_video_test-id.mp4"
                video.write_bytes(b"fake video")
                info["requested_downloads"] = [{"filepath": str(video)}]
            return info

        def sanitize_info(self, value: dict) -> dict:
            return value

        def prepare_filename(self, value: dict) -> str:
            return str(dest / "Sample_video_test-id.mp4")

    if fetch_video.main(["https://example.invalid/video", "--dest", str(dest)], ydl_factory=FakeYDL) != 0:
        return False
    video = dest / "Sample_video_test-id.mp4"
    folder = dest / f"{video.stem}.analysis"
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    accepted = load_manifest(folder, manifest["source"], {"interval": 5})
    preflight_pp = option_sets[0].get("postprocessors", [])
    download_pp = option_sets[1].get("postprocessors", [])
    sponsorblock_pp = [pp for pp in download_pp if pp.get("key") == "SponsorBlock"]
    has_sponsorblock = (not preflight_pp and len(sponsorblock_pp) == 1
                        and sponsorblock_pp[0].get("categories") == fetch_video.SPONSOR_CATEGORIES
                        and sponsorblock_pp[0].get("when") == "after_filter"
                        and all(pp.get("key") not in {"ModifyChapters", "SponsorBlockRemove"}
                                and "remove" not in pp for pp in download_pp)
                        and "sponsorblock_mark" not in option_sets[1]
                        and "sponsorblock_remove" not in option_sets[1])
    return (calls == [False, True, True] and video.is_file() and (folder / "metadata.json").is_file()
            and manifest["params"]["fetch"]["url"].startswith("https://")
            and "fetch" in accepted["stages"] and has_sponsorblock)


def check_subtitle_failure_is_best_effort() -> bool:
    try:
        import fetch_video
    except ImportError:
        return False
    dest = Path(tempfile.mkdtemp(prefix="video-analysis-subtitle-failure-"))
    option_sets: list[dict] = []
    info = {"_type": "video", "id": "short-id", "title": "Short", "language": "en",
            "webpage_url": "https://example.invalid/short"}
    video = dest / "Short_short-id.mp4"

    class FakeYDL:
        def __init__(self, options: dict) -> None:
            self.options = options
            option_sets.append(options)

        def __enter__(self):
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def extract_info(self, url: str, *, download: bool):
            if not download:
                return info
            if self.options.get("skip_download"):
                raise RuntimeError("HTTP Error 429: Too Many Requests")
            video.write_bytes(b"video")
            info["requested_downloads"] = [{"filepath": str(video)}]
            return info

        def sanitize_info(self, value: dict) -> dict:
            return value

        def prepare_filename(self, value: dict) -> str:
            return str(video)

    from contextlib import redirect_stdout
    output = io.StringIO()
    with redirect_stdout(output):
        result = fetch_video.main(["https://example.invalid/short", "--dest", str(dest)],
                                  ydl_factory=FakeYDL)
    work = dest / f"{video.stem}.analysis"
    manifest = json.loads((work / "manifest.json").read_text(encoding="utf-8"))
    subtitles = manifest["stages"]["fetch"]["subtitles"]
    return (result == 0 and video.is_file() and (work / "metadata.json").is_file()
            and subtitles == {"requested": ["en"], "saved": [], "missing": ["en"]}
            and len(option_sets) == 3 and "writesubtitles" not in option_sets[1]
            and "writeautomaticsub" not in option_sets[1] and "ignoreerrors" not in option_sets[1]
            and option_sets[2].get("ignoreerrors") is True
            and output.getvalue().startswith("WARNING: subtitles unavailable for en (HTTP Error 429"))


def check_legacy_manifest() -> bool:
    try:
        from extract_video import load_manifest
    except ImportError:
        return False
    folder = Path(tempfile.mkdtemp(prefix="video-analysis-legacy-"))
    (folder / "manifest.json").write_text(json.dumps({
        "source": {"path": "video.mp4"}, "params": {"interval": 5}, "stages": {},
    }), encoding="utf-8")
    migrated = load_manifest(folder, {"path": "video.mp4"}, {"interval": 5})
    return migrated["schema_version"] == 2 and migrated["params"]["extract"]["interval"] == 5


def check_playlist_live_refusal() -> bool:
    try:
        import fetch_video
    except ImportError:
        return False
    for info in ({"_type": "playlist"}, {"_type": "multi_video"}, {"entries": []},
                 {"is_live": True}, {"live_status": "is_upcoming"}):
        calls: list[bool] = []

        class FakeYDL:
            def __init__(self, options: dict) -> None:
                pass

            def __enter__(self):
                return self

            def __exit__(self, *_: object) -> None:
                return None

            def extract_info(self, url: str, *, download: bool):
                calls.append(download)
                return info

        if fetch_video.main(["https://example.invalid/video"], ydl_factory=FakeYDL) != 3 or calls != [False]:
            return False
    return True


def check_default_offline() -> bool:
    fetch_tree = ast.parse((HERE / "fetch_video.py").read_text(encoding="utf-8"))
    test_tree = ast.parse((HERE / "selftest.py").read_text(encoding="utf-8"))
    top_level_ytdlp = any(isinstance(node, (ast.Import, ast.ImportFrom)) and any(
        (alias.name == "yt_dlp" or alias.name.startswith("yt_dlp.")) for alias in node.names)
                          for node in fetch_tree.body)
    network_branch = any(isinstance(node, ast.If) and isinstance(node.test, ast.Attribute)
                         and node.test.attr == "network" and any(
                             isinstance(child, ast.Call) and isinstance(child.func, ast.Name)
                             and child.func.id == "check_network_fetch" for child in ast.walk(node))
                         for node in ast.walk(test_tree))
    return "yt_dlp" not in sys.modules and not top_level_ytdlp and network_branch


def check_network_fetch(url: str, folder: Path) -> bool:
    fetch = subprocess.run([sys.executable, str(HERE / "fetch_video.py"), url, "--dest", str(folder)],
                           check=False, capture_output=True, text=True)
    CHILD_OUTPUT.append(fetch.stdout + fetch.stderr)
    if fetch.returncode != 0:
        return False
    manifests = list(folder.glob("*.analysis/manifest.json"))
    videos = [path for path in folder.iterdir() if path.is_file() and path.suffix.lower() in
              {".mp4", ".mkv", ".webm", ".mov"}]
    if not manifests or not videos:
        return False
    return extract(videos[-1], "--skip-transcript") == 0


def check_documentation_contract() -> bool:
    skill = (HERE.parent / "SKILL.md").read_text(encoding="utf-8")
    readme = (HERE.parents[1] / "README.md").read_text(encoding="utf-8")
    return all("fetch_video.py" in text and "setup_tools.py --check" in text
               and "selftest.py --network" in text and "frames.json" in text
               for text in (skill, readme))


def check_script_help_contract() -> bool:
    scripts = ("extract_video.py", "fetch_video.py", "setup_tools.py", "selftest.py")
    for script in scripts:
        result = subprocess.run([sys.executable, str(HERE / script), "-h"],
                                capture_output=True, text=True, check=False)
        if result.returncode != 0:
            return False
        output = result.stdout
        if any(len(line) > 79 for line in output.splitlines()):
            return False
        if "Exit codes:" not in output:
            return False
        options_start = output.find("options:")
        if options_start < 0:
            return False
        description = output[:options_start]
        if len(re.findall(r"(?<=[.!?])\s+[A-Z]", description)) < 1:
            return False
        lines = output.splitlines()
        for section in ("positional arguments:", "options:"):
            try:
                start = lines.index(section) + 1
            except ValueError:
                if section == "positional arguments:" and script in {"setup_tools.py", "selftest.py"}:
                    continue
                return False
            end = next((index for index in range(start, len(lines))
                        if lines[index] in {"positional arguments:", "options:"}
                        or lines[index].startswith("Example:")
                        or lines[index].startswith("Exit codes:")), len(lines))
            entries = [index for index in range(start, end)
                       if re.match(r"^  \S", lines[index])]
            for index in entries:
                line = lines[index]
                has_inline_help = bool(re.search(r"\S\s{2,}\S", line))
                next_entry = next((candidate for candidate in entries if candidate > index), end)
                has_wrapped_help = any(re.match(r"^\s{4,}\S", following)
                                       for following in lines[index + 1:next_entry])
                if not has_inline_help and not has_wrapped_help:
                    return False
    return True


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
    global LAST_EXTRACT_OUTPUT
    result = subprocess.run([sys.executable, str(HERE / "extract_video.py"), str(video), *extra],
                            check=False, capture_output=True, text=True)
    LAST_EXTRACT_OUTPUT = result.stdout + result.stderr
    CHILD_OUTPUT.append(LAST_EXTRACT_OUTPUT)
    return result.returncode


def run_check(name: str, check: Callable[[], bool]) -> bool:
    CHILD_OUTPUT.clear()
    stdout, stderr = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            passed = bool(check())
    except Exception as error:
        passed = False
        stderr.write(f"{type(error).__name__}: {error}\n")
    details = stdout.getvalue() + stderr.getvalue() + "".join(CHILD_OUTPUT)
    if not passed and details.strip():
        CHECK_OUTPUTS[name] = details
    return passed


def main() -> int:
    parser = argparse.ArgumentParser(
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=("Build a synthetic video and check the extraction pipeline and\n"
                     "related offline contracts.\n"
                     "Reads the installed tools and writes test outputs beside a\n"
                     "generated video in a temporary folder or the selected work\n"
                     "directory; it never deletes files.\n"
                     "Run after setup_tools.py and before analyzing real videos;\n"
                     "the default run is offline."),
        epilog=("Example: python selftest.py --work-dir .video-test\n"
                "Exit codes: 0 all checks passed; 1 one or more checks failed;\n"
                "2 invalid arguments."))
    parser.add_argument("--full", action="store_true",
                        help="also run Whisper transcription and use its cached or downloaded model")
    parser.add_argument("--work-dir", type=Path,
                        help="folder for synthetic video and analysis (default: a fresh system temp folder)")
    parser.add_argument("--network", metavar="URL",
                        help="also fetch and extract one real online video URL")
    args = parser.parse_args()
    folder = args.work_dir or Path(tempfile.mkdtemp(prefix="video-analysis-selftest-"))
    folder.mkdir(parents=True, exist_ok=True)
    video = folder / "synthetic video é.mp4"
    make_video(video)
    work = folder / "synthetic video é.analysis"
    skip = [] if args.full else ["--skip-transcript"]

    first = extract(video, *skip, "--at", "00:00:12.250")
    first_output = LAST_EXTRACT_OUTPUT
    manifest = json.loads((work / "manifest.json").read_text(encoding="utf-8"))
    frames_mtime = (work / "frames" / "f00001.jpg").stat().st_mtime
    second = extract(video, *skip)
    second_output = LAST_EXTRACT_OUTPUT
    conflict = extract(video, *skip, "--interval", "3")
    conflict_output = LAST_EXTRACT_OUTPUT
    orphan = folder / "orphan.mp4"
    orphan.write_bytes(video.read_bytes())
    (folder / "orphan.analysis").mkdir()
    (folder / "orphan.analysis" / "stray.txt").write_text("not ours", encoding="utf-8")
    orphan_refused = extract(orphan, *skip)
    orphan_output = LAST_EXTRACT_OUTPUT
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
    for name, passed, output in (
        ("first run exit 0", first == 0, first_output),
        ("rerun exit 0 and frames untouched", second == 0, second_output),
        ("changed parameters refused with exit 3", conflict == 3, conflict_output),
        ("non-empty folder without manifest refused with exit 3", orphan_refused == 3, orphan_output),
    ):
        if not passed and output.strip():
            CHECK_OUTPUTS[name] = output
    if args.full:
        with wave.open(str(work / "audio.wav"), "rb") as stream:
            checks["wav is 16 kHz mono PCM16"] = (stream.getframerate(), stream.getnchannels(),
                                                  stream.getsampwidth()) == (16_000, 1, 2)
        checks["transcript files written"] = (work / "transcript.json").exists() and (work / "transcript.txt").exists()
    try:
        checks["ffmpeg resolution precedence"] = run_check("ffmpeg resolution precedence", check_tool_resolution)
        checks["yt-dlp resolved as module when CLI absent"] = run_check("yt-dlp resolved as module when CLI absent", check_ytdlp_module_resolution)
        checks["JS runtime prefers deno then node"] = run_check("JS runtime prefers deno then node", check_js_runtime)
        checks["setup installs only missing dependencies"] = run_check("setup installs only missing dependencies", check_setup_plan)
        checks["metadata normalization fixture"] = run_check("metadata normalization fixture", check_metadata_fixture)
        checks["fetch manifest accepted by extract"] = run_check("fetch manifest accepted by extract", check_fetch_manifest)
        checks["subtitle failure does not fail the fetch"] = run_check(
            "subtitle failure does not fail the fetch", check_subtitle_failure_is_best_effort)
        checks["v0.1 manifest remains compatible"] = run_check("v0.1 manifest remains compatible", check_legacy_manifest)
        checks["playlist and live are refused before download"] = run_check("playlist and live are refused before download", check_playlist_live_refusal)
        checks["default selftest is offline"] = run_check("default selftest is offline", check_default_offline)
        checks["documentation contract current"] = run_check("documentation contract current", check_documentation_contract)
        checks["every script help describes itself"] = run_check("every script help describes itself", check_script_help_contract)
        checks["offset container PTS preserved"] = run_check("offset container PTS preserved", lambda: check_offset_pts(folder))
        if args.network:
            checks["network fetch and extract"] = run_check(
                "network fetch and extract", lambda: check_network_fetch(args.network, folder / "network"))
    except Exception as error:  # report failures as named checks, not a traceback
        print(f"FAIL  new checks raised {type(error).__name__}: {error}")
        checks["new checks completed"] = False
    for name, passed in checks.items():
        print(f"{'PASS' if passed else 'FAIL'}  {name}")
        if not passed and CHECK_OUTPUTS.get(name):
            for line in CHECK_OUTPUTS[name].rstrip().splitlines():
                print(f"    {line}")
    print(f"test folder (send to the Recycle Bin when done): {folder}")
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
