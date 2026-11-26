"""Pure transcript-correction contracts used by the correction stage."""
from __future__ import annotations

import re
import argparse
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

CHUNK_SIZE = 40
PROMPT_VERSION = "asr-correction-v1"
SYSTEM_PROMPT = (
    "You fix automatic speech-recognition errors in a transcript. Correct only words that were misheard: "
    "wrong word, misspelled name or technical term, wrong homophone, grammar broken by mishearing. "
    "Keep the speaker's wording, register and language. Do not rephrase, summarize, translate, add or remove content. "
    "Output exactly one line per input line, same numbers, format: <number><TAB><corrected text>. No other text."
)


@dataclass(frozen=True, slots=True)
class SourceIdentity:
    kind: str
    language: str | None
    file: str


@dataclass(frozen=True, slots=True)
class TokenUsage:
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    total_tokens: int = 0


@dataclass(frozen=True, slots=True)
class ProviderResult:
    text: str | None
    refusal: bool = False
    error: bool = False
    usage: TokenUsage = TokenUsage()


@dataclass(frozen=True, slots=True)
class CorrectionInput:
    start: float
    end: float
    text: str


@dataclass(frozen=True, slots=True)
class CorrectedSegment:
    start: float
    end: float
    original_text: str
    corrected_text: str
    status: str


def build_context(title: str | None, description: str | None, language: str,
                  chapter_titles: Sequence[str]) -> str:
    """Build bounded, ordered video context for one provider request."""
    description_text = (description or "")[:1000]
    return (f"Video title: {title or ''}\nDescription: {description_text}\nLanguage: {language}\n"
            f"Chapter titles: {', '.join(chapter_titles)}")


def build_chunk_prompt(context: str, segments: Sequence[CorrectionInput], first_id: int) -> str:
    """Format one globally numbered chunk of transcript text."""
    body = "\n".join(f"{first_id + offset}\t{segment.text}" for offset, segment in enumerate(segments))
    return f"{context}\nTranscript lines:\n{body}\n"


def chunk_segments(segments: Sequence[CorrectionInput]) -> list[tuple[int, list[CorrectionInput]]]:
    """Split ordered segments into fixed-size chunks with one-based global IDs."""
    return [(offset + 1, list(segments[offset:offset + CHUNK_SIZE]))
            for offset in range(0, len(segments), CHUNK_SIZE)]


def parse_numbered_lines(response: str | None, expected_ids: Sequence[int]) -> dict[int, str] | None:
    """Accept only a complete, unique set of nonempty numbered transcript lines."""
    if not response:
        return None
    found: dict[int, str] = {}
    for line in response.splitlines():
        if not line.strip():
            continue
        match = re.fullmatch(r"(\d+)\t(.+)", line)
        if match is None:
            return None
        identifier, text = int(match.group(1)), match.group(2).strip()
        if not text or identifier in found:
            return None
        found[identifier] = text
    if set(found) != set(expected_ids):
        return None
    return found


def normalized_words(text: str) -> list[str]:
    """Normalize Unicode words, case, and common apostrophe forms."""
    text = unicodedata.normalize("NFC", text).casefold()
    text = text.replace("’", "'").replace("‘", "'").replace("`", "'")
    return re.findall(r"\w+", text, flags=re.UNICODE)


def word_edit_distance(original: Sequence[str], corrected: Sequence[str]) -> int:
    """Return word-level Levenshtein distance using linear memory."""
    previous = list(range(len(corrected) + 1))
    for row, word in enumerate(original, 1):
        current = [row]
        for column, other in enumerate(corrected, 1):
            current.append(min(previous[column - 1] + (word != other),
                               previous[column] + 1, current[column - 1] + 1))
        previous = current
    return previous[-1]


def _guard_status(original_text: str, corrected_text: str) -> str:
    original = normalized_words(original_text)
    corrected = normalized_words(corrected_text)
    edit_ratio = word_edit_distance(original, corrected) / max(1, len(original))
    if edit_ratio > 0.4:
        return "rejected_edit_ratio"
    length_ratio = len(corrected) / max(1, len(original))
    if not 0.6 <= length_ratio <= 1.6:
        return "rejected_length_ratio"
    return "unchanged" if original_text == corrected_text else "corrected"


