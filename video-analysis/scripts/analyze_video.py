"""Fetch, extract, and bundle one video from a URL or local path."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from urllib.parse import urlsplit

from build_bundle import main as bundle_main
from correct_transcript import main as correct_main
from extract_video import main as extract_main
from fetch_video import main as fetch_main


def is_url(source: str) -> bool:
    parsed = urlsplit(source)
    return parsed.scheme.casefold() in {"http", "https"} and bool(parsed.netloc)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=("Analyze one local video or HTTP(S) URL from start to finish.\n"
                     "URLs are fetched before extraction; local paths go straight to extraction.\n"
                     "The command then builds bundle.json beside the source video.\n"
                     "It runs each script in process and stops at the first failed step."),
        epilog=("Example: python analyze_video.py lecture.mp4 --skip-transcript\n"
                "Exit codes: 0 success; 1 processing failure; 2 invalid arguments or input;\n"
                "3 unsupported URL or manifest conflict; 4 remote-AI consent required."))
    parser.add_argument("source", help="local video path or one HTTP(S) video URL")
    parser.add_argument("--dest", type=Path, help="download folder for URL sources")
    parser.add_argument("--sub-langs", nargs="+", metavar="LANG",
                        help="subtitle languages to fetch (URLs only)")
    parser.add_argument("--comments", type=int, default=100, metavar="N",
                        help="save at most N top-level comments from a URL (0 to disable; maximum 100)")
    parser.add_argument("--interval", type=float, help="base frame interval in seconds")
    parser.add_argument("--dedup-threshold", type=float, help="key-frame difference threshold")
    parser.add_argument("--model", help="Whisper model name")
    parser.add_argument("--correct-model", help="transcript correction model")
    parser.add_argument("--host", choices=("claude-code", "codex", "openclaw"), help="host providing AI models")
    parser.add_argument("--provider", choices=("auto", "claude-code", "codex", "openai", "local"),
                        help="transcript correction provider")
    parser.add_argument("--retry-refused", action="store_true", help="retry refused correction chunks")
    parser.add_argument("--base-url", help="OpenAI-compatible correction endpoint")
    parser.add_argument("--ai-locality", choices=("local", "remote"), help="correction endpoint locality")
    parser.add_argument("--allow-remote-ai", action="store_true", help="consent to remote correction of local media")
    parser.add_argument("--no-correct", action="store_true", help="record an intentional correction skip")
    parser.add_argument("--language", help="transcript and caption language")
    parser.add_argument("--skip-frames", action="store_true", help="skip sampled frame extraction")
    parser.add_argument("--skip-transcript", action="store_true", help="skip Whisper transcription")
    parser.add_argument("--at", action="append", help="also extract a frame at this timestamp")
    parser.add_argument("--fixed-interval", action="store_true", help="disable adaptive sampling")
    parser.add_argument("--motion-threshold", type=float, help="adaptive scene-change threshold")
    parser.add_argument("--motion-margin", type=float, help="adaptive context margin in seconds")
    parser.add_argument("--ocr-min-score", type=float, help="minimum OCR confidence from 0 to 1")
    parser.add_argument("--min-chapter", type=float, help="minimum fallback chapter length in seconds")
    parser.add_argument("--max-chapter", type=float, help="maximum fallback chapter length in seconds")
    parser.add_argument("--target-chapter", type=float, help="target fallback chapter length in seconds")
    parser.add_argument("--skip-ocr", action="store_true", help="omit key-frame OCR")
    return parser


def _step_args(args: argparse.Namespace, video: str) -> tuple[list[str], list[str], list[str]]:
    extract = [video]
    correct = [video]
    bundle = [video]
    for name, option in (("interval", "--interval"), ("dedup_threshold", "--dedup-threshold"),
                         ("model", "--model"), ("language", "--language"),
                         ("motion_threshold", "--motion-threshold"), ("motion_margin", "--motion-margin")):
        value = getattr(args, name)
        if value is not None:
            extract.extend((option, str(value)))
    if args.language is not None:
        bundle.extend(("--language", args.language))
        correct.extend(("--language", args.language))
    if args.correct_model is not None:
        correct.extend(("--model", args.correct_model))
    if args.provider is not None:
        correct.extend(("--provider", args.provider))
    if args.host is not None:
        correct.extend(("--host", args.host))
    if args.retry_refused:
        correct.append("--retry-refused")
    if args.base_url is not None:
        correct.extend(("--base-url", args.base_url))
    if args.ai_locality is not None:
        correct.extend(("--ai-locality", args.ai_locality))
    if args.allow_remote_ai:
        correct.append("--allow-remote-ai")
    if args.no_correct:
        correct.append("--no-correct")
    for name, option in (("skip_frames", "--skip-frames"), ("skip_transcript", "--skip-transcript"),
                         ("fixed_interval", "--fixed-interval")):
        if getattr(args, name):
            extract.append(option)
    for timestamp in args.at or []:
        extract.extend(("--at", timestamp))
    if args.ocr_min_score is not None:
        bundle.extend(("--ocr-min-score", str(args.ocr_min_score)))
    for name, option in (("min_chapter", "--min-chapter"), ("max_chapter", "--max-chapter"),
                         ("target_chapter", "--target-chapter")):
        value = getattr(args, name)
        if value is not None:
            bundle.extend((option, str(value)))
    if args.skip_ocr:
        bundle.append("--skip-ocr")
    return extract, correct, bundle


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
    except SystemExit as error:
        return 0 if error.code == 0 else 2
    url = is_url(args.source)
    if not 0 <= args.comments <= 100:
        print("--comments must be between 0 and 100", file=sys.stderr)
        return 2
    if not url and args.sub_langs:
        print("--sub-langs is only valid for an HTTP(S) URL", file=sys.stderr)
        return 2
    if not url and args.comments != 100:
        print("--comments is only valid for an HTTP(S) URL", file=sys.stderr)
        return 2
    video = args.source
    if url:
        print("STEP fetch", file=sys.stderr)
        fetch_args = [args.source]
        fetch_args.extend(("--comments", str(args.comments)))
        if args.dest is not None:
            fetch_args.extend(("--dest", str(args.dest)))
        if args.sub_langs:
            fetch_args.extend(("--sub-langs", *args.sub_langs))
        fetched: list[Path] = []
        code = fetch_main(fetch_args, on_video=fetched.append)
        if code:
            return code
        if not fetched:
            return 1
        video = str(fetched[0])
    extract_args, correct_args, bundle_args = _step_args(args, video)
    print("STEP extract", file=sys.stderr)
    code = extract_main(extract_args)
    if code:
        return code
    print("STEP correct", file=sys.stderr)
    code = correct_main(correct_args)
    if code:
        return code
    print("STEP bundle", file=sys.stderr)
    return bundle_main(bundle_args)


if __name__ == "__main__":
    sys.exit(main())
