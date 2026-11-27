"""Check the video-analysis tools and extraction flow; run with -h for details."""
from __future__ import annotations

import argparse
import ast
import contextlib
import io
import json
import hashlib
import os
import re
import subprocess
import sys
import tempfile
import wave
import zipfile
from pathlib import Path
from typing import Callable
from dataclasses import replace
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
    comment_options = option_sets[3].get("extractor_args", {}).get("youtube", {})
    return (calls == [False, True, True, False] and video.is_file() and (folder / "metadata.json").is_file()
            and manifest["source"].get("kind") == "url"
            and manifest["params"]["fetch"]["url"].startswith("https://")
            and option_sets[1]["outtmpl"] == str(dest.resolve() / f"{fetch_video.safe_video_stem(info['title'], info['id'])}.%(ext)s")
            and "fetch" in accepted["stages"] and has_sponsorblock and has_progress
            and option_sets[3].get("getcomments") is True
            and comment_options == {"comment_sort": ["top"], "max_comments": ["100", "100", "0", "0", "1"]})


def check_sheet_facts() -> bool:
    import sheet_facts
    from tools import make_temp_folder
    video = make_temp_folder("sheet-facts-") / "video.mp4"
    video.write_bytes(b"video-bytes")
    raw = {"title": "raw", "channel": "Channel", "channel_url": "https://example.invalid/c",
           "upload_date": "20240102", "duration": 12, "width": 640, "height": 360,
           "view_count": 4, "like_count": -1, "comment_count": "8", "timestamp": 1,
           "subtitles": {"fr": [{}], "": [{}]}, "automatic_captions": {"en": [{}]},
           "description": "See https://example.invalid/a). https://example.invalid/a) javascript:bad"}
    facts = sheet_facts.build_sheet_facts(video, metadata={"title": "normalized"}, raw_info=raw,
        manifest={}, bundle={"source": {"duration": 9.5}, "speech": {"language": "fr"}})
    expected = {"title": "normalized", "channel_name": "Channel", "channel_url": "https://example.invalid/c",
                "youtube_publication_date": "2024-01-02",
                "duration_s": 9.5, "file_size_mb": len(b"video-bytes") / 1_048_576, "resolution": "640x360",
                "view_count": 4, "manual_subtitle_languages": ["fr"], "automatic_caption_language_count": 1,
                "audio_language": "fr", "description_links": ["https://example.invalid/a"]}
    fallback = sheet_facts.build_sheet_facts(video, metadata={"upload_date": "bad", "duration": 0},
        raw_info={"timestamp": 0, "view_count": 1.5, "channel_url": "javascript:bad",
                  "uploader_url": "https://example.invalid/u", "description": "https://example.invalid/d"},
        manifest={"duration_s": 4}, bundle={})
    return (facts == expected and list(facts) == list(sheet_facts.FACT_KEYS[:1]) + [k for k in sheet_facts.FACT_KEYS[1:] if k in facts]
            and fallback == {"channel_url": "https://example.invalid/u", "youtube_publication_date": "1970-01-01",
                             "duration_s": 4, "file_size_mb": len(b"video-bytes") / 1_048_576,
                             "description_links": ["https://example.invalid/d"]}
            and sheet_facts.canonical_sha256(facts) == sheet_facts.canonical_sha256(dict(reversed(list(facts.items())))))


def check_sheet_accounting() -> bool:
    import sheet_cost
    sheet = {"chapters": [{"blocks": [{"frame": {"file": "a.jpg"}}, {"frame": None}],
                           "callouts": [{"sources": ["1", "2"]}]}]}
    manifest = {"stages": {"correct_transcript": {"status": "done", "usage": {"total_tokens": 30},
                                                    "reported_cost_usd": 0.25}}}
    result = sheet_cost.build_sheet_accounting(manifest=manifest, provider="claude-code", model="sonnet",
        brief_text="abcd", bundle_text="efgh", sheet_text="12345678", sheet=sheet)
    return (result["tokens"] == {"measured": 30, "estimated_input": 1602, "estimated_output": 2,
                                 "estimated": 1604, "total": 1634}
            and result["usd"]["measured"] == 0.25 and result["usd"]["estimated"] > 0
            and result["usd"]["total"] == result["usd"]["estimated"] + 0.25
            and "2026-11-03" in result["basis"]["estimated"])


def check_sheet_accounting_incomplete() -> bool:
    import sheet_cost
    base = {"stages": {"correct_transcript": {"status": "done", "usage": {"input_tokens": 2,
        "output_tokens": 3}, "reported_cost_usd": None}}}
    sheet = {"chapters": []}
    missing = sheet_cost.build_sheet_accounting(manifest=base, provider=None, model="unknown",
        brief_text="", bundle_text="", sheet_text="", sheet=sheet)
    skipped = sheet_cost.build_sheet_accounting(manifest={"stages": {"correct_transcript": {"status": "skipped"}}},
        provider=None, model="claude-haiku-5-5", brief_text="", bundle_text="", sheet_text="", sheet=sheet)
    return missing["tokens"]["measured"] == 5 and missing["usd"]["measured"] == "n/a" \
        and missing["usd"]["total"] == "n/a" and skipped["usd"]["measured"] == 0.0


def check_comments_fetch() -> bool:
    import fetch_video
    from tools import make_temp_folder
    root = make_temp_folder("comments-fetch-")
    dest = root / "downloads"
    info = {"_type": "video", "id": "comment-id", "title": "Comment fixture", "language": "fr",
            "webpage_url": "https://example.invalid/video", "duration": 12, "requested_downloads": [],
            "comments": [{"text": "sentinel", "author": "PRIVATE", "parent": "root"}]}
    video = dest / "comment_fixture_comment_id.mp4"
    calls = []
    options = []
    class FakeYDL:
        def __init__(self, opts): self.opts = opts; options.append(opts)
        def __enter__(self): return self
        def __exit__(self, *_): return None
        def extract_info(self, url, *, download=True):
            calls.append((self.opts, download))
            if download:
                video.parent.mkdir(parents=True, exist_ok=True); video.write_bytes(b"v")
                info["requested_downloads"] = [{"filepath": str(video)}]
            if self.opts.get("getcomments"):
                return {"comments": [{"text": "top", "parent": "root", "like_count": "2", "timestamp": 4,
                    "is_pinned": 1, "reply_count": 3, "author": "SECRET", "author_url": "SECRET_URL"},
                    {"text": "reply", "parent": "top", "author": "SECRET"}]}
            return info
        def sanitize_info(self, value): return value
        def prepare_filename(self, value): return str(video)
    code = fetch_video.main(["https://example.invalid/video", "--dest", str(dest), "--comments", "1"], ydl_factory=FakeYDL)
    folder = video.parent / f"{video.stem}.analysis"
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    raw = (folder / "raw_info.json").read_text(encoding="utf-8")
    comments = json.loads((folder / "comments.json").read_text(encoding="utf-8"))
    before = sum(bool(opts.get("getcomments")) for opts, _ in calls)
    rerun = fetch_video.main(["https://example.invalid/video", "--dest", str(dest), "--comments", "1"], ydl_factory=FakeYDL)
    after = sum(bool(opts.get("getcomments")) for opts, _ in calls)
    artifact = (folder / "comments.json").read_bytes()
    conflict = fetch_video.main(["https://example.invalid/video", "--dest", str(dest), "--comments", "2"],
                                ydl_factory=FakeYDL)
    preserved = (folder / "comments.json").read_bytes() == artifact
    legacy = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    legacy["params"]["extract"] = {"interval": 5}
    legacy["stages"].update({"extract": {"status": "done", "frames": 4},
                             "correct_transcript": {"status": "skipped"}, "bundle": {"status": "done"}})
    legacy["params"].pop("comments", None)
    legacy["stages"].pop("comments", None)
    old_params, old_stages = json.loads(json.dumps(legacy["params"])), json.loads(json.dumps(legacy["stages"]))
    (folder / "comments.json").unlink()
    (folder / "manifest.json").write_text(json.dumps(legacy), encoding="utf-8")
    old_downloads = sum(download for _, download in calls)
    old_comment_passes = after
    upgrade = fetch_video.main(["https://example.invalid/video", "--dest", str(dest), "--comments", "2"],
                               ydl_factory=FakeYDL)
    upgraded = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    return (code == rerun == upgrade == 0 and conflict == 3 and preserved
            and manifest["params"]["comments"] == {"limit": 1, "sort": "top"}
            and manifest["stages"]["comments"] == {"status": "done", "output": "comments.json", "count": 1}
            and comments == [{"text": "top", "like_count": 2, "timestamp": 4, "is_pinned": True, "reply_count": 3}]
            and "SECRET" not in raw and "comments" not in json.loads(raw) and after == before
            and upgraded["params"] == {**old_params, "comments": {"limit": 2, "sort": "top"}}
            and upgraded["stages"] == {**old_stages, "comments": {"status": "done", "output": "comments.json", "count": 1}}
            and sum(download for _, download in calls) == old_downloads
            and sum(bool(opts.get("getcomments")) for opts, _ in calls) == old_comment_passes + 1)


def check_comments_states() -> bool:
    import fetch_video
    from tools import make_temp_folder
    valid = fetch_video.main(["https://example.invalid/x", "--comments", "101"], ydl_factory=lambda _: None) == 2
    negative = fetch_video.main(["https://example.invalid/x", "--comments", "-1"], ydl_factory=lambda _: None) == 2
    root = make_temp_folder("comments-states-")
    video = root / "video.mp4"; video.write_bytes(b"v")
    class FailedYDL:
        def __init__(self, _): pass
        def __enter__(self): return self
        def __exit__(self, *_): return None
        def extract_info(self, *_args, **_kwargs): raise RuntimeError("offline failure")
    disabled, disabled_warning = fetch_video.run_comments_substage("url", video=video, analysis=root,
        limit=0, ydl_factory=FailedYDL)
    unavailable, warning = fetch_video.run_comments_substage("url", video=video, analysis=root,
        limit=1, ydl_factory=FailedYDL)
    return (valid and negative and disabled == {"status": "disabled", "count": 0}
            and disabled_warning is None and unavailable == {"status": "unavailable", "count": 0,
            "reason": "offline failure"} and warning == "offline failure"
            and fetch_video.sanitize_comments({"comments": []}, 0) == [])


def check_analyze_video_comments() -> bool:
    import analyze_video
    calls = []
    def fetch(argv, *, on_video=None):
        calls.append(argv); on_video(Path("downloaded.mp4")); return 0
    with (patch.object(analyze_video, "fetch_main", side_effect=fetch),
          patch.object(analyze_video, "extract_main", return_value=0),
          patch.object(analyze_video, "correct_main", return_value=0),
          patch.object(analyze_video, "bundle_main", return_value=0),
          contextlib.redirect_stderr(io.StringIO())):
        code = analyze_video.main(["https://example.invalid/talk", "--comments", "5"])
    with contextlib.redirect_stderr(io.StringIO()):
        local = analyze_video.main(["local.mp4", "--comments", "5"])
    return code == 0 and "--comments" in calls[0] and calls[0][calls[0].index("--comments") + 1] == "5" and local == 2


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
            and len(option_sets) == 4 and "writesubtitles" not in option_sets[1]
            and "writeautomaticsub" not in option_sets[1] and "ignoreerrors" not in option_sets[1]
            and option_sets[2].get("ignoreerrors") is True
            and option_sets[3].get("getcomments") is True
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


def check_script_help_contract() -> bool:
    scripts = ("extract_video.py", "fetch_video.py", "setup_tools.py", "selftest.py", "build_bundle.py",
               "analyze_video.py", "prepare_sheet.py", "render_sheet.py")
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


