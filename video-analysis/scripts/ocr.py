"""Lazy adapter for local RapidOCR key-frame recognition."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable, Iterable, TextIO

from progress import ProgressWriter


def default_ocr_threads(cpu_count: int | None) -> int:
    """Choose a bounded ONNX Runtime intra-op thread count."""
    return max(1, min(8, (cpu_count or 0) // 2))


class KeyFrameOcr:
    """Create one OCR engine on first use and recognize only key frames."""

    def __init__(self, engine_factory: Callable[[], Any] | None = None, min_score: float = 0.5,
                 *, stream: TextIO | None = None,
                 monotonic: Callable[[], float] | None = None):
        self.engine_factory = engine_factory
        self.min_score = min_score
        self.stream = stream
        self.monotonic = monotonic
        self._engine: Any = None

    def _get_engine(self) -> Any:
        if self._engine is None:
            if self.engine_factory is None:
                from rapidocr import RapidOCR
                # RapidOCR re-applies its own log level at init, so it must be passed as a parameter.
                self._engine = RapidOCR(params={
                    "Global.log_level": "warning",
                    "EngineConfig.onnxruntime.intra_op_num_threads": default_ocr_threads(os.cpu_count()),
                })
            else:
                self._engine = self.engine_factory()
        return self._engine

    def __call__(self, frames: Iterable[tuple[str, Path]]) -> dict[str, str]:
        frame_list = list(frames)
        results = {}
        progress = ProgressWriter(self.stream, self.monotonic)
        total = len(frame_list)
        for index, (filename, image) in enumerate(frame_list, start=1):
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
            percent = index * 100 / total
            progress.update(percent, lambda value: f"OCR {max(1, min(total, int((value or 0) * total / 100 + 0.999)))}"
                            f"/{total}", final=index == total)
        return results


def ocr_key_frames(frames: Iterable[tuple[str, Path]], engine_factory: Callable[[], Any] | None = None,
                   min_score: float = 0.5, *, stream: TextIO | None = None,
                   monotonic: Callable[[], float] | None = None) -> dict[str, str]:
    return KeyFrameOcr(engine_factory, min_score, stream=stream, monotonic=monotonic)(frames)
