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


def check_app_temp_folder() -> bool:
    try:
        from tools import TEMP_DIR_ENV, app_temp_dir, app_temp_path, make_temp_folder
    except ImportError:
        return False
    skill_temp_path = HERE.parent / "_temp"
    override_path = Path(__file__).resolve().parents[1] / "temp-override-check"
    if app_temp_path({}) != skill_temp_path or app_temp_path({TEMP_DIR_ENV: str(override_path)}) != override_path:
        return False
    try:
        app_temp_path({TEMP_DIR_ENV: "relative-temp-override"})
    except ValueError:
        pass
    else:
        return False
    temp_folder = make_temp_folder("video-analysis-temp-check-")
    return temp_folder.parent == app_temp_dir()


def check_temp_calls_are_centralized() -> bool:
    occurrences = {
        source.name
        for source in HERE.glob("*.py")
        if any(call in source.read_text(encoding="utf-8")
               for call in ("tempfile." + "mkdtemp(", "tempfile." + "gettempdir("))
    }
    tools_source = (HERE / "tools.py").read_text(encoding="utf-8")
    helper_call = "tempfile." + "mkdtemp(" + "prefix=prefix, dir=app_temp_dir())"
    return occurrences == {"tools.py"} and helper_call in tools_source


def check_fetch_manifest() -> bool:
    try:
        import fetch_video
        from extract_video import load_manifest
    except ImportError:
        return False
    from tools import make_temp_folder
    dest = make_temp_folder("video-analysis-manifest-")
    info = {"_type": "video", "id": "test-id", "title": "Sample video", "language": "fr",
            "webpage_url": "https://example.invalid/video", "duration": 12, "requested_downloads": []}
    video_name = f"{fetch_video.safe_video_stem(info['title'], info['id'])}.mp4"
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
                video = dest / video_name
                video.write_bytes(b"fake video")
                info["requested_downloads"] = [{"filepath": str(video)}]
            return info

        def sanitize_info(self, value: dict) -> dict:
            return value

        def prepare_filename(self, value: dict) -> str:
            return str(dest / video_name)

    if fetch_video.main(["https://example.invalid/video", "--dest", str(dest)], ydl_factory=FakeYDL) != 0:
        return False
    video = dest / video_name
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
    has_progress = all(option.get("noprogress") is True
                       and len(option.get("progress_hooks", [])) == 1 for option in option_sets)
    return (calls == [False, True, True] and video.is_file() and (folder / "metadata.json").is_file()
            and manifest["params"]["fetch"]["url"].startswith("https://")
            and option_sets[1]["outtmpl"] == str(dest.resolve() / f"{fetch_video.safe_video_stem(info['title'], info['id'])}.%(ext)s")
            and "fetch" in accepted["stages"] and has_sponsorblock and has_progress)


def check_safe_video_stem() -> bool:
    import fetch_video
    examples = (
        ("Avez-vous des électrons qui se trouvent dans les noyaux de vos atomes au moment où on se parle ?",
         "Nim6yyD4QzI", "avez_vous_des_electrons_qui_se_trouvent_Nim6yyD4QzI"),
        ("éèê", "AbC-123", "eee_AbC_123"),
        ("", "Nim6yyD4QzI", "video_Nim6yyD4QzI"),
        ("Title", "abc-def", "title_abc_def"),
        ("word " * 40, "Nim6yyD4QzI", "word_word_word_word_word_word_word_word_Nim6yyD4QzI"),
    )
    return all(fetch_video.safe_video_stem(title, video_id) == expected
               and len(fetch_video.safe_video_stem(title, video_id)) <= 60
               and re.fullmatch(r"[A-Za-z0-9_]+", fetch_video.safe_video_stem(title, video_id))
               for title, video_id, expected in examples)


def check_subtitle_failure_is_best_effort() -> bool:
    try:
        import fetch_video
    except ImportError:
        return False
    from tools import make_temp_folder
    dest = make_temp_folder("video-analysis-subtitle-failure-")
    option_sets: list[dict] = []
    info = {"_type": "video", "id": "short-id", "title": "Short", "language": "en",
            "webpage_url": "https://example.invalid/short"}
    video = dest / f"{fetch_video.safe_video_stem(info['title'], info['id'])}.mp4"

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
    from tools import make_temp_folder
    folder = make_temp_folder("video-analysis-legacy-")
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
               and "build_bundle.py" in text and "bundle.json" in text
               for text in (skill, readme))


def check_script_help_contract() -> bool:
    scripts = ("extract_video.py", "fetch_video.py", "setup_tools.py", "selftest.py", "build_bundle.py",
               "analyze_video.py")
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
    frames = sorted((work / "frames").glob("t*.jpg"))
    labels = (work / "keyframes.txt").read_text(encoding="utf-8")
    return (completed == 0 and frames and frames[0].name == "t000002.000.jpg"
            and abs(frame_time(frames[0], 2, loaded_stamps) - 2.0) <= 0.05
            and abs(stamps[frames[0].name] - 2.0) <= 0.05
            and "00:00:02.000" in labels and frame_label(frames[0], 2, loaded_stamps) == "00:00:02.000"
            and resolve_ffmpeg() is not None)