def _sheet_fixture(root: Path) -> tuple[Path, Path, dict, dict]:
    analysis = root / "sample.analysis"
    frames = analysis / "frames"
    frames.mkdir(parents=True)
    frame = frames / "frame.jpg"
    from PIL import Image
    Image.new("RGB", (12, 8), "red").save(frame)
    bundle = {"source": {"url": "https://youtu.be/abc", "duration": 5},
              "speech": {"language": "fr"}, "chapters": []}
    (analysis / "bundle.json").write_text(json.dumps(bundle), encoding="utf-8")
    digest = hashlib.sha256((analysis / "bundle.json").read_bytes()).hexdigest()
    writer = {"host": "claude-code", "model": "claude-sonnet-test", "role": "default"}
    labels = {key: key.replace("_", " ").title() for key in ("source", "introduction", "summary", "chapters_rationale", "glossary", "references", "figure", "cost", "tokens", "estimated", "measured", "series", "comments", "author_email", "recording_date", "correction", "complement", "question")}
    labels["facts"] = {key: key.replace("_", " ").title() for key in __import__("sheet_schema").FACT_KEYS}
    sheet = {"schema_version": 2, "source": {"bundle_sha256": digest, "title": "Sample",
             "url": "https://youtu.be/abc"}, "language": "fr", "title": "Lesson",
             "introduction": "Intro", "summary": ["Résumé"], "chapter_rationale": "Rationale", "context": {"recording_date": None, "series": None, "comments_summary": None, "author_email": None}, "labels": labels,
             "chapters": [{"title": "Chapter", "start": 1, "end": 5, "blocks": [{"text": "Text with useful details", "frame": {"file": "frames/frame.jpg", "time": 2}}], "callouts": []}], "references": [], "glossary": [], "writer": writer}
    manifest = {"schema_version": 2, "source": {"kind": "local"}, "params": {}, "stages": {}}
    (analysis / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return analysis, frame, sheet, writer


def _sheet_error(value: dict, analysis: Path, writer: dict, field: str, reason: str,
                 digest: str | None = None) -> bool:
    from sheet_schema import SheetValidationError, validate_sheet
    current_digest = digest or value["source"]["bundle_sha256"]
    bundle = json.loads((analysis / "bundle.json").read_text(encoding="utf-8"))
    expected_url = bundle.get("source", {}).get("url")
    try:
        validate_sheet(value, analysis_dir=analysis, expected_bundle_sha256=current_digest,
                       expected_writer=writer, expected_source_url=expected_url,
                       expected_duration_s=float(bundle.get("source", {}).get("duration", 5)))
    except SheetValidationError as error:
        return (error.field, error.reason) == (field, reason)
    return False


def _prepare_case(root: Path, *, locality: str = "remote", kind: str = "local",
                  language: str = "auto") -> tuple[Path, Path, object]:
    from host_models import ConfigReader
    from prepare_sheet import SheetPrepareDeps, SheetPrepareRequest
    root.mkdir(parents=True, exist_ok=True)
    video = root / "sample.mp4"
    video.write_bytes(b"video")
    analysis = root / "sample.analysis"
    analysis.mkdir()
    (analysis / "bundle.json").write_text(json.dumps({"speech": {"language": "fr"}}), encoding="utf-8")
    manifest = {"schema_version": 2, "source": {"path": str(video), "kind": kind},
                "params": {}, "stages": {}}
    (analysis / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    reader = ConfigReader(root, root, lambda _: None)
    deps = SheetPrepareDeps(environ={"ANTHROPIC_MODEL": "claude-sonnet-test",
        "VIDEO_ANALYSIS_AI_LOCALITY": locality}, config_reader=reader)
    request = SheetPrepareRequest(host="claude-code", language=language)
    return video, analysis, (request, deps)


def check_sheet_schema_valid() -> bool:
    from sheet_schema import format_hhmmss, validate_sheet
    with tempfile.TemporaryDirectory(dir=os.environ["VIDEO_ANALYSIS_TEMP_DIR"]) as temp:
        analysis, _, sheet, writer = _sheet_fixture(Path(temp))
        accepted = validate_sheet(sheet, analysis_dir=analysis,
            expected_bundle_sha256=sheet["source"]["bundle_sha256"], expected_writer=writer,
            expected_source_url="https://youtu.be/abc", expected_duration_s=5)
        return (accepted == sheet and set(accepted["labels"]) >= {
            "source", "introduction", "summary", "glossary", "figure", "facts"}
            and format_hhmmss(360000.9) == "100:00:00")


def check_sheet_schema_required_unknown_labels() -> bool:
    with tempfile.TemporaryDirectory(dir=os.environ["VIDEO_ANALYSIS_TEMP_DIR"]) as temp:
        analysis, _, valid, writer = _sheet_fixture(Path(temp))
        missing = dict(valid); del missing["title"]
        unknown = dict(valid); unknown["extra"] = True
        labels = dict(valid); del labels["labels"]
        wrong_labels = dict(valid); wrong_labels["labels"] = {**valid["labels"], "extra": "x"}
        nested_missing = dict(valid); nested_missing["chapters"] = [dict(valid["chapters"][0])]
        del nested_missing["chapters"][0]["title"]
        return (_sheet_error(missing, analysis, writer, "title", "is required")
            and _sheet_error(unknown, analysis, writer, "$", "must contain only schema fields")
            and _sheet_error(labels, analysis, writer, "labels", "is required")
            and _sheet_error(wrong_labels, analysis, writer, "labels", "must contain only schema fields")
            and _sheet_error(nested_missing, analysis, writer, "chapters[0].title", "is required"))


def check_sheet_schema_types_text() -> bool:
    with tempfile.TemporaryDirectory(dir=os.environ["VIDEO_ANALYSIS_TEMP_DIR"]) as temp:
        analysis, _, valid, writer = _sheet_fixture(Path(temp))
        wrong = json.loads(json.dumps(valid)); wrong["schema_version"] = True
        empty = json.loads(json.dumps(valid)); empty["chapters"][0]["blocks"][0]["text"] = "  "
        labels = json.loads(json.dumps(valid)); labels["labels"]["summary"] = ""
        return (_sheet_error(wrong, analysis, writer, "schema_version", "must be schema version 2")
            and _sheet_error(empty, analysis, writer, "chapters[0].blocks[0].text", "must be a non-empty string")
            and _sheet_error(labels, analysis, writer, "labels.summary", "must be a non-empty string"))


def check_sheet_schema_intervals() -> bool:
    with tempfile.TemporaryDirectory(dir=os.environ["VIDEO_ANALYSIS_TEMP_DIR"]) as temp:
        analysis, _, valid, writer = _sheet_fixture(Path(temp))
        wrong = json.loads(json.dumps(valid)); wrong["chapters"][0]["start"] = 5
        return _sheet_error(wrong, analysis, writer, "chapters[0]", "must satisfy 0 <= start < end")


def check_sheet_schema_ordering() -> bool:
    with tempfile.TemporaryDirectory(dir=os.environ["VIDEO_ANALYSIS_TEMP_DIR"]) as temp:
        analysis, _, valid, writer = _sheet_fixture(Path(temp))
        wrong = json.loads(json.dumps(valid)); wrong["chapters"].append({**wrong["chapters"][0], "start": 4, "end": 6})
        return _sheet_error(wrong, analysis, writer, "chapters[1]", "must be ordered and non-overlapping")


def check_sheet_schema_frame_time() -> bool:
    with tempfile.TemporaryDirectory(dir=os.environ["VIDEO_ANALYSIS_TEMP_DIR"]) as temp:
        analysis, _, valid, writer = _sheet_fixture(Path(temp))
        wrong = json.loads(json.dumps(valid)); wrong["chapters"][0]["blocks"][0]["frame"]["time"] = 6
        return _sheet_error(wrong, analysis, writer, "chapters[0].blocks[0].frame.time", "must be within chapter interval")


def check_sheet_schema_frame_paths() -> bool:
    with tempfile.TemporaryDirectory(dir=os.environ["VIDEO_ANALYSIS_TEMP_DIR"]) as temp:
        analysis, _, valid, writer = _sheet_fixture(Path(temp))
        paths = ("/tmp/frame.jpg", "C:/frame.jpg", "../frame.jpg", "frames\\frame.jpg", "frames/./frame.jpg")
        for path in paths:
            wrong = json.loads(json.dumps(valid)); wrong["chapters"][0]["blocks"][0]["frame"]["file"] = path
            if not _sheet_error(wrong, analysis, writer, "chapters[0].blocks[0].frame.file",
                                "must be a normalized relative path inside the analysis folder"):
                return False
        wrong = json.loads(json.dumps(valid)); wrong["chapters"][0]["blocks"][0]["frame"]["file"] = "frames/frame.gif"
        if not _sheet_error(wrong, analysis, writer, "chapters[0].blocks[0].frame.file",
                            "must be a JPEG or PNG image"):
            return False
        (analysis / "frames" / "not-image.png").write_bytes(b"not an image")
        wrong = json.loads(json.dumps(valid)); wrong["chapters"][0]["blocks"][0]["frame"]["file"] = "frames/not-image.png"
        return _sheet_error(wrong, analysis, writer, "chapters[0].blocks[0].frame.file",
                            "must be a JPEG or PNG image")


def check_sheet_schema_missing_frame() -> bool:
    with tempfile.TemporaryDirectory(dir=os.environ["VIDEO_ANALYSIS_TEMP_DIR"]) as temp:
        analysis, _, valid, writer = _sheet_fixture(Path(temp))
        wrong = json.loads(json.dumps(valid)); wrong["chapters"][0]["blocks"][0]["frame"]["file"] = "frames/missing.jpg"
        return _sheet_error(wrong, analysis, writer, "chapters[0].blocks[0].frame.file", "file does not exist")


def check_sheet_schema_digest() -> bool:
    with tempfile.TemporaryDirectory(dir=os.environ["VIDEO_ANALYSIS_TEMP_DIR"]) as temp:
        analysis, _, valid, writer = _sheet_fixture(Path(temp))
        return _sheet_error(valid, analysis, writer, "source.bundle_sha256",
                            "does not match current bundle.json", "0" * 64)


def check_sheet_schema_writer() -> bool:
    with tempfile.TemporaryDirectory(dir=os.environ["VIDEO_ANALYSIS_TEMP_DIR"]) as temp:
        analysis, _, valid, writer = _sheet_fixture(Path(temp))
        return _sheet_error(valid, analysis, {**writer, "model": "other"}, "writer",
                            "does not match sheet preparation")


def check_sheet_v2_duration_summary() -> bool:
    from sheet_schema import SheetValidationError, validate_sheet
    with tempfile.TemporaryDirectory(dir=os.environ["VIDEO_ANALYSIS_TEMP_DIR"]) as temp:
        analysis, _, sheet, writer = _sheet_fixture(Path(temp))
        try:
            validate_sheet(sheet, analysis_dir=analysis, expected_bundle_sha256=sheet["source"]["bundle_sha256"],
                           expected_writer=writer, expected_source_url="https://youtu.be/abc", expected_duration_s=1200)
        except SheetValidationError as error:
            return error.field == "summary" and "2 paragraphs" in error.reason
        return False


def check_sheet_v2_blocks_references() -> bool:
    from sheet_schema import SheetValidationError, validate_sheet
    with tempfile.TemporaryDirectory(dir=os.environ["VIDEO_ANALYSIS_TEMP_DIR"]) as temp:
        analysis, _, sheet, writer = _sheet_fixture(Path(temp))
        sheet["references"] = [{"id": 1, "url": "https://example.org/ref", "title": "Reference", "origin": "source"}]
        sheet["chapters"][0]["callouts"] = [{"kind": "correction", "text": "A correction", "sources": [1]}]
        valid = validate_sheet(sheet, analysis_dir=analysis, expected_bundle_sha256=sheet["source"]["bundle_sha256"], expected_writer=writer, expected_source_url="https://youtu.be/abc", expected_duration_s=5)
        sheet["chapters"][0]["callouts"][0]["sources"] = [2]
        try:
            validate_sheet(sheet, analysis_dir=analysis, expected_bundle_sha256=sheet["source"]["bundle_sha256"], expected_writer=writer, expected_source_url="https://youtu.be/abc", expected_duration_s=5)
        except SheetValidationError as error:
            return valid["references"][0]["id"] == 1 and error.field.endswith("sources")
        return False


def check_sheet_v2_context_budget() -> bool:
    from sheet_schema import SheetValidationError, validate_sheet
    with tempfile.TemporaryDirectory(dir=os.environ["VIDEO_ANALYSIS_TEMP_DIR"]) as temp:
        analysis, _, sheet, writer = _sheet_fixture(Path(temp))
        sheet["context"]["author_email"] = {"value": "author@example.org", "evidence": "Published slide"}
        sheet["context"]["series"] = {"theme": "Series", "first_video_url": "javascript:alert(1)"}
        try:
            validate_sheet(sheet, analysis_dir=analysis, expected_bundle_sha256=sheet["source"]["bundle_sha256"], expected_writer=writer, expected_source_url="https://youtu.be/abc", expected_duration_s=5)
        except SheetValidationError as error:
            return error.field == "context.series.first_video_url"
        return False


def check_renderer_v1_refusal() -> bool:
    from render_sheet import run_render
    with tempfile.TemporaryDirectory(dir=os.environ["VIDEO_ANALYSIS_TEMP_DIR"]) as temp:
        video, analysis, sheet = _render_fixture(Path(temp))
        sheet["schema_version"] = 1
        (analysis / "sheet.json").write_text(json.dumps(sheet), encoding="utf-8")
        error = io.StringIO()
        with contextlib.redirect_stderr(error):
            result = run_render(video)
        return result == 3 and error.getvalue().strip() == "regenerate sheet.json with the v0.6.0 brief" and not (analysis / "sample.docx").exists()


def check_render_v2_identity() -> bool:
    from render_sheet import run_render
    with tempfile.TemporaryDirectory(dir=os.environ["VIDEO_ANALYSIS_TEMP_DIR"]) as temp:
        video, analysis, _ = _render_fixture(Path(temp))
        if run_render(video) != 0: return False
        manifest = json.loads((analysis / "manifest.json").read_text(encoding="utf-8"))
        params = manifest["params"]["sheet_render"]
        return all(key in params for key in ("schema_version", "facts_sha256", "accounting_sha256", "brief_sha256")) and params["schema_version"] == 2


def check_docx_v2_layout() -> bool:
    from render_sheet import render_document
    with tempfile.TemporaryDirectory(dir=os.environ["VIDEO_ANALYSIS_TEMP_DIR"]) as temp:
        _, analysis, sheet = _render_fixture(Path(temp))
        sheet["references"] = [{"id": 1, "url": "https://example.org/ref", "title": "Ref", "origin": "source"}]
        sheet["chapters"][0]["callouts"] = [{"kind": "complement", "text": "Extra detail", "sources": [1]}]
        doc = render_document(sheet, analysis_dir=analysis)
        xml = doc._element.xml
        targets = [rel.target_ref for rel in doc.part.rels.values() if rel.reltype.endswith("/hyperlink")]
        return ("E3F0FB" in xml and 'w:val="single"' in xml and "w:insideV" in xml and 'w:insideH w:val="nil"' in xml
                and "https://example.org/ref" in targets and "ℹ" in xml
                and len(doc.inline_shapes) == 1 and doc.inline_shapes[0].width < 6 * 914400 * .38)


def check_prepare_v2_identity() -> bool:
    from prepare_sheet import run_prepare
    with tempfile.TemporaryDirectory(dir=os.environ["VIDEO_ANALYSIS_TEMP_DIR"]) as temp:
        video, analysis, (request, deps) = _prepare_case(Path(temp), locality="local")
        if run_prepare(video, request, deps) != 0: return False
        manifest = json.loads((analysis / "manifest.json").read_text(encoding="utf-8"))
        params, stage = manifest["params"]["sheet_prepare"], manifest["stages"]["sheet_prepare"]
        return (params.get("brief_version") == 2 and params.get("sheet_schema_version") == 2
                and len(params.get("facts_sha256", "")) == 64 and params.get("comments_sha256") is None
                and len(stage.get("brief_sha256", "")) == 64)


def check_sheet_model_facts_rejected() -> bool:
    with tempfile.TemporaryDirectory(dir=os.environ["VIDEO_ANALYSIS_TEMP_DIR"]) as temp:
        analysis, _, sheet, writer = _sheet_fixture(Path(temp))
        sheet["facts"] = {"view_count": 999}
        sheet["accounting"] = {"usd": {"total": 0}}
        return _sheet_error(sheet, analysis, writer, "$", "must contain only schema fields")


def check_sheet_seek_links() -> bool:
    from sheet_schema import build_seek_link
    return (build_seek_link("https://www.youtube.com/watch?v=x&list=y&t=2s", 9.8)
            == "https://www.youtube.com/watch?v=x&list=y&t=9s"
            and build_seek_link("https://youtu.be/x?feature=y", 10) == "https://youtu.be/x?feature=y&t=10s"
            and build_seek_link("https://vimeo.com/123?share=1#old", 11) == "https://vimeo.com/123?share=1#t=11s"
            and build_seek_link("https://example.com/video", 12) is None
            and build_seek_link("https://vimeo.com/", 12) is None
            and all(build_seek_link(url, 12) is None for url in (
                "file:///etc/passwd", r"\\host\share", "javascript:alert(1)",
                "http://user:pw@host/video", "https://host/video\n")))


def check_sheet_prepare_default_provenance() -> bool:
    from prepare_sheet import run_prepare
    with tempfile.TemporaryDirectory(dir=os.environ["VIDEO_ANALYSIS_TEMP_DIR"]) as temp:
        video, analysis, (request, deps) = _prepare_case(Path(temp))
        request = replace(request, allow_remote_ai=True)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = run_prepare(video, request, deps)
        manifest = json.loads((analysis / "manifest.json").read_text(encoding="utf-8"))
        params = manifest["params"]["sheet_prepare"]
        brief = (analysis / "sheet.brief.md").read_text(encoding="utf-8")
        return (result == 0 and params["model"] == "claude-sonnet-test"
            and params["language"] == "fr" and "role=default" in output.getvalue()
            and "host default" not in params["model"] and "claude-haiku" not in brief)


def check_sheet_prepare_consent_matrix() -> bool:
    from prepare_sheet import run_prepare
    with tempfile.TemporaryDirectory(dir=os.environ["VIDEO_ANALYSIS_TEMP_DIR"]) as temp:
        root = Path(temp)
        video, analysis, (request, deps) = _prepare_case(root / "denied")
        before = {p.name: p.read_bytes() for p in analysis.iterdir()}
        denied = run_prepare(video, request, deps)
        after = {p.name: p.read_bytes() for p in analysis.iterdir()}
        denied_manifest = json.loads((analysis / "manifest.json").read_text(encoding="utf-8"))
        only_manifest = set(before) == set(after) and all(
            before[name] == after[name] for name in before if name != "manifest.json")
        denied_ok = (denied == 4 and only_manifest
            and denied_manifest["stages"]["sheet_prepare"] == {"status": "denied",
                "consent": {"required": True, "granted": False}})
        approved_video, approved_dir, (approved_request, approved_deps) = _prepare_case(root / "approved")
        approved = run_prepare(approved_video, replace(approved_request, allow_remote_ai=True), approved_deps)
        local_video, _, (local_request, local_deps) = _prepare_case(root / "local", locality="local")
        local_ok = run_prepare(local_video, local_request, local_deps) == 0
        url_video, url_dir, (url_request, url_deps) = _prepare_case(root / "url", kind="url")
        url_ok = run_prepare(url_video, url_request, url_deps) == 0
        approved_manifest = json.loads((approved_dir / "manifest.json").read_text(encoding="utf-8"))
        consent_ok = approved_manifest["stages"]["sheet_prepare"]["consent"].get("method") == "flag"
        return denied_ok and approved == 0 and consent_ok and local_ok and url_ok


def check_sheet_prepare_brief_contract() -> bool:
    from prepare_sheet import run_prepare
    from sheet_schema import SHEET_JSON_SCHEMA_V2
    with tempfile.TemporaryDirectory(dir=os.environ["VIDEO_ANALYSIS_TEMP_DIR"]) as temp:
        video, analysis, (request, deps) = _prepare_case(Path(temp), locality="local")
        if run_prepare(video, request, deps) != 0:
            return False
        brief = (analysis / "sheet.brief.md").read_text(encoding="utf-8")
        manifest = json.loads((analysis / "manifest.json").read_text(encoding="utf-8"))
        params = manifest["params"]["sheet_prepare"]
        return (json.dumps(SHEET_JSON_SCHEMA_V2, ensure_ascii=False, indent=2) in brief
            and str((analysis / "bundle.json").resolve()) in brief
            and str((analysis / "sheet.json").resolve()) in brief
            and "Copy `bundle.json` `source.url` exactly" in brief
            and params["host"] in brief and params["model"] in brief
            and "low-cost" in brief and "do not call a provider" in brief.lower()
            and "never betray the author's message" in brief and "Never debunk religion" in brief
            and "official institutions" in brief and "comments.json only when it exists" in brief
            and '"title"' in brief and "Script-owned facts" in brief
            and manifest["stages"]["sheet_prepare"]["consent"] == {
                "required": False, "granted": True, "method": "not-required"})


def check_sheet_prepare_resume_conflicts() -> bool:
    from prepare_sheet import run_prepare
    with tempfile.TemporaryDirectory(dir=os.environ["VIDEO_ANALYSIS_TEMP_DIR"]) as temp:
        video, analysis, (request, deps) = _prepare_case(Path(temp), locality="local")
        if run_prepare(video, request, deps) != 0:
            return False
        brief = analysis / "sheet.brief.md"
        before = brief.read_bytes()
        resumed = run_prepare(video, request, deps) == 0 and brief.read_bytes() == before
        brief.unlink()
        recreated = run_prepare(video, request, deps) == 0 and brief.exists()
        (analysis / "manifest.json").write_text(json.dumps({"schema_version": 2, "source": {"kind": "local"},
            "params": {}, "stages": {}}))
        brief_conflict = run_prepare(video, request, deps) == 3 and brief.exists()
        return resumed and recreated and brief_conflict


def _render_fixture(root: Path, *, prepared: bool = True) -> tuple[Path, Path, dict]:
    from PIL import Image
    root.mkdir(parents=True, exist_ok=True)
    video = root / "sample.mp4"; video.write_bytes(b"video")
    analysis = root / "sample.analysis"; analysis.mkdir()
    (analysis / "frames").mkdir()
    Image.new("RGB", (12, 8), "red").save(analysis / "frames" / "frame.png")
    bundle_path = analysis / "bundle.json"
    bundle_path.write_text(json.dumps({"source": {"url": "https://youtu.be/abc", "duration": 5},
                                       "speech": {"language": "fr"}}), encoding="utf-8")
    digest = hashlib.sha256(bundle_path.read_bytes()).hexdigest()
    writer = {"host": "claude-code", "model": "claude-sonnet-test", "role": "default"}
    labels = {key: key for key in ("source", "introduction", "summary", "chapters_rationale", "glossary", "references", "figure", "cost", "tokens", "estimated", "measured", "series", "comments", "author_email", "recording_date", "correction", "complement", "question")}
    labels.update({"source": "Origine", "introduction": "Présentation", "summary": "Résumé", "glossary": "Lexique", "figure": "Image à"})
    labels["facts"] = {key: key for key in __import__("sheet_schema").FACT_KEYS}
    sheet = {"schema_version": 2, "source": {"bundle_sha256": digest, "title": "Sample",
        "url": "https://youtu.be/abc"}, "language": "fr", "title": "Lesson",
        "introduction": "Intro text", "summary": ["Summary text"], "chapter_rationale": "Rationale", "context": {"recording_date": None, "series": None, "comments_summary": None, "author_email": None}, "labels": labels, "references": [], "chapters": [{"title": "Chapter",
        "start": 1, "end": 5, "blocks": [{"text": "Chapter text with enough details", "frame": {"file": "frames/frame.png", "time": 2}}], "callouts": []}], "glossary": [
        {"term": "Term", "definition": "Meaning"}], "writer": writer}
    (analysis / "sheet.json").write_text(json.dumps(sheet, ensure_ascii=False), encoding="utf-8")
    (analysis / "sheet.brief.md").write_text("test brief", encoding="utf-8")
    manifest = {"schema_version": 2, "source": {"kind": "local"}, "params": {}, "stages": {}}
    if prepared:
        from sheet_facts import build_sheet_facts, canonical_sha256
        facts = build_sheet_facts(video, metadata={}, raw_info={}, manifest=manifest, bundle=json.loads(bundle_path.read_text()))
        manifest["params"]["sheet_prepare"] = {"brief_version": 2, "sheet_schema_version": 2, "facts_sha256": canonical_sha256(facts), "comments_sha256": None, "bundle_sha256": digest, "host": writer["host"],
            "model": writer["model"], "provider": "anthropic", "locality": "local", "language": "fr"}
        manifest["stages"]["sheet_prepare"] = {"status": "ok", "brief_sha256": hashlib.sha256(b"test brief").hexdigest(), "consent": {"required": False,
            "granted": True, "method": "not-required"}}
    (analysis / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return video, analysis, sheet


def _docx_template(path: Path, *, custom: bool = False) -> None:
    from docx import Document
    document = Document()
    document.sections[0].left_margin = 914400
    document.sections[0].right_margin = 914400
    document.sections[0].header.paragraphs[0].text = "Template header"
    document.sections[0].footer.paragraphs[0].text = "Template footer"
    if custom:
        document.styles.add_style("Caption", 1)
        document.styles["Title"].font.bold = True
    document.save(path)


def _as_dotx(source: Path, target: Path) -> None:
    with zipfile.ZipFile(source) as incoming, zipfile.ZipFile(target, "w") as outgoing:
        for item in incoming.infolist():
            content = incoming.read(item.filename)
            if item.filename == "[Content_Types].xml":
                content = content.replace(
                    b"application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml",
                    b"application/vnd.openxmlformats-officedocument.wordprocessingml.template.main+xml")
            outgoing.writestr(item, content)


def check_default_docx_render() -> bool:
    from render_sheet import FIXED_CORE_TIME, render_document
    from docx import Document
    from docx.oxml.ns import qn
    with tempfile.TemporaryDirectory(dir=os.environ["VIDEO_ANALYSIS_TEMP_DIR"]) as temp:
        _, analysis, sheet = _render_fixture(Path(temp))
        doc = render_document(sheet, analysis_dir=analysis)
        paragraphs = [(p.style.name, p.text) for p in doc.paragraphs]
        text = [value for _, value in paragraphs]
        hyperlinks = doc.part.element.xpath(".//w:hyperlink")
        has_hyperlink_style = "Hyperlink" in [style.name for style in doc.styles]
        visible_hyperlinks = bool(hyperlinks)
        if not has_hyperlink_style:
            for link in hyperlinks:
                for run in link.xpath(".//w:r"):
                    props = run.find(qn("w:rPr"))
                    color = props.find(qn("w:color")) if props is not None else None
                    underline = props.find(qn("w:u")) if props is not None else None
                    if (color is None or color.get(qn("w:val")) != "0563C1"
                            or underline is None or underline.get(qn("w:val")) != "single"):
                        visible_hyperlinks = False
        localized = render_document(sheet, analysis_dir=analysis)
        localized_style = localized.styles.add_style("Hyperlink", 2)
        localized_style._element.set(qn("w:styleId"), "Lienhypertexte")
        localized_link = localized.paragraphs[1]._p
        from render_sheet import _hyperlink
        _hyperlink(localized.paragraphs[1], "localized", "https://example.com")
        style_values = [node.get(qn("w:val")) for node in localized_link.xpath(".//w:rStyle")]
        table_text = " ".join(cell.text for table in doc.tables for row in table.rows for cell in row.cells)
        return (visible_hyperlinks and not has_hyperlink_style and style_values == ["Lienhypertexte"]
            and text[0] == "Lesson" and "Sample" in table_text and "Présentation" in text
            and "Résumé" in text and "Lexique" in text and "Chapter text" in table_text and "Image à" in table_text
            and len(doc.inline_shapes) == 1 and doc.core_properties.title == "Lesson"
            and doc.core_properties.author == "video-analysis"
            and doc.core_properties.created == FIXED_CORE_TIME
            and doc.core_properties.modified == FIXED_CORE_TIME)


def check_sheet_source_url_security() -> bool:
    from render_sheet import render_document, run_render
    from sheet_schema import SheetValidationError, validate_sheet
    with tempfile.TemporaryDirectory(dir=os.environ["VIDEO_ANALYSIS_TEMP_DIR"]) as temp:
        _, analysis, sheet = _render_fixture(Path(temp))
        supported = render_document(sheet, analysis_dir=analysis)
        supported_targets = [rel.target_ref for rel in supported.part.rels.values() if rel.is_external]
        if supported_targets != ["https://youtu.be/abc", "https://youtu.be/abc?t=1s",
                                 "https://youtu.be/abc?t=2s"]:
            return False
        expected_writer = sheet["writer"]
        digest = sheet["source"]["bundle_sha256"]
        mismatching = dict(sheet)
        mismatching["source"] = {**sheet["source"], "url": "https://example.invalid/video"}
        try:
            validate_sheet(mismatching, analysis_dir=analysis, expected_bundle_sha256=digest,
                expected_writer=expected_writer, expected_source_url="https://youtu.be/abc", expected_duration_s=5)
        except SheetValidationError as error:
            if (error.field, error.reason) != ("source.url", "does not match current bundle.json"):
                return False
        else:
            return False
        for unsafe in ("file:///etc/passwd", r"\\host\share", "javascript:alert(1)",
                       "http://user:pw@host/video"):
            unsafe_sheet = {**sheet, "source": {**sheet["source"], "url": unsafe}}
            document = render_document(unsafe_sheet, analysis_dir=analysis)
            targets = [rel.target_ref for rel in document.part.rels.values() if rel.is_external]
            source_line = document.paragraphs[1]
            if targets or source_line._p.xpath(".//w:hyperlink"):
                return False
        video, _, _ = _render_fixture(Path(temp) / "mismatch")
        mismatch_analysis = video.parent / "sample.analysis"
        mismatch_sheet = json.loads((mismatch_analysis / "sheet.json").read_text(encoding="utf-8"))
        mismatch_sheet["source"]["url"] = "https://example.invalid/video"
        (mismatch_analysis / "sheet.json").write_text(json.dumps(mismatch_sheet), encoding="utf-8")
        if run_render(video) != 3 or (mismatch_analysis / "sample.docx").exists():
            return False
        from unittest.mock import patch
        from docx.image.exceptions import UnrecognizedImageError
        image_video, _, _ = _render_fixture(Path(temp) / "image-exception")
        with patch("render_sheet.render_document", side_effect=UnrecognizedImageError("bad image")):
            if run_render(image_video) != 3:
                return False
        return True


def check_docx_template_preservation() -> bool:
    from render_sheet import render_document
    with tempfile.TemporaryDirectory(dir=os.environ["VIDEO_ANALYSIS_TEMP_DIR"]) as temp:
        root = Path(temp); _, analysis, sheet = _render_fixture(root)
        template = root / "styled.docx"; _docx_template(template)
        doc = render_document(sheet, analysis_dir=analysis, template=template)
        section = doc.sections[0]
        return (section.left_margin == 914400 and section.right_margin == 914400
            and section.header.paragraphs[0].text == "Template header"
            and section.footer.paragraphs[0].text == "Template footer")


def check_dotx_template() -> bool:
    from render_sheet import run_render
    from docx import Document
    with tempfile.TemporaryDirectory(dir=os.environ["VIDEO_ANALYSIS_TEMP_DIR"]) as temp:
        root = Path(temp); video, analysis, _ = _render_fixture(root)
        docx = root / "template.docx"; dotx = root / "template.dotx"
        _docx_template(docx); _as_dotx(docx, dotx); original = dotx.read_bytes()
        if run_render(video, dotx) != 0 or dotx.read_bytes() != original:
            return False
        output = analysis / "sample.docx"
        Document(output)
        with zipfile.ZipFile(output) as package:
            types = package.read("[Content_Types].xml")
        if not (b"wordprocessingml.document.main+xml" in types and b"wordprocessingml.template.main+xml" not in types):
            return False
        video2, analysis2, _ = _render_fixture(root / "named-docx")
        disguised = root / "template-named-docx.docx"
        disguised.write_bytes(dotx.read_bytes())
        if run_render(video2, disguised) != 0:
            return False
        bad_template = root / "broken.docx"; bad_template.write_bytes(b"not a docx")
        video3, _, _ = _render_fixture(root / "broken-template")
        return run_render(video3, bad_template) == 2


def check_missing_styles_fallback() -> bool:
    from render_sheet import render_document
    from docx import Document
    with tempfile.TemporaryDirectory(dir=os.environ["VIDEO_ANALYSIS_TEMP_DIR"]) as temp:
        root = Path(temp); _, analysis, sheet = _render_fixture(root)
        template = root / "sparse.docx"; _docx_template(template)
        doc = Document(template)
        for name in ("Title", "Heading 1", "Heading 2", "List Bullet", "Caption"):
            style = doc.styles[name]
            style._element.getparent().remove(style._element)
        doc.save(template)
        rendered = render_document(sheet, analysis_dir=analysis, template=template)
        if not all(p.style.name == "Normal" for p in rendered.paragraphs):
            return False
        from docx.oxml.ns import qn
        cases = []
        for name, remove_section, remove_dimensions in (("no-section", True, False), ("no-dimensions", False, True)):
            source = root / f"{name}-source.docx"; target = root / f"{name}.docx"
            _docx_template(source)
            with zipfile.ZipFile(source) as incoming, zipfile.ZipFile(target, "w") as outgoing:
                for item in incoming.infolist():
                    payload = incoming.read(item.filename)
                    if item.filename == "word/document.xml":
                        from lxml import etree
                        xml = etree.fromstring(payload)
                        sect = xml.find(f".//{qn('w:body')}/{qn('w:sectPr')}")
                        if sect is not None:
                            if remove_section:
                                sect.getparent().remove(sect)
                            elif remove_dimensions:
                                for tag in (qn("w:pgSz"), qn("w:pgMar")):
                                    node = sect.find(tag)
                                    if node is not None: sect.remove(node)
                        payload = etree.tostring(xml, xml_declaration=True, encoding="UTF-8", standalone=True)
                    outgoing.writestr(item, payload)
            try:
                result = render_document(sheet, analysis_dir=analysis, template=target)
                cases.append(len(result.inline_shapes) == 1 and result.inline_shapes[0].width == int(6 * 914400 * .38) - 164592)
            except Exception:
                cases.append(False)
        return all(cases)


def check_render_resume_preserves_docx() -> bool:
    from render_sheet import run_render
    from docx import Document
    with tempfile.TemporaryDirectory(dir=os.environ["VIDEO_ANALYSIS_TEMP_DIR"]) as temp:
        video, analysis, _ = _render_fixture(Path(temp))
        if run_render(video) != 0:
            return False
        output = analysis / "sample.docx"; doc = Document(output)
        doc.add_paragraph("User edit"); doc.save(output); before = output.read_bytes()
        return run_render(video) == 0 and output.read_bytes() == before


def check_changed_sheet_refuses_docx() -> bool:
    from render_sheet import run_render
    with tempfile.TemporaryDirectory(dir=os.environ["VIDEO_ANALYSIS_TEMP_DIR"]) as temp:
        video, analysis, _ = _render_fixture(Path(temp))
        if run_render(video) != 0:
            return False
        output = analysis / "sample.docx"; before = output.read_bytes()
        path = analysis / "sheet.json"; value = json.loads(path.read_text()); value["title"] = "Changed"
        path.write_text(json.dumps(value), encoding="utf-8")
        return run_render(video) == 3 and output.read_bytes() == before


def check_untracked_docx_refused() -> bool:
    from render_sheet import run_render
    with tempfile.TemporaryDirectory(dir=os.environ["VIDEO_ANALYSIS_TEMP_DIR"]) as temp:
        video, analysis, _ = _render_fixture(Path(temp)); output = analysis / "sample.docx"
        output.write_bytes(b"user data")
        return run_render(video) == 3 and output.read_bytes() == b"user data"


def check_render_requires_preparation() -> bool:
    from render_sheet import run_render
    with tempfile.TemporaryDirectory(dir=os.environ["VIDEO_ANALYSIS_TEMP_DIR"]) as temp:
        video, _, _ = _render_fixture(Path(temp), prepared=False)
        return run_render(video) == 4


def check_render_rejects_changed_bundle() -> bool:
    from render_sheet import run_render
    with tempfile.TemporaryDirectory(dir=os.environ["VIDEO_ANALYSIS_TEMP_DIR"]) as temp:
        video, analysis, _ = _render_fixture(Path(temp))
        bundle = analysis / "bundle.json"
        bundle.write_text('{"source":{"url":"https://youtu.be/abc"},"changed":true}', encoding="utf-8")
        if run_render(video) != 3 or (analysis / "sample.docx").exists():
            return False
        _, malformed_analysis, _ = _render_fixture(Path(temp) / "malformed-bundle")
        malformed_video = malformed_analysis.parent / "sample.mp4"
        (malformed_analysis / "bundle.json").write_text("{", encoding="utf-8")
        if run_render(malformed_video) != 2:
            return False
        _, manifest_analysis, _ = _render_fixture(Path(temp) / "malformed-manifest")
        manifest_video = manifest_analysis.parent / "sample.mp4"
        (manifest_analysis / "manifest.json").write_text("{", encoding="utf-8")
        return run_render(manifest_video) == 2


def check_setup_docx_dependency() -> bool:
    import setup_tools
    from tools import Tool
    missing = setup_tools.missing_packages(module_lookup=lambda name: (
        (_ for _ in ()).throw(ImportError(name)) if name == "docx" else object()),
        ffmpeg=Tool("ffmpeg", "path"))
    return "python-docx" in missing and setup_tools.PACKAGES.get("docx") == "python-docx"


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






def check_bundle_corrected_speech_provenance() -> bool:
    import build_bundle
    from tools import make_temp_folder
    folder = make_temp_folder("bundle-corrected-")
    transcript = {"language": "en", "segments": [{"start": 0, "end": 2, "text": "original"}]}
    transcript_path = folder / "transcript.json"
    transcript_path.write_text(json.dumps(transcript), encoding="utf-8")
    stat = transcript_path.stat()
    correction = {"schema_version": 2, "source": {"kind": "whisper", "language": "en", "file": "transcript.json"},
                  "provider": "openai",
                  "model": "test-model", "endpoint": {"base_url_identity": None, "locality": "local"},
                  "prompt_version": "asr-correction-v1",
                  "segments": [{"start": 0, "end": 2, "original_text": "original",
                                "corrected_text": "corrected", "status": "corrected",
                                "provider": "openai", "model": "test-model"}]}
    (folder / "transcript.corrected.json").write_text(json.dumps(correction), encoding="utf-8")
    inputs = SimpleNamespace(work=folder, transcript=transcript, raw_info={},
                             metadata={"language": "en", "video_stem": "sample"},
                             manifest={"params": {"correct_transcript": {"provider": "openai", "model": "test-model",
                                 "base_url_identity": None, "locality": "local", "source": {
                                 "kind": "whisper", "language": "en", "file": "transcript.json",
                                 "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}}}})
    speech = build_bundle._read_speech(inputs, "auto")
    return speech.source == "corrected" and speech.file == "transcript.corrected.json" \
        and speech.corrected_from == "whisper" and [item.text for item in speech.segments] == ["corrected"]


def check_bundle_ignores_stale_corrected_speech() -> bool:
    import build_bundle
    from tools import make_temp_folder
    folder = make_temp_folder("bundle-stale-corrected-")
    transcript = {"language": "en", "segments": [{"start": 0, "end": 1, "text": "original"}]}
    path = folder / "transcript.json"
    path.write_text(json.dumps(transcript), encoding="utf-8")
    (folder / "transcript.corrected.json").write_text("{}", encoding="utf-8")
    inputs = SimpleNamespace(work=folder, transcript=transcript, raw_info={},
                             metadata={"language": "en", "video_stem": "sample"}, manifest={"params": {}})
    speech = build_bundle._read_speech(inputs, "auto")
    return speech.source == "whisper" and [item.text for item in speech.segments] == ["original"]


def check_bundle_resume_keys_selected_speech_input() -> bool:
    import build_bundle
    args = SimpleNamespace(skip_ocr=True, language="auto", ocr_min_score=.5,
                           min_chapter=30, max_chapter=600, target_chapter=300)
    original = build_bundle._bundle_params(args, {}, speech=SimpleNamespace(
        source="whisper", file="transcript.json", language="en", size=10, mtime_ns=20,
        corrected_from=None))
    changed = build_bundle._bundle_params(args, {}, speech=SimpleNamespace(
        source="corrected", file="transcript.corrected.json", language="en", size=15,
        mtime_ns=25, corrected_from="whisper"))
    return original.get("speech_input") != changed.get("speech_input")






def check_analyze_video_local_pipeline() -> bool:
    import analyze_video
    calls = []
    with (patch.object(analyze_video, "extract_main", side_effect=lambda argv: calls.append(("extract", argv)) or 0),
          patch.object(analyze_video, "correct_main", side_effect=lambda argv: calls.append(("correct", argv)) or 0),
          patch.object(analyze_video, "bundle_main", side_effect=lambda argv: calls.append(("bundle", argv)) or 0),
          contextlib.redirect_stderr(io.StringIO())):
        code = analyze_video.main(["C:\\videos\\talk.mp4", "--skip-transcript", "--skip-ocr",
                                   "--language", "fr", "--interval", "3", "--at", "00:00:10"])
    return (code == 0 and [call[0] for call in calls] == ["extract", "correct", "bundle"]
            and "--skip-transcript" in calls[0][1] and "--skip-ocr" in calls[2][1]
            and all(calls[index][1][calls[index][1].index("--language") + 1] == "fr"
                    for index in (0, 1, 2)))


def check_analyze_video_url_pipeline() -> bool:
    import analyze_video
    calls = []
    def fetch(argv, *, on_video=None):
        calls.append(("fetch", argv))
        on_video(Path("downloaded.mp4"))
        return 0
    with (patch.object(analyze_video, "fetch_main", side_effect=fetch),
          patch.object(analyze_video, "extract_main", side_effect=lambda argv: calls.append(("extract", argv)) or 0),
          patch.object(analyze_video, "correct_main", side_effect=lambda argv: calls.append(("correct", argv)) or 0),
          patch.object(analyze_video, "bundle_main", side_effect=lambda argv: calls.append(("bundle", argv)) or 0),
          contextlib.redirect_stderr(io.StringIO())):
        code = analyze_video.main(["https://example.invalid/talk", "--dest", "downloads", "--sub-langs", "fr"])
    return code == 0 and [call[0] for call in calls] == ["fetch", "extract", "correct", "bundle"]


def check_analyze_video_correction_order_and_flags() -> bool:
    import analyze_video
    calls = []
    with patch.object(analyze_video, "extract_main", side_effect=lambda argv: calls.append(("extract", argv)) or 0), \
            patch.object(analyze_video, "correct_main", side_effect=lambda argv: calls.append(("correct", argv)) or 0), \
            patch.object(analyze_video, "bundle_main", side_effect=lambda argv: calls.append(("bundle", argv)) or 0), \
            contextlib.redirect_stderr(io.StringIO()):
        code = analyze_video.main(["talk.mp4", "--model", "whisper-small", "--correct-model", "fixer",
                                   "--base-url", "http://localhost/v1", "--ai-locality", "local",
                                   "--allow-remote-ai", "--no-correct", "--provider", "codex",
                                   "--retry-refused", "--language", "de"])
    names = [call[0] for call in calls]
    correct_args = calls[names.index("correct")][1] if "correct" in names else []
    return (code == 0 and names == ["extract", "correct", "bundle"]
            and calls[0][1][calls[0][1].index("--model") + 1] == "whisper-small"
            and "--model" in correct_args and correct_args[correct_args.index("--model") + 1] == "fixer"
            and all(option in correct_args for option in ("--base-url", "--ai-locality", "--allow-remote-ai",
                                                           "--no-correct", "--language", "--provider",
                                                           "--retry-refused")))


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


def check_bundle_cli_sponsorblock_segments() -> bool:
    from tools import make_temp_folder
    folder = make_temp_folder("bundle-sponsorblock-")
    video = folder / "lecture.mp4"
    video.write_bytes(b"identity only")
    work = folder / "lecture.analysis"
    work.mkdir()
    (work / "metadata.json").write_text(json.dumps({"duration": 30}), encoding="utf-8")
    (work / "transcript.json").write_text(json.dumps({"language": "en", "segments": [
        {"start": 19, "end": 22, "text": "sponsored speech"},
        {"start": 25, "end": 27, "text": "ordinary speech"}]}), encoding="utf-8")
    (work / "frames.json").write_text(json.dumps({"f000020.jpg": 20}), encoding="utf-8")
    (work / "sponsorblock.json").write_text(json.dumps([
        {"start_time": 0, "end_time": 17.9, "category": "intro",
         "title": "Intermission/Intro Animation", "type": "skip",
         "_categories": [["intro", 0, 17.9, "Intermission/Intro Animation"]]},
        {"start_time": 18.5, "end_time": 24.8, "category": "sponsor",
         "title": "Sponsor", "type": "skip",
         "_categories": [["sponsor", 18.5, 24.8, "Sponsor"]]}]), encoding="utf-8")
    stat = video.stat()
    (work / "manifest.json").write_text(json.dumps({"schema_version": 2,
        "source": {"path": str(video.resolve()), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns},
        "params": {"fetch": {}, "extract": {"interval": 5}}, "stages": {}}), encoding="utf-8")
    result = subprocess.run([sys.executable, str(HERE / "build_bundle.py"), str(video), "--skip-ocr"],
                            capture_output=True, text=True, check=False)
    CHILD_OUTPUT.append(result.stdout + result.stderr)
    if result.returncode != 0:
        CHECK_OUTPUTS["bundle CLI handles SponsorBlock segments"] = result.stdout + result.stderr
        return False
    bundle = json.loads((work / "bundle.json").read_text(encoding="utf-8"))
    chapter = bundle["chapters"][0]
    segments = chapter["speech_segments"]
    return (segments[0]["is_sponsor"] and not segments[1]["is_sponsor"]
            and chapter["frames"][0]["is_sponsor"]
            and chapter["sponsor_overlap"]
            and "sponsored speech" not in chapter["speech_text"]
            and "ordinary speech" in chapter["speech_text"])


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


def check_manifest_source_kinds() -> bool:
    from extract_video import load_manifest
    from tools import make_temp_folder
    folder = make_temp_folder("source-kind-")
    video = folder / "local.mp4"
    video.write_bytes(b"local")
    stat = video.stat()
    identity = {"path": str(video.resolve()), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}
    local_work = folder / "local.analysis"
    local_work.mkdir()
    local = load_manifest(local_work, identity, {"interval": 5})
    legacy = {"schema_version": 2, "source": identity,
              "params": {"fetch": {"url": "https://example.invalid/v"}, "extract": {"interval": 5}},
              "stages": {}}
    legacy_work = folder / "legacy.analysis"
    legacy_work.mkdir()
    (legacy_work / "manifest.json").write_text(json.dumps(legacy), encoding="utf-8")
    migrated = load_manifest(legacy_work, identity, {"interval": 5})
    return local["source"].get("kind") == "local" and migrated["source"].get("kind") == "url"


def check_extract_preserves_correction_state() -> bool:
    from extract_video import load_manifest
    from tools import make_temp_folder
    folder = make_temp_folder("correction-preserve-")
    identity = {"path": "video.mp4", "size": 5, "mtime_ns": 10, "kind": "local"}
    correction = {"model": "test-model", "source": {"kind": "whisper"}}
    stage = {"status": "skipped", "reason": "manual subtitles"}
    data = {"schema_version": 2, "source": {key: value for key, value in identity.items() if key != "kind"},
            "params": {"fetch": {}, "extract": {"interval": 5}, "correct_transcript": correction},
            "stages": {"correct_transcript": stage}}
    (folder / "manifest.json").write_text(json.dumps(data), encoding="utf-8")
    result = load_manifest(folder, {key: value for key, value in identity.items() if key != "kind"},
                          {"interval": 5})
    return (result["params"].get("correct_transcript") == correction
            and result["stages"].get("correct_transcript") == stage
            and result["source"].get("kind") == "local")


def check_correction_exact_numbered_lines() -> bool:
    from correct_transcript import parse_numbered_lines
    parsed = parse_numbered_lines("1\tBonjour.\n2\tMerci.", [1, 2])
    for invalid in ("1\tBonjour.", "1\tBonjour.\n1\tEncore", "1\tBonjour.\n2\tMerci.\n3\tExtra",
                    "Voici la correction\n1\tBonjour.\n2\tMerci.", "1\t \n2\tMerci."):
        if parse_numbered_lines(invalid, [1, 2]) is not None:
            return False
    return parsed == {1: "Bonjour.", 2: "Merci."}


def check_correction_guards() -> bool:
    from correct_transcript import CorrectionInput, ProviderResult, correct_chunk
    originals = [CorrectionInput(0, 1, "The cat sat on the mat today"),
                 CorrectionInput(1, 2, "One two three four five six seven eight nine ten"),
                 CorrectionInput(2, 3, "This phrase contains several familiar words here")]
    response = "1\tThe cat sat on a mat today\n2\tOne\n3\tCompletely unrelated alternatives now replace these words"
    result = correct_chunk(originals, ProviderResult(response))
    return ([item.status for item in result] == ["corrected", "rejected_edit_ratio", "rejected_edit_ratio"]
            and [item.corrected_text for item in result[1:]] == [item.text for item in originals[1:]])


def check_correction_safe_fallbacks() -> bool:
    from correct_transcript import CorrectionInput, ProviderResult, correct_chunk
    originals = [CorrectionInput(0, 1, "Original one"), CorrectionInput(1, 2, "Original two")]
    refused = correct_chunk(originals, ProviderResult(None, refusal=True))
    malformed = correct_chunk(originals, ProviderResult("1\tEdited one"))
    return ([item.status for item in refused] == ["fallback_refusal"] * 2
            and [item.corrected_text for item in refused] == [item.text for item in originals]
            and [item.status for item in malformed] == ["fallback_malformed"] * 2
            and [item.corrected_text for item in malformed] == [item.text for item in originals])


def check_correction_shared_helpers_and_speech_reader() -> bool:
    from bundle import SpeechSource, read_speech_segments
    from tools import media_source_kind, same_video
    from correct_transcript import CorrectTranscriptConfig
    video = {"path": "video.mp4", "size": 12, "mtime_ns": 34}
    manifest = {"params": {"fetch": {"url": "https://example.invalid/v"}}}
    return (media_source_kind(manifest) == "url" and same_video(video, dict(video))
            and not same_video(video, {**video, "size": 13})
            and callable(read_speech_segments) and CorrectTranscriptConfig is not None)


def check_correction_shared_runtime_helpers() -> bool:
    import correct_transcript as correction
    from host_models import ConfigReader
    settings = correction.CorrectionSettings("openai", "test-model", "https://local.invalid/v1",
                                             "local", "local-no-key", True, False, False)
    deps = correction.CorrectionDeps({}, client_factory=lambda **kwargs: kwargs,
        config_reader=ConfigReader(Path.cwd(), Path("/sentinel-test-home"), lambda path: None))
    client = correction.make_client(settings, deps)
    context = correction._build_stage_context({
        "title": "Title", "description": "Description", "chapters": [{"title": "Chapter"}]}, "fr")
    accepted, method, error = correction.ask_or_refuse_remote_ai(settings)
    tree = ast.parse((HERE / "correct_transcript.py").read_text(encoding="utf-8"))
    functions = [node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]
    def is_small(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
        args = node.args.posonlyargs + node.args.args + node.args.kwonlyargs
        if args and args[0].arg == "self":
            args = args[1:]
        return node.end_lineno - node.lineno + 1 <= 40 and len(args) <= 4
    small_functions = all(is_small(node) for node in functions)
    return (client == {"api_key": "local-no-key", "base_url": "https://local.invalid/v1",
                       "timeout": 900.0, "max_retries": 2}
            and "Title" in context and "Chapter" in context and "fr" in context
            and accepted and method == "flag" and error is None and small_functions)


def check_correction_consent_matrix() -> bool:
    from correct_transcript import consent_required
    return (consent_required("local", "remote")
            and not consent_required("url", "remote")
            and not consent_required("local", "local"))


def call_correction(video: Path, **options: Any) -> int:
    from correct_transcript import CorrectionDeps, CorrectionRequest, run_correction
    from host_models import ConfigReader
    if options.get("client_factory") is not None and "provider" not in options:
        options["provider"] = "openai"
    request_fields = {"language", "model", "base_url", "locality", "allow_remote_ai",
                      "no_correct", "api_key", "provider", "retry_refused", "host"}
    request = CorrectionRequest(**{key: options.pop(key) for key in list(options) if key in request_fields})
    reader = options.pop("config_reader", ConfigReader(Path.cwd(), Path("/sentinel-fixture-home"),
                                                         lambda path: None))
    deps = CorrectionDeps(options.pop("environ", {}),
                          options.pop("client_factory", None), options.pop("interactive", None),
                          options.pop("which", __import__("shutil").which),
                          options.pop("subprocess_runner", subprocess.run), reader)
    if options:
        raise TypeError(f"unknown correction test options: {', '.join(options)}")
    return run_correction(video, request, deps)


def check_correction_skip_modes() -> bool:
    from correct_transcript import correction_skip_reason
    return (correction_skip_reason(no_correct=True, source="whisper", api_key=None) == "disabled"
            and correction_skip_reason(no_correct=False, source="none", api_key="secret") == "no speech source"
            and correction_skip_reason(no_correct=False, source="whisper", api_key=None) == "no API key")


def check_correction_consent_matrix_blocks_before_provider_call() -> bool:
    from tools import make_temp_folder
    folder = make_temp_folder("correction-consent-")
    video = folder / "local.mp4"
    video.write_bytes(b"video")
    work = folder / "local.analysis"
    work.mkdir()
    (work / "manifest.json").write_text(json.dumps({"schema_version": 2,
        "source": {"kind": "local", "path": str(video.resolve()), "size": video.stat().st_size,
                   "mtime_ns": video.stat().st_mtime_ns}, "params": {"fetch": {}, "extract": {}}, "stages": {}}), encoding="utf-8")
    (work / "transcript.json").write_text(json.dumps({"language": "fr", "segments": [{"start": 0, "end": 1, "text": "Bonjour."}]}), encoding="utf-8")
    class NeverClient:
        def correct(self, *args: Any, **kwargs: Any) -> Any:
            raise AssertionError("provider called before consent")
    result = call_correction(video, client_factory=lambda **kwargs: NeverClient(), environ={
        "OPENAI_API_KEY": "fake", "VIDEO_ANALYSIS_AI_LOCALITY": "remote"}, allow_remote_ai=False,
        interactive=False)
    class AllowedClient:
        def correct(self, **kwargs: Any) -> Any:
            return {"text": "1\tBonjour."}
    allowed = call_correction(video, client_factory=lambda **kwargs: AllowedClient(), api_key="fake",
                             locality="remote", allow_remote_ai=True)
    stage = json.loads((work / "manifest.json").read_text(encoding="utf-8"))["stages"]["correct_transcript"]
    return result == 4 and allowed == 0 and stage.get("consent") == {"granted": True, "method": "flag"}


def check_correction_skip_modes_stay_offline() -> bool:
    from tools import make_temp_folder
    folder = make_temp_folder("correction-skip-")
    video = folder / "skip.mp4"
    video.write_bytes(b"video")
    (folder / "skip.analysis").mkdir()
    class NeverClient:
        def __call__(self, **kwargs: Any) -> Any:
            raise AssertionError("provider constructed for a skipped correction")
    disabled = call_correction(video, client_factory=NeverClient(), no_correct=True)
    (folder / "skip.analysis" / "transcript.json").write_text(json.dumps({"language": "en",
        "segments": [{"start": 0, "end": 1, "text": "offline"}]}), encoding="utf-8")
    missing_key = call_correction(video, client_factory=NeverClient(), provider="auto",
                                  environ={}, locality="remote")
    stage = json.loads((folder / "skip.analysis" / "manifest.json").read_text(encoding="utf-8"))["stages"]["correct_transcript"]
    return disabled == 0 and missing_key == 0 and stage.get("reason") == "no available correction provider"


def check_manual_subtitles_skip_correction_unchanged() -> bool:
    from tools import make_temp_folder
    folder = make_temp_folder("correction-manual-")
    video = folder / "manual.mp4"
    video.write_bytes(b"video")
    work = folder / "manual.analysis"
    work.mkdir()
    original = "WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nManual words\n"
    (work / "manual.fr.vtt").write_text(original, encoding="utf-8")
    (work / "raw_info.json").write_text(json.dumps({"language": "fr", "subtitles": {"fr": [{}]}}), encoding="utf-8")
    (work / "transcript.json").write_text(json.dumps({"language": "fr", "segments": []}), encoding="utf-8")
    result = call_correction(video, client_factory=lambda **kwargs: (_ for _ in ()).throw(
        AssertionError("manual subtitles must not call provider")), api_key="fake", locality="local")
    return result == 0 and (work / "manual.fr.vtt").read_text(encoding="utf-8") == original


def check_correction_resume_and_conflict_are_safe() -> bool:
    from tools import make_temp_folder
    folder = make_temp_folder("correction-resume-")
    video = folder / "resume.mp4"
    video.write_bytes(b"video")
    work = folder / "resume.analysis"
    work.mkdir()
    (work / "transcript.json").write_text(json.dumps({"language": "fr", "segments": [{"start": 0, "end": 1, "text": "Bonjour."}]}), encoding="utf-8")
    class FakeClient:
        def correct(self, **kwargs: Any) -> Any:
            return {"text": "1\tBonjour."}
    options = {"api_key": "fake", "locality": "local"}
    first = call_correction(video, client_factory=lambda **kwargs: FakeClient(), **options)
    second = call_correction(video, client_factory=lambda **kwargs: (_ for _ in ()).throw(
        AssertionError("completed correction was not resumed")), **options)
    changed = call_correction(video, client_factory=lambda **kwargs: FakeClient(), api_key="fake",
                             locality="local", model="different")
    return first == 0 and second == 0 and changed == 3


def check_correction_fake_client_writes_validated_output() -> bool:
    from tools import make_temp_folder
    folder = make_temp_folder("correction-output-")
    video = folder / "output.mp4"
    video.write_bytes(b"video")
    work = folder / "output.analysis"
    work.mkdir()
    source = {"language": "fr", "segments": [{"start": 0, "end": 1, "text": "Bonjour monde."}]}
    (work / "transcript.json").write_text(json.dumps(source), encoding="utf-8")
    class FakeClient:
        def correct(self, **kwargs: Any) -> Any:
            return {"text": "1\tBonjour, monde." , "usage": {"prompt_tokens": 8,
                    "prompt_tokens_details": {"cached_tokens": 2}, "completion_tokens": 3,
                    "completion_tokens_details": {"reasoning_tokens": 1}, "total_tokens": 11}}
    result = call_correction(video, client_factory=lambda **kwargs: FakeClient(), api_key="fake", locality="local")
    output = json.loads((work / "transcript.corrected.json").read_text(encoding="utf-8"))
    manifest = json.loads((work / "manifest.json").read_text(encoding="utf-8"))
    usage = manifest["stages"]["correct_transcript"]["usage"]
    return (result == 0 and output["segments"][0]["corrected_text"] == "Bonjour, monde."
            and output["schema_version"] == 2 and usage["cached_input_tokens"] == 2
            and usage["reasoning_tokens"] == 1)


def check_correction_calls_bounded_and_usage_recorded() -> bool:
    from tools import make_temp_folder
    import threading
    folder = make_temp_folder("correction-concurrency-")
    video = folder / "many.mp4"
    video.write_bytes(b"video")
    work = folder / "many.analysis"
    work.mkdir()
    segments = [{"start": i, "end": i + 1, "text": "same words here"} for i in range(81)]
    (work / "transcript.json").write_text(json.dumps({"language": "en", "segments": segments}), encoding="utf-8")
    lock = threading.Lock()
    active = 0
    maximum = 0
    class FakeClient:
        def correct(self, **kwargs: Any) -> Any:
            nonlocal active, maximum
            with lock:
                active += 1
                maximum = max(maximum, active)
            try:
                lines = kwargs["user_prompt"].split("Transcript lines:\n", 1)[1].splitlines()
                return {"text": "\n".join(f"{line.split(chr(9), 1)[0]}\tsame words here" for line in lines),
                        "usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3}}
            finally:
                with lock:
                    active -= 1
    result = call_correction(video, client_factory=lambda **kwargs: FakeClient(), api_key="fake", locality="local")
    manifest = json.loads((work / "manifest.json").read_text(encoding="utf-8"))
    usage = manifest["stages"]["correct_transcript"]["usage"]
    return result == 0 and manifest["stages"]["correct_transcript"]["chunks"] == 3 and maximum <= 2 and usage["total_tokens"] == 9


def check_correction_provider_errors_fall_back_per_chunk() -> bool:
    from tools import make_temp_folder
    folder = make_temp_folder("correction-fallback-")
    video = folder / "error.mp4"
    video.write_bytes(b"video")
    work = folder / "error.analysis"
    work.mkdir()
    source = {"language": "en", "segments": [{"start": 0, "end": 1, "text": "original words"}]}
    (work / "transcript.json").write_text(json.dumps(source), encoding="utf-8")
    class ErrorClient:
        def correct(self, **kwargs: Any) -> Any:
            raise RuntimeError("provider unavailable")
    error_output = io.StringIO()
    with contextlib.redirect_stderr(error_output):
        result = call_correction(video, client_factory=lambda **kwargs: ErrorClient(), api_key="fake", locality="local")
    output = json.loads((work / "transcript.corrected.json").read_text(encoding="utf-8"))
    return (result == 0 and output["segments"][0]["corrected_text"] == "original words"
            and output["segments"][0]["status"] == "fallback_provider_error"
            and error_output.getvalue().strip() == "correction provider failure: RuntimeError")


def check_local_endpoint_without_api_key_uses_placeholder() -> bool:
    from tools import make_temp_folder
    folder = make_temp_folder("correction-local-key-")
    video = folder / "local.mp4"
    video.write_bytes(b"video")
    work = folder / "local.analysis"
    work.mkdir()
    (work / "transcript.json").write_text(json.dumps({"language": "en", "segments": [{"start": 0, "end": 1, "text": "same words"}]}), encoding="utf-8")
    observed = {}
    class FakeClient:
        def correct(self, **kwargs: Any) -> Any:
            return {"text": "1\tsame words"}
    def factory(**kwargs: Any) -> FakeClient:
        observed.update(kwargs)
        return FakeClient()
    result = call_correction(video, client_factory=factory, locality="local", provider="local",
                            base_url="http://127.0.0.1/v1",
                            environ={"VIDEO_ANALYSIS_AI_LOCALITY": "local"})
    output = (work / "manifest.json").read_text(encoding="utf-8")
    return (result == 0 and observed.get("api_key") == "local-no-key"
            and observed.get("timeout") == 900 and observed.get("max_retries") == 2
            and "local-no-key" not in output)


def check_provider_selection_matrix() -> bool:
    import correct_transcript as correction
    from host_models import ConfigReader
    def which(name: str) -> str | None:
        return {"claude": "claude-bin", "codex.cmd": "codex-shim"}.get(name)
    matrix = [
        correction.resolve_provider({"CLAUDECODE": "1", "CODEX_THREAD_ID": "x"}, which=which)
        == ("claude-code", "claude-bin"),
        correction.resolve_provider({"CODEX_SESSION_ID": "x"}, which=which) == ("codex", "codex-shim"),
        correction.resolve_provider({"OPENAI_API_KEY": "secret"}, which=which) == ("openai", None),
        correction.resolve_provider({}, which=which) == (None, None),
        correction.resolve_provider({"OPENAI_API_KEY": "x"}, "local", which) == ("local", None),
        correction.provider_default_model("codex") == "gpt-6-luna",
    ]
    reader = ConfigReader(Path.cwd(), Path("/sentinel-home"), lambda path: (
        '[agents.defaults.model]\nprimary = "ollama/llama"' if path.name == "openclaw.json" else None))
    codex_reader = ConfigReader(Path.cwd(), Path("/sentinel-home"), lambda path:
        'model = "gpt-5.6-sol"\nmodel_provider = "ollama"' if path.name == "config.toml" else None)
    codex_openai = correction._resolve_settings(correction.CorrectionRequest(provider="openai"),
        correction.CorrectionDeps({"CODEX_SESSION_ID": "x", "VIDEO_ANALYSIS_HOST_AI": "local"},
                                  which=which, config_reader=codex_reader))
    openclaw_openai = correction._resolve_settings(correction.CorrectionRequest(host="openclaw"),
        correction.CorrectionDeps({"OPENAI_API_KEY": "secret"}, which=which, config_reader=reader))
    conflict_video = Path.cwd() / "conflict-not-written.mp4"
    conflict_output = io.StringIO()
    with contextlib.redirect_stderr(conflict_output):
        conflict = correction.run_correction(conflict_video, correction.CorrectionRequest(
            provider="openai", host="openclaw"), correction.CorrectionDeps({}, config_reader=reader))
    matrix.extend([codex_openai.locality == "remote" and codex_openai.model == "gpt-6-luna",
                   openclaw_openai.provider == "openai" and openclaw_openai.locality == "remote",
                   conflict == 2 and "explicit host conflicts" in conflict_output.getvalue()
                   and not conflict_video.exists() and not conflict_video.with_suffix(".analysis").exists()])
    return all(matrix)


def check_cli_adapters_are_offline_and_account_usage() -> bool:
    import correct_transcript as correction
    calls = []
    claude_json = json.dumps({"result": "1\tbonjour", "usage": {"input_tokens": 3,
        "cache_creation_input_tokens": 2, "cache_read_input_tokens": 1, "output_tokens": 4},
        "total_cost_usd": .01})
    codex_jsonl = "\n".join([json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "1\thello"}}),
                               json.dumps({"type": "turn.completed", "usage": {"input_tokens": 5,
                                   "cached_input_tokens": 2, "output_tokens": 3, "reasoning_output_tokens": 4}})])
    def runner(command, **kwargs):
        calls.append((command, kwargs))
        result = claude_json if command[0] == "claude-bin" else codex_jsonl
        return SimpleNamespace(returncode=0, stdout=result, stderr="")
    claude = correction.ClaudeCodeClient(executable="claude-bin", runner=runner).correct(
        model="haiku", system_prompt="system secret", user_prompt="transcript secret")
    codex = correction.CodexClient(executable="codex-shim", runner=runner).correct(
        model="sol", system_prompt="system secret", user_prompt="transcript secret")
    a, b = calls
    return (claude.text == "1\tbonjour" and claude.usage.input_tokens == 6
            and claude.usage.cached_input_tokens == 1 and claude.reported_cost_usd == .01
            and codex.usage.reasoning_tokens == 4 and codex.usage.total_tokens == 12
            and codex.text == "1\thello"
            and a[0] == ["claude-bin", "-p", "--model", "haiku", "--output-format", "json",
                         "--system-prompt", "system secret", "--tools", "", "--strict-mcp-config",
                         "--disable-slash-commands", "--no-session-persistence", "--setting-sources", "project"]
            and b[0][-1] == "-" and b[1]["shell"] is False and b[1]["timeout"] == 900
            and "transcript secret" not in " ".join(a[0] + b[0])
            and all("transcript secret" in call[1]["input"] for call in calls))


def check_prose_refusals_are_retryable() -> bool:
    import correct_transcript as correction
    return (correction.is_prose_refusal("I cannot provide that transcript", [1])
            and correction.is_prose_refusal("Copyright policy: I must refuse", [1])
            and not correction.is_prose_refusal("1\tI cannot attend", [1])
            and not correction.is_prose_refusal("Please review the copyright policy", [1]))


def check_retry_sends_only_refused_chunks() -> bool:
    import correct_transcript as correction
    from host_models import ConfigReader
    from tools import make_temp_folder
    folder = make_temp_folder("correction-retry-refused-")
    video = folder / "sample.mp4"
    video.write_bytes(b"video")
    work = folder / "sample.analysis"
    work.mkdir()
    segments = [{"start": index, "end": index + 1, "text": f"spoken words {index}"}
                for index in range(41)]
    (work / "transcript.json").write_text(json.dumps({"language": "en", "segments": segments}),
                                          encoding="utf-8")
    prompts = []
    retry_notices = []
    retry_output = io.StringIO()
    config_reads = []
    def runner(command, **kwargs):
        prompt = kwargs["input"]
        selected = command[command.index("--model") + 1] if "--model" in command else "host default"
        prompts.append((selected, prompt))
        if selected == "host default":
            retry_notices.append(retry_output.getvalue())
        ids = [int(match.group(1)) for match in re.finditer(r"(?m)^(\d+)\t", prompt)]
        if prompts[-1][0] in {"haiku", "claude-haiku-5-5"} and ids[0] == 1:
            answer = "I cannot provide a correction for this chunk."
        else:
            answer = "\n".join(f"{number}\tspoken words {number - 1}" for number in ids)
        return SimpleNamespace(returncode=0, stdout=json.dumps({"result": answer, "usage": {
            "input_tokens": 1, "output_tokens": 1}}), stderr="")
    env = {"CLAUDECODE": "1"}
    deps = {"environ": env, "which": lambda command: "claude.exe" if command == "claude" else None,
            "subprocess_runner": runner,
            "config_reader": ConfigReader(Path.cwd(), Path("/sentinel-fixture-home"),
                                           lambda path: config_reads.append(path) or None)}
    initial_output = io.StringIO()
    with contextlib.redirect_stdout(initial_output):
        initial = call_correction(video, provider="claude-code", locality="local", **deps)
    with contextlib.redirect_stdout(retry_output):
        retry = call_correction(video, provider="claude-code", locality="local", retry_refused=True, **deps)
    output = json.loads((work / "transcript.corrected.json").read_text(encoding="utf-8"))
    manifest = json.loads((work / "manifest.json").read_text(encoding="utf-8"))
    stage = manifest["stages"]["correct_transcript"]
    retry_prompt = prompts[-1][1] if prompts else ""
    retry_ids = [int(match.group(1)) for match in re.finditer(r"(?m)^(\d+)\t", retry_prompt)]
    command_output = initial_output.getvalue()
    return (initial == retry == 0 and len(prompts) == 3 and retry_ids == list(range(1, 41))
            and prompts[-1][0] == "host default" and retry_notices
            and retry_notices[0].splitlines()[0].startswith(
                "AI selection: host=claude-code model=host default role=default")
            and "--provider claude-code" in command_output and "--retry-refused" in command_output
            and "--model sonnet" in command_output and "--ai-locality local" in command_output
            and sys.executable in command_output and str(video) in command_output
            and stage["refused_chunks"] == [] and len(stage["attempts"]) == 2
            and output["segments"][40]["model"] in {"haiku", "claude-haiku-5-5"}
            and all(row["model"] == "host default" for row in output["segments"][:40])
            and stage["attempts"][-1]["model"] == "host default"
            and (Path.home() / ".claude/settings.json") not in config_reads)


def check_host_adapters_preserve_routing_and_optional_model_flags() -> bool:
    import correct_transcript as correction
    from host_models import ConfigReader
    calls = []
    runner_kwargs = []
    def runner(command, **kwargs):
        calls.append(command)
        runner_kwargs.append(kwargs)
        if command[0] == "openclaw-bin":
            return SimpleNamespace(returncode=0, stdout=json.dumps({"status": "unverified-status", "text": "1\thello",
                "usage": {"input_tokens": 2}, "model": "vendor/model", "provider": "vendor"}), stderr="")
        if command[0] == "claude-bin":
            return SimpleNamespace(returncode=0, stdout=json.dumps({"result": "1\thello"}), stderr="")
        return SimpleNamespace(returncode=0, stdout=json.dumps({"type": "item.completed", "item": {
            "type": "agent_message", "text": "1\thello"}}), stderr="")
    correction.ClaudeCodeClient(executable="claude-bin", runner=runner).correct(
        model=None, system_prompt="s", user_prompt="u", invocation=correction.ModelInvocation.HOST_DEFAULT)
    correction.CodexClient(executable="codex-bin", runner=runner).correct(
        model=None, system_prompt="s", user_prompt="u", invocation=correction.ModelInvocation.HOST_DEFAULT)
    prompt = "s\n\nu"
    result = correction.OpenClawClient(executable="openclaw-bin", runner=runner).correct(
        model="vendor/model", system_prompt="s", user_prompt="u")
    def failing_runner(command, **kwargs):
        return SimpleNamespace(returncode=7, stdout="model not found", stderr="connection refused")
    failed = correction.OpenClawClient(executable="openclaw-bin", runner=failing_runner).correct(
        model=None, system_prompt="s", user_prompt="u")
    def invalid_runner(command, **kwargs):
        return SimpleNamespace(returncode=0, stdout="[]", stderr="")
    invalid = correction.OpenClawClient(executable="openclaw-bin", runner=invalid_runner).correct(
        model=None, system_prompt="s", user_prompt="u")
    from tools import make_temp_folder
    folder = make_temp_folder("correction-openclaw-provenance-")
    video = folder / "openclaw.mp4"
    video.write_bytes(b"video")
    work = folder / "openclaw.analysis"
    work.mkdir()
    (work / "transcript.json").write_text(json.dumps({"language": "en", "segments": [
        {"start": 0, "end": 1, "text": "same words"}]}), encoding="utf-8")
    def openclaw_runner(command, **kwargs):
        return SimpleNamespace(returncode=0, stdout=json.dumps({"status": "completed", "output": "1\tsame words",
            "usage": {"input_tokens": 3}, "model": "vendor/model", "provider": "vendor"}), stderr="")
    integrated = call_correction(video, provider="openclaw", host="openclaw", locality="local", environ={},
        which=lambda name: "openclaw-bin" if name == "openclaw" else None, subprocess_runner=openclaw_runner,
        config_reader=ConfigReader(Path.cwd(), Path("/sentinel-openclaw-home"),
            lambda path: '{"agents":{"defaults":{"model":{"primary":"vendor/model"}}}}'
            if path.name == "openclaw.json" else None))
    segment = json.loads((work / "transcript.corrected.json").read_text(encoding="utf-8"))["segments"][0]
    default_video = folder / "default.mp4"
    default_video.write_bytes(b"video")
    default_work = folder / "default.analysis"
    default_work.mkdir()
    (default_work / "transcript.json").write_text(json.dumps({"language": "en", "segments": [
        {"start": 0, "end": 1, "text": "default words"}]}), encoding="utf-8")
    default_call = call_correction(default_video, provider="openclaw", host="openclaw", locality="local",
        environ={}, which=lambda name: "openclaw-bin" if name == "openclaw" else None,
        subprocess_runner=lambda command, **kwargs: SimpleNamespace(returncode=0,
            stdout=json.dumps({"content": "1\tdefault words"}), stderr=""),
        config_reader=ConfigReader(Path.cwd(), Path("/sentinel-empty-openclaw-home"), lambda path: None))
    default_manifest = json.loads((default_work / "manifest.json").read_text(encoding="utf-8"))
    default_output = json.loads((default_work / "transcript.corrected.json").read_text(encoding="utf-8"))
    from tools import app_temp_dir
    neutral = str(app_temp_dir() / "correction-neutral")
    return ("--model" not in calls[0] and "-m" not in calls[1]
            and calls[2][:4] == ["openclaw-bin", "infer", "model", "run"]
            and "agent" not in calls[2] and prompt not in calls[2]
            and calls[2][calls[2].index("--model"):calls[2].index("--model") + 2] == ["--model", "vendor/model"]
            and calls[2][-1] == "--json" and runner_kwargs[2].get("input") == prompt
            and runner_kwargs[2].get("cwd") == neutral
            and result.text == "1\thello" and result.usage.input_tokens == 2
            and failed.failure_category == correction.FailureCategory.TRANSPORT
            and invalid.error and "--model" in calls[-1] and calls[-1][calls[-1].index("--model") + 1] == "vendor/model"
            and result.reported_model == "vendor/model" and result.reported_provider == "vendor"
            and default_call == 0 and default_output["segments"][0]["model"] == "host default"
            and default_manifest["stages"]["correct_transcript"]["attempts"][0]["model"] == "host default"
            and integrated == 0 and segment["model"] == "vendor/model" and segment["provider"] == "vendor")


def check_low_cost_first_chunk_fallback_is_bounded_and_selective() -> bool:
    import correct_transcript as correction
    from tools import make_temp_folder
    folder = make_temp_folder("correction-first-fallback-")
    video = folder / "pilot.mp4"
    video.write_bytes(b"video")
    work = folder / "pilot.analysis"
    work.mkdir()
    segments = [{"start": i, "end": i + 1, "text": f"spoken words {i}"} for i in range(41)]
    (work / "transcript.json").write_text(json.dumps({"language": "en", "segments": segments}), encoding="utf-8")
    calls, output = [], io.StringIO()
    def runner(command, **kwargs):
        model = command[command.index("--model") + 1]
        calls.append(model)
        if len(calls) == 1:
            return SimpleNamespace(returncode=1, stdout="", stderr="connection reset")
        ids = [int(match.group(1)) for match in re.finditer(r"(?m)^(\d+)\t", kwargs["input"])]
        answer = "\n".join(f"{number}\tspoken words {number - 1}" for number in ids)
        return SimpleNamespace(returncode=0, stdout=json.dumps({"result": answer}), stderr="")
    with contextlib.redirect_stdout(output):
        result = call_correction(video, provider="claude-code", locality="local",
            environ={"CLAUDECODE": "1", "ANTHROPIC_MODEL": "claude-sonnet-5-5"},
            which=lambda name: "claude-bin" if name == "claude" else None, subprocess_runner=runner)
    manifest = json.loads((work / "manifest.json").read_text(encoding="utf-8"))
    stage = manifest["stages"]["correct_transcript"]
    primary = (result == 0 and calls == ["haiku", "claude-sonnet-5-5", "claude-sonnet-5-5"]
            and output.getvalue().count("AI fallback:") == 1
            and stage["selection"]["fallback"] is True
            and all(row["model"] == "claude-sonnet-5-5" for row in json.loads(
                (work / "transcript.corrected.json").read_text(encoding="utf-8"))["segments"]))
    one = [SimpleNamespace(start=0, end=1, text="same words")]
    class ScriptedClient:
        def __init__(self, first, second):
            self.first, self.second, self.models = first, second, []
        def correct(self, *, model, **kwargs):
            self.models.append(model)
            return self.first if len(self.models) == 1 else self.second
    fallback_target = correction.ModelFallback("claude-code", "session", correction.ModelInvocation.OVERRIDE)
    unavailable = ScriptedClient(correction.ProviderResult(None, error=True,
        failure_category=correction.FailureCategory.MODEL_UNAVAILABLE), correction.ProviderResult("1\tsame words"))
    unavailable_run = correction.ChunkRun(unavailable, "cheap", "", fallback=fallback_target)
    unavailable_result = correction._correct_chunks(unavailable_run, one)
    succeeds = ScriptedClient(correction.ProviderResult("1\tsame words"), correction.ProviderResult(None))
    success_result = correction._correct_chunks(
        correction.ChunkRun(succeeds, "cheap", "", fallback=fallback_target), one)
    default_down = ScriptedClient(correction._failure_result(TimeoutError()),
                                  correction._failure_result(TimeoutError()))
    down_run = correction.ChunkRun(default_down, "cheap", "", fallback=fallback_target)
    correction._correct_chunks(down_run, one)
    non_switch = []
    for response in (correction.ProviderResult(None, refusal=True),
                     correction.ProviderResult("bad numbered output"),
                     correction.ProviderResult(None, error=True, failure_category=correction.FailureCategory.PROVIDER)):
        client = ScriptedClient(response, correction.ProviderResult("1\tsame words"))
        result = correction._correct_chunks(correction.ChunkRun(client, "cheap", "", fallback=fallback_target), one)
        non_switch.append(result[-1] is None and client.models == ["cheap"])
    return (primary and unavailable.models == ["cheap", "session"]
            and unavailable_result[-1]["reason"] == "model-unavailable"
            and succeeds.models == ["cheap"] and success_result[-1] is None
            and default_down.models == ["cheap", "session"] and all(non_switch))


def check_selection_notice_precedes_calls_and_omits_secrets() -> bool:
    from tools import make_temp_folder
    folder = make_temp_folder("correction-notice-")
    video = folder / "notice.mp4"
    video.write_bytes(b"video")
    work = folder / "notice.analysis"
    work.mkdir()
    (work / "transcript.json").write_text(json.dumps({"language": "en", "segments": [
        {"start": 0, "end": 1, "text": "original words"}]}), encoding="utf-8")
    seen = []
    captured = io.StringIO()
    def runner(command, **kwargs):
        seen.append(captured.getvalue())
        return SimpleNamespace(returncode=0, stdout=json.dumps({"result": "1\toriginal words"}), stderr="")
    with contextlib.redirect_stdout(captured):
        result = call_correction(video, provider="claude-code", locality="local", api_key="sentinel-secret",
            environ={"CLAUDECODE": "1"}, which=lambda name: "claude-bin" if name == "claude" else None,
            subprocess_runner=runner)
    lines = captured.getvalue().splitlines()
    return (result == 0 and lines[0].startswith("AI selection: host=claude-code ")
            and "role=low-cost" in lines[0] and len(seen) == 1 and "AI selection:" in seen[0]
            and "sentinel-secret" not in captured.getvalue())


def check_selection_manifest_and_resume_identity_are_strict() -> bool:
    from tools import make_temp_folder
    folder = make_temp_folder("correction-selection-identity-")
    video = folder / "identity.mp4"
    video.write_bytes(b"video")
    work = folder / "identity.analysis"
    work.mkdir()
    (work / "transcript.json").write_text(json.dumps({"language": "en", "segments": [
        {"start": 0, "end": 1, "text": "original words"}]}), encoding="utf-8")
    client = lambda **kwargs: SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
        create=lambda **call: SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
            content="1\toriginal words", refusal=False))], usage=None))))
    first = call_correction(video, client_factory=client, api_key="fake", locality="local", environ={})
    unchanged = call_correction(video, client_factory=client, api_key="fake", locality="local", environ={})
    manifest_path = work / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    identity = manifest["params"]["correct_transcript"]["selection_identity"]
    strict = set(identity) == {"host", "provider", "model", "endpoint_identity", "locality"}
    changes_conflict = True
    for key in tuple(identity):
        old_value = identity[key]
        identity[key] = "changed"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        changes_conflict = changes_conflict and call_correction(
            video, client_factory=client, api_key="fake", locality="local", environ={}) == 3
        identity[key] = old_value
    del identity["host"]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    second = call_correction(video, client_factory=client, api_key="fake", locality="local", environ={})
    return first == 0 and unchanged == 0 and strict and changes_conflict and second == 3


