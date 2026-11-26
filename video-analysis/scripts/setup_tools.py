"""Install and check video-analysis tools; run with -h for details."""
from __future__ import annotations

import argparse
import importlib
import importlib.metadata
import os
import subprocess
import sys
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

from tools import Tool, resolve_ffmpeg, resolve_js_runtime, resolve_ytdlp

PACKAGES: dict[str, str] = {
    "PIL": "Pillow",
    "numpy": "numpy",
    "faster_whisper": "faster-whisper",
    "yt_dlp": "yt-dlp[default]",
    "imageio_ffmpeg": "imageio-ffmpeg",
    "rapidocr": "rapidocr",
    "openai": "openai",
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
    ready = report_ocr() and ready
    ready = report_correction() and ready
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


def report_ocr() -> bool:
    try:
        module = importlib.import_module("rapidocr")
    except ImportError:
        print("OCR: rapidocr missing")
        return False
    version = getattr(module, "__version__", None)
    if not version:
        try:
            version = importlib.metadata.version("rapidocr")
        except importlib.metadata.PackageNotFoundError:
            version = "unknown"
    print(f"OCR: rapidocr {version}")
    return True


def report_correction() -> bool:
    """Report host, OpenAI, and declared-local correction readiness without secrets."""
    claude = shutil.which("claude")
    codex = shutil.which("codex") or shutil.which("codex.cmd")
    print(f"Claude Code: {claude or 'missing'}")
    print(f"Codex: {codex or 'missing'}")
    print("Host providers use the host's authenticated account")
    env = os.environ
    if "CLAUDECODE" in env:
        selected = "claude-code" if claude else None
        if not claude:
            print("Claude Code host detected but its executable is missing")
    elif "CODEX_THREAD_ID" in env or "CODEX_SESSION_ID" in env:
        selected = "codex" if codex else None
        if not codex:
            print("Codex host detected but its executable is missing")
    elif env.get("OPENAI_API_KEY"):
        selected = "openai"
    elif env.get("VIDEO_ANALYSIS_CORRECT_PROVIDER") == "local" and env.get("OPENAI_BASE_URL") and env.get("VIDEO_ANALYSIS_AI_LOCALITY") == "local":
        selected = "local"
    else:
        selected = None
    print(f"Correction auto provider: {selected or 'none'}")
    if selected is None:
        print("No available automatic provider; correction will be skipped")
    try:
        module = importlib.import_module("openai")
    except ImportError:
        print("OpenAI: openai missing")
        return selected in {None, "claude-code", "codex"}
    version = getattr(module, "__version__", None)
    if not version:
        try:
            version = importlib.metadata.version("openai")
        except importlib.metadata.PackageNotFoundError:
            version = "unknown"
    print(f"OpenAI: openai {version}")
    print("API key present" if env.get("OPENAI_API_KEY") else "API key missing")
    return True


def warm_ocr() -> bool:
    import numpy as np
    from PIL import Image, ImageDraw, ImageFont
    from ocr import ocr_key_frames
    image = Image.new("RGB", (520, 120), "white")
    draw = ImageDraw.Draw(image)
    draw.text((12, 30), "R\u00e9sum\u00e9", fill="black", font=ImageFont.load_default())
    try:
        result = ocr_key_frames([("setup-warmup.png", np.asarray(image))], min_score=0.5)
    except Exception as error:
        print(f"OCR warmup failed: {error}")
        return False
    print("OCR model ready" if result.get("setup-warmup.png") is not None else "OCR warmup failed")
    return result.get("setup-warmup.png") is not None


def run_selftest() -> int:
    result = subprocess.run([sys.executable, str(Path(__file__).with_name("selftest.py"))], check=False)
    return result.returncode


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=("Install missing video-analysis packages, verify tools, and warm OCR.\n"
                     "--check runs only the default offline self-test without setup probes.\n"
                     "Normal setup may install packages or cache the Whisper model using the network.\n"
                     "Successful setup ends by running the default offline self-test."),
        epilog=("Example: python setup_tools.py --check\n"
                "Exit codes: 0 ready; 1 dependency, tool, or model failure; 2 invalid arguments."))
    parser.add_argument("--check", action="store_true",
                        help="run only the default offline self-test")
    parser.add_argument("--model", default="small",
                        help="Whisper model to cache or check (default: %(default)s)")
    parser.add_argument("--no-model-download", action="store_true",
                        help="skip checking or caching the Whisper model")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exit_signal:
        return 0 if exit_signal.code == 0 else 2

    if args.check:
        return run_selftest()
    ffmpeg = resolve_ffmpeg()
    absent = missing_packages(ffmpeg=ffmpeg)
    if absent:
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
    if not args.no_model_download and not cache_model(args.model):
        return 1
    if not warm_ocr():
        return 1
    if not ready:
        print("NOT READY")
        return 1
    if run_selftest() != 0:
        print("SELFTEST FAILED")
        return 1
    print("READY")
    return 0


if __name__ == "__main__":
    sys.exit(main())
