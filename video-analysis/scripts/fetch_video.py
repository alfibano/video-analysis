"""Fetch one online video and prepare its .analysis hand-off folder."""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path
from typing import Any, Callable

from metadata import normalize_metadata
from tools import resolve_ffmpeg, resolve_js_runtime

SPONSOR_CATEGORIES = ["sponsor", "selfpromo", "interaction", "intro", "outro", "preview",
                      "music_offtopic", "filler"]
SUBTITLE_SUFFIXES = {".vtt", ".srt", ".ass", ".lrc", ".json3", ".ttml"}


class UnsupportedVideo(Exception):
    """The URL resolves to a playlist or a live broadcast."""


def refuse_unsupported(info: dict[str, Any]) -> None:
    if info.get("_type") in {"playlist", "multi_video"} or "entries" in info:
        raise UnsupportedVideo("playlists and multi-video URLs are not supported")
    if info.get("is_live") or info.get("live_status") in {"is_live", "is_upcoming"}:
        raise UnsupportedVideo("live and upcoming broadcasts are not supported")


def build_ydl_options(dest: Path, info: dict[str, Any] | None = None,
                      sub_langs: list[str] | None = None) -> dict[str, Any]:
    languages = sub_langs
    if languages is None:
        language = (info or {}).get("language") or (info or {}).get("language_code")
        languages = [language] if language else []
        if "en" not in languages:
            languages.append("en")
    ffmpeg = resolve_ffmpeg()
    js_runtime = resolve_js_runtime()
    options: dict[str, Any] = {
        "noplaylist": True,
        "restrictfilenames": True,
        "outtmpl": str(dest / "%(title).120B_%(id)s.%(ext)s"),
        "merge_output_format": "mp4",
        "writesubtitles": True,
        "writeautomaticsub": True,
        "subtitleslangs": languages,
        "quiet": True,
        "no_warnings": True,
    }
    # yt-dlp's Python API needs the postprocessor; the similarly named CLI
    # options are ignored here. Only request chapters for the download pass.
    if info is not None:
        options["postprocessors"] = [{
            "key": "SponsorBlock",
            "categories": SPONSOR_CATEGORIES,
            "when": "after_filter",
        }]
    if ffmpeg is not None:
        options["ffmpeg_location"] = ffmpeg.path
    if js_runtime is not None:
        options["js_runtimes"] = {Path(js_runtime.path).stem: {"path": js_runtime.path}}
    return options


def _youtube_dl_factory() -> Callable[[dict[str, Any]], Any]:
    import yt_dlp
    return yt_dlp.YoutubeDL


def download_video(url: str, ydl: Any, *, download: bool = True) -> dict[str, Any]:
    """Extract video info, downloading by default and allowing metadata preflight."""
    return ydl.extract_info(url, download=download)


def _video_path(info: dict[str, Any], ydl: Any) -> Path:
    downloads = info.get("requested_downloads") or []
    filepath = downloads[0].get("filepath") if downloads and isinstance(downloads[0], dict) else None
    filepath = filepath or info.get("filepath")
    if filepath:
        return Path(filepath).resolve()
    return Path(ydl.prepare_filename(info)).resolve()


def _bounded_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)


def write_context(video: Path, info: dict[str, Any], ydl: Any, fetch_params: dict[str, Any]) -> Path:
    work = video.parent / f"{video.stem}.analysis"
    work.mkdir(exist_ok=True)
    sanitized = ydl.sanitize_info(info)
    (work / "metadata.json").write_text(_bounded_json(normalize_metadata(info)), encoding="utf-8")
    (work / "raw_info.json").write_text(_bounded_json(sanitized), encoding="utf-8")
    (work / "sponsorblock.json").write_text(
        _bounded_json(info.get("sponsorblock_chapters") or []), encoding="utf-8")

    subtitles = info.get("requested_subtitles") or {}
    for subtitle in subtitles.values():
        source = subtitle.get("filepath") if isinstance(subtitle, dict) else None
        if source:
            source_path = Path(source)
            if source_path.is_file() and source_path.resolve() != (work / source_path.name).resolve():
                shutil.move(str(source_path), str(work / source_path.name))
    # Also collect sidecar captions produced by versions that omit requested_subtitles paths.
    for source_path in video.parent.glob(f"{video.stem}*.??*"):
        if source_path.suffix.lower() in SUBTITLE_SUFFIXES and source_path.parent != work:
            target = work / source_path.name
            if not target.exists():
                shutil.move(str(source_path), str(target))

    stat = video.stat()
    identity = {"path": str(video.resolve()), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}
    manifest = {"schema_version": 2, "source": identity,
                "params": {"fetch": fetch_params, "extract": {}},
                "stages": {"fetch": {"status": "done"}}}
    (work / "manifest.json").write_text(_bounded_json(manifest), encoding="utf-8")
    return work


def main(argv: list[str] | None = None, *, ydl_factory: Callable[[dict[str, Any]], Any] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Download one video and prepare analysis metadata.")
    parser.add_argument("url")
    parser.add_argument("--dest", type=Path, default=Path.cwd())
    parser.add_argument("--sub-langs", nargs="+", help="subtitle languages (default: video language and en)")
    try:
        args = parser.parse_args(argv)
    except SystemExit as error:
        return 0 if error.code == 0 else 2
    try:
        args.dest.mkdir(parents=True, exist_ok=True)
        preflight_options = build_ydl_options(args.dest.resolve(), sub_langs=args.sub_langs)
        factory = ydl_factory or _youtube_dl_factory()
        with factory(preflight_options) as ydl:
            preflight_info = download_video(args.url, ydl, download=False)
        refuse_unsupported(preflight_info)
        options = build_ydl_options(args.dest.resolve(), preflight_info, args.sub_langs)
        with factory(options) as ydl:
            info = download_video(args.url, ydl)
            video = _video_path(info, ydl)
            fetch_params = {"url": args.url, "sub_langs": options["subtitleslangs"]}
            work = write_context(video, info, ydl, fetch_params)
        print(f"FETCHED: {video}")
        print(f"WORK FOLDER: {work}")
        return 0
    except UnsupportedVideo as error:
        print(f"REFUSED: {error}")
        return 3
    except ImportError as error:
        print(f"yt-dlp is unavailable; run setup_tools.py: {error}")
        return 1
    except Exception as error:
        print(f"FAILED: {error}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
