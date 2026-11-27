"""Safely correct selected transcript text and record a resumable analysis stage."""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import unicodedata
from enum import Enum
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
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
SEGMENT_STATUSES = frozenset({"unchanged", "corrected", "rejected_edit_ratio", "rejected_length_ratio",
                              "fallback_refusal", "fallback_malformed", "fallback_provider_error"})


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
    reported_cost_usd: float | None = None
    failure_category: FailureCategory | None = None
    reported_provider: str | None = None
    reported_model: str | None = None


class FailureCategory(Enum):
    TRANSPORT = "transport"
    MODEL_UNAVAILABLE = "model-unavailable"
    PROVIDER = "provider"


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
    provider: str | None = None
    model: str | None = None


@dataclass(frozen=True, slots=True)
class CorrectionSettings:
    provider: str
    model: str | None
    base_url: str | None
    locality: str
    api_key: str | None
    allow_remote_ai: bool
    no_correct: bool
    interactive: bool | None
    executable: str | None = None
    host: str = "unknown"
    default_model: str | None = None
    selection_reason: str = "configured provider default"
    model_role: str = "default"
    endpoint_identity: str | None = None


class ModelInvocation(Enum):
    HOST_DEFAULT = "host-default"
    OVERRIDE = "override"


@dataclass(frozen=True, slots=True)
class CorrectionRequest:
    language: str = "auto"
    model: str | None = None
    base_url: str | None = None
    locality: str | None = None
    allow_remote_ai: bool = False
    no_correct: bool = False
    api_key: str | None = None
    provider: str | None = None
    retry_refused: bool = False
    host: str | None = None


@dataclass(frozen=True, slots=True)
class CorrectionDeps:
    environ: Mapping[str, str]
    client_factory: Callable[..., Any] | None = None
    interactive: bool | None = None
    which: Callable[[str], str | None] = shutil.which
    subprocess_runner: Callable[..., Any] = subprocess.run
    config_reader: Any = None


@dataclass(frozen=True, slots=True)
class SourceSelectionInputs:
    transcript: Any
    raw_info: Mapping[str, Any]
    metadata: Mapping[str, Any]
    language: str


@dataclass(frozen=True, slots=True)
class CorrectionResult:
    rows: Sequence[CorrectedSegment]
    usage: dict[str, int]
    chunk_count: int
    reported_cost_usd: float | None = None
    cost_complete: bool = True
    attempt: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class ChunkRun:
    client: Any
    model: str
    context: str
    max_workers: int = 2
    chunk_ordinals: Sequence[int] | None = None
    invocation: ModelInvocation = ModelInvocation.OVERRIDE
    fallback: ModelFallback | None = None


@dataclass(frozen=True, slots=True)
class ModelFallback:
    host: str
    model: str | None
    invocation: ModelInvocation


@dataclass(frozen=True, slots=True)
class EscalationChoice:
    model: str | None


@dataclass(frozen=True, slots=True)
class RetryTarget:
    output: dict[str, Any]
    chunks: Sequence[int]


@dataclass(frozen=True, slots=True)
class PreparedCorrection:
    state: CorrectionStage
    settings: CorrectionSettings
    retry_model: str
    missing_implicit_host: str | None = None


@dataclass(frozen=True, slots=True)
class CorrectionRuntime:
    request: CorrectionRequest
    deps: CorrectionDeps
    missing_implicit_host: str | None = None


@dataclass(frozen=True, slots=True)
class CorrectionStage:
    video: Path
    work: Path
    metadata: dict[str, Any]
    source: Any
    speech: Sequence[Any]
    source_value: dict[str, Any]
    params: dict[str, Any]
    manifest: dict[str, Any]
    stage: dict[str, Any]
    previous: Any
    output_path: Path


def _load_correction_stage(video: Path, work: Path, language: str,
                           settings: CorrectionSettings) -> tuple[CorrectionStage | None, int | None]:
    from tools import media_source_kind, same_video
    if not video.is_file() or not work.is_dir():
        print("video or analysis folder is missing", file=sys.stderr)
        return None, 2
    metadata = _load_json(work / "metadata.json", {})
    raw_info = _load_json(work / "raw_info.json", {})
    transcript = _load_json(work / "transcript.json", {})
    manifest = _load_json(work / "manifest.json", {"schema_version": 2, "params": {}, "stages": {}})
    identity = {"path": str(video), "size": video.stat().st_size, "mtime_ns": video.stat().st_mtime_ns}
    recorded_source = manifest.get("source") or {}
    if recorded_source and not same_video(recorded_source, identity):
        print("correction manifest conflict: video identity changed", file=sys.stderr)
        return None, 3
    source_kind = media_source_kind(manifest)
    manifest["source"] = {**identity, "kind": source_kind}
    source, speech = _source_data(video, work, SourceSelectionInputs(
        transcript, raw_info, metadata, language))
    source_file = Path(source.file).name if source.file else None
    source_value = {"kind": source.source, "language": source.language, "file": source_file}
    source_path = work / (source_file or "")
    stat = source_path.stat() if source_path.is_file() else None
    source_value.update(size=stat.st_size if stat else 0, mtime_ns=stat.st_mtime_ns if stat else 0)
    selection_identity = {"host": settings.host, "provider": settings.provider, "model": settings.model,
                          "endpoint_identity": settings.endpoint_identity, "locality": settings.locality}
    params = {"provider": settings.provider, "model": settings.model,
              "selection_identity": selection_identity,
              "base_url_identity": (_base_url_identity(settings.base_url)
                                    if settings.provider in {"openai", "local"} else None),
              "locality": settings.locality, "prompt_version": PROMPT_VERSION,
              "chunk_size": CHUNK_SIZE, "source": source_value}
    params_root = manifest.setdefault("params", {})
    stages = manifest.setdefault("stages", {})
    state = CorrectionStage(video, work, metadata, source, speech, source_value, params, manifest,
                            stages.get("correct_transcript") or {}, params_root.get("correct_transcript"),
                            work / "transcript.corrected.json")
    return state, None


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


