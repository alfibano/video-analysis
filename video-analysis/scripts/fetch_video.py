"""Fetch one online video and prepare its .analysis hand-off folder."""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from urllib.parse import parse_qs, urlparse
import unicodedata
from pathlib import Path
from typing import Any, Callable, Mapping

from metadata import normalize_metadata
from progress import ProgressWriter
from tools import resolve_ffmpeg, resolve_js_runtime

SPONSOR_CATEGORIES = ["sponsor", "selfpromo", "interaction", "intro", "outro", "preview",
                      "music_offtopic", "filler"]
SUBTITLE_SUFFIXES = {".vtt", ".srt", ".ass", ".lrc", ".json3", ".ttml"}
MAX_COMMENTS = 100


def safe_video_stem(title: str, video_id: str) -> str:
    """Create a short ASCII filename stem while preserving leading title words."""
    safe_id = re.sub(r"[^A-Za-z0-9]", "_", str(video_id))
    folded = unicodedata.normalize("NFKD", str(title)).encode("ascii", "ignore").decode("ascii")
    title_part = re.sub(r"[^a-z0-9]+", "_", folded.lower()).strip("_")
    if not title_part:
        title_part = "video"
    maximum = min(40, 60 - 1 - len(safe_id))
    if len(title_part) > maximum:
        prefix = title_part[:maximum]
        boundary = prefix.rfind("_")
        title_part = prefix[:boundary] if boundary > 0 else prefix
    if not title_part:
        title_part = "video"
    return f"{title_part}_{safe_id}"


class UnsupportedVideo(Exception):
    """The URL resolves to a playlist or a live broadcast."""


def make_progress_hook(*, stream: Any = None, monotonic: Callable[[], float] | None = None
                       ) -> Callable[[dict[str, Any]], None]:
    """Create a bounded yt-dlp progress hook for percentage or unknown-size downloads."""
    progress = ProgressWriter(stream, monotonic)
    latest_bytes = 0
    total_bytes: int | None = None
    current_filename: str | None = None

    def hook(status: dict[str, Any]) -> None:
        nonlocal latest_bytes, total_bytes, current_filename, progress
        filename = str(status.get("filename") or "")
        if filename and filename != current_filename:
            progress = ProgressWriter(stream, monotonic)
            latest_bytes, total_bytes = 0, None
            current_filename = filename
        downloaded = status.get("downloaded_bytes")
        if isinstance(downloaded, (int, float)):
            latest_bytes = max(0, int(downloaded))
        total = status.get("total_bytes") or status.get("total_bytes_estimate")
        if isinstance(total, (int, float)) and total > 0:
            total_bytes = int(total)
        percent = latest_bytes * 100 / total_bytes if total_bytes else None

        def render(value: float | None) -> str:
            if value is None:
                return f"FETCH {latest_bytes / (1024 * 1024):.1f} MB"
            return f"FETCH {int(value)}%"

        state = status.get("status")
        if state in {"finished", "error"}:
            if state == "finished":
                progress.update(100.0 if total_bytes else None, render, final=True)
            else:
                progress.update(None, lambda _: "FETCH failed", final=True)
            latest_bytes, total_bytes, current_filename = 0, None, None
        else:
            progress.update(percent, render)
    return hook


def refuse_unsupported(info: dict[str, Any]) -> None:
    if info.get("_type") in {"playlist", "multi_video"} or "entries" in info:
        raise UnsupportedVideo("playlists and multi-video URLs are not supported")
    if info.get("is_live") or info.get("live_status") in {"is_live", "is_upcoming"}:
        raise UnsupportedVideo("live and upcoming broadcasts are not supported")


