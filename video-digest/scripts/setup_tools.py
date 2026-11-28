"""Install and check Video Digest tools; run with -h for details."""
from __future__ import annotations

import argparse
import importlib
import importlib.metadata
import os
import subprocess
import sys
import shutil
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from tools import Tool, env_value, resolve_ffmpeg, resolve_js_runtime, resolve_ytdlp
from host_models import ConfigReader, ExplicitInput, resolve_host_model

PACKAGES: dict[str, str] = {
    "PIL": "Pillow",
    "numpy": "numpy",
    "faster_whisper": "faster-whisper",
    "yt_dlp": "yt-dlp[default]",
    "imageio_ffmpeg": "imageio-ffmpeg",
    "rapidocr": "rapidocr",
    "openai": "openai",
    "docx": "python-docx",
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


GPU_SPEECH_PACKAGES = ("nvidia-cublas-cu12", "nvidia-cudnn-cu12>=9,<10")


def cuda_device_count() -> int:
    try:
        return int(importlib.import_module("ctranslate2").get_cuda_device_count())
    except Exception:
        return 0


def missing_gpu_speech_packages() -> list[str]:
    absent = []
    for distribution, package in zip(("nvidia-cublas-cu12", "nvidia-cudnn-cu12"), GPU_SPEECH_PACKAGES):
        try:
            importlib.metadata.version(distribution)
        except importlib.metadata.PackageNotFoundError:
            absent.append(package)
    return absent


def ensure_gpu_speech_dependencies(gpu_probe: Callable[[], bool] | None = None,
                                   missing: Callable[[], list[str]] = missing_gpu_speech_packages) -> bool:
    """Install large optional CUDA runtime wheels only on hosts with an NVIDIA GPU."""
    probe = gpu_probe or (lambda: cuda_device_count() > 0)
    try:
        has_gpu = bool(probe())
    except Exception:
        has_gpu = False
    if not has_gpu:
        return True
    try:
        absent = missing()
    except Exception:
        return True
    if not absent:
        return True
    print("GPU speech libraries are about 1 GB and only speed up Whisper.")
    return install(absent)


def gpu_speech_readiness(gpu_probe: Callable[[], bool] | None = None,
                         missing: Callable[[], list[str]] = missing_gpu_speech_packages) -> str:
    probe = gpu_probe or (lambda: cuda_device_count() > 0)
    try:
        if not probe():
            return "GPU speech: CPU only (no NVIDIA GPU detected)"
    except Exception as error:
        return f"GPU speech: CPU only ({type(error).__name__})"
    try:
        absent = missing()
    except Exception as error:
        return f"GPU speech: CPU only ({type(error).__name__})"
    return "GPU speech: CPU only (missing " + ", ".join(absent) + ")" if absent else "GPU speech: ready (CUDA)"


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
    print(gpu_speech_readiness())
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


def report_correction(environ: Mapping[str, str] | None = None, config_reader: Any = None) -> bool:
    """Report host, OpenAI, and declared-local correction readiness without secrets."""
    claude = shutil.which("claude")
    codex = shutil.which("codex") or shutil.which("codex.cmd")
    openclaw = shutil.which("openclaw") or shutil.which("openclaw.cmd")
    print(f"Claude Code: {claude or 'missing'}")
    print(f"Codex: {codex or 'missing'}")
    print(f"OpenClaw: {openclaw or 'missing'}")
    print("Host providers use the host's authenticated account")
    env = environ if environ is not None else os.environ
    host = None
    try:
        reader = config_reader or ConfigReader(Path.cwd(), Path.home(),
                                                lambda path: path.read_text(encoding="utf-8") if path.is_file() else None)
        host = resolve_host_model(env, reader, ExplicitInput())
        print(f"Resolved host: {host.host}")
        print(f"Default model: {host.default_model or 'host default'}")
        print(f"Low-cost model: {host.low_cost_model or 'none'}")
        print(f"Resolved provider: {host.provider}")
        print(f"Resolved locality: {host.locality}")
        print(f"Selection reason: {dict(host.reasons).get('default_model', 'unknown')}")
    except Exception as error:
        print(f"Resolver unavailable: {type(error).__name__}")
    host_executables = {"claude-code": claude, "codex": codex, "openclaw": openclaw}
    if host is not None and host.host in host_executables:
        selected = host.host if host_executables[host.host] else None
        if selected is None:
            print(f"{host.host} host detected but its executable is missing")
    elif env.get("OPENAI_API_KEY"):
        selected = "openai"
    elif env_value(env, "CORRECT_PROVIDER") == "local" and env.get("OPENAI_BASE_URL") and env_value(env, "AI_LOCALITY") == "local":
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
        description=("Install missing Video Digest packages, verify tools, and warm OCR.\n"
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
    # After the main install: the GPU probe needs ctranslate2, which faster-whisper brings.
    ensure_gpu_speech_dependencies()
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