def resolve_provider(environ: Mapping[str, str], explicit: str | None = None,
                     which: Callable[[str], str | None] = shutil.which) -> tuple[str | None, str | None]:
    """Resolve explicit, environment, or host-account provider without invoking it."""
    provider = explicit if explicit not in (None, "auto") else environ.get("VIDEO_ANALYSIS_CORRECT_PROVIDER")
    if provider is None or provider == "auto":
        if "CLAUDECODE" in environ:
            provider = "claude-code"
        elif "CODEX_THREAD_ID" in environ or "CODEX_SESSION_ID" in environ:
            provider = "codex"
        elif environ.get("OPENAI_API_KEY"):
            provider = "openai"
        else:
            return None, None
    executable = None
    if provider == "claude-code":
        executable = which("claude")
    elif provider == "codex":
        executable = which("codex") or which("codex.cmd")
    elif provider == "openclaw":
        executable = which("openclaw") or which("openclaw.cmd")
    return provider, executable


def provider_default_model(provider: str) -> str:
    return {"claude-code": "claude-haiku-5-5", "codex": "gpt-6-luna",
            "openai": "gpt-6-luna", "local": "gpt-6-luna"}[provider]


def is_prose_refusal(text: str | None, expected_ids: Sequence[int]) -> bool:
    """Identify explicit refusals only when no expected numbered answer line exists."""
    if not text:
        return False
    expected = set(expected_ids)
    if any((match := re.match(r"^\s*(\d+)\t", line)) and int(match.group(1)) in expected
           for line in text.splitlines()):
        return False
    normalized = unicodedata.normalize("NFKC", text).casefold()
    refusal = ("cannot", "can't", "unable to", "must refuse", "ne peux pas",
               "ne suis pas en mesure", "dois refuser")
    denial = ("cannot", "can't", "unable", "must refuse", "refuse", "ne peux pas", "dois refuser")
    copyright = ("copyright policy", "copyrighted text", "droit d'auteur", "droits d'auteur")
    return any(item in normalized for item in refusal) or (
        any(item in normalized for item in copyright) and any(item in normalized for item in denial))


class OpenAIChatClient:
    """Lazy official SDK adapter for the bounded Chat Completions contract."""

    def __init__(self, *, api_key: str, base_url: str | None = None,
                 timeout: float = 120.0, max_retries: int = 2) -> None:
        from openai import OpenAI
        options: dict[str, Any] = {"api_key": api_key, "timeout": timeout, "max_retries": max_retries}
        if base_url:
            options["base_url"] = base_url
        self._client = OpenAI(**options)

    def correct(self, *, model: str | None, system_prompt: str, user_prompt: str,
                invocation: ModelInvocation = ModelInvocation.OVERRIDE) -> Any:
        if model is None:
            raise ValueError("OpenAI SDK requires an explicit model")
        return self._client.chat.completions.create(
            model=model, reasoning_effort="low",
            messages=[{"role": "system", "content": system_prompt},
                      {"role": "user", "content": user_prompt}])


def parse_claude_result(stdout: str) -> ProviderResult:
    """Normalize Claude's JSON response and usage without retaining raw output."""
    data = json.loads(stdout)
    usage = data.get("usage") or {}
    cache_write = int(usage.get("cache_creation_input_tokens") or 0)
    cached = int(usage.get("cache_read_input_tokens") or 0)
    input_tokens = int(usage.get("input_tokens") or 0) + cache_write + cached
    output_tokens = int(usage.get("output_tokens") or 0)
    total = int(usage.get("total_tokens") or (input_tokens + output_tokens))
    cost = data.get("total_cost_usd")
    return ProviderResult(data.get("result"), usage=TokenUsage(
        input_tokens, cached, output_tokens, 0, total),
        reported_cost_usd=float(cost) if cost is not None else None)


def parse_codex_result(stdout: str) -> ProviderResult:
    """Read the last completed Codex answer and turn usage from JSONL."""
    answer, usage = None, {}
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        item = event.get("item") or {}
        if event.get("type") == "item.completed" and item.get("type") == "agent_message":
            answer = item.get("text")
        if event.get("type") == "turn.completed":
            usage = event.get("usage") or {}
    if answer is None:
        raise ValueError("missing Codex answer")
    input_tokens = int(usage.get("input_tokens") or 0)
    cached = int(usage.get("cached_input_tokens") or 0)
    output_tokens = int(usage.get("output_tokens") or 0)
    reasoning = int(usage.get("reasoning_output_tokens") or 0)
    total = int(usage.get("total_tokens") or (input_tokens + output_tokens + reasoning))
    return ProviderResult(answer, usage=TokenUsage(input_tokens, cached, output_tokens, reasoning, total))


def _classify_failure(error: BaseException | None = None, output: str = "") -> FailureCategory:
    text = output.lower()
    error_name = type(error).__name__.lower() if error is not None else ""
    transport_error = isinstance(error, (TimeoutError, ConnectionError, subprocess.TimeoutExpired))
    transport_error = transport_error or any(word in error_name for word in ("timeout", "connection"))
    if transport_error or any(phrase in text for phrase in (
            "timed out", "timeout", "connection refused", "connection reset", "connection error")):
        return FailureCategory.TRANSPORT
    if any(phrase in text for phrase in ("model not found", "model_not_found", "unknown model",
                                         "model unavailable", "model is unavailable", "model not available",
                                         "no such model", "does not exist")):
        return FailureCategory.MODEL_UNAVAILABLE
    return FailureCategory.PROVIDER


def _failure_result(error: BaseException | None = None, output: str = "") -> ProviderResult:
    return ProviderResult(None, error=True, failure_category=_classify_failure(error, output))


