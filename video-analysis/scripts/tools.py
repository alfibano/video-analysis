"""Resolve external tools using one shared precedence policy."""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from typing import Any, Callable, Literal, Mapping


@dataclass(frozen=True, slots=True)
class Tool:
    path: str
    origin: Literal["env", "path", "bundled", "module"]
    version: str | None = None


def resolve_command(command: str, *, env_var: str | None = None,
                    env: Mapping[str, str] | None = None,
                    which: Callable[[str], str | None] = shutil.which) -> Tool | None:
    environ = os.environ if env is None else env
    override = environ.get(env_var, "").strip() if env_var else ""
    if override:
        return Tool(override, "env")
    path = which(command)
    return Tool(path, "path") if path else None


def resolve_ffmpeg(*, env: Mapping[str, str] | None = None,
                   which: Callable[[str], str | None] = shutil.which,
                   imageio_lookup: Callable[[], str] | None = None) -> Tool | None:
    command = resolve_command("ffmpeg", env_var="VIDEO_ANALYSIS_FFMPEG", env=env, which=which)
    if command:
        return command
    if imageio_lookup is None:
        try:
            import imageio_ffmpeg
        except ImportError:
            return None
        imageio_lookup = imageio_ffmpeg.get_ffmpeg_exe
    try:
        return Tool(imageio_lookup(), "bundled")
    except (ImportError, RuntimeError, OSError):
        return None


def resolve_js_runtime(*, which: Callable[[str], str | None] = shutil.which) -> Tool | None:
    for runtime in ("deno", "node"):
        path = which(runtime)
        if path:
            return Tool(path, "path")
    return None


def resolve_ytdlp(*, env: Mapping[str, str] | None = None,
                  which: Callable[[str], str | None] = shutil.which,
                  module_lookup: Callable[[str], Any] | None = None) -> Tool | None:
    environ = os.environ if env is None else env
    override = environ.get("VIDEO_ANALYSIS_YTDLP", "").strip()
    if override:
        return Tool(override, "env")
    path = which("yt-dlp")
    if path:
        return Tool(path, "path")
    if module_lookup is None:
        try:
            import yt_dlp
        except ImportError:
            return None
    else:
        try:
            yt_dlp = module_lookup("yt_dlp")
        except ImportError:
            return None
    module_file = getattr(yt_dlp, "__file__", None)
    version = getattr(getattr(yt_dlp, "version", None), "__version__", None)
    if not module_file:
        return None
    return Tool(str(module_file), "module", str(version) if version is not None else None)
