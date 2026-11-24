"""Lazy adapter for local RapidOCR key-frame recognition."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Iterable


class KeyFrameOcr:
    """Create one OCR engine on first use and recognize only key frames."""

    def __init__(self, engine_factory: Callable[[], Any] | None = None, min_score: float = 0.5):
        self.engine_factory = engine_factory
        self.min_score = min_score
        self._engine: Any = None

    def _get_engine(self) -> Any:
        if self._engine is None:
            if self.engine_factory is None:
                from rapidocr import RapidOCR
                # RapidOCR re-applies its own log level at init, so it must be passed as a parameter.
                self._engine = RapidOCR(params={"Global.log_level": "warning"})
            else:
                self._engine = self.engine_factory()
        return self._engine

    def __call__(self, frames: Iterable[tuple[str, Path]]) -> dict[str, str]:
        results = {}
        for filename, image in frames:
            result = self._get_engine()(image)
            texts = getattr(result, "txts", None)
            scores = getattr(result, "scores", None)
            accepted = []
            if texts is not None:
                for text, score in zip(texts, scores or ()):
                    try:
                        if float(score) >= self.min_score and text:
                            accepted.append(str(text))
                    except (TypeError, ValueError):
                        continue
            results[filename] = "\n".join(accepted)
        return results


def ocr_key_frames(frames: Iterable[tuple[str, Path]], engine_factory: Callable[[], Any] | None = None,
                   min_score: float = 0.5) -> dict[str, str]:
    return KeyFrameOcr(engine_factory, min_score)(frames)
