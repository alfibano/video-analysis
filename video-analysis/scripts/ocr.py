"""Lazy adapter for local RapidOCR key-frame recognition."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, TextIO

from progress import ProgressWriter


OCR_COMPARE_SIZE = (320, 180)
OCR_REUSE_RMSE = 0.01
OCR_REUSE_ALGORITHM = "gray-rmse-320x180-v1"


@dataclass(frozen=True, slots=True)
class OCRStats:
    texts: dict[str, str]
    reused: int


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

    @staticmethod
    def _comparison_image(image: Path | Any) -> Any:
        from PIL import Image
        if not isinstance(image, (str, Path)):
            # The setup warm-up passes an in-memory array instead of a frame file.
            return Image.fromarray(image).convert("L").resize(OCR_COMPARE_SIZE, resample=Image.Resampling.BOX)
        with Image.open(image) as opened:
            return opened.convert("L").resize(OCR_COMPARE_SIZE, resample=Image.Resampling.BOX)

    @staticmethod
    def _rmse(left: Any, right: Any) -> float:
        from PIL import ImageChops, ImageStat
        difference = ImageChops.difference(left, right)
        return ImageStat.Stat(difference).rms[0] / 255

    def _accepted_text(self, result: Any) -> str:
        texts = getattr(result, "txts", None)
        scores = getattr(result, "scores", None)
        if texts is None:
            return ""
        accepted = []
        for text, score in zip(texts, scores or ()):
            try:
                if float(score) >= self.min_score and text:
                    accepted.append(str(text))
            except (TypeError, ValueError):
                continue
        return "\n".join(accepted)

    def __call__(self, frames: Iterable[tuple[str, Path]]) -> OCRStats:
        frame_list = list(frames)
        results = {}
        reused = 0
        previous_image = None
        previous_text = ""
        progress = ProgressWriter(self.stream, self.monotonic)
        total = len(frame_list)
        for index, (filename, image) in enumerate(frame_list, start=1):
            comparison = self._comparison_image(image)
            if previous_image is not None and self._rmse(previous_image, comparison) < OCR_REUSE_RMSE:
                results[filename] = previous_text
                reused += 1
            else:
                previous_image = comparison
                previous_text = self._accepted_text(self._get_engine()(image))
                results[filename] = previous_text
            percent = index * 100 / total
            progress.update(percent, lambda value: f"OCR {max(1, min(total, int((value or 0) * total / 100 + 0.999)))}"
                            f"/{total}", final=index == total)
        return OCRStats(results, reused)


def ocr_key_frames(frames: Iterable[tuple[str, Path]], engine_factory: Callable[[], Any] | None = None,
                   min_score: float = 0.5, *, stream: TextIO | None = None,
                   monotonic: Callable[[], float] | None = None) -> dict[str, str]:
    return ocr_key_frames_with_stats(frames, engine_factory, min_score,
                                     stream=stream, monotonic=monotonic).texts


def ocr_key_frames_with_stats(frames: Iterable[tuple[str, Path]],
                              engine_factory: Callable[[], Any] | None = None,
                              min_score: float = 0.5, *, stream: TextIO | None = None,
                              monotonic: Callable[[], float] | None = None) -> OCRStats:
    return KeyFrameOcr(engine_factory, min_score, stream=stream, monotonic=monotonic)(frames)