def check_adaptive_sampling_and_timestamp_names(folder: Path) -> bool:
    from extract_video import ExtractionError, _write_frame, frame_filename
    video = folder / "adaptive.mp4"
    make_video(video)
    completed = extract(video, "--skip-transcript")
    work = folder / "adaptive.analysis"
    stamps = json.loads((work / "frames.json").read_text(encoding="utf-8"))
    names = list(stamps)
    pts = [stamps[name] for name in names]
    manifest = json.loads((work / "manifest.json").read_text(encoding="utf-8"))
    params = manifest["params"]["extract"]
    collision = folder / "collision-frames"
    collision.mkdir()
    collision_stamps: dict[str, float] = {}
    _write_frame(collision, 1.0001, b"\0\0\0", (1, 1), collision_stamps)
    try:
        _write_frame(collision, 1.0002, b"\0\0\0", (1, 1), collision_stamps)
    except ExtractionError:
        collision_refused = True
    else:
        collision_refused = False
    try:
        frame_filename(1_000_000)
    except ExtractionError:
        range_refused = True
    else:
        range_refused = False
    return (completed == 0 and all(name == frame_filename(stamps[name]) for name in names)
            and names == sorted(names) and pts == sorted(pts)
            and frame_filename(999_999.999) == "t999999.999.jpg"
            and collision_refused and range_refused
            and all(any(abs(value - target) < 0.05 for value in pts)
                    for target in (0, 5, 9, 10, 11, 15))
            and params.get("sampling_mode") == "adaptive"
            and params.get("sampling_fps") == 1
            and params.get("motion_threshold") == 6.0
            and params.get("motion_margin_s") == 1.0
            and params.get("sampler_version") == 1
            and params.get("frame_naming") == "pts_ms_v1")


def check_fixed_interval_opt_out(folder: Path) -> bool:
    video = folder / "fixed.mp4"
    make_video(video)
    completed = extract(video, "--skip-transcript", "--fixed-interval")
    work = folder / "fixed.analysis"
    stamps = json.loads((work / "frames.json").read_text(encoding="utf-8"))
    params = json.loads((work / "manifest.json").read_text(encoding="utf-8"))["params"]["extract"]
    changed = extract(video, "--skip-transcript", "--fixed-interval", "--motion-threshold", "7")
    switched = extract(video, "--skip-transcript")
    return (completed == 0 and len(stamps) == 4
            and all(abs(stamps[name] - target) < 0.05 for name, target in zip(stamps, (0, 5, 10, 15)))
            and params.get("sampling_mode") == "fixed"
            and changed == 3 and switched == 3)


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


def check_bundle_chapters() -> bool:
    from bundle import discover_chapters
    chapters = discover_chapters(
        [{"start_time": 0, "end_time":":bad", "title":"bad"},
         {"start_time": 20, "end_time": 80, "title":"second"},
         {"start_time": 0, "end_time": 25, "title":"first"}],
        100, transcript_segments=[])
    fallback = discover_chapters([], 650, transcript_segments=[
        {"start": 290, "end": 292}, {"start": 588, "end": 590}])
    two_windows = discover_chapters([], 500)
    pause_windows = discover_chapters([], 610, transcript_segments=[
        {"start": 270, "end": 275}, {"start": 285, "end": 290}])
    short = discover_chapters([], 20)
    return ([(item.start, item.end, item.title) for item in chapters] ==
            [(0.0, 25.0, "first"), (25.0, 80.0, "second")] and
            all(item.source == "metadata" for item in chapters) and
            fallback[0].source == "fallback" and fallback[-1].end == 650 and
            all(item.end - item.start <= 600 for item in fallback) and
            [(item.start, item.end) for item in two_windows] == [(0, 300), (300, 500)] and
            pause_windows[0].end == 280 and pause_windows[1].start == 280 and
            len(short) == 1 and (short[0].start, short[0].end) == (0, 20))


def check_bundle_subtitle_priority() -> bool:
    from bundle import dedupe_rolling_cues, parse_vtt, select_speech_source
    fixture_dir = HERE / "fixtures"
    raw = json.loads((fixture_dir / "raw_info_subtitles.json").read_text(encoding="utf-8"))
    chosen = select_speech_source("fr", raw, ["video.fr.vtt"], transcript_language="en", video_stem="video")
    rolling = (fixture_dir / "youtube_auto.fr.vtt").read_text(encoding="utf-8")
    cues = dedupe_rolling_cues(parse_vtt(rolling, rolling=True))
    expected = ("Bonjour \u00e0 tous, aujourd'hui nous allons parler des \u00e9quations. "
                "C'est une le\u00e7on importante. Prenez votre cahier et notez")
    return (chosen.source == "manual" and chosen.language == "fr" and
            " ".join(cue.text for cue in cues) == expected)


def check_bundle_subtitle_parsers() -> bool:
    from bundle import parse_srt, parse_vtt
    fixture_dir = HERE / "fixtures"
    vtt = parse_vtt((fixture_dir / "manual.fr.vtt").read_text(encoding="utf-8"))
    srt = parse_srt((fixture_dir / "sample.en.srt").read_text(encoding="utf-8"))
    return (len(vtt) == 2 and vtt[0].text == "Bonjour à tous" and vtt[0].start == 0 and
            len(srt) == 2 and srt[1].text == "Second line" and srt[1].end == 4)


def check_bundle_frame_pts() -> bool:
    from bundle import Chapter, align_frames
    chapters = [Chapter(0, 5, "One", "test"), Chapter(5, 10, "Two", "test")]
    frames = align_frames({"f00001.jpg": 1.5, "f00002.jpg": 7.5}, chapters, {"f00002.jpg"})
    return (frames[0].chapter_index == 0 and frames[0].pts == 1.5 and
            frames[1].chapter_index == 1 and frames[1].is_key)