def check_refusal_escalation_follows_family_table() -> bool:
    import correct_transcript as correction
    from host_models import model_sibling
    no_bigger = correction._escalation_choice("codex", "GLM-5.3-Flash", "glm-session-default")
    return (model_sibling("anthropic", "haiku", "bigger") == "sonnet"
            and model_sibling("glm", "GLM-5.3-Flash", "bigger") is None
            and model_sibling("anthropic", "sonnet", "bigger") == "sonnet"
            and no_bigger == correction.EscalationChoice("glm-session-default")
            and correction._escalation_choice("claude-code", "session-model", "session-model") is None
            and correction._escalation_choice("openclaw", "host default", None) is None)


def check_setup_reports_resolved_host_readiness_without_secrets() -> bool:
    import setup_tools
    from host_models import ConfigReader
    output = io.StringIO()
    fake_reader = ConfigReader(Path.cwd(), Path("/sentinel-setup-home"), lambda path: None)
    with patch.object(setup_tools.shutil, "which", side_effect=lambda name: "openclaw-bin" if name == "openclaw" else None), \
            patch.object(setup_tools, "resolve_host_model", side_effect=RuntimeError("safe resolver failure")), \
            patch.object(setup_tools.importlib, "import_module", side_effect=ImportError()), \
            contextlib.redirect_stdout(output):
        setup_tools.report_correction({"VIDEO_ANALYSIS_HOST": "openclaw", "OPENAI_API_KEY": "secret"}, fake_reader)
    rendered = output.getvalue()
    return ("OpenClaw: openclaw-bin" in rendered and "Resolver unavailable:" in rendered
            and "secret" not in rendered and str(Path.home()) not in rendered)


