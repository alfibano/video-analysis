"""Install dependencies and check the video-analysis tools.

Usage:
    python setup_tools.py [--check] [--model NAME] [--no-model-download]

Flags:
    --check              Report readiness without installing packages or downloading a model.
                         Reports ffmpeg, yt-dlp, and JavaScript runtime paths and origins.
    --model NAME         Whisper model to cache or check (default: small).
    --no-model-download  Skip checking/caching the Whisper model.

Exit codes: 0 ready, 1 dependency/tool/model failure, 2 invalid arguments.
"""
from __future__ import annotations

import argparse
import importlib
import subprocess
import sys
from collections.abc import Callable
from typing import Any

from tools import Tool, resolve_ffmpeg, resolve_js_runtime, resolve_ytdlp

PACKAGES: dict[str, str] = {
    "PIL": "Pillow",
    "numpy": "numpy",
    "faster_whisper": "faster-whisper",
    "yt_dlp": "yt-dlp[default]",
    "imageio_ffmpeg": "imageio-ffmpeg",
}


def plan_install_packages(missing: list[str], *, ffmpeg_available: bool) -> list[str]:
    return [package for package in missing if not (ffmpeg_available and package == "imageio-ffmpeg")]


def missing_packages(*, module_lookup: Callable[[str], Any] = importlib.import_module,
                     ffmpeg: Tool | None = None) -> list[str]:
    absent = []
    for module, package in PACKAGES.items():
        try:
            module_lookup(module)
        except ImportError:
            absent.append(package)
    return plan_install_packages(absent, ffmpeg_available=bool(ffmpeg and ffmpeg.origin != "bundled"))


def install(packages: list[str]) -> bool:
    command = [sys.executable, "-m", "pip", "install", "--user", "--quiet", *packages]
    print("installing:", " ".join(packages))
    return subprocess.run(command, check=False).returncode == 0


def report_tool(name: str, tool: Tool | None) -> bool:
    if tool is None:
        print(f"{name}: missing")
        return False
    print(f"{name}: {tool.path} ({tool.origin})")
    return True


def report_tools(ffmpeg: Tool | None, ytdlp: Tool | None, js_runtime: Tool | None) -> bool:
    """Report every required external tool and return whether all are present."""
    ready = report_tool("ffmpeg", ffmpeg)
    ready = report_tool("yt-dlp", ytdlp) and ready
    if js_runtime is None:
        print("JavaScript runtime: missing (install deno; node is also supported)")
        ready = False
    else:
        ready = report_tool("JavaScript runtime", js_runtime) and ready
    return ready


def ffmpeg_works(tool: Tool | None = None) -> bool:
    resolved = tool or resolve_ffmpeg()
    if resolved is None:
        print("ffmpeg unavailable: install ffmpeg or imageio-ffmpeg")
        return False
    result = subprocess.run([resolved.path, "-version"], capture_output=True, text=True, check=False)
    return result.returncode == 0


def model_cached(name: str) -> bool:
    from faster_whisper.utils import download_model
    try:
        download_model(name, local_files_only=True)
    except Exception:  # noqa: BLE001 - huggingface raises several types for "not in cache"
        print(f"Whisper model '{name}' is not cached yet (run without --check)")
        return False
    return True


def cache_model(name: str) -> bool:
    from faster_whisper import WhisperModel
    print(f"caching Whisper model '{name}' (first time downloads it, then works offline)")
    WhisperModel(name, device="cpu", compute_type="int8")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="Bootstrap the tools of the video-analysis skill.")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--model", default="small")
    parser.add_argument("--no-model-download", action="store_true")
    try:
        args = parser.parse_args()
    except SystemExit as exit_signal:
        return 0 if exit_signal.code == 0 else 2

    ffmpeg = resolve_ffmpeg()
    absent = missing_packages(ffmpeg=ffmpeg)
    if args.check:
        ready = report_tools(ffmpeg, resolve_ytdlp(), resolve_js_runtime())
        if absent:
            print("missing:", ", ".join(absent))
            return 1
    elif absent:
        if not install(absent):
            print("pip install failed")
            return 1
        ffmpeg = resolve_ffmpeg()
        absent = missing_packages(ffmpeg=ffmpeg)
        if absent:
            print("still missing after install:", ", ".join(absent))
            return 1
        ready = report_tools(ffmpeg, resolve_ytdlp(), resolve_js_runtime())
    else:
        ready = report_tools(ffmpeg, resolve_ytdlp(), resolve_js_runtime())
    if not ffmpeg_works(ffmpeg):
        return 1
    if args.check and not args.no_model_download and not model_cached(args.model):
        return 1
    if not args.check and not args.no_model_download and not cache_model(args.model):
        return 1
    if not ready:
        print("NOT READY")
        return 1
    print("READY")
    return 0


if __name__ == "__main__":
    sys.exit(main())