def check_bundle_sponsors() -> bool:
    from bundle import Chapter, FrameAssignment, SpeechSegment, build_bundle, parse_sponsors
    sponsors = parse_sponsors([{"segment": [0, 3], "category":"sponsor"},
                               {"start_time": 7, "end_time": 9, "title": "other"}])
    bundle = build_bundle({"title":"Test", "duration":10}, [Chapter(0, 10, "Chapter 1", "test")],
                          [SpeechSegment(1, 4, "ad"), SpeechSegment(5, 7, "content")],
                          [FrameAssignment("f.jpg", 2, 0, True)], sponsors)
    chapter = bundle["chapters"][0]
    return (chapter["speech_text"] == "content" and
            chapter["speech_segments"][0]["is_sponsor"] and
            chapter["frames"][0]["is_sponsor"] and
            [(item.start, item.end) for item in sponsors] == [(0, 3), (7, 9)] and
            chapter["sponsor_overlap"][0]["overlap_s"] == 3)


def check_ocr_key_frames() -> bool:
    from ocr import ocr_key_frames
    seen = []
    class FakeOutput:
        txts = ["Résumé", "low score"]
        scores = [0.95, 0.2]
    class FakeEngine:
        def __call__(self, frame):
            seen.append(frame)
            return FakeOutput()
    results = ocr_key_frames([("a.jpg", Path("a.jpg"))], lambda: FakeEngine())
    class EmptyOutput:
        txts = None
        scores = None
    empty = ocr_key_frames([("c.jpg", Path("c.jpg"))], lambda: lambda _: EmptyOutput())
    return seen == [Path("a.jpg")] and results == {"a.jpg": "Résumé"} and empty == {"c.jpg": ""}


def check_ocr_is_lazy() -> bool:
    import ast
    tree = ast.parse((HERE / "ocr.py").read_text(encoding="utf-8"))
    return not any((isinstance(node, ast.Import) and any(alias.name.startswith("rapidocr") for alias in node.names))
                   or (isinstance(node, ast.ImportFrom) and (node.module or "").startswith("rapidocr"))
                   for node in tree.body)


def check_ocr_thread_tuning_and_progress() -> bool:
    from ocr import KeyFrameOcr, default_ocr_threads, ocr_key_frames
    import build_bundle
    bundle_args = SimpleNamespace(skip_ocr=False, language=None, ocr_min_score=0.5,
                                  min_chapter=30, max_chapter=300, target_chapter=120)
    with patch.object(build_bundle, "_ocr_version", return_value="test"), \
            patch("os.cpu_count", return_value=14):
        bundle_params = build_bundle._bundle_params(bundle_args)
    bundle_args.skip_ocr = True
    with patch.object(build_bundle, "_ocr_version", return_value="skipped"):
        skipped_params = build_bundle._bundle_params(bundle_args)
    counts = {value: default_ocr_threads(value) for value in (None, 1, 14, 20, 64)}
    class FakeEngine:
        def __call__(self, _):
            return SimpleNamespace(txts=["text"], scores=[0.9])
    frames = [(f"f{i}.jpg", Path(f"f{i}.jpg")) for i in range(10)]
    class FakeRapidOCR:
        def __init__(self, *, params: dict[str, object]):
            self.params = params
        def __call__(self, _):
            return SimpleNamespace(txts=["text"], scores=[0.9])
    with patch.dict(sys.modules, {"rapidocr": SimpleNamespace(RapidOCR=FakeRapidOCR)}):
        with patch("ocr.os.cpu_count", return_value=8):
            configured = KeyFrameOcr()._get_engine().params
    output = io.StringIO()
    now = [0.0]
    result = ocr_key_frames(frames, lambda: FakeEngine(), stream=output,
                            monotonic=lambda: now[0])
    tty_output = io.StringIO()
    tty_output.isatty = lambda: True
    clock = [0.0]
    def advance_clock() -> float:
        clock[0] += 0.5
        return clock[0]
    ocr_key_frames(frames, lambda: FakeEngine(), stream=tty_output, monotonic=advance_clock)
    tty_updates = tty_output.getvalue().count("\r")
    return (counts == {None: 1, 1: 1, 14: 7, 20: 8, 64: 8}
            and configured == {"Global.log_level": "warning",
                               "EngineConfig.onnxruntime.intra_op_num_threads": 4}
            and bundle_params["ocr_intra_op_threads"] == 7
            and skipped_params["ocr_intra_op_threads"] is None
            and result["f0.jpg"] == "text"
            and "OCR 1/10" in output.getvalue() and "OCR 10/10" in output.getvalue()
            and 1 <= len(output.getvalue().splitlines()) <= 12
            and tty_updates / clock[0] <= 2)


def check_ocr_runtime_setting_not_in_resume_key() -> bool:
    import build_bundle
    from types import SimpleNamespace as NS
    base = {"language": None, "ocr_min_score": 0.5, "min_chapter": 30,
            "max_chapter": 300, "target_chapter": 120, "skip_ocr": False,
            "ocr_engine": "test", "ocr_intra_op_threads": 4}
    alternate = {**base, "ocr_intra_op_threads": 8}
    inputs = NS(manifest={"source": {"path": "video"}, "params": {"bundle": base},
                          "stages": {"bundle": {"status": "done"}}},
                identity={"path": "video"}, work=Path("."))
    with patch.object(Path, "exists", return_value=True):
        resumed = build_bundle.resume_outcome(inputs, alternate) == 0
        changed_semantics = {**alternate, "ocr_min_score": 0.6}
        refused = build_bundle.resume_outcome(inputs, changed_semantics) == 3
    return resumed and refused