def check_analyze_video_forwards_explicit_host() -> bool:
    import analyze_video
    args = analyze_video._parser().parse_args(["sample.mp4", "--host", "openclaw"])
    _, forwarded, _ = analyze_video._step_args(args, "sample.mp4")
    return forwarded[forwarded.index("--host") + 1] == "openclaw"


def check_consent_eof_and_noninteractive_stdout_exit_4() -> bool:
    import correct_transcript as correction
    from tools import make_temp_folder
    folder = make_temp_folder("correction-consent-eof-")
    video = folder / "local.mp4"
    video.write_bytes(b"video")
    work = folder / "local.analysis"
    work.mkdir()
    (work / "manifest.json").write_text(json.dumps({"schema_version": 2, "source": {
        "kind": "local", "path": str(video.resolve()), "size": video.stat().st_size,
        "mtime_ns": video.stat().st_mtime_ns}, "params": {}, "stages": {}}), encoding="utf-8")
    (work / "transcript.json").write_text(json.dumps({"language": "en", "segments": [
        {"start": 0, "end": 1, "text": "hello there"}]}), encoding="utf-8")
    args = {"provider": "openai", "api_key": "fake", "locality": "remote",
            "environ": {"OPENAI_API_KEY": "fake"},
            "client_factory": lambda **_: (_ for _ in ()).throw(AssertionError("constructed before consent"))}
    with patch("sys.stdin.isatty", return_value=True), patch("sys.stdout.isatty", return_value=True), \
            patch("builtins.input", side_effect=EOFError), contextlib.redirect_stderr(io.StringIO()):
        eof = call_correction(video, **args)
    with patch("sys.stdin.isatty", return_value=True), patch("sys.stdout.isatty", return_value=False), \
            contextlib.redirect_stderr(io.StringIO()):
        noninteractive = call_correction(video, **args)
    return eof == 4 and noninteractive == 4