def ordered_unique(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        key = value.casefold()
        if key not in seen:
            result.append(value)
            seen.add(key)
    return result


def caption_language_candidates(info: Mapping[str, Any]) -> list[str]:
    """The video language, its base tag and YouTube's '-orig' variant (fr-FR -> fr-FR, fr, fr-orig)."""
    language = info.get("language") or info.get("language_code")
    if not isinstance(language, str) or not language:
        return []
    base = language.split("-", 1)[0]
    return ordered_unique([language, base, f"{base}-orig"])


def default_subtitle_languages(info: Mapping[str, Any]) -> list[str]:
    return ordered_unique([*caption_language_candidates(info), "en"])


def build_ydl_options(dest: Path, info: dict[str, Any] | None = None,
                      sub_langs: list[str] | None = None) -> dict[str, Any]:
    languages = sub_langs
    if languages is None:
        languages = default_subtitle_languages(info or {})
    ffmpeg = resolve_ffmpeg()
    js_runtime = resolve_js_runtime()
    options: dict[str, Any] = {
        "noplaylist": True,
        "restrictfilenames": True,
        "outtmpl": str(dest / (f"{safe_video_stem(str(info.get('title') or ''), str(info.get('id') or 'video'))}.%(ext)s"
                               if info is not None else "%(id)s.%(ext)s")),
        "merge_output_format": "mp4",
        "subtitleslangs": languages,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "progress_hooks": [make_progress_hook()],
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


def build_subtitle_options(dest: Path, languages: list[str],
                           info: dict[str, Any] | None = None) -> dict[str, Any]:
    options = build_ydl_options(dest, info=info, sub_langs=languages)
    options.update({"skip_download": True, "writesubtitles": True,
                    "writeautomaticsub": True, "ignoreerrors": True})
    return options


def build_comment_options(dest: Path, limit: int) -> dict[str, Any]:
    if not 0 <= limit <= MAX_COMMENTS:
        raise ValueError(f"comment limit must be between 0 and {MAX_COMMENTS}")
    options = build_ydl_options(dest)
    options.update({"skip_download": True, "getcomments": True,
                    "extractor_args": {"youtube": {"comment_sort": ["top"],
                                     "max_comments": [str(limit), str(limit), "0", "0", "1"]}}})
    return options


def _clean_count(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError):
        return 0
    return max(0, result)


def sanitize_comments(info: Mapping[str, Any], limit: int) -> list[dict[str, Any]]:
    comments = info.get("comments")
    if not isinstance(comments, list) or limit <= 0:
        return []
    clean = []
    for comment in comments:
        if not isinstance(comment, Mapping) or comment.get("parent") != "root":
            continue
        text = comment.get("text")
        if not isinstance(text, str) or not text.strip():
            continue
        stamp = comment.get("timestamp")
        try:
            timestamp = int(stamp) if stamp is not None else None
        except (TypeError, ValueError, OverflowError):
            timestamp = None
        clean.append({"text": text.strip(), "like_count": _clean_count(comment.get("like_count")),
                      "timestamp": timestamp, "is_pinned": bool(comment.get("is_pinned")),
                      "reply_count": _clean_count(comment.get("reply_count"))})
        if len(clean) >= limit:
            break
    return clean


def run_comments_substage(url: str, *, video: Path, analysis: Path, limit: int,
                          ydl_factory: Callable[[dict[str, Any]], Any]
                          ) -> tuple[dict[str, Any], str | None]:
    if limit == 0:
        return {"status": "disabled", "count": 0}, None
    try:
        with ydl_factory(build_comment_options(video.parent, limit)) as ydl:
            info = ydl.extract_info(url, download=False) or {}
        clean = sanitize_comments(info, limit)
        (analysis / "comments.json").write_text(_bounded_json(clean), encoding="utf-8")
        return {"status": "done", "output": "comments.json", "count": len(clean)}, None
    except Exception as error:
        reason = " ".join(str(error).split())[:160] or type(error).__name__
        return {"status": "unavailable", "count": 0, "reason": reason}, reason


def subtitle_result(info: dict[str, Any] | None, requested: list[str], dest: Path,
                   video: Path) -> tuple[dict[str, Any], dict[str, list[str]]]:
    info = info or {}
    subtitles = info.get("requested_subtitles") or {}
    saved = []
    for language, subtitle in subtitles.items():
        source = subtitle.get("filepath") if isinstance(subtitle, dict) else None
        if source and Path(source).is_file():
            saved.append(language)
    # yt-dlp versions differ in whether subtitle paths are returned. Match sidecars
    # emitted beside the video by their language suffix as a fallback.
    for path in dest.glob(f"{video.stem}*.??*"):
        if path.suffix.lower() not in SUBTITLE_SUFFIXES:
            continue
        for language in requested:
            if f".{language}." in path.name and language not in saved:
                saved.append(language)
    missing = [language for language in requested if language not in saved]
    return info, {"requested": requested, "saved": saved, "missing": missing}


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


def write_context(video: Path, info: dict[str, Any], ydl: Any, fetch_params: dict[str, Any],
                  subtitles: dict[str, list[str]]) -> Path:
    work = video.parent / f"{video.stem}.analysis"
    work.mkdir(exist_ok=True)
    sanitized = ydl.sanitize_info(info)
    sanitized = dict(sanitized) if isinstance(sanitized, Mapping) else {}
    sanitized.pop("comments", None)
    (work / "metadata.json").write_text(_bounded_json(normalize_metadata(info)), encoding="utf-8")
    (work / "raw_info.json").write_text(_bounded_json(sanitized), encoding="utf-8")
    (work / "sponsorblock.json").write_text(
        _bounded_json(info.get("sponsorblock_chapters") or []), encoding="utf-8")

    _move_subtitle_sidecars(video, work, info)

    stat = video.stat()
    identity = {"path": str(video.resolve()), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}
    manifest = {"schema_version": 2, "source": {**identity, "kind": "url"},
                "params": {"fetch": fetch_params, "extract": {}},
                "stages": {"fetch": {"status": "done", "subtitles": subtitles}}}
    (work / "manifest.json").write_text(_bounded_json(manifest), encoding="utf-8")
    return work


def _url_video_id(url: object) -> str | None:
    if not isinstance(url, str):
        return None
    parsed = urlparse(url)
    query_id = parse_qs(parsed.query).get("v", [None])[0]
    if query_id:
        return query_id
    host = parsed.hostname or ""
    if host.casefold() in {"youtu.be", "www.youtu.be"}:
        return parsed.path.strip("/").split("/")[0] or None
    return None


def _existing_fetch(dest: Path, stem: str, url: str,
                    preflight_info: Mapping[str, Any]) -> tuple[Path, Path, dict[str, Any]] | None:
    work = dest / f"{stem}.analysis"
    if not work.exists():
        return None
    manifest_path = work / "manifest.json"
    if not manifest_path.is_file():
        raise ValueError("existing analysis folder has no fetch manifest")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise ValueError("existing fetch manifest is invalid")
    source = manifest.get("source", {})
    stages = manifest.get("stages", {})
    params = manifest.get("params", {})
    fetch = params.get("fetch", {}) if isinstance(params, Mapping) else {}
    fetch_stage = stages.get("fetch") if isinstance(stages, Mapping) else None
    path = Path(str(source.get("path", ""))).resolve() if isinstance(source, Mapping) else Path()
    current_id = preflight_info.get("id") or _url_video_id(url)
    current_extractor = preflight_info.get("extractor_key") or preflight_info.get("extractor")
    saved_id = fetch.get("video_id") if isinstance(fetch, Mapping) else None
    saved_extractor = fetch.get("extractor_key") if isinstance(fetch, Mapping) else None
    legacy_url_id = _url_video_id(fetch.get("url")) if isinstance(fetch, Mapping) else None
    identity_matches = (saved_id == current_id if saved_id is not None else
                        isinstance(current_id, str) and
                        (current_id in stem or legacy_url_id == current_id))
    extractor_matches = (saved_extractor == current_extractor if saved_extractor is not None else True)
    if (not isinstance(source, Mapping) or source.get("kind") != "url"
            or not isinstance(fetch, Mapping) or not identity_matches or not extractor_matches
            or not isinstance(fetch_stage, Mapping)
            or fetch_stage.get("status") != "done"
            or not path.is_file() or path.parent != dest.resolve() or path.stem != stem):
        raise ValueError("existing analysis folder does not match this video identity")
    stat = path.stat()
    if source.get("size") != stat.st_size or source.get("mtime_ns") != stat.st_mtime_ns:
        raise ValueError("existing downloaded video identity changed")
    return work, path, manifest


def _resume_comments(work: Path, video: Path, manifest: dict[str, Any], url: str, limit: int,
                     factory: Callable[[dict[str, Any]], Any]) -> int:
    params = {"limit": limit, "sort": "top"}
    old_params = manifest.get("params", {}).get("comments")
    stage = manifest.get("stages", {}).get("comments")
    if old_params != params and (work / "comments.json").exists():
        print("comments conflict: requested limit differs from existing comments.json")
        return 3
    if old_params == params and isinstance(stage, Mapping) and stage.get("status") in {"done", "disabled"}:
        return 0
    stage, warning = run_comments_substage(url, video=video, analysis=work, limit=limit,
                                           ydl_factory=factory)
    manifest.setdefault("params", {})["comments"] = params
    manifest.setdefault("stages", {})["comments"] = stage
    (work / "manifest.json").write_text(_bounded_json(manifest), encoding="utf-8")
    if warning:
        print(f"WARNING: comments unavailable ({warning})")
    return 0


def _ordered_subset(values: list[str], candidates: list[str]) -> bool:
    positions = {value.casefold(): index for index, value in enumerate(candidates)}
    last = -1
    for value in values:
        index = positions.get(value.casefold(), -1)
        if index <= last:
            return False
        last = index
    return True


def _has_matching_caption(saved: object, info: Mapping[str, Any]) -> bool:
    candidates = {language.casefold() for language in caption_language_candidates(info)}
    return isinstance(saved, list) and any(
        isinstance(language, str) and language.casefold() in candidates for language in saved)


def _move_subtitle_sidecars(video: Path, work: Path, info: Mapping[str, Any]) -> None:
    subtitles = info.get("requested_subtitles") or {}
    sources = [Path(str(item["filepath"])) for item in subtitles.values()
               if isinstance(item, Mapping) and item.get("filepath")]
    sources.extend(path for path in video.parent.glob(f"{video.stem}*.??*")
                   if path.suffix.lower() in SUBTITLE_SUFFIXES)
    for source in sources:
        if source.is_file() and source.resolve() != (work / source.name).resolve():
            target = work / source.name
            if not target.exists():
                shutil.move(str(source), str(target))


def _refresh_subtitle_artifacts(work: Path, video: Path, manifest: dict[str, Any], url: str,
                                info: Mapping[str, Any], requested: list[str],
                                factory: Callable[[dict[str, Any]], Any]) -> None:
    subtitle_info: dict[str, Any] = {}
    try:
        with factory(build_subtitle_options(video.parent, requested, dict(info))) as ydl:
            subtitle_info = download_video(url, ydl)
            sanitized = ydl.sanitize_info(subtitle_info)
    except Exception as error:
        print(f"WARNING: subtitle refresh failed ({' '.join(str(error).split())[:160]})")
        sanitized = {}
    subtitle_info, status = subtitle_result(subtitle_info, requested, video.parent, video)
    _move_subtitle_sidecars(video, work, subtitle_info)
    raw_path = work / "raw_info.json"
    raw_info = json.loads(raw_path.read_text(encoding="utf-8")) if raw_path.is_file() else {}
    sanitized = dict(sanitized) if isinstance(sanitized, Mapping) else {}
    raw_info["requested_subtitles"] = sanitized.get("requested_subtitles") or subtitle_info.get("requested_subtitles") or {}
    raw_info.pop("comments", None)
    raw_path.write_text(_bounded_json(raw_info), encoding="utf-8")
    manifest["params"]["fetch"]["sub_langs"] = requested
    manifest["stages"]["fetch"]["subtitles"].update(status)
    (work / "manifest.json").write_text(_bounded_json(manifest), encoding="utf-8")


def _resume_subtitles(work: Path, video: Path, manifest: dict[str, Any], url: str,
                      info: Mapping[str, Any], requested: list[str], explicit: bool,
                      factory: Callable[[dict[str, Any]], Any]) -> int:
    params = manifest.setdefault("params", {}).setdefault("fetch", {})
    old_languages = params.get("sub_langs", [])
    if not isinstance(old_languages, list):
        old_languages = []
    defaults = default_subtitle_languages(info)
    if explicit and old_languages != requested:
        print("fetch conflict: requested subtitle languages differ from the existing fetch")
        return 3
    if not explicit and old_languages != requested and not _ordered_subset(old_languages, defaults):
        print("fetch conflict: requested subtitle languages differ from the existing fetch")
        return 3
    subtitle_stage = manifest.setdefault("stages", {}).setdefault("fetch", {}).setdefault("subtitles", {})
    saved = subtitle_stage.get("saved", []) if isinstance(subtitle_stage, Mapping) else []
    if old_languages == requested and _has_matching_caption(saved, info):
        return 0
    _refresh_subtitle_artifacts(work, video, manifest, url, info, requested, factory)
    return 0


def main(argv: list[str] | None = None, *, ydl_factory: Callable[[dict[str, Any]], Any] | None = None,
         on_video: Callable[[Path], None] | None = None) -> int:
    parser = argparse.ArgumentParser(
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=("Fetch one online video, refusing playlists and live or upcoming\n"
                     "streams.\n"
                     "Reads a video URL and network metadata, then writes the video into\n"
                     "--dest and subtitles, metadata, and manifest into\n"
                     "<video folder>/<video name>.analysis/. It never deletes files.\n"
                     "Next, run extract_video.py on the downloaded video to create\n"
                     "frames and a transcript."),
        epilog=("Example: python fetch_video.py \"https://example.com/video\" --dest videos\n"
                "Exit codes: 0 success; 1 download or tool failure; 2 invalid\n"
                "arguments; 3 unsupported URL."))
    parser.add_argument("url", help="URL of one online video")
    parser.add_argument("--dest", type=Path, default=Path("."),
                        help="folder for the downloaded video (default: %(default)s)")
    parser.add_argument("--sub-langs", nargs="+", metavar="LANG",
                        help="subtitle languages to save (default: video language variants and en)")
    parser.add_argument("--comments", type=int, default=MAX_COMMENTS, metavar="N",
                        help="save at most N top-level comments (0 to disable; maximum 100)")
    try:
        args = parser.parse_args(argv)
    except SystemExit as error:
        return 0 if error.code == 0 else 2
    if not 0 <= args.comments <= MAX_COMMENTS:
        print(f"--comments must be between 0 and {MAX_COMMENTS}", file=sys.stderr)
        return 2
    try:
        args.dest.mkdir(parents=True, exist_ok=True)
        preflight_options = build_ydl_options(args.dest.resolve(), sub_langs=args.sub_langs)
        factory = ydl_factory or _youtube_dl_factory()
        with factory(preflight_options) as ydl:
            preflight_info = download_video(args.url, ydl, download=False)
        refuse_unsupported(preflight_info)
        stem = safe_video_stem(str(preflight_info.get("title") or ""),
                               str(preflight_info.get("id") or "video"))
        try:
            existing = _existing_fetch(args.dest.resolve(), stem, args.url, preflight_info)
        except (OSError, ValueError, json.JSONDecodeError) as error:
            print(f"FETCH REFUSED: {error}")
            return 3
        if existing is not None:
            work, video, manifest = existing
            requested_languages = (args.sub_langs if args.sub_langs is not None
                                   else default_subtitle_languages(preflight_info))
            if on_video is not None:
                on_video(video)
            code = _resume_subtitles(work, video, manifest, args.url, preflight_info,
                                     requested_languages, args.sub_langs is not None, factory)
            if code == 0:
                code = _resume_comments(work, video, manifest, args.url, args.comments, factory)
            if code == 0:
                print(f"FETCHED: {video}")
                print(f"WORK FOLDER: {work}")
            return code
        options = build_ydl_options(args.dest.resolve(), preflight_info, args.sub_langs)
        with factory(options) as ydl:
            info = download_video(args.url, ydl)
            video = _video_path(info, ydl)
            context_ydl = ydl
        if on_video is not None:
            on_video(video)
        languages = options["subtitleslangs"]
        subtitle_info = {}
        subtitle_error = None
        try:
            with factory(build_subtitle_options(args.dest.resolve(), languages, preflight_info)) as ydl:
                subtitle_info = download_video(args.url, ydl)
        except Exception as error:
            subtitle_error = " ".join(str(error).split())[:160] or type(error).__name__
        subtitle_info, subtitle_status = subtitle_result(
            subtitle_info, languages, args.dest.resolve(), video)
        info["requested_subtitles"] = subtitle_info.get("requested_subtitles") or {}
        fetch_params = {"url": args.url, "sub_langs": languages,
                        "extractor_key": preflight_info.get("extractor_key") or preflight_info.get("extractor"),
                        "video_id": preflight_info.get("id")}
        work = write_context(video, info, context_ydl, fetch_params, subtitle_status)
        manifest_path = work / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        comment_stage, comment_error = run_comments_substage(args.url, video=video, analysis=work,
                                                              limit=args.comments, ydl_factory=factory)
        manifest["params"]["comments"] = {"limit": args.comments, "sort": "top"}
        manifest["stages"]["comments"] = comment_stage
        manifest_path.write_text(_bounded_json(manifest), encoding="utf-8")
        if subtitle_status["missing"]:
            reason = subtitle_error or "no matching subtitle files were produced"
            print(f"WARNING: subtitles unavailable for {', '.join(subtitle_status['missing'])} ({reason})")
        if comment_error:
            print(f"WARNING: comments unavailable ({comment_error})")
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