def check_fetch_progress_policy() -> bool:
    from fetch_video import build_ydl_options, make_progress_hook
    output = io.StringIO()
    hook = make_progress_hook(stream=output, monotonic=lambda: 0.0)
    for percent in (1, 4, 10, 11, 57, 99, 100):
        hook({"status": "downloading", "downloaded_bytes": percent,
              "total_bytes": 100})
    hook({"status": "finished", "downloaded_bytes": 100, "total_bytes": 100})
    redirected = output.getvalue().splitlines()
    tty_output = io.StringIO()
    tty_output.isatty = lambda: True
    hook_tty = make_progress_hook(stream=tty_output, monotonic=lambda: 0.0)
    for percent in (1, 4, 10, 11, 57, 99, 100):
        hook_tty({"status": "downloading", "downloaded_bytes": percent,
                  "total_bytes": 100})
    hook_tty({"status": "finished", "downloaded_bytes": 100, "total_bytes": 100})
    unknown_output = io.StringIO()
    unknown = make_progress_hook(stream=unknown_output, monotonic=lambda: 0.0)
    unknown({"status": "downloading", "downloaded_bytes": 1024})
    unknown({"status": "downloading", "downloaded_bytes": 2048})
    unknown({"status": "finished", "downloaded_bytes": 2048})
    two_files = io.StringIO()
    consecutive = make_progress_hook(stream=two_files, monotonic=lambda: 0.0)
    for filename, downloaded, total, status in (
        ("video.mp4", 1, 100, "downloading"), ("video.mp4", 57, 100, "downloading"),
        ("video.mp4", 100, 100, "downloading"), ("video.mp4", 100, 100, "finished"),
        ("audio.m4a", 1, 20, "downloading"), ("audio.m4a", 11, 20, "downloading"),
        ("audio.m4a", 20, 20, "downloading"), ("audio.m4a", 20, 20, "finished"),
    ):
        consecutive({"status": status, "filename": filename,
                     "downloaded_bytes": downloaded, "total_bytes": total})
    options = build_ydl_options(Path("."))
    return (redirected == ["FETCH 1%", "FETCH 10%", "FETCH 20%", "FETCH 30%",
                           "FETCH 40%", "FETCH 50%", "FETCH 60%", "FETCH 70%",
                           "FETCH 80%", "FETCH 90%", "FETCH 100%"]
            and tty_output.getvalue().count("\r") <= 2
            and "\n" in tty_output.getvalue() and options.get("noprogress") is True
            and len(options.get("progress_hooks", [])) == 1
            and unknown_output.getvalue().splitlines() == ["FETCH 0.0 MB", "FETCH 0.0 MB"]
            and two_files.getvalue().splitlines() == [
                "FETCH 1%", "FETCH 10%", "FETCH 20%", "FETCH 30%", "FETCH 40%",
                "FETCH 50%", "FETCH 60%", "FETCH 70%", "FETCH 80%", "FETCH 90%", "FETCH 100%",
                "FETCH 5%", "FETCH 10%", "FETCH 20%", "FETCH 30%", "FETCH 40%",
                "FETCH 50%", "FETCH 60%", "FETCH 70%", "FETCH 80%", "FETCH 90%", "FETCH 100%"])


def check_setup_ocr_readiness() -> bool:
    import setup_tools
    fake = SimpleNamespace(__version__="9.8.7")
    output = io.StringIO()
    with patch.object(setup_tools.importlib, "import_module", return_value=fake), \
            contextlib.redirect_stdout(output):
        present = setup_tools.report_ocr()
    missing_output = io.StringIO()
    with patch.object(setup_tools.importlib, "import_module", side_effect=ImportError("missing")), \
            contextlib.redirect_stdout(missing_output):
        missing = setup_tools.report_ocr()
    return (present and not missing and "OCR: rapidocr 9.8.7" in output.getvalue()
            and "OCR: rapidocr missing" in missing_output.getvalue())


def check_analyze_video_local_pipeline() -> bool:
    import analyze_video
    calls = []
    with patch.object(analyze_video, "extract_main", side_effect=lambda argv: calls.append(("extract", argv)) or 0), \
            patch.object(analyze_video, "bundle_main", side_effect=lambda argv: calls.append(("bundle", argv)) or 0), \
            contextlib.redirect_stderr(io.StringIO()):
        code = analyze_video.main(["C:\\videos\\talk.mp4", "--skip-transcript", "--skip-ocr",
                                   "--language", "fr", "--interval", "3", "--at", "00:00:10"])
    return (code == 0 and [call[0] for call in calls] == ["extract", "bundle"]
            and calls[0][1][0] == "C:\\videos\\talk.mp4"
            and "--skip-transcript" in calls[0][1] and "--skip-ocr" in calls[1][1]
            and calls[0][1][calls[0][1].index("--language") + 1] == "fr"
            and calls[1][1][calls[1][1].index("--language") + 1] == "fr"
            and "--at" in calls[0][1] and "--interval" in calls[0][1])


