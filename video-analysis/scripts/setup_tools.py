"""Install and verify the tools of the video-analysis skill. No system ffmpeg, no admin rights.

Usage:
    python setup_tools.py [--check] [--model small] [--no-model-download]

  default              install missing packages (pip --user), verify ffmpeg, cache the Whisper model
  --check              verify only, install nothing
  --model NAME         Whisper model to cache (default: small)
  --no-model-download  skip caching the model

Exit codes: 0 ready, 1 dependency or ffmpeg failure, 2 invalid arguments.
"""
from __future__ import annotations

import argparse
import importlib
import subprocess
import sys

PACKAGES: dict[str, str] = {
    "imageio_ffmpeg": "imageio-ffmpeg",
    "PIL": "Pillow",
    "numpy": "numpy",
    "faster_whisper": "faster-whisper",
}


def missing_packages() -> list[str]:
    absent = []
    for module, package in PACKAGES.items():
        try:
            importlib.import_module(module)
        except ImportError:
            absent.append(package)
    return absent


def install(packages: list[str]) -> bool:
    command = [sys.executable, "-m", "pip", "install", "--user", "--quiet", *packages]
    print("installing:", " ".join(packages))
    return subprocess.run(command, check=False).returncode == 0


def ffmpeg_works() -> bool:
    try:
        import imageio_ffmpeg
        exe = imageio_ffmpeg.get_ffmpeg_exe()
    except (ImportError, RuntimeError) as error:
        print(f"ffmpeg unavailable: {error}")
        return False
    result = subprocess.run([exe, "-version"], capture_output=True, text=True, check=False)
    first_line = result.stdout.splitlines()[0] if result.stdout else "no output"
    print(f"ffmpeg: {exe}\n  {first_line}")
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
    parser = argparse.ArgumentParser(description="Bootstrap the video-analysis tools.")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--model", default="small")
    parser.add_argument("--no-model-download", action="store_true")
    try:
        args = parser.parse_args()
    except SystemExit as exit_signal:
        return 0 if exit_signal.code == 0 else 2

    absent = missing_packages()
    if absent and args.check:
        print("missing:", ", ".join(absent))
        return 1
    if absent and not install(absent):
        print("pip install failed")
        return 1
    if missing_packages():
        print("still missing after install:", ", ".join(missing_packages()))
        return 1
    if not ffmpeg_works():
        return 1
    if args.check and not args.no_model_download and not model_cached(args.model):
        return 1
    if not args.check and not args.no_model_download and not cache_model(args.model):
        return 1
    print("READY")
    return 0


if __name__ == "__main__":
    sys.exit(main())