def check_v050_documentation_and_release_contract() -> bool:
    readme = (HERE.parents[1] / "README.md").read_text(encoding="utf-8")
    skill = (HERE.parent / "SKILL.md").read_text(encoding="utf-8")
    release = (HERE.parents[1] / "RELEASE_NOTES.md").read_text(encoding="utf-8")
    platform = (HERE.parents[1] / "docs" / "platform-plan.md").read_text(encoding="utf-8")
    docs = readme + skill
    commands = all(name in docs for name in ("prepare_sheet.py", "render_sheet.py"))
    outputs = all(name in docs for name in ("sheet.brief.md", "sheet.json", "<video stem>.docx"))
    sheet_flow = all(term in skill for term in ("current session default model", "never delegated",
        "most telling frame", "ON SCREEN", "SAID", "INFERENCE", "exit 3", "exit 4"))
    labels_language = all(term in docs for term in ("labels", "sheet language", "speech language"))
    release_contract = "v0.5.0" in release and "2026-11-27" in release
    platform_status = "steps 1–4 implemented" in platform or "steps 1-4 implemented" in platform
    pipeline_boundary = "stops at `bundle.json`" in docs or "stops at bundle.json" in docs
    count_contract = all(term in readme + release + (HERE.parents[1] / "docs" / "selftest.md").read_text(encoding="utf-8")
                         for term in ("DEFAULT_CHECKS=114", "FULL_CHECKS=116", "116 passed"))
    return all((commands, outputs, sheet_flow, labels_language, release_contract,
                platform_status, pipeline_boundary, count_contract))