def check_analyze_video_url_pipeline() -> bool:
    import analyze_video
    calls = []
    def fetch(argv, *, on_video=None):
        calls.append(("fetch", argv))
        on_video(Path("downloaded.mp4"))
        return 0
    with patch.object(analyze_video, "fetch_main", side_effect=fetch), \
            patch.object(analyze_video, "extract_main", side_effect=lambda argv: calls.append(("extract", argv)) or 0), \
            patch.object(analyze_video, "bundle_main", side_effect=lambda argv: calls.append(("bundle", argv)) or 0), \
            contextlib.redirect_stderr(io.StringIO()):
        code = analyze_video.main(["https://example.invalid/talk", "--dest", "downloads", "--sub-langs", "fr"])
    return (code == 0 and [call[0] for call in calls] == ["fetch", "extract", "bundle"]
            and calls[1][1][0] == "downloaded.mp4")


def check_analyze_video_stops_on_failure() -> bool:
    import analyze_video
    calls = []
    for failure in (1, 2, 3):
        with patch.object(analyze_video, "extract_main", side_effect=lambda argv: calls.append("extract") or failure), \
                patch.object(analyze_video, "bundle_main", side_effect=lambda argv: calls.append("bundle") or 0), \
                contextlib.redirect_stderr(io.StringIO()):
            code = analyze_video.main(["talk.mp4"])
        if code != failure or calls != ["extract"]:
            return False
        calls.clear()
    with patch.object(analyze_video, "extract_main", return_value=0), \
            patch.object(analyze_video, "bundle_main", return_value=0), \
            contextlib.redirect_stderr(io.StringIO()):
        invalid_local = analyze_video.main(["C:\\videos\\talk.mp4", "--sub-langs", "fr"])
    with patch.object(analyze_video, "fetch_main", return_value=3), \
            patch.object(analyze_video, "extract_main", side_effect=lambda argv: calls.append("unexpected")), \
            contextlib.redirect_stderr(io.StringIO()):
        fetch_failure = analyze_video.main(["https://example.invalid/talk"])
    return invalid_local == 2 and fetch_failure == 3 and calls == []


def check_setup_entry_modes() -> bool:
    import setup_tools
    calls = []
    with patch.object(setup_tools, "run_selftest", side_effect=lambda: calls.append("selftest") or 0), \
            patch.object(setup_tools, "missing_packages", side_effect=lambda **kwargs: calls.append("missing") or []), \
            patch.object(setup_tools, "report_tools", side_effect=lambda *args: calls.append("report") or True), \
            patch.object(setup_tools, "ffmpeg_works", side_effect=lambda *args: calls.append("ffmpeg") or True), \
            patch.object(setup_tools, "model_cached", side_effect=lambda *args: calls.append("model") or True), \
            patch.object(setup_tools, "warm_ocr", side_effect=lambda: calls.append("warm") or True):
        with patch.object(setup_tools, "run_selftest", return_value=7):
            check_code = setup_tools.main(["--check"])
        check_calls = list(calls)
        calls.clear()
        normal_code = setup_tools.main(["--no-model-download"])
        normal_calls = list(calls)
        with patch.object(setup_tools, "run_selftest", return_value=5):
            failed_selftest = setup_tools.main(["--no-model-download"])
    run_call = SimpleNamespace(returncode=9)
    with patch.object(setup_tools.subprocess, "run", return_value=run_call) as subprocess_run, \
            patch.object(setup_tools.sys, "executable", "python-test"):
        returned = setup_tools.run_selftest()
    command = subprocess_run.call_args.args[0]
    return (check_code == 7 and check_calls == [] and normal_code == 0
            and normal_calls.count("selftest") == 1 and normal_calls.index("selftest") > normal_calls.index("warm")
            and failed_selftest == 1 and returned == 9 and command ==
            ["python-test", str(HERE / "selftest.py")] and "shell" not in subprocess_run.call_args.kwargs)


def check_transient_key_frames_are_not_ocrd() -> bool:
    import build_bundle
    from bundle import FrameAssignment
    from types import SimpleNamespace as NS
    frames = {"frames/a.jpg": 0.0, "frames/b.jpg": 0.8, "frames/c.jpg": 1.8}
    inputs = NS(manifest={"params": {"extract": {"sampling_mode": "adaptive", "sampling_fps": 1}}},
                frames=frames, keyframes=["a.jpg", "b.jpg", "c.jpg"], work=Path("."))
    assignments = [FrameAssignment(name, pts, 0, True) for name, pts in frames.items()]
    seen = []
    with patch("ocr.ocr_key_frames", side_effect=lambda values, **kwargs: seen.extend(values) or {values[0][0]: "text"}):
        texts, ocr_frames, skipped = build_bundle.run_ocr(inputs, assignments,
                                                           SimpleNamespace(skip_ocr=False, ocr_min_score=0.5))
    adaptive_skips, window = build_bundle.transient_key_frames(
        frames, inputs.keyframes, {"sampling_mode": "adaptive", "sampling_fps": 1})
    fixed_skips, fixed_window = build_bundle.transient_key_frames(
        frames, inputs.keyframes, {"sampling_mode": "fixed", "sampling_fps": 1})
    legacy_inputs = NS(manifest={"source": {"path": "video"}, "params": {"bundle": {"ocr_engine": "skipped"}},
                                 "stages": {"bundle": {"status": "done"}}}, identity={"path": "video"},
                       work=Path("."))
    with patch.object(Path, "exists", return_value=True):
        legacy_resume = build_bundle.resume_outcome(
            legacy_inputs, {"ocr_engine": "skipped", "ocr_skip_transient_s": None}) == 0
        adaptive_conflict = build_bundle.resume_outcome(
            legacy_inputs, {"ocr_engine": "skipped", "ocr_skip_transient_s": 1.05}) == 3
    output_frames = __import__("bundle")._chapter_frames(0, assignments, [], texts, skipped)
    params = build_bundle._bundle_params(
        SimpleNamespace(skip_ocr=True, language="auto", ocr_min_score=0.5,
                        min_chapter=30, max_chapter=600, target_chapter=300),
        {"sampling_mode": "adaptive", "sampling_fps": 1})
    return (len(seen) == 1 and seen[0][0] == "frames/c.jpg" and len(ocr_frames) == 1
            and skipped == {"frames/a.jpg": "transient", "frames/b.jpg": "transient"}
            and texts == {"frames/c.jpg": "text"} and adaptive_skips == {"a.jpg", "b.jpg"}
            and window == 1.05 and not fixed_skips and fixed_window is None
            and legacy_resume and adaptive_conflict
            and params["ocr_skip_transient_s"] == 1.05
            and output_frames[0]["ocr_text"] is None
            and output_frames[0]["ocr_skipped"] == "transient")


