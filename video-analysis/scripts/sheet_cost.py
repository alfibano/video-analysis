"""Measured correction usage and conservative writing-cost estimates."""
from __future__ import annotations

import math
import re
from typing import Any, Mapping

PRICE_BASIS_DATE = "2026-11-03"
MODEL_PRICES_USD_PER_MTOK: dict[str, dict[str, Any]] = {
    "claude-fable-5-2": {"input": 8.0, "output": 40.0},
    "claude-opus-5-5": {"input": 4.0, "output": 20.0},
    "claude-sonnet-5-5": {"input": 2.0, "output": 10.0},
    "claude-haiku-5-5": {"input": 0.10, "output": 0.50},
}
_ALIASES = {"opus": "claude-opus-5-5", "sonnet": "claude-sonnet-5-5",
            "haiku": "claude-haiku-5-5", "fable": "claude-fable-5-2"}


def _nonnegative(value: Any) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        return None
    return value


def _price(provider: str | None, model: str, endpoint_identity: str | None) -> dict[str, Any] | None:
    if endpoint_identity != "anthropic-native":
        return None
    normalized = re.sub(r"\s+", "", model.casefold()).replace("_", "-")
    normalized = re.sub(r"\[(?:1m)\]$", "", normalized)
    if normalized in _ALIASES:
        if (provider or "").casefold().replace("_", "-") not in {"claude-code", "anthropic", "claude"}:
            return None
        normalized = _ALIASES[normalized]
    return MODEL_PRICES_USD_PER_MTOK.get(normalized)


def referenced_frame_count(sheet: Mapping[str, Any]) -> int:
    chapters = sheet.get("chapters", [])
    if not isinstance(chapters, list):
        return 0
    return sum(1 for chapter in chapters if isinstance(chapter, Mapping)
               for block in chapter.get("blocks", []) if isinstance(block, Mapping)
               and isinstance(block.get("frame"), Mapping))


def _correction(manifest: Mapping[str, Any]) -> tuple[int, float | str]:
    stages = manifest.get("stages", {})
    stage = stages.get("correct_transcript", {}) if isinstance(stages, Mapping) else {}
    if not isinstance(stage, Mapping) or stage.get("status") != "done":
        return 0, 0.0
    usage = stage.get("usage", {})
    usage = usage if isinstance(usage, Mapping) else {}
    total = _nonnegative(usage.get("total_tokens"))
    if total is None:
        total = sum(_nonnegative(usage.get(k)) or 0 for k in ("input_tokens", "output_tokens", "reasoning_tokens"))
    cost = _nonnegative(stage.get("reported_cost_usd"))
    return int(total), cost if cost is not None else "n/a"


def build_sheet_accounting(*, manifest: Mapping[str, Any], provider: str | None, model: str,
                           endpoint_identity: str | None = None,
                           brief_text: str, bundle_text: str, sheet_text: str,
                           sheet: Mapping[str, Any]) -> dict[str, Any]:
    measured_tokens, measured_usd = _correction(manifest)
    frames = referenced_frame_count(sheet)
    input_tokens = math.ceil((len(brief_text) + len(bundle_text)) / 4) + 1600 * frames
    output_tokens = math.ceil(len(sheet_text) / 4)
    rates = _price(provider, model, endpoint_identity)
    estimated_usd: float | str = (input_tokens * rates["input"] + output_tokens * rates["output"]) / 1_000_000 \
        if rates else "n/a"
    total_usd: float | str = measured_usd + estimated_usd if isinstance(measured_usd, (int, float)) \
        and isinstance(estimated_usd, (int, float)) else "n/a"
    return {"tokens": {"measured": measured_tokens, "estimated_input": input_tokens,
            "estimated_output": output_tokens, "estimated": input_tokens + output_tokens,
            "total": measured_tokens + input_tokens + output_tokens},
        "usd": {"measured": measured_usd, "estimated": estimated_usd, "total": total_usd},
        "basis": {"measured": "correction manifest",
                  "estimated": f"character/frame estimate; API list prices as of {PRICE_BASIS_DATE}"}}