def check_host_detection_and_provenance() -> bool:
    try:
        from host_models import ConfigReader, ExplicitInput, resolve_host_model
    except ImportError:
        return False
    reader = ConfigReader(Path("/project"), Path("/home"), lambda _: None)
    explicit = resolve_host_model({"VIDEO_ANALYSIS_HOST": "codex", "CLAUDECODE": "1"}, reader,
                                  ExplicitInput(host="openclaw"))
    detected = [resolve_host_model(env, reader, ExplicitInput()) for env in
                ({"VIDEO_ANALYSIS_HOST": "codex"}, {"CLAUDECODE": "1"}, {"CODEX_SESSION_ID": "x"}, {})]
    return (explicit.host == "openclaw" and [item.host for item in detected] ==
            ["codex", "claude-code", "codex", "unknown"]
            and "explicit" in dict(explicit.reasons).get("host", "").lower()
            and all(item.host != "openclaw" for item in detected))


def check_claude_model_resolution_precedence() -> bool:
    try:
        from host_models import ConfigReader, ExplicitInput, resolve_host_model
    except ImportError:
        return False
    cwd, home = Path("/project"), Path("/home")
    files = {cwd / ".claude/settings.local.json": '{"model":"local"}',
             cwd / ".claude/settings.json": '{"model":"project"}',
             home / ".claude/settings.json": '{"model":"user"}'}
    reader = ConfigReader(cwd, home, lambda path: files.get(path))
    marked = {"CLAUDECODE": "1"}
    local = resolve_host_model(marked, reader, ExplicitInput())
    project_reader = ConfigReader(cwd, home,
                                  lambda path: None if path == cwd / ".claude/settings.local.json" else files.get(path))
    project = resolve_host_model(marked, project_reader, ExplicitInput())
    user_reader = ConfigReader(cwd, home,
                               lambda path: files.get(path) if path == home / ".claude/settings.json" else None)
    user = resolve_host_model(marked, user_reader, ExplicitInput())
    env = resolve_host_model({**marked, "ANTHROPIC_MODEL": "env"}, reader, ExplicitInput())
    cheap_env = resolve_host_model({**marked, "ANTHROPIC_MODEL": "x", "ANTHROPIC_DEFAULT_HAIKU_MODEL": "cheap"}, reader, ExplicitInput())
    cheap_old = resolve_host_model({**marked, "ANTHROPIC_MODEL": "x", "ANTHROPIC_SMALL_FAST_MODEL": "old"}, reader, ExplicitInput())
    native = resolve_host_model({**marked, "ANTHROPIC_MODEL": "claude-sonnet-4"}, reader, ExplicitInput())
    native_without_model = resolve_host_model(marked, ConfigReader(cwd, home, lambda _: None), ExplicitInput())
    native_defaults = [(model, resolve_host_model(
        {**marked, **({"ANTHROPIC_MODEL": model} if model is not None else {})},
        reader if model is not None else ConfigReader(cwd, home, lambda _: None), ExplicitInput()))
        for model in ("opus[1m]", "sonnet", "default", None, "sonnet[1m]", "opus", "opusplan",
                      "claude-sonnet-4", "claude-opus-4")]
    haiku_defaults = [(model, resolve_host_model({**marked, "ANTHROPIC_MODEL": model}, reader, ExplicitInput()))
                      for model in ("haiku", "claude-haiku-4")]
    custom_glm = resolve_host_model({**marked, "ANTHROPIC_MODEL": "glm-5.3",
                                     "ANTHROPIC_BASE_URL": "https://custom.invalid"}, reader, ExplicitInput())
    custom = resolve_host_model({**marked, "ANTHROPIC_MODEL": "vendor-special-model",
                                 "ANTHROPIC_BASE_URL": "https://custom.invalid"}, reader, ExplicitInput())
    empty = resolve_host_model(marked, ConfigReader(cwd, home, lambda _: None), ExplicitInput())
    return (local.default_model == "local" and project.default_model == "project"
            and user.default_model == "user" and env.default_model == "env"
            and cheap_env.low_cost_model == "cheap" and cheap_old.low_cost_model == "old"
            and native.low_cost_model == "haiku" and custom.low_cost_model is None
            and native_without_model.low_cost_model == "haiku"
            and all(item.low_cost_model == "haiku" for _, item in native_defaults)
            and all(item.low_cost_model == model and
                    dict(item.reasons).get("low_cost_model") == "configured model already has the low-cost role"
                    for model, item in haiku_defaults)
            and custom_glm.low_cost_model == "GLM-5.3-Flash"
            and empty.default_model is None)