def correct_chunk(segments: Sequence[CorrectionInput], response: ProviderResult, *,
                  first_id: int = 1) -> list[CorrectedSegment]:
    """Validate a whole provider chunk and retain originals for unsafe results."""
    outcome = response
    fallback = ("fallback_refusal" if outcome.refusal else
                "fallback_provider_error" if outcome.error else "fallback_malformed")
    parsed = None if outcome.refusal or outcome.error else parse_numbered_lines(
        outcome.text, list(range(first_id, first_id + len(segments))))
    if parsed is None:
        return [CorrectedSegment(item.start, item.end, item.text, item.text, fallback)
                for item in segments]
    result = []
    for offset, item in enumerate(segments):
        corrected = parsed[first_id + offset]
        status = _guard_status(item.text, corrected)
        if status.startswith("rejected_"):
            corrected = item.text
        result.append(CorrectedSegment(item.start, item.end, item.text, corrected, status))
    return result


@dataclass(frozen=True, slots=True)
class CorrectTranscriptConfig:
    model: str = "gpt-6-luna"
    base_url: str | None = None
    locality: str = "remote"
    language: str = "auto"


def consent_required(source_kind: str, locality: str) -> bool:
    return source_kind == "local" and locality != "local"


def correction_skip_reason(*, no_correct: bool, source: str, api_key: str | None) -> str | None:
    if no_correct:
        return "disabled"
    if source == "manual":
        return "manual subtitles"
    if source == "none":
        return "no speech source"
    if not api_key:
        return "no API key"
    return None


class OpenAIChatClient:
    """Lazy official SDK adapter for the bounded Chat Completions contract."""

    def __init__(self, *, api_key: str, base_url: str | None = None,
                 timeout: float = 120.0, max_retries: int = 2) -> None:
        from openai import OpenAI
        options: dict[str, Any] = {"api_key": api_key, "timeout": timeout, "max_retries": max_retries}
        if base_url:
            options["base_url"] = base_url
        self._client = OpenAI(**options)

    def correct(self, *, model: str, system_prompt: str, user_prompt: str) -> Any:
        return self._client.chat.completions.create(
            model=model, reasoning_effort="low",
            messages=[{"role": "system", "content": system_prompt},
                      {"role": "user", "content": user_prompt}])


def _value(item: Any, key: str, default: Any = None) -> Any:
    return item.get(key, default) if isinstance(item, Mapping) else getattr(item, key, default)


def _provider_result(response: Any) -> ProviderResult:
    if isinstance(response, ProviderResult):
        return response
    choices = _value(response, "choices", []) or []
    message = _value(choices[0], "message") if choices else None
    text = _value(message, "content") if message is not None else _value(response, "text")
    refusal = bool(_value(message, "refusal")) if message is not None else False
    usage_data = _value(response, "usage")
    prompt_details = _value(usage_data, "prompt_tokens_details")
    completion_details = _value(usage_data, "completion_tokens_details")
    usage = TokenUsage(
        int(_value(usage_data, "prompt_tokens", 0) or 0),
        int(_value(prompt_details, "cached_tokens", 0) or 0),
        int(_value(usage_data, "completion_tokens", 0) or 0),
        int(_value(completion_details, "reasoning_tokens", 0) or 0),
        int(_value(usage_data, "total_tokens", 0) or 0))
    return ProviderResult(text, refusal, False, usage)


def _base_url_identity(base_url: str | None) -> str:
    from urllib.parse import urlsplit, urlunsplit
    value = base_url or "https://api.openai.com/v1"
    parts = urlsplit(value)
    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    return urlunsplit((parts.scheme, host, parts.path.rstrip("/"), "", ""))


def _load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


def _write_manifest(path: Path, manifest: dict[str, Any]) -> None:
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")


def _source_data(video: Path, work: Path, transcript: Any, raw_info: Mapping[str, Any],
                 metadata: Mapping[str, Any], language: str) -> tuple[Any, list[Any]]:
    from bundle import read_speech_segments, select_speech_source
    transcript_language = transcript.get("language") if isinstance(transcript, Mapping) else None
    info = dict(raw_info)
    info.setdefault("language", metadata.get("language"))
    source = select_speech_source(language, info, [item for item in work.iterdir() if item.is_file()],
                                  transcript_language=transcript_language,
                                  transcript_available=isinstance(transcript, Mapping)
                                  and isinstance(transcript.get("segments"), list),
                                  video_stem=video.stem)
    return source, read_speech_segments(source, work, transcript if isinstance(transcript, Mapping) else {})