def check_build_bundle(video: Path) -> bool:
    result = subprocess.run([sys.executable, str(HERE / "build_bundle.py"), str(video), "--skip-ocr"],
                            capture_output=True, text=True, check=False)
    CHILD_OUTPUT.append(result.stdout + result.stderr)
    if result.returncode != 0:
        return False
    work = video.parent / f"{video.stem}.analysis"
    bundle = json.loads((work / "bundle.json").read_text(encoding="utf-8"))
    manifest = json.loads((work / "manifest.json").read_text(encoding="utf-8"))
    chapters = bundle["chapters"]
    frames = [frame for chapter in chapters for frame in chapter["frames"]]
    bundle_text = (work / "bundle.json").read_text(encoding="utf-8")
    path_values = [bundle["source"]["video"], bundle["speech"]["file"]]
    path_values.extend(frame["file"] for frame in frames)
    paths_resolve = all(value is None or not Path(value).is_absolute()
                        and not re.match(r"^[A-Za-z]:", value)
                        and (work / value).resolve().is_file()
                        for value in path_values)
    return (chapters and chapters[0]["start"] == 0 and chapters[-1]["end"] >= 19.9
            and bundle["schema_version"] == 2 and bundle["path_base"] == "analysis_folder"
            and bundle["source"]["video"] == f"../{video.name}"
            and not re.search(r"[A-Za-z]:[\\/]", bundle_text) and paths_resolve
            and bundle["source"]["duration"] >= 19.9
            and all(frame["pts"] is not None for frame in frames)
            and bundle["speech"]["source"] in {"whisper", "none"}
            and manifest["params"]["bundle"]["ocr_engine"] == "skipped"
            and manifest["params"]["bundle"]["ocr_intra_op_threads"] is None)


def check_bundle_without_fetch() -> bool:
    from tools import make_temp_folder
    folder = make_temp_folder("bundle-no-fetch-")
    video = folder / "local.mp4"
    video.write_bytes(b"identity only")
    work = folder / "local.analysis"
    work.mkdir()
    (work / "metadata.json").write_text(json.dumps({"duration": 8, "chapters": []}), encoding="utf-8")
    (work / "frames.json").write_text(json.dumps({"f00001.jpg": 0}), encoding="utf-8")
    (work / "frames").mkdir()
    (work / "frames" / "f00001.jpg").write_bytes(b"placeholder")
    (work / "keyframes.txt").write_text("00:00:00.000  frames/f00001.jpg\n", encoding="utf-8")
    stat = video.stat()
    (work / "manifest.json").write_text(json.dumps({"schema_version": 2,
        "source": {"path": str(video.resolve()), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns},
        "params": {"fetch": {}, "extract": {"interval": 5}}, "stages": {}}), encoding="utf-8")
    result = subprocess.run([sys.executable, str(HERE / "build_bundle.py"), str(video), "--skip-ocr"],
                            capture_output=True, text=True, check=False)
    CHILD_OUTPUT.append(result.stdout + result.stderr)
    if result.returncode != 0:
        return False
    bundle_path = work / "bundle.json"
    bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    bundle["schema_version"] = 1
    bundle_path.write_text(json.dumps(bundle), encoding="utf-8")
    refusal = subprocess.run([sys.executable, str(HERE / "build_bundle.py"), str(video), "--skip-ocr"],
                             capture_output=True, text=True, check=False)
    CHILD_OUTPUT.append(refusal.stdout + refusal.stderr)
    return bundle["speech"]["source"] == "none" and refusal.returncode == 3


