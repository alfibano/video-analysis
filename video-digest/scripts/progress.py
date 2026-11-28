"""Shared throttling policy for progress written to terminals and redirected streams."""
from __future__ import annotations

import sys
import time
from typing import Callable, TextIO


class ProgressWriter:
    """Write bounded progress updates, adapting to whether the stream is a terminal."""

    def __init__(self, stream: TextIO | None = None,
                 monotonic: Callable[[], float] | None = None) -> None:
        self.stream = stream or sys.stderr
        self.monotonic = monotonic or time.monotonic
        try:
            self.terminal = bool(self.stream.isatty())
        except (AttributeError, OSError):
            self.terminal = False
        self.last_update: float | None = None
        self.last_bucket = 0
        self.started = False

    def update(self, percent: float | None, render: Callable[[float | None], str],
               *, final: bool = False) -> None:
        """Render one update; redirected streams include each newly crossed 10% bucket."""
        if self.terminal:
            now = self.monotonic()
            if self.last_update is None or now - self.last_update >= 0.5 or final:
                self.stream.write("\r" + render(percent))
                if final:
                    self.stream.write("\n")
                self.stream.flush()
                self.last_update = now
            self.started = True
            return

        if final:
            self.stream.write(render(100.0 if percent is not None else None) + "\n")
            self.stream.flush()
            self.started = True
            return
        if percent is None:
            if not self.started:
                self.stream.write(render(None) + "\n")
                self.stream.flush()
                self.started = True
            return

        if not self.started:
            self.stream.write(render(percent) + "\n")
            self.stream.flush()
            self.started = True
        bucket = min(9, int(percent // 10))
        for crossed in range(self.last_bucket + 1, bucket + 1):
            self.stream.write(render(float(crossed * 10)) + "\n")
        self.last_bucket = max(self.last_bucket, bucket)
        self.stream.flush()