class ClaudeCodeClient:
    """Ephemeral Claude Code adapter using the logged-in host account."""

    def __init__(self, *, executable: str, runner: Callable[..., Any]) -> None:
        self.executable, self.runner = executable, runner

    def correct(self, *, model: str | None, system_prompt: str, user_prompt: str,
                invocation: ModelInvocation = ModelInvocation.OVERRIDE) -> ProviderResult:
        from tools import app_temp_dir
        neutral = app_temp_dir() / "correction-neutral"
        neutral.mkdir(parents=True, exist_ok=True)
        command = [self.executable, "-p"]
        if invocation is ModelInvocation.OVERRIDE and model:
            command.extend(("--model", model))
        command.extend(["--output-format", "json",
                   "--system-prompt", system_prompt, "--tools", "", "--strict-mcp-config",
                   "--disable-slash-commands", "--no-session-persistence", "--setting-sources", "project"])
        try:
            done = self.runner(command, input=user_prompt, capture_output=True, text=True,
                               encoding="utf-8", cwd=str(neutral), timeout=900,
                               shell=False, check=False)
            if done.returncode != 0:
                return _failure_result(output=f"{done.stdout}\n{done.stderr}")
            return parse_claude_result(done.stdout)
        except Exception as error:
            return _failure_result(error)


class CodexClient:
    """Ephemeral Codex adapter using the logged-in host account."""

    def __init__(self, *, executable: str, runner: Callable[..., Any]) -> None:
        self.executable, self.runner = executable, runner

    def correct(self, *, model: str | None, system_prompt: str, user_prompt: str,
                invocation: ModelInvocation = ModelInvocation.OVERRIDE) -> ProviderResult:
        from tools import app_temp_dir
        neutral = app_temp_dir() / "correction-neutral"
        neutral.mkdir(parents=True, exist_ok=True)
        command = [self.executable, "exec"]
        if invocation is ModelInvocation.OVERRIDE and model:
            command.extend(("-m", model))
        command.extend(["--json", "--skip-git-repo-check",
                   "--ephemeral", "-s", "read-only", "-C", str(neutral), "-c",
                   'model_reasoning_effort="low"', "-"])
        try:
            done = self.runner(command, input=f"{system_prompt}\n\n{user_prompt}",
                               capture_output=True, text=True, encoding="utf-8",
                               cwd=str(neutral), timeout=900, shell=False, check=False)
            if done.returncode != 0:
                return _failure_result(output=f"{done.stdout}\n{done.stderr}")
            return parse_codex_result(done.stdout)
        except Exception as error:
            return _failure_result(error)


class OpenClawClient:
    """One-shot OpenClaw inference adapter; transcript data never enters argv."""

    def __init__(self, *, executable: str, runner: Callable[..., Any]) -> None:
        self.executable, self.runner = executable, runner

    def correct(self, *, model: str | None, system_prompt: str, user_prompt: str,
                invocation: ModelInvocation = ModelInvocation.OVERRIDE) -> ProviderResult:
        from tools import app_temp_dir
        neutral = app_temp_dir() / "correction-neutral"
        neutral.mkdir(parents=True, exist_ok=True)
        command = [self.executable, "infer", "model", "run", "--prompt-stdin"]
        if invocation is ModelInvocation.OVERRIDE and model:
            command.extend(("--model", model))
        command.append("--json")
        try:
            done = self.runner(command, input=f"{system_prompt}\n\n{user_prompt}", capture_output=True,
                               text=True, encoding="utf-8", cwd=str(neutral), timeout=900,
                               shell=False, check=False)
            if done.returncode:
                return _failure_result(output=f"{done.stdout}\n{done.stderr}")
            payload = json.loads(done.stdout)
            if not isinstance(payload, dict):
                return _failure_result(output="OpenClaw returned a non-object JSON envelope")
            text = next((payload[key] for key in ("text", "output", "final", "content")
                         if isinstance(payload.get(key), str)), None)
            return ProviderResult(text, usage=_openclaw_usage(payload.get("usage")),
                                  reported_provider=payload.get("provider"),
                                  reported_model=payload.get("model"))
        except Exception as error:
            return _failure_result(error)


def _openclaw_usage(value: Any) -> TokenUsage:
    if not isinstance(value, Mapping):
        return TokenUsage()
    incoming, outgoing = _usage_count(value.get("input_tokens")), _usage_count(value.get("output_tokens"))
    return TokenUsage(incoming, _usage_count(value.get("cached_input_tokens")), outgoing,
                      _usage_count(value.get("reasoning_tokens")),
                      _usage_count(value.get("total_tokens")) or incoming + outgoing)