def check_bundle_manual_subtitle() -> bool:
    from tools import make_temp_folder
    folder = make_temp_folder("bundle-manual-subtitle-")
    video = folder / "lecture.mp4"
    video.write_bytes(b"identity only")
    work = folder / "lecture.analysis"
    work.mkdir()
    (work / "metadata.json").write_text(json.dumps({"duration": 10, "language": "fr"}), encoding="utf-8")
    (work / "raw_info.json").write_text(json.dumps({"language": "fr", "subtitles": {"fr": [{}]},
        "automatic_captions": {"fr": [{}]}}), encoding="utf-8")
    (work / "lecture.fr.vtt").write_text("WEBVTT\n\n00:00:01.000 --> 00:00:03.000\nTexte manuel\n", encoding="utf-8")
    stat = video.stat()
    (work / "manifest.json").write_text(json.dumps({"schema_version": 2,
        "source": {"path": str(video.resolve()), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns},
        "params": {"fetch": {}, "extract": {"interval": 5}}, "stages": {}}), encoding="utf-8")
    result = subprocess.run([sys.executable, str(HERE / "build_bundle.py"), str(video), "--skip-ocr"],
                            capture_output=True, text=True, check=False)
    CHILD_OUTPUT.append(result.stdout + result.stderr)
    if result.returncode:
        return False
    bundle = json.loads((work / "bundle.json").read_text(encoding="utf-8"))
    return bundle["speech"]["source"] == "manual" and bundle["chapters"][0]["speech_text"] == "Texte manuel"


def check_bundle_manifest_resume_conflict(video: Path) -> bool:
    work = video.parent / f"{video.stem}.analysis"
    target = HERE / "build_bundle.py"
    command = [sys.executable, str(target), str(video), "--skip-ocr"]
    before = (work / "bundle.json").stat().st_mtime_ns
    resume = subprocess.run(command, capture_output=True, text=True, check=False)
    after = (work / "bundle.json").stat().st_mtime_ns
    changed = subprocess.run([sys.executable, str(target), str(video), "--skip-ocr", "--language", "fr"],
                             capture_output=True, text=True, check=False)
    CHILD_OUTPUT.extend((resume.stdout + resume.stderr, changed.stdout + changed.stderr))
    return resume.returncode == 0 and before == after and changed.returncode == 3


def check_extract_preserves_bundle_params() -> bool:
    from extract_video import load_manifest
    from tools import make_temp_folder
    folder = make_temp_folder("bundle-preserve-")
    data = {"schema_version": 2, "source": {"path": "video.mp4"},
            "params": {"fetch": {}, "extract": {"interval": 5}, "bundle": {"ocr_engine": "skipped"}},
            "stages": {"bundle": {"status": "done"}}}
    (folder / "manifest.json").write_text(json.dumps(data), encoding="utf-8")
    result = load_manifest(folder, {"path": "video.mp4"}, {"interval": 5})
    return result["params"]["bundle"] == {"ocr_engine": "skipped"} and "bundle" in result["stages"]


def check_real_ocr_smoke() -> tuple[bool, str]:
    try:
        from ocr import ocr_key_frames
        import numpy as np
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        return True, "SKIP"
    image = Image.new("RGB", (800, 160), "white")
    font = next((ImageFont.truetype(name, 56) for name in ("arial.ttf", "DejaVuSans.ttf")
                 if _font_available(ImageFont, name)), ImageFont.load_default())
    ImageDraw.Draw(image).text((20, 35), "R\u00e9sum\u00e9", fill="black", font=font)
    try:
        recognized = ocr_key_frames([("smoke.png", np.asarray(image))]).get("smoke.png", "")
    except ImportError:
        return True, "SKIP"
    import unicodedata
    normalized = "".join(char for char in unicodedata.normalize("NFKD", recognized.casefold())
                         if not unicodedata.combining(char))
    return "resume" in normalized, "PASS" if "resume" in normalized else f"FAIL: OCR read {recognized!r}"


