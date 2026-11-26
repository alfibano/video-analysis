"""Pure transcript-correction contracts used by the correction stage."""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Sequence

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


def correct_chunk(segments: Sequence[CorrectionInput], response: str | None, *,
                  refusal: bool = False, provider_error: bool = False,
                  first_id: int = 1) -> list[CorrectedSegment]:
    """Validate a whole provider chunk and retain originals for unsafe results."""
    fallback = ("fallback_refusal" if refusal else "fallback_provider_error" if provider_error
                else "fallback_malformed")
    parsed = None if refusal or provider_error else parse_numbered_lines(
        response, list(range(first_id, first_id + len(segments))))
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