def _valid_output(path: Path, params: Mapping[str, Any], source: Any, segments: Sequence[Any]) -> bool:
    try:
        output = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    expected_source = {key: params["source"][key] for key in ("kind", "language", "file")}
    return (output.get("schema_version") == 1 and output.get("source") == expected_source
            and output.get("model") == params["model"] and output.get("prompt_version") == PROMPT_VERSION
            and isinstance(output.get("segments"), list) and len(output["segments"]) == len(segments)
            and all(row.get("start") == item.start and row.get("end") == item.end
                    and row.get("original_text") == item.text
                    for row, item in zip(output["segments"], segments)))


def run_correction(video: Path, *, language: str = "auto", model: str | None = None,
                   base_url: str | None = None, locality: str | None = None,
                   allow_remote_ai: bool = False, no_correct: bool = False,
                   api_key: str | None = None, environ: Mapping[str, str] | None = None,
                   client_factory: Callable[..., Any] | None = None,
                   interactive: bool | None = None) -> int:
    """Select, authorize, run, and safely persist one correction stage."""
    from bundle import SpeechSource
    from tools import media_source_kind, same_video
    environ = os.environ if environ is None else environ
    video = video.resolve()
    work = video.parent / f"{video.stem}.analysis"
    if not video.is_file() or not work.is_dir():
        print("video or analysis folder is missing", file=sys.stderr)
        return 2
    model = model or environ.get("VIDEO_ANALYSIS_CORRECT_MODEL") or "gpt-6-luna"
    base_url = base_url or environ.get("OPENAI_BASE_URL") or None
    locality = locality or environ.get("VIDEO_ANALYSIS_AI_LOCALITY") or "remote"
    api_key = api_key if api_key is not None else environ.get("OPENAI_API_KEY")
    if not api_key and locality == "local" and base_url:
        api_key = "local-no-key"
    metadata = _load_json(work / "metadata.json", {})
    raw_info = _load_json(work / "raw_info.json", {})
    transcript = _load_json(work / "transcript.json", {})
    manifest = _load_json(work / "manifest.json", {"schema_version": 2, "params": {}, "stages": {}})
    identity = {"path": str(video), "size": video.stat().st_size, "mtime_ns": video.stat().st_mtime_ns}
    recorded_source = manifest.get("source") or {}
    if recorded_source and not same_video(recorded_source, identity):
        print("correction manifest conflict: video identity changed", file=sys.stderr)
        return 3
    source_kind = media_source_kind(manifest)
    manifest["source"] = {**identity, "kind": source_kind}
    source, speech = _source_data(video, work, transcript, raw_info, metadata, language)
    source_file = Path(source.file).name if source.file else None
    source_value = {"kind": source.source, "language": source.language, "file": source_file}
    source_path = work / (source_file or "")
    stat = source_path.stat() if source_path.is_file() else None
    source_value.update(size=stat.st_size if stat else 0, mtime_ns=stat.st_mtime_ns if stat else 0)
    params = {"model": model, "base_url_identity": _base_url_identity(base_url), "locality": locality,
              "prompt_version": PROMPT_VERSION, "chunk_size": CHUNK_SIZE, "source": source_value}
    params_root = manifest.setdefault("params", {})
    stages = manifest.setdefault("stages", {})
    stage = stages.get("correct_transcript") or {}
    previous = params_root.get("correct_transcript")
    output_path = work / "transcript.corrected.json"
    if stage.get("status") == "done":
        if previous != params or not _valid_output(output_path, params, source, speech):
            print("correction manifest conflict: completed output or settings changed", file=sys.stderr)
            return 3
        print("CORRECTION: resumed")
        return 0
    skip = correction_skip_reason(no_correct=no_correct, source=source.source, api_key=api_key)
    if skip:
        stages["correct_transcript"] = {"status": "skipped", "reason": skip}
        _write_manifest(work / "manifest.json", manifest)
        print(f"CORRECTION: skipped | {skip}")
        return 0
    if previous and previous != params and stage.get("status") == "done":
        return 3
    consent: dict[str, Any] | None = None
    if consent_required(source_kind, locality):
        if allow_remote_ai:
            consent = {"granted": True, "method": "flag"}
        else:
            can_prompt = sys.stdin.isatty() if interactive is None else interactive
            if can_prompt:
                accepted = input("Send transcript text and video context to the configured remote AI? [y/N] ").strip().casefold() == "y"
                if accepted:
                    consent = {"granted": True, "method": "interactive"}
            if consent is None:
                stages["correct_transcript"] = {"status": "denied", "consent": {"granted": False}}
                _write_manifest(work / "manifest.json", manifest)
                print("Remote AI consent is required for transcripts from local media; pass --allow-remote-ai.", file=sys.stderr)
                return 4
    factory = client_factory or (lambda **kwargs: OpenAIChatClient(**kwargs))
    client = factory(api_key=api_key, base_url=base_url, timeout=120.0, max_retries=2)
    language_value = source.language or "unknown"
    chapter_titles = [str(item.get("title") or "") for item in metadata.get("chapters", []) if isinstance(item, Mapping)]
    context = build_context(metadata.get("title"), metadata.get("description"), language_value, chapter_titles)
    chunks = chunk_segments([CorrectionInput(item.start, item.end, item.text) for item in speech])
    def call_chunk(chunk: tuple[int, list[CorrectionInput]]) -> tuple[int, list[CorrectedSegment], TokenUsage]:
        first_id, items = chunk
        try:
            response = client.correct(model=model, system_prompt=SYSTEM_PROMPT,
                                      user_prompt=build_chunk_prompt(context, items, first_id))
            outcome = _provider_result(response)
        except Exception:
            outcome = ProviderResult(None, error=True)
        return first_id, correct_chunk(items, outcome, first_id=first_id), outcome.usage
    results: dict[int, tuple[list[CorrectedSegment], TokenUsage]] = {}
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(call_chunk, chunk) for chunk in chunks]
        for future in as_completed(futures):
            first_id, corrected, usage = future.result()
            results[first_id] = (corrected, usage)
    corrected_rows: list[CorrectedSegment] = []
    usage_totals = {key: 0 for key in ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_tokens", "total_tokens")}
    for first_id, _ in chunks:
        rows, usage = results[first_id]
        corrected_rows.extend(rows)
        for key in usage_totals:
            usage_totals[key] += getattr(usage, key)
    output = {"schema_version": 1, "source": {key: source_value[key] for key in ("kind", "language", "file")},
              "model": model, "endpoint": {"base_url_identity": params["base_url_identity"], "locality": locality},
              "prompt_version": PROMPT_VERSION,
              "segments": [{"start": item.start, "end": item.end, "original_text": item.original_text,
                            "corrected_text": item.corrected_text, "status": item.status}
                           for item in corrected_rows]}
    output_path.write_text(json.dumps(output, ensure_ascii=False, indent=1), encoding="utf-8")
    params_root["correct_transcript"] = params
    counts: dict[str, int] = {}
    for item in corrected_rows:
        counts[item.status] = counts.get(item.status, 0) + 1
    stage_value: dict[str, Any] = {"status": "done", "output": output_path.name,
                                   "chunks": len(chunks), "segments": len(corrected_rows),
                                   "segment_statuses": counts, "usage": usage_totals}
    if consent is not None:
        stage_value["consent"] = consent
    stages["correct_transcript"] = stage_value
    _write_manifest(work / "manifest.json", manifest)
    print(f"CORRECTION: complete | corrected {counts.get('corrected', 0)} | "
          f"rejected {sum(v for k, v in counts.items() if k.startswith('rejected_'))} | "
          f"fallback {sum(v for k, v in counts.items() if k.startswith('fallback_'))}")
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=("Correct selected transcript lines with the configured AI endpoint. "
                     "Output is written to <video folder>/<video name>.analysis/transcript.corrected.json."),
        epilog="Exit codes: 0 success, skip, or resume; 1 processing failure; 2 usage or input error; 3 manifest conflict; 4 remote-AI consent required.")
    parser.add_argument("video", type=Path, help="video whose .analysis transcript should be corrected")
    parser.add_argument("--language", default="auto", help="speech language (default: auto)")
    parser.add_argument("--model", help="correction model (default: VIDEO_ANALYSIS_CORRECT_MODEL or gpt-6-luna)")
    parser.add_argument("--base-url", help="OpenAI-compatible endpoint base URL")
    parser.add_argument("--ai-locality", choices=("local", "remote"), help="explicit endpoint trust declaration (default: remote)")
    parser.add_argument("--allow-remote-ai", action="store_true", help="consent to send transcript text and video context from local media to remote AI")
    parser.add_argument("--no-correct", action="store_true", help="record an intentional offline skip")
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
    except SystemExit as error:
        return 0 if error.code == 0 else 2
    try:
        return run_correction(args.video, language=args.language, model=args.model, base_url=args.base_url,
                              locality=args.ai_locality, allow_remote_ai=args.allow_remote_ai,
                              no_correct=args.no_correct)
    except Exception as error:
        print(f"correction failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