def _font_available(font_module: object, name: str) -> bool:
    try:
        font_module.truetype(name, 56)
    except OSError:
        return False
    return True


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
                        help="folder for synthetic video and analysis (default: a fresh folder in video-analysis/_temp)")
    parser.add_argument("--network", metavar="URL",
                        help="also fetch and extract one real online video URL")
    args = parser.parse_args()
    from tools import make_temp_folder
    folder = args.work_dir or make_temp_folder("video-analysis-selftest-")
    folder.mkdir(parents=True, exist_ok=True)
    video = folder / "synthetic video é.mp4"
    make_video(video)
    work = folder / "synthetic video é.analysis"
    skip = [] if args.full else ["--skip-transcript"]

    first = extract(video, *skip, "--at", "00:00:12.250")
    first_output = LAST_EXTRACT_OUTPUT
    manifest = json.loads((work / "manifest.json").read_text(encoding="utf-8"))
    frames_mtime = next((work / "frames").glob("*.jpg")).stat().st_mtime
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
        "adaptive base grid and change margin": manifest["stages"]["frames"]["frames"] >= 6,
        "1 contact sheet": len(list((work / "sheets").glob("*.jpg"))) == 1,
        "scene change detected (2 key frames)": manifest["stages"]["frames"]["keyframes"] == 2,
        "exact frame at 00:00:12.250": (work / "at" / "at_00h00m12s250.jpg").exists(),
        "audio detected": manifest["has_audio"] is True,
        "rerun exit 0 and frames untouched": second == 0
        and next((work / "frames").glob("*.jpg")).stat().st_mtime == frames_mtime,
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
    checks["build bundle writes JSON from extract outputs"] = run_check(
        "build bundle writes JSON from extract outputs", lambda: check_build_bundle(video))
    checks["downloaded video names are short and safe"] = run_check(
        "downloaded video names are short and safe", check_safe_video_stem)
    checks["bundle manifest resumes and conflicts safely"] = run_check(
        "bundle manifest resumes and conflicts safely", lambda: check_bundle_manifest_resume_conflict(video))
    try:
        checks["ffmpeg resolution precedence"] = run_check("ffmpeg resolution precedence", check_tool_resolution)
        checks["yt-dlp resolved as module when CLI absent"] = run_check("yt-dlp resolved as module when CLI absent", check_ytdlp_module_resolution)
        checks["JS runtime prefers deno then node"] = run_check("JS runtime prefers deno then node", check_js_runtime)
        checks["setup installs only missing dependencies"] = run_check("setup installs only missing dependencies", check_setup_plan)
        checks["OCR readiness reported by setup"] = run_check("OCR readiness reported by setup", check_setup_ocr_readiness)
        checks["setup installs then tests and check only tests"] = run_check(
            "setup installs then tests and check only tests", check_setup_entry_modes)
        checks["analyze_video local pipeline"] = run_check(
            "analyze_video local pipeline", check_analyze_video_local_pipeline)
        checks["analyze_video URL pipeline"] = run_check(
            "analyze_video URL pipeline", check_analyze_video_url_pipeline)
        checks["analyze_video stops on failure"] = run_check(
            "analyze_video stops on failure", check_analyze_video_stops_on_failure)
        checks["transient key frames are not OCR'd"] = run_check(
            "transient key frames are not OCR'd", check_transient_key_frames_are_not_ocrd)
        checks["metadata normalization fixture"] = run_check("metadata normalization fixture", check_metadata_fixture)
        checks["temporary folders live in the app _temp folder"] = run_check(
            "temporary folders live in the app _temp folder",
            lambda: check_app_temp_folder() and check_temp_calls_are_centralized())
        checks["fetch manifest accepted by extract"] = run_check("fetch manifest accepted by extract", check_fetch_manifest)
        checks["subtitle failure does not fail the fetch"] = run_check(
            "subtitle failure does not fail the fetch", check_subtitle_failure_is_best_effort)
        checks["v0.1 manifest remains compatible"] = run_check("v0.1 manifest remains compatible", check_legacy_manifest)
        checks["playlist and live are refused before download"] = run_check("playlist and live are refused before download", check_playlist_live_refusal)
        checks["default selftest is offline"] = run_check("default selftest is offline", check_default_offline)
        checks["documentation contract current"] = run_check("documentation contract current", check_documentation_contract)
        checks["every script help describes itself"] = run_check("every script help describes itself", check_script_help_contract)
        checks["offset container PTS preserved"] = run_check("offset container PTS preserved", lambda: check_offset_pts(folder))
        checks["adaptive sampling and timestamp names"] = run_check(
            "adaptive sampling and timestamp names", lambda: check_adaptive_sampling_and_timestamp_names(folder))
        checks["fixed interval opt out"] = run_check(
            "fixed interval opt out", lambda: check_fixed_interval_opt_out(folder))
        checks["chapter source precedence and bounds"] = run_check("chapter source precedence and bounds", check_bundle_chapters)
        checks["subtitle priority and rolling VTT dedupe"] = run_check("subtitle priority and rolling VTT dedupe", check_bundle_subtitle_priority)
        checks["srt and vtt normalize to segments"] = run_check("srt and vtt normalize to segments", check_bundle_subtitle_parsers)
        checks["frames align by exact PTS"] = run_check("frames align by exact PTS", check_bundle_frame_pts)
        checks["sponsor spans remain flagged and summary text excludes them"] = run_check(
            "sponsor spans remain flagged and summary text excludes them", check_bundle_sponsors)
        checks["OCR runs on key frames only"] = run_check("OCR runs on key frames only", check_ocr_key_frames)
        checks["OCR adapter is lazy"] = run_check("OCR adapter is lazy", check_ocr_is_lazy)
        checks["OCR uses bounded ONNX threads and reports progress"] = run_check(
            "OCR uses bounded ONNX threads and reports progress", check_ocr_thread_tuning_and_progress)
        checks["OCR runtime thread count does not invalidate bundle resume"] = run_check(
            "OCR runtime thread count does not invalidate bundle resume", check_ocr_runtime_setting_not_in_resume_key)
        checks["fetch progress is bounded by output mode"] = run_check(
            "fetch progress is bounded by output mode", check_fetch_progress_policy)
        checks["bundle works without fetch outputs"] = run_check("bundle works without fetch outputs", check_bundle_without_fetch)
        checks["bundle with fake fetch outputs prefers manual subtitles"] = run_check(
            "bundle with fake fetch outputs prefers manual subtitles", check_bundle_manual_subtitle)
        checks["extract preserves bundle manifest params"] = run_check(
            "extract preserves bundle manifest params", check_extract_preserves_bundle_params)
        real_ocr_ok, real_ocr_status = check_real_ocr_smoke()
        checks["real OCR smoke"] = real_ocr_ok
        CHECK_OUTPUTS["real OCR smoke"] = real_ocr_status
        if args.network:
            checks["network fetch and extract"] = run_check(
                "network fetch and extract", lambda: check_network_fetch(args.network, folder / "network"))
    except Exception as error:  # report failures as named checks, not a traceback
        print(f"FAIL  new checks raised {type(error).__name__}: {error}")
        checks["new checks completed"] = False
    for name, passed in checks.items():
        prefix = CHECK_OUTPUTS.get(name) if name == "real OCR smoke" else None
        print(f"{prefix or ('PASS' if passed else 'FAIL')}  {name}")
        if not passed and CHECK_OUTPUTS.get(name):
            for line in CHECK_OUTPUTS[name].rstrip().splitlines():
                print(f"    {line}")
    print(f"test folder (send to the Recycle Bin when done; _temp holds all test runs): {folder}")
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
