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

HERE = Path(__file__).resolve().parent


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
    for name, passed in checks.items():
        print(f"{'PASS' if passed else 'FAIL'}  {name}")
    print(f"test folder (send to the Recycle Bin when done): {folder}")
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