def check_codex_model_resolution_precedence() -> bool:
    try:
        from host_models import ConfigReader, ExplicitInput, resolve_host_model
    except ImportError:
        return False
    home = Path("/codex-home")
    base = ('profile="work"\n[model_providers.test]\nbase_url="https://u:secret@example.invalid/v1?key=x"\n'
            '[profiles.work]\nmodel="gpt-6-luna"\nmodel_provider="test"\n')
    overlay = 'model="deepseek-v4-pro"\noss_provider="ollama"\n'
    files = {home / "config.toml": base, home / "work.config.toml": overlay}
    reader = ConfigReader(Path("/project"), Path("/home"), lambda path: files.get(path))
    result = resolve_host_model({"CODEX_SESSION_ID": "x", "CODEX_HOME": str(home)}, reader, ExplicitInput())
    reason = dict(result.reasons).get("default_model", "").lower()
    endpoint_config = ('model="gpt-6-luna"\nmodel_provider="test"\n'
                       '[model_providers.test]\nbase_url="https://u:secret@example.invalid/v1?key=x"\n')
    url_result = resolve_host_model({"CODEX_SESSION_ID": "x", "CODEX_HOME": str(home)},
                                    ConfigReader(Path("/project"), Path("/home"),
                                                 lambda path: endpoint_config if path == home / "config.toml" else None),
                                    ExplicitInput())
    default_home = Path("/home/.codex")
    home_config = 'model="gpt-6-luna"\nmodel_provider="test"\n'
    default_result = resolve_host_model({"CODEX_SESSION_ID": "x"},
                                        ConfigReader(Path("/project"), Path("/home"),
                                                     lambda path: home_config if path == default_home / "config.toml" else None),
                                        ExplicitInput())
    custom_provider = ('model="deepseek-v4-pro"\nmodel_provider="openrouter"\n')
    remote_model = resolve_host_model({"CODEX_SESSION_ID": "x", "CODEX_HOME": str(home)},
                                      ConfigReader(Path("/project"), Path("/home"),
                                                   lambda path: custom_provider if path == home / "config.toml" else None),
                                      ExplicitInput())
    return (result.default_model == "deepseek-v4-pro" and result.provider == "openai"
            and result.locality == "remote" and result.endpoint_identity == "provider:openai"
            and remote_model.provider == "openrouter" and remote_model.locality == "remote"
            and remote_model.low_cost_model == "deepseek-flash"
            and url_result.endpoint_identity == "https://example.invalid/v1"
            and default_result.default_model == "gpt-6-luna"
            and all(flag in reason for flag in ("configured default", "--oss", "-m", "--profile"))
            and "secret" not in repr(result))


def check_openclaw_json5_resolution() -> bool:
    try:
        from host_models import ConfigReader, ExplicitInput, resolve_host_model
    except ImportError:
        return False
    cwd, state = Path("/project"), Path("/state")
    path = cwd / "openclaw.json"
    document = """{ // line comment\n /* block comment */ agents: { defaults: { model: { primary: 'anthropic/claude-sonnet-4', }, }, },
 providers: { anthropic: { baseUrl: 'https://u:secret@example.invalid/v1?token=x#frag', }, }, note: 'literal // marker,}', }"""
    home_path = Path("/home/.openclaw/openclaw.json")
    state_path = state / "openclaw.json"
    alternate = '{agents:{defaults:{model:{primary:"qwen/qwen3-max"}}}}'
    files = {path: document, state_path: alternate, home_path: alternate}
    reader = ConfigReader(cwd, Path("/home"), lambda candidate: files.get(candidate))
    env = {"OPENCLAW_CONFIG_PATH": str(path), "OPENCLAW_STATE_DIR": str(state)}
    result = resolve_host_model(env, reader, ExplicitInput(host="openclaw"))
    state_result = resolve_host_model({"OPENCLAW_STATE_DIR": str(state)}, reader,
                                      ExplicitInput(host="openclaw"))
    home_result = resolve_host_model({}, reader, ExplicitInput(host="openclaw"))
    malformed = resolve_host_model(env, ConfigReader(cwd, Path("/home"), lambda _: "{broken"),
                                    ExplicitInput(host="openclaw"))
    empty = resolve_host_model(env, ConfigReader(cwd, Path("/home"), lambda _: ""),
                               ExplicitInput(host="openclaw"))
    absent = resolve_host_model(env, ConfigReader(cwd, Path("/home"), lambda _: None),
                                ExplicitInput(host="openclaw"))
    missing = resolve_host_model({}, ConfigReader(cwd, Path("/home"), lambda _: "{}"),
                                 ExplicitInput(host="openclaw"))
    return (result.default_model == "anthropic/claude-sonnet-4" and result.provider == "anthropic"
            and result.endpoint_identity == "https://example.invalid/v1"
            and state_result.default_model == "qwen/qwen3-max"
            and home_result.default_model == "qwen/qwen3-max"
            and malformed.default_model is None
            and dict(malformed.reasons).get("default_model") == "OpenClaw config was malformed"
            and empty.default_model is None and absent.default_model is None
            and dict(empty.reasons).get("default_model") == "no OpenClaw config found"
            and dict(absent.reasons).get("default_model") == "no OpenClaw config found"
            and missing.default_model is None
            and "secret" not in repr(result))


def check_model_family_siblings() -> bool:
    try:
        from host_models import ConfigReader, ExplicitInput, LOCAL_PROVIDERS, MODEL_FAMILIES, resolve_host_model
    except ImportError:
        return False
    examples = (("anthropic", "claude-sonnet-4", "haiku"), ("openai", "gpt-6-luna", "gpt-6-luna"),
                ("glm", "glm-5.3-air", "GLM-5.3-Flash"), ("deepseek", "deepseek-v4-pro", "deepseek-flash"),
                ("qwen", "qwen3-max", "qwen-flash"), ("mistral", "mistral-large-latest", "mistral-small-latest"),
                ("gemini", "gemini-3.5-pro", "gemini-3.5-flash-lite"), ("kimi", "kimi-k2", None),
                ("ollama", "custom", None))
    results = []
    for provider, model, _ in examples:
        body = json.dumps({"agents": {"defaults": {"model": {"primary": f"{provider}/{model}"}}}})
        target = Path("/fixture/openclaw.json")
        reader = ConfigReader(Path("/project"), Path("/home"), lambda path, body=body: body if path == target else None)
        results.append(resolve_host_model({"OPENCLAW_CONFIG_PATH": str(target)}, reader,
                                          ExplicitInput(host="openclaw")))
    expected = [f"{provider}/{cheap}" if cheap else None for provider, _, cheap in examples]
    bigger = [family.bigger for family in MODEL_FAMILIES]
    codex_home = Path("/fixture/codex")
    codex_config = 'model="deepseek-v4-pro"\nmodel_provider="openrouter"\n'
    foreign_provider = resolve_host_model({"CODEX_SESSION_ID": "x", "CODEX_HOME": str(codex_home)},
        ConfigReader(Path("/project"), Path("/home"), lambda path: codex_config
                     if path == codex_home / "config.toml" else None), ExplicitInput())
    return (len(MODEL_FAMILIES) == 8 and bigger == ["sonnet", "gpt-5.6-sol", None,
            "deepseek-v4-pro", None, None, None, None]
            and LOCAL_PROVIDERS == {"ollama", "lmstudio", "local"}
            and not hasattr(ExplicitInput, "provider")
            and [item.low_cost_model for item in results] == expected
            and all(item.default_model == f"{provider}/{model}"
                    for item, (provider, model, _) in zip(results, examples))
            and results[1].low_cost_model == results[1].default_model
            and foreign_provider.low_cost_model == "deepseek-flash")


def check_locality_evidence() -> bool:
    try:
        from host_models import ConfigReader, ExplicitInput, resolve_host_model
    except ImportError:
        return False
    cases = (("custom", "http://127.0.0.1:11434/v1", {}, "remote"), ("ollama", None, {}, "local"),
             ("custom", None, {"VIDEO_ANALYSIS_AI_LOCALITY": "local"}, "local"),
             ("custom", None, {"VIDEO_ANALYSIS_HOST_AI": "local"}, "remote"),
             ("custom", None, {"VIDEO_ANALYSIS_AI_LOCALITY": "nearby"}, "remote"),
             ("custom", None, {"VIDEO_ANALYSIS_AI_LOCALITY": "remote"}, "remote"))
    for index, (provider, endpoint, env, _) in enumerate(cases):
        body = {"agents": {"defaults": {"model": {"primary": f"{provider}/model"}}}}
        if endpoint:
            body["providers"] = {provider: {"baseUrl": endpoint}}
        target = Path(f"/fixture/{index}.json")
        source = json.dumps(body)
        config = ConfigReader(Path("/project"), Path("/home"),
                              lambda path, target=target, source=source: source if path == target else None)
        cases_result = resolve_host_model({**env, "OPENCLAW_CONFIG_PATH": str(target)}, config,
                                          ExplicitInput(host="openclaw"))
        if cases_result.locality != cases[index][3]:
            return False
    return True


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