def _usage_count(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


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
    cost = _value(usage_data, "cost_usd")
    return ProviderResult(text, refusal, False, usage,
                          float(cost) if cost is not None else None)


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


def _source_data(video: Path, work: Path, inputs: SourceSelectionInputs) -> tuple[Any, list[Any]]:
    from bundle import read_speech_segments, select_speech_source
    transcript_language = inputs.transcript.get("language") if isinstance(inputs.transcript, Mapping) else None
    info = dict(inputs.raw_info)
    info.setdefault("language", inputs.metadata.get("language"))
    source = select_speech_source(inputs.language, info, [item for item in work.iterdir() if item.is_file()],
                                  transcript_language=transcript_language,
                                  transcript_available=isinstance(inputs.transcript, Mapping)
                                  and isinstance(inputs.transcript.get("segments"), list),
                                  video_stem=video.stem)
    return source, read_speech_segments(
        source, work, inputs.transcript if isinstance(inputs.transcript, Mapping) else {})


def _correction_source_matches(work: Path, params: Mapping[str, Any], source: Any) -> bool:
    if source.source not in {"whisper", "automatic"}:
        return False
    source_path = work / Path(source.file or "")
    try:
        stat = source_path.stat()
        source_params = params["source"]
    except (OSError, KeyError, TypeError):
        return False
    if not isinstance(source_params, Mapping):
        return False
    source_file = Path(source.file or "").name
    if (source_params.get("kind") != source.source or source_params.get("language") != source.language
            or source_params.get("file") != source_file or source_params.get("size") != stat.st_size
            or source_params.get("mtime_ns") != stat.st_mtime_ns):
        return False
    return True


def _correction_output_matches(output: Any, params: Mapping[str, Any], source: Any,
                               segments: Sequence[Any]) -> list[str] | None:
    if not isinstance(output, dict) or not _correction_source_matches_from_params(params, source):
        return None
    expected_source = {key: params["source"][key] for key in ("kind", "language", "file")}
    rows = output.get("segments")
    endpoint = {"base_url_identity": params.get("base_url_identity"),
                "locality": params.get("locality")}
    if (output.get("schema_version") != 2 or output.get("source") != expected_source
            or output.get("provider") != params.get("provider")
            or output.get("model") != params.get("model") or output.get("endpoint") != endpoint
            or output.get("prompt_version") != PROMPT_VERSION or not isinstance(rows, list)
            or len(rows) != len(segments)):
        return None
    corrected: list[str] = []
    for row, original in zip(rows, segments):
        if (not isinstance(row, dict) or row.get("start") != original.start
                or row.get("end") != original.end or row.get("original_text") != original.text
                or not isinstance(row.get("corrected_text"), str)
                or not isinstance(row.get("provider"), str) or not row.get("provider")
                or not isinstance(row.get("model"), str) or not row.get("model")
                or not row["corrected_text"].strip() or row.get("status") not in SEGMENT_STATUSES):
            return None
        corrected.append(row["corrected_text"])
    return corrected


def _correction_source_matches_from_params(params: Mapping[str, Any], source: Any) -> bool:
    source_params = params.get("source")
    return (isinstance(source_params, Mapping) and source_params.get("kind") == source.source
            and source_params.get("language") == source.language
            and source_params.get("file") == Path(source.file or "").name)


def load_matching_correction(work: Path, params: Mapping[str, Any], source: Any,
                              segments: Sequence[Any]) -> list[str] | None:
    """Load corrected text only when its complete source and segment contract still matches."""
    if not _correction_source_matches(work, params, source):
        return None
    try:
        output = json.loads((work / "transcript.corrected.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    return _correction_output_matches(output, params, source, segments)


def _resolve_settings(request: CorrectionRequest, deps: CorrectionDeps) -> CorrectionSettings:
    from host_models import ExplicitInput, resolve_host_model
    explicit_provider = (request.provider if request.provider not in (None, "auto")
                         else deps.environ.get("VIDEO_ANALYSIS_CORRECT_PROVIDER"))
    explicit_host = request.host or deps.environ.get("VIDEO_ANALYSIS_HOST")
    if explicit_provider in {"claude-code", "codex", "openclaw"}:
        explicit_host = explicit_provider
    host = resolve_host_model(deps.environ, _config_reader(deps),
                              ExplicitInput(host=explicit_host, locality=request.locality))
    provider, executable = resolve_provider(deps.environ, request.provider, deps.which)
    if not provider and request.host and "OPENAI_API_KEY" not in deps.environ:
        provider = request.host
        executable = deps.which({"claude-code": "claude", "codex": "codex", "openclaw": "openclaw"}[provider])
    base_url = request.base_url or deps.environ.get("OPENAI_BASE_URL") or None
    locality = host.locality if provider == host.host else _sdk_locality(request, deps.environ)
    api_key = request.api_key if request.api_key is not None else deps.environ.get("OPENAI_API_KEY")
    if provider in {"local", "openai"} and not api_key and locality == "local" and base_url:
        api_key = "local-no-key"
    explicit_model = request.model or deps.environ.get("VIDEO_ANALYSIS_CORRECT_MODEL")
    host_provider = provider in {"claude-code", "codex", "openclaw"} and provider == host.host
    default_model = host.default_model if host_provider else provider_default_model(provider) if provider in {"openai", "local"} else None
    low_model = host.low_cost_model if host_provider else provider_default_model(provider) if provider in {"openai", "local"} else None
    model = explicit_model if explicit_model is not None else low_model or default_model
    role = "default" if explicit_model is not None or model is None or model == default_model else "low-cost"
    reason = "explicit model selection" if explicit_model is not None else dict(host.reasons).get("low_cost_model", "host default") if host_provider else "SDK provider default"
    return CorrectionSettings(provider or "none", model, base_url, locality, api_key,
                              request.allow_remote_ai, request.no_correct, deps.interactive, executable,
                              host.host, default_model, reason, role, host.endpoint_identity)


def _sdk_locality(request: CorrectionRequest, environ: Mapping[str, str]) -> str:
    return request.locality or environ.get("VIDEO_ANALYSIS_AI_LOCALITY") or "remote"


def _host_provider_conflicts(request: CorrectionRequest, environ: Mapping[str, str]) -> bool:
    selected = request.provider if request.provider not in (None, "auto") else environ.get("VIDEO_ANALYSIS_CORRECT_PROVIDER")
    conflict = bool(request.host and selected and selected != "auto" and selected != request.host)
    if conflict:
        print("explicit host conflicts with correction provider", file=sys.stderr)
    return conflict


def _ready_settings(settings: CorrectionSettings, request: CorrectionRequest,
                    deps: CorrectionDeps) -> tuple[CorrectionSettings, str | None, int | None]:
    known = {"none", "claude-code", "codex", "openclaw", "openai", "local"}
    if settings.provider not in known:
        print("unknown correction provider", file=sys.stderr)
        return settings, None, 2
    explicit = bool((request.provider and request.provider != "auto") or
                    (deps.environ.get("VIDEO_ANALYSIS_CORRECT_PROVIDER") not in (None, "auto")))
    explicit_local = request.locality == "local" or deps.environ.get("VIDEO_ANALYSIS_AI_LOCALITY") == "local"
    if settings.provider == "local" and (not settings.base_url or settings.locality != "local" or not explicit_local):
        print("local provider requires a base URL and explicit local locality", file=sys.stderr)
        return settings, None, 2
    host_provider = settings.provider in {"claude-code", "codex", "openclaw"}
    if host_provider and not settings.executable and explicit:
        print(f"{settings.provider} executable is unavailable", file=sys.stderr)
        return settings, None, 2
    if host_provider and not settings.executable:
        missing = settings.provider
        settings = CorrectionSettings("none", settings.model, settings.base_url, settings.locality,
            settings.api_key, settings.allow_remote_ai, settings.no_correct, settings.interactive,
            None, settings.host, settings.default_model, settings.selection_reason, settings.model_role,
            settings.endpoint_identity)
        return settings, missing, None
    return settings, None, None


def _config_reader(deps: CorrectionDeps) -> Any:
    if deps.config_reader is not None:
        return deps.config_reader
    from host_models import ConfigReader
    return ConfigReader(Path.cwd(), Path.home(),
                        lambda path: path.read_text(encoding="utf-8") if path.is_file() else None)


def _resume_decision(state: CorrectionStage) -> int | None:
    if state.stage.get("status") != "done":
        return None
    if (state.previous != state.params
            or load_matching_correction(state.work, state.params, state.source, state.speech) is None):
        print("correction manifest conflict: completed output or settings changed", file=sys.stderr)
        return 3
    print("CORRECTION: resumed")
    return 0


def ask_or_refuse_remote_ai(settings: CorrectionSettings) -> tuple[bool, str | None, int | None]:
    """Ask for transcript-sharing consent, or return the flag grant/refusal."""
    if settings.allow_remote_ai:
        return True, "flag", None
    can_prompt = (sys.stdin.isatty() and sys.stdout.isatty() if settings.interactive is None
                  else settings.interactive)
    accepted = False
    if can_prompt:
        try:
            accepted = input("Send transcript text and video context to the configured remote AI? [y/N] ").strip().casefold() == "y"
        except EOFError:
            accepted = False
    if accepted:
        return True, "interactive", None
    print("Remote AI consent is required for transcripts from local media; pass --allow-remote-ai.",
          file=sys.stderr)
    return False, None, 4


def _build_stage_context(metadata: Mapping[str, Any], language: str) -> str:
    chapters = [str(item.get("title") or "") for item in metadata.get("chapters", [])
                if isinstance(item, Mapping)]
    return build_context(metadata.get("title"), metadata.get("description"), language, chapters)


def make_client(settings: CorrectionSettings, deps: CorrectionDeps) -> Any:
    """Construct the selected provider adapter using the shared runtime dependencies."""
    if settings.provider == "claude-code":
        return ClaudeCodeClient(executable=settings.executable or "claude", runner=deps.subprocess_runner)
    if settings.provider == "codex":
        return CodexClient(executable=settings.executable or "codex", runner=deps.subprocess_runner)
    if settings.provider == "openclaw":
        return OpenClawClient(executable=settings.executable or "openclaw", runner=deps.subprocess_runner)
    factory = deps.client_factory or (lambda **kwargs: OpenAIChatClient(**kwargs))
    local = settings.locality == "local"
    return factory(api_key=settings.api_key, base_url=settings.base_url,
                   timeout=900.0 if local else 120.0, max_retries=2)


def _consent_decision(state: CorrectionStage, settings: CorrectionSettings
                      ) -> tuple[dict[str, Any] | None, int | None]:
    manifest = state.manifest
    if not consent_required(manifest["source"]["kind"], settings.locality):
        return None, None
    accepted, method, error = ask_or_refuse_remote_ai(settings)
    if accepted:
        return {"granted": True, "method": method}, None
    manifest["stages"]["correct_transcript"] = {"status": "denied", "consent": {"granted": False}}
    _write_manifest(state.work / "manifest.json", manifest)
    return None, error


def _run_chunk(run: ChunkRun, chunk: tuple[int, list[CorrectionInput]]) -> tuple[
        int, list[CorrectedSegment], TokenUsage, float | None, FailureCategory | None]:
    first_id, items = chunk
    try:
        response = run.client.correct(model=run.model, system_prompt=SYSTEM_PROMPT,
                                      user_prompt=build_chunk_prompt(run.context, items, first_id),
                                      invocation=run.invocation)
        outcome = _provider_result(response)
        if not outcome.refusal and is_prose_refusal(outcome.text, list(range(first_id, first_id + len(items)))):
            outcome = ProviderResult(outcome.text, refusal=True, usage=outcome.usage,
                                     reported_cost_usd=outcome.reported_cost_usd)
    except Exception as error:  # Any provider failure must fall back to the original chunk.
        print(f"correction provider failure: {type(error).__name__}", file=sys.stderr)
        outcome = _failure_result(error)
    category = outcome.failure_category or (FailureCategory.PROVIDER if outcome.error else None)
    rows = [CorrectedSegment(row.start, row.end, row.original_text, row.corrected_text, row.status,
                             outcome.reported_provider, outcome.reported_model)
            for row in correct_chunk(items, outcome, first_id=first_id)]
    return (first_id, rows, outcome.usage,
            outcome.reported_cost_usd, category)


def _collect_chunks(run: ChunkRun, chunks: Sequence[tuple[int, list[CorrectionInput]]]
                    ) -> tuple[list[CorrectedSegment], dict[str, int], int, float | None, bool]:
    results: dict[int, tuple[list[CorrectedSegment], TokenUsage, float | None]] = {}
    with ThreadPoolExecutor(max_workers=run.max_workers) as pool:
        futures = [pool.submit(_run_chunk, run, chunk) for chunk in chunks]
        for future in as_completed(futures):
            first_id, rows, usage, cost, _ = future.result()
            results[first_id] = (rows, usage, cost)
    corrected: list[CorrectedSegment] = []
    totals = {key: 0 for key in ("input_tokens", "cached_input_tokens", "output_tokens",
                                 "reasoning_tokens", "total_tokens")}
    reported_cost = 0.0
    cost_complete = bool(chunks)
    for first_id, _ in chunks:
        rows, usage, cost = results[first_id]
        corrected.extend(rows)
        for key in totals:
            totals[key] += getattr(usage, key)
        if cost is None:
            cost_complete = False
        else:
            reported_cost += cost
    return corrected, totals, len(chunks), (reported_cost if cost_complete else None), cost_complete


def _correct_chunks(run: ChunkRun, speech: Sequence[Any]
                    ) -> tuple[list[CorrectedSegment], dict[str, int], int, float | None, bool,
                               dict[str, Any] | None]:
    chunks = chunk_segments([CorrectionInput(item.start, item.end, item.text) for item in speech])
    if run.chunk_ordinals is not None:
        chunks = [chunk for ordinal, chunk in enumerate(chunks, 1) if ordinal in run.chunk_ordinals]
    if run.fallback is None or not chunks:
        return (*_collect_chunks(run, chunks), None)
    return _run_first_chunk_pilot(run, chunks)


def _run_first_chunk_pilot(run: ChunkRun, chunks: Sequence[tuple[int, list[CorrectionInput]]]
                           ) -> tuple[Any, ...]:
    first, remaining = chunks[0], chunks[1:]
    pilot = _run_chunk(run, first)
    if pilot[4] not in {FailureCategory.TRANSPORT, FailureCategory.MODEL_UNAVAILABLE}:
        rest = _collect_chunks(run, remaining)
        return _combine_chunk_runs((pilot[:4],), rest, len(chunks), None)
    target = run.fallback
    print(f"AI fallback: host={target.host} from={run.model or 'host default'} "
          f"to={target.model or 'host default'} reason=first-chunk {pilot[4].value}")
    default_run = ChunkRun(run.client, target.model, run.context, run.max_workers,
                           invocation=target.invocation)
    retried = _run_chunk(default_run, first)
    rest = _collect_chunks(default_run, remaining)
    return _combine_chunk_runs((retried[:4],), rest, len(chunks),
                               {"mode": "fallback", "model": target.model or "host default",
                                "reason": pilot[4].value, "chunks": list(range(1, len(chunks) + 1))})


def _combine_chunk_runs(first: Sequence[tuple[Any, ...]], rest: tuple[Any, ...], count: int,
                        attempt: dict[str, Any] | None) -> tuple[Any, ...]:
    rows = [row for result in first for row in result[1]] + list(rest[0])
    usage = {key: sum(getattr(result[2], key) for result in first) + rest[1][key]
             for key in rest[1]}
    costs = [result[3] for result in first]
    cost_complete = (rest[4] or rest[2] == 0) and all(cost is not None for cost in costs)
    cost = sum(costs) + (rest[3] or 0.0) if cost_complete else None
    return rows, usage, count, cost, cost_complete, attempt


def _write_correction_result(state: CorrectionStage, settings: CorrectionSettings,
                             consent: dict[str, Any] | None, result: CorrectionResult) -> None:
    rows = result.rows
    output = {"schema_version": 2,
              "source": {key: state.source_value[key] for key in ("kind", "language", "file")},
              "provider": settings.provider,
              "model": settings.model,
              "endpoint": {"base_url_identity": state.params["base_url_identity"],
                           "locality": settings.locality},
              "prompt_version": PROMPT_VERSION,
              "segments": [{"start": item.start, "end": item.end, "original_text": item.original_text,
                            "corrected_text": item.corrected_text, "status": item.status,
                            "provider": item.provider or settings.provider,
                            "model": item.model or settings.model} for item in rows]}
    state.output_path.write_text(json.dumps(output, ensure_ascii=False, indent=1), encoding="utf-8")
    state.manifest.setdefault("params", {})["correct_transcript"] = state.params
    counts: dict[str, int] = {}
    for item in rows:
        counts[item.status] = counts.get(item.status, 0) + 1
    stage: dict[str, Any] = {"status": "done", "output": state.output_path.name,
                             "chunks": result.chunk_count, "segments": len(rows),
                             "segment_statuses": counts, "usage": result.usage}
    refused_chunks = _refused_chunk_numbers(rows)
    stage["refused_chunks"] = refused_chunks
    stage["selection"] = _selection_manifest(settings, result.attempt)
    attempt = result.attempt or {"mode": "initial", "model": settings.model,
                                 "chunks": list(range(1, result.chunk_count + 1))}
    attempt = {**attempt, "model": attempt.get("model") or settings.model or "host default"}
    stage["attempts"] = [{**attempt, "provider": settings.provider, "usage": result.usage,
                          "reported_cost_usd": result.reported_cost_usd if result.cost_complete else None,
                          "result_counts": counts}]
    stage["reported_cost_usd"] = result.reported_cost_usd if result.cost_complete else None
    if consent is not None:
        stage["consent"] = consent
    state.manifest["stages"]["correct_transcript"] = stage
    _write_manifest(state.work / "manifest.json", state.manifest)
    summary = (f"CORRECTION: complete | corrected {counts.get('corrected', 0)} | "
               f"rejected {sum(v for k, v in counts.items() if k.startswith('rejected_'))} | "
               f"fallback {sum(v for k, v in counts.items() if k.startswith('fallback_'))}")
    print(summary + (f" | refused chunks {', '.join(map(str, refused_chunks))}" if refused_chunks else ""))


def _selection_manifest(settings: CorrectionSettings, attempt: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"host": settings.host, "model": settings.model, "model_role": settings.model_role,
            "reason": settings.selection_reason, "fallback": attempt is not None,
            "provider": settings.provider,
            "endpoint_identity": settings.endpoint_identity, "locality": settings.locality}


def _print_selection_notice(settings: CorrectionSettings) -> None:
    model = settings.model or "host default"
    print(f"AI selection: host={settings.host} model={model} role={settings.model_role} "
          f"reason={settings.selection_reason} locality={settings.locality}")


def _refused_chunk_numbers(rows: Sequence[CorrectedSegment]) -> list[int]:
    return [index + 1 for index, start in enumerate(range(0, len(rows), CHUNK_SIZE))
            if rows[start:start + CHUNK_SIZE]
            and all(row.status == "fallback_refusal" for row in rows[start:start + CHUNK_SIZE])]


def _retry_refused(state: CorrectionStage, settings: CorrectionSettings, request: CorrectionRequest,
                   deps: CorrectionDeps) -> int:
    stage = state.stage
    retry_target, error = _load_retry_targets(state)
    if error is not None:
        return error
    targets = retry_target.chunks
    if not targets:
        print("CORRECTION: no refused chunks to retry")
        return 0
    _print_selection_notice(settings)
    if settings.provider != state.params.get("provider"):
        print("retry provider changed", file=sys.stderr)
        return 2
    if settings.model == state.params["model"]:
        print("retry model must differ from the recorded model", file=sys.stderr)
        return 2
    consent_record = stage.get("consent")
    needs_consent = consent_required(state.manifest["source"]["kind"], settings.locality)
    if needs_consent and not (isinstance(consent_record, Mapping) and consent_record.get("granted")):
        accepted, _, error = ask_or_refuse_remote_ai(settings)
        if not accepted:
            return error or 4
    if settings.provider not in {"claude-code", "codex", "openclaw"}:
        print("refused-chunk retry is supported for host-account providers", file=sys.stderr)
        return 2
    client = make_client(settings, deps)
    context = _build_stage_context(state.metadata, state.source.language or "unknown")
    invocation = ModelInvocation.HOST_DEFAULT if settings.model is None else ModelInvocation.OVERRIDE
    run = ChunkRun(client, settings.model, context, 2, targets, invocation)
    new_rows, new_usage, chunk_count, cost, cost_complete, _ = _correct_chunks(run, state.speech)
    result = CorrectionResult(new_rows, new_usage, chunk_count, cost, cost_complete)
    return _write_retry_result(state, settings, retry_target, result)


def _load_retry_targets(state: CorrectionStage) -> tuple[RetryTarget | None, int | None]:
    stage = state.stage
    if stage.get("status") != "done" or not isinstance(stage.get("refused_chunks"), list):
        print("refused-chunk retry requires a completed correction stage", file=sys.stderr)
        return None, 3
    if state.previous != state.params or load_matching_correction(
            state.work, state.params, state.source, state.speech) is None:
        print("completed correction settings or output changed", file=sys.stderr)
        return None, 3
    targets = [int(value) for value in stage["refused_chunks"]]
    output = json.loads(state.output_path.read_text(encoding="utf-8"))
    expected_refused = [index + 1 for index, start in enumerate(range(0, len(output.get("segments", [])), CHUNK_SIZE))
                        if all(row.get("status") == "fallback_refusal"
                               for row in output["segments"][start:start + CHUNK_SIZE])]
    if stage["refused_chunks"] != expected_refused:
        print("recorded refused chunks do not match the corrected output", file=sys.stderr)
        return None, 3
    return RetryTarget(output, targets), None


def _write_retry_result(state: CorrectionStage, settings: CorrectionSettings,
                        target: RetryTarget, result: CorrectionResult) -> int:
    output, targets, new_rows = target.output, target.chunks, result.rows
    new_usage, cost, cost_complete = result.usage, result.reported_cost_usd, result.cost_complete
    stage = state.stage
    rows = output["segments"]
    retry_counts: dict[str, int] = {}
    cursor = 0
    for ordinal in targets:
        start = (ordinal - 1) * CHUNK_SIZE
        length = min(CHUNK_SIZE, len(rows) - start)
        for offset in range(length):
            item = new_rows[cursor]
            rows[start + offset].update(corrected_text=item.corrected_text, status=item.status,
                                       provider=settings.provider, model=settings.model or "host default")
            retry_counts[item.status] = retry_counts.get(item.status, 0) + 1
            cursor += 1
    state.output_path.write_text(json.dumps(output, ensure_ascii=False, indent=1), encoding="utf-8")
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    stage["refused_chunks"] = [index + 1 for index, start in enumerate(range(0, len(rows), CHUNK_SIZE))
                               if all(row["status"] == "fallback_refusal"
                                      for row in rows[start:start + CHUNK_SIZE])]
    stage["segment_statuses"] = counts
    stage["usage"] = {key: int(stage.get("usage", {}).get(key, 0)) + new_usage.get(key, 0)
                      for key in new_usage}
    stage["attempts"].append({"mode": "retry-refused", "provider": settings.provider,
                              "model": settings.model or "host default", "chunks": targets, "usage": new_usage,
                              "reported_cost_usd": cost if cost_complete else None,
                              "result_counts": retry_counts})
    all_costs = [attempt.get("reported_cost_usd") for attempt in stage["attempts"]]
    stage["reported_cost_usd"] = sum(all_costs) if all(value is not None for value in all_costs) else None
    _write_manifest(state.work / "manifest.json", state.manifest)
    remaining = stage["refused_chunks"]
    print(f"CORRECTION: retried {len(targets)} refused chunks | remaining {len(remaining)}")
    return 0


def _prepare_correction(video: Path, request: CorrectionRequest,
                        deps: CorrectionDeps) -> tuple[PreparedCorrection | None, int | None]:
    video = video.resolve()
    work = video.parent / f"{video.stem}.analysis"
    if _host_provider_conflicts(request, deps.environ):
        return None, 2
    settings = _resolve_settings(request, deps)
    settings, missing_implicit_host, readiness_error = _ready_settings(settings, request, deps)
    if readiness_error is not None:
        return None, readiness_error
    retry_model = request.model if request.model is not None else settings.default_model
    if request.retry_refused:
        saved_manifest = _load_json(work / "manifest.json", {})
        saved_params = (saved_manifest.get("params") or {}).get("correct_transcript") or {}
        if saved_params.get("model"):
            settings = CorrectionSettings(settings.provider, saved_params["model"], settings.base_url,
                                          settings.locality, settings.api_key, settings.allow_remote_ai,
                                          settings.no_correct, settings.interactive, settings.executable,
                                          settings.host, settings.default_model, settings.selection_reason,
                                          settings.model_role, settings.endpoint_identity)
    state, load_error = _load_correction_stage(video, work, request.language, settings)
    if state is None:
        return None, load_error or 1
    return PreparedCorrection(state, settings, retry_model, missing_implicit_host), None


def _run_loaded_correction(prepared: PreparedCorrection, request: CorrectionRequest,
                           deps: CorrectionDeps) -> int:
    state, settings = prepared.state, prepared.settings
    if request.retry_refused:
        retry_settings = CorrectionSettings(settings.provider, prepared.retry_model, settings.base_url,
                                            settings.locality, settings.api_key, settings.allow_remote_ai,
                                            settings.no_correct, settings.interactive, settings.executable,
                                            settings.host, settings.default_model, "user-selected refusal retry",
                                            "default", settings.endpoint_identity)
        return _retry_refused(state, retry_settings, request, deps)
    resumed = _resume_decision(state)
    if resumed is not None:
        return resumed
    runtime = CorrectionRuntime(request, deps, prepared.missing_implicit_host)
    return _continue_correction(state, settings, runtime)


def _continue_correction(state: CorrectionStage, settings: CorrectionSettings,
                         runtime: CorrectionRuntime) -> int:
    api_key = settings.api_key if settings.provider in {"openai", "local"} else (
        "host-account" if settings.provider in {"claude-code", "codex", "openclaw"} else None)
    skip = correction_skip_reason(no_correct=settings.no_correct, source=state.source.source, api_key=api_key)
    if settings.provider == "none" and skip == "no API key":
        skip = (f"{runtime.missing_implicit_host} executable is unavailable" if runtime.missing_implicit_host
                else "no available correction provider")
    if skip:
        state.manifest["stages"]["correct_transcript"] = {"status": "skipped", "reason": skip}
        _write_manifest(state.work / "manifest.json", state.manifest)
        print(f"CORRECTION: skipped | {skip}")
        return 0
    return _execute_correction(state, settings, runtime)


def _execute_correction(state: CorrectionStage, settings: CorrectionSettings,
                        runtime: CorrectionRuntime) -> int:
    _print_selection_notice(settings)
    consent, consent_error = _consent_decision(state, settings)
    if consent_error is not None:
        return consent_error
    client = make_client(settings, runtime.deps)
    context = _build_stage_context(state.metadata, state.source.language or "unknown")
    invocation = ModelInvocation.HOST_DEFAULT if settings.model is None else ModelInvocation.OVERRIDE
    run = ChunkRun(client, settings.model, context, 1 if settings.locality == "local" else 2,
                   invocation=invocation)
    run = _fallback_enabled_run(run, settings)
    corrected_rows, usage_totals, chunk_count, reported_cost, cost_complete, attempt = _correct_chunks(run, state.speech)
    effective_model = (attempt.get("model") if attempt else None) or settings.model or "host default"
    corrected_rows = [CorrectedSegment(row.start, row.end, row.original_text, row.corrected_text,
                                        row.status, row.provider or settings.provider,
                                        row.model or effective_model) for row in corrected_rows]
    _write_correction_result(state, settings, consent,
                             CorrectionResult(corrected_rows, usage_totals, chunk_count,
                                               reported_cost, cost_complete, attempt))
    _print_escalation_if_needed(state, settings, runtime.request, consent)
    return 0


def _fallback_enabled_run(run: ChunkRun, settings: CorrectionSettings) -> ChunkRun:
    eligible = (settings.model_role == "low-cost" and settings.model != settings.default_model
                and settings.provider in {"claude-code", "codex", "openclaw"})
    if not eligible:
        return run
    invocation = ModelInvocation.OVERRIDE if settings.default_model else ModelInvocation.HOST_DEFAULT
    fallback = ModelFallback(settings.host, settings.default_model, invocation)
    return ChunkRun(run.client, run.model, run.context, run.max_workers,
                    invocation=run.invocation, fallback=fallback)


def _print_escalation_if_needed(state: CorrectionStage, settings: CorrectionSettings,
                                request: CorrectionRequest, consent: dict[str, Any] | None) -> None:
    stage = state.manifest["stages"]["correct_transcript"]
    refused = stage.get("refused_chunks", [])
    attempts = stage.get("attempts", [])
    effective_model = attempts[-1].get("model", settings.model) if attempts else settings.model
    if effective_model == "host default":
        effective_model = settings.default_model
    choice = _escalation_choice(settings.provider, effective_model, settings.default_model)
    if refused and choice and settings.provider in {"claude-code", "codex", "openclaw"}:
        command = [sys.executable, str(Path(__file__).resolve()), str(state.video), "--provider", settings.provider,
                   "--language", request.language, "--retry-refused"]
        if request.host:
            command.extend(("--host", request.host))
        if choice.model:
            command.extend(("--model", choice.model))
        if settings.locality == "local":
            command.extend(("--ai-locality", "local"))
        if consent:
            command.append("--allow-remote-ai")
        rendered = subprocess.list2cmdline(command) if os.name == "nt" else shlex.join(command)
        print(f"Refused chunks: {', '.join(map(str, refused))}. Escalation command: {rendered}")


def _escalation_choice(provider: str, effective: str | None,
                       default: str | None) -> EscalationChoice | None:
    from host_models import model_sibling
    effective_model = None if effective == "host default" else effective
    if effective_model == default:
        return None
    family_provider = "anthropic" if provider == "claude-code" else "openai"
    return EscalationChoice(model_sibling(family_provider, effective_model, "bigger") or default)


def run_correction(video: Path, request: CorrectionRequest | None = None,
                   deps: CorrectionDeps | None = None) -> int:
    """Select, authorize, run, and safely persist one correction stage."""
    request, deps = request or CorrectionRequest(), deps or CorrectionDeps(os.environ)
    prepared, error = _prepare_correction(video, request, deps)
    return error if prepared is None else _run_loaded_correction(prepared, request, deps)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=("Correct selected transcript lines with the configured AI endpoint. "
                     "Output is written to <video folder>/<video name>.analysis/transcript.corrected.json."),
        epilog="Exit codes: 0 success, skip, or resume; 1 processing failure; 2 usage or input error; 3 manifest conflict; 4 remote-AI consent required.")
    parser.add_argument("video", type=Path, help="video whose .analysis transcript should be corrected")
    parser.add_argument("--language", default="auto", help="speech language (default: auto)")
    parser.add_argument("--provider", choices=("auto", "claude-code", "codex", "openclaw", "openai", "local"),
                        help="correction provider (default: VIDEO_ANALYSIS_CORRECT_PROVIDER or auto)")
    parser.add_argument("--host", choices=("claude-code", "codex", "openclaw"), help="AI host supplying model routing")
    parser.add_argument("--model", help="correction model override (default: provider-specific cheap model)")
    parser.add_argument("--retry-refused", action="store_true", help="retry only previously refused chunks")
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
        request = CorrectionRequest(language=args.language, model=args.model, base_url=args.base_url,
                                    locality=args.ai_locality, allow_remote_ai=args.allow_remote_ai,
                                    no_correct=args.no_correct, provider=args.provider,
                                    retry_refused=args.retry_refused, host=args.host)
        return run_correction(args.video, request)
    except Exception as error:
        print(f"correction failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