def check_setup_reports_correction_readiness_without_secrets() -> bool:
    import setup_tools
    from host_models import ConfigReader
    fake = SimpleNamespace(__version__="1.2.3")
    reader = ConfigReader(Path.cwd(), Path("/sentinel-setup-home"), lambda path: None)
    present_out, missing_out, host_out, codex_out, local_out = (io.StringIO(), io.StringIO(), io.StringIO(),
                                                                io.StringIO(), io.StringIO())
    with patch.dict(__import__("os").environ, {"OPENAI_API_KEY": "sentinel-secret"}, clear=True), \
            patch.object(setup_tools.importlib, "import_module", return_value=fake), \
            patch.object(setup_tools.shutil, "which", return_value=None), \
            contextlib.redirect_stdout(present_out):
        present = setup_tools.report_correction({"OPENAI_API_KEY": "sentinel-secret"}, reader)
    with patch.dict(__import__("os").environ, {}, clear=True), \
            patch.object(setup_tools.importlib, "import_module", return_value=fake), \
            patch.object(setup_tools.shutil, "which", return_value=None), \
            contextlib.redirect_stdout(missing_out):
        missing = setup_tools.report_correction({}, reader)
    with patch.dict(__import__("os").environ, {"CLAUDECODE": "1"}, clear=True), \
            patch.object(setup_tools.importlib, "import_module", return_value=fake), \
            patch.object(setup_tools.shutil, "which", side_effect=lambda name: "claude.exe" if name == "claude" else None), \
            contextlib.redirect_stdout(host_out):
        claude = setup_tools.report_correction({"CLAUDECODE": "1"}, reader)
    with patch.dict(__import__("os").environ, {"CODEX_SESSION_ID": "x"}, clear=True), \
            patch.object(setup_tools.importlib, "import_module", return_value=fake), \
            patch.object(setup_tools.shutil, "which", side_effect=lambda name: "codex.cmd" if name == "codex" else None), \
            contextlib.redirect_stdout(codex_out):
        codex = setup_tools.report_correction({"CODEX_SESSION_ID": "x"}, reader)
    with patch.dict(__import__("os").environ, {"VIDEO_ANALYSIS_CORRECT_PROVIDER": "local",
            "OPENAI_BASE_URL": "http://127.0.0.1/v1", "VIDEO_ANALYSIS_AI_LOCALITY": "local"}, clear=True), \
            patch.object(setup_tools.importlib, "import_module", return_value=fake), \
            patch.object(setup_tools.shutil, "which", return_value=None), \
            contextlib.redirect_stdout(local_out):
        local = setup_tools.report_correction({"VIDEO_ANALYSIS_CORRECT_PROVIDER": "local",
            "OPENAI_BASE_URL": "http://127.0.0.1/v1", "VIDEO_ANALYSIS_AI_LOCALITY": "local"}, reader)
    absent = setup_tools.missing_packages(module_lookup=lambda module: (_ for _ in ()).throw(ImportError())
                                          if module == "openai" else object())
    output = present_out.getvalue() + missing_out.getvalue()
    readiness = output + host_out.getvalue() + codex_out.getvalue() + local_out.getvalue()
    conditions = [present, missing, claude, codex, local, "OpenAI: openai 1.2.3" in output
            and "API key present" in present_out.getvalue()
            and "Correction auto provider: openai" in present_out.getvalue()
            and "Correction auto provider: none" in missing_out.getvalue()
            and "API key missing" in missing_out.getvalue()
            and "Claude Code: claude.exe" in host_out.getvalue()
            and "Correction auto provider: claude-code" in host_out.getvalue()
            and "Codex: codex.cmd" in codex_out.getvalue()
            and "Correction auto provider: codex" in codex_out.getvalue()
            and "Correction auto provider: local" in local_out.getvalue()
            and "Host providers use the host's authenticated account" in readiness
            and "sentinel-secret" not in readiness and "openai" in absent]
    return all(conditions)


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
        checks["sheet facts are deterministic sourced and omit missing values"] = run_check(
            "sheet facts are deterministic sourced and omit missing values", check_sheet_facts)
        checks["sheet accounting combines measured correction and estimated writing"] = run_check(
            "sheet accounting combines measured correction and estimated writing", check_sheet_accounting)
        checks["sheet accounting marks incomplete or unknown USD as n/a"] = run_check(
            "sheet accounting marks incomplete or unknown USD as n/a", check_sheet_accounting_incomplete)
        checks["comments fetch is bounded private and independently resumable"] = run_check(
            "comments fetch is bounded private and independently resumable", check_comments_fetch)
        checks["comments disable and unavailable states are explicit"] = run_check(
            "comments disable and unavailable states are explicit", check_comments_states)
        checks["analyze_video forwards bounded comments"] = run_check(
            "analyze_video forwards bounded comments", check_analyze_video_comments)
        checks["sheet schema accepts strict v2 and rejects unknown nested fields"] = run_check(
            "sheet schema accepts strict v2 and rejects unknown nested fields", check_sheet_schema_valid)
        checks["sheet schema enforces duration proportional summary paragraphs"] = run_check(
            "sheet schema enforces duration proportional summary paragraphs", check_sheet_v2_duration_summary)
        checks["sheet schema validates blocks frames references and callout sources"] = run_check(
            "sheet schema validates blocks frames references and callout sources", check_sheet_v2_blocks_references)
        checks["sheet schema enforces context evidence safe URLs and callout budget"] = run_check(
            "sheet schema enforces context evidence safe URLs and callout budget", check_sheet_v2_context_budget)
        checks["sheet brief embeds facts and the complete editorial contract"] = run_check(
            "sheet brief embeds facts and the complete editorial contract", check_sheet_prepare_brief_contract)
        checks["sheet preparation tracks schema facts comments and brief identity"] = run_check(
            "sheet preparation tracks schema facts comments and brief identity", check_prepare_v2_identity)
        checks["renderer refuses schema v1 with the v0.6.0 regeneration message"] = run_check(
            "renderer refuses schema v1 with the v0.6.0 regeneration message", check_renderer_v1_refusal)
        checks["default DOCX renders facts accounting context blocks callouts and references"] = run_check(
            "default DOCX renders facts accounting context blocks callouts and references", check_default_docx_render)
        checks["DOCX v2 hyperlinks borders shading and image widths are correct"] = run_check(
            "DOCX v2 hyperlinks borders shading and image widths are correct", check_docx_v2_layout)
        checks["DOCX templates preserve styles headers footers and section properties"] = run_check(
            "DOCX templates preserve styles headers footers and section properties", check_docx_template_preservation)
        checks["render resume identity includes recomputed facts accounting and brief"] = run_check(
            "render resume identity includes recomputed facts accounting and brief", check_render_v2_identity)
        checks["render never trusts model authored facts or accounting"] = run_check(
            "render never trusts model authored facts or accounting", check_sheet_model_facts_rejected)
        checks["sheet schema accepts valid v2"] = run_check(
            "sheet schema accepts valid v2", check_sheet_schema_valid)
        checks["sheet schema rejects missing and unknown fields with named errors"] = run_check(
            "sheet schema rejects missing and unknown fields with named errors", check_sheet_schema_required_unknown_labels)
        checks["sheet schema rejects wrong types and empty required text"] = run_check(
            "sheet schema rejects wrong types and empty required text", check_sheet_schema_types_text)
        checks["sheet schema rejects invalid chapter intervals"] = run_check(
            "sheet schema rejects invalid chapter intervals", check_sheet_schema_intervals)
        checks["sheet schema rejects unordered and overlapping chapters"] = run_check(
            "sheet schema rejects unordered and overlapping chapters", check_sheet_schema_ordering)
        checks["sheet schema rejects frame time outside its chapter"] = run_check(
            "sheet schema rejects frame time outside its chapter", check_sheet_schema_frame_time)
        checks["sheet schema blocks absolute drive traversal and non-normal frame paths"] = run_check(
            "sheet schema blocks absolute drive traversal and non-normal frame paths", check_sheet_schema_frame_paths)
        checks["sheet schema rejects missing frame files"] = run_check(
            "sheet schema rejects missing frame files", check_sheet_schema_missing_frame)
        checks["sheet schema rejects stale bundle digest"] = run_check(
            "sheet schema rejects stale bundle digest", check_sheet_schema_digest)
        checks["sheet schema rejects writer identity mismatch"] = run_check(
            "sheet schema rejects writer identity mismatch", check_sheet_schema_writer)
        checks["seek links cover YouTube Vimeo and unsupported sources"] = run_check(
            "seek links cover YouTube Vimeo and unsupported sources", check_sheet_seek_links)
        checks["sheet preparation uses host default model provenance"] = run_check(
            "sheet preparation uses host default model provenance", check_sheet_prepare_default_provenance)
        checks["sheet preparation consent matrix enforces write boundaries"] = run_check(
            "sheet preparation consent matrix enforces write boundaries", check_sheet_prepare_consent_matrix)
        checks["sheet brief contains exact schema and writer values"] = run_check(
            "sheet brief contains exact schema and writer values", check_sheet_prepare_brief_contract)
        checks["sheet preparation resumes and conflicts safely"] = run_check(
            "sheet preparation resumes and conflicts safely", check_sheet_prepare_resume_conflicts)
        checks["default DOCX render has ordered content images and properties"] = run_check(
            "default DOCX render has ordered content images and properties", check_default_docx_render)
        checks["sheet source URL must match bundle and be a safe web link"] = run_check(
            "sheet source URL must match bundle and be a safe web link", check_sheet_source_url_security)
        checks["DOCX template reuses styles headers footers and section properties"] = run_check(
            "DOCX template reuses styles headers footers and section properties", check_docx_template_preservation)
        checks["DOTX template renders as document content type"] = run_check(
            "DOTX template renders as document content type", check_dotx_template)
        checks["missing template styles fall back to Normal"] = run_check(
            "missing template styles fall back to Normal", check_missing_styles_fallback)
        checks["sheet render rerun preserves user-edited DOCX"] = run_check(
            "sheet render rerun preserves user-edited DOCX", check_render_resume_preserves_docx)
        checks["changed sheet refuses and leaves DOCX untouched"] = run_check(
            "changed sheet refuses and leaves DOCX untouched", check_changed_sheet_refuses_docx)
        checks["untracked DOCX is refused"] = run_check(
            "untracked DOCX is refused", check_untracked_docx_refused)
        checks["sheet render requires successful preparation"] = run_check(
            "sheet render requires successful preparation", check_render_requires_preparation)
        checks["sheet render rejects changed bundle digest"] = run_check(
            "sheet render rejects changed bundle digest", check_render_rejects_changed_bundle)
        checks["setup maps docx import to python-docx"] = run_check(
            "setup maps docx import to python-docx", check_setup_docx_dependency)
        checks["ffmpeg resolution precedence"] = run_check("ffmpeg resolution precedence", check_tool_resolution)
        checks["host resolver detects explicit and marked hosts with provenance"] = run_check(
            "host resolver detects explicit and marked hosts with provenance", check_host_detection_and_provenance)
        checks["Claude model resolution honors env and settings precedence"] = run_check(
            "Claude model resolution honors env and settings precedence", check_claude_model_resolution_precedence)
        checks["Codex model resolution honors CODEX_HOME profile and provider"] = run_check(
            "Codex model resolution honors CODEX_HOME profile and provider", check_codex_model_resolution_precedence)
        checks["OpenClaw model resolution tolerates JSON5 and fails closed"] = run_check(
            "OpenClaw model resolution tolerates JSON5 and fails closed", check_openclaw_json5_resolution)
        checks["family table selects cheap and bigger siblings"] = run_check(
            "family table selects cheap and bigger siblings", check_model_family_siblings)
        checks["locality requires provider evidence or explicit declaration"] = run_check(
            "locality requires provider evidence or explicit declaration", check_locality_evidence)
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
        checks["setup reports correction readiness without secrets"] = run_check(
            "setup reports correction readiness without secrets", check_setup_reports_correction_readiness_without_secrets)
        checks["OCR uses bounded ONNX threads and reports progress"] = run_check(
            "OCR uses bounded ONNX threads and reports progress", check_ocr_thread_tuning_and_progress)
        checks["OCR runtime thread count does not invalidate bundle resume"] = run_check(
            "OCR runtime thread count does not invalidate bundle resume", check_ocr_runtime_setting_not_in_resume_key)
        checks["fetch progress is bounded by output mode"] = run_check(
            "fetch progress is bounded by output mode", check_fetch_progress_policy)
        checks["bundle works without fetch outputs"] = run_check("bundle works without fetch outputs", check_bundle_without_fetch)
        checks["bundle CLI handles SponsorBlock segments"] = run_check(
            "bundle CLI handles SponsorBlock segments", check_bundle_cli_sponsorblock_segments)
        checks["bundle with fake fetch outputs prefers manual subtitles"] = run_check(
            "bundle with fake fetch outputs prefers manual subtitles", check_bundle_manual_subtitle)
        checks["bundle uses matching corrected speech provenance"] = run_check(
            "bundle uses matching corrected speech provenance", check_bundle_corrected_speech_provenance)
        checks["bundle ignores stale corrected speech"] = run_check(
            "bundle ignores stale corrected speech", check_bundle_ignores_stale_corrected_speech)
        checks["bundle resume keys selected speech input"] = run_check(
            "bundle resume keys selected speech input", check_bundle_resume_keys_selected_speech_input)
        checks["extract preserves bundle manifest params"] = run_check(
            "extract preserves bundle manifest params", check_extract_preserves_bundle_params)
        checks["manifest records local and URL source kinds"] = run_check(
            "manifest records local and URL source kinds", check_manifest_source_kinds)
        checks["extract preserves correction manifest state"] = run_check(
            "extract preserves correction manifest state", check_extract_preserves_correction_state)
        checks["correction contract parses exact numbered lines"] = run_check(
            "correction contract parses exact numbered lines", check_correction_exact_numbered_lines)
        checks["correction guard rejects paraphrase and omission"] = run_check(
            "correction guard rejects paraphrase and omission", check_correction_guards)
        checks["correction refusal and malformed chunks keep originals"] = run_check(
            "correction refusal and malformed chunks keep originals", check_correction_safe_fallbacks)
        checks["correction DRY shared source and speech helpers"] = run_check(
            "correction DRY shared source and speech helpers", check_correction_shared_helpers_and_speech_reader)
        checks["correction runtime helpers are shared"] = run_check(
            "correction runtime helpers are shared", check_correction_shared_runtime_helpers)
        checks["correction consent matrix blocks before provider call"] = run_check(
            "correction consent matrix blocks before provider call", check_correction_consent_matrix_blocks_before_provider_call)
        checks["correction skip modes stay offline"] = run_check(
            "correction skip modes stay offline", check_correction_skip_modes_stay_offline)
        checks["manual subtitles skip correction unchanged"] = run_check(
            "manual subtitles skip correction unchanged", check_manual_subtitles_skip_correction_unchanged)
        checks["correction resume and conflict are safe"] = run_check(
            "correction resume and conflict are safe", check_correction_resume_and_conflict_are_safe)
        checks["correction fake client writes validated output"] = run_check(
            "correction fake client writes validated output", check_correction_fake_client_writes_validated_output)
        checks["correction calls are bounded and usage is recorded"] = run_check(
            "correction calls are bounded and usage is recorded", check_correction_calls_bounded_and_usage_recorded)
        checks["correction provider errors fall back per chunk"] = run_check(
            "correction provider errors fall back per chunk", check_correction_provider_errors_fall_back_per_chunk)
        checks["correction local endpoint runs without API key"] = run_check(
            "correction local endpoint runs without API key", check_local_endpoint_without_api_key_uses_placeholder)
        checks["correction provider selection matrix"] = run_check(
            "correction provider selection matrix", check_provider_selection_matrix)
        checks["correction CLI adapters are offline and account usage"] = run_check(
            "correction CLI adapters are offline and account usage", check_cli_adapters_are_offline_and_account_usage)
        checks["correction prose refusals are retryable"] = run_check(
            "correction prose refusals are retryable", check_prose_refusals_are_retryable)
        checks["correction retry sends only refused chunks"] = run_check(
            "correction retry sends only refused chunks", check_retry_sends_only_refused_chunks)
        checks["host adapters preserve routing and optional model flags"] = run_check(
            "host adapters preserve routing and optional model flags", check_host_adapters_preserve_routing_and_optional_model_flags)
        checks["low-cost first chunk fallback is bounded and selective"] = run_check(
            "low-cost first chunk fallback is bounded and selective", check_low_cost_first_chunk_fallback_is_bounded_and_selective)
        checks["selection notice precedes calls and omits secrets"] = run_check(
            "selection notice precedes calls and omits secrets", check_selection_notice_precedes_calls_and_omits_secrets)
        checks["selection manifest and resume identity are strict"] = run_check(
            "selection manifest and resume identity are strict", check_selection_manifest_and_resume_identity_are_strict)
        checks["refusal escalation follows family table"] = run_check(
            "refusal escalation follows family table", check_refusal_escalation_follows_family_table)
        checks["setup reports resolved host readiness without secrets"] = run_check(
            "setup reports resolved host readiness without secrets", check_setup_reports_resolved_host_readiness_without_secrets)
        checks["analyze_video forwards explicit host"] = run_check(
            "analyze_video forwards explicit host", check_analyze_video_forwards_explicit_host)
        checks["correction consent EOF and noninteractive stdout exit 4"] = run_check(
            "correction consent EOF and noninteractive stdout exit 4", check_consent_eof_and_noninteractive_stdout_exit_4)
        checks["v0.5.0 documentation release and measured-count contract"] = run_check(
            "v0.5.0 documentation release and measured-count contract",
            check_v050_documentation_and_release_contract)
        checks["analyze_video correction order and flags"] = run_check(
            "analyze_video correction order and flags", check_analyze_video_correction_order_and_flags)
        checks["analyze_video stops on failure"] = run_check(
            "analyze_video stops on failure", check_analyze_video_stops_on_failure)
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
