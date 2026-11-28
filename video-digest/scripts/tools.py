"""Resolve external tools using one shared precedence policy."""
from __future__ import annotations

import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Literal, Mapping


PRIMARY_ENV_PREFIX = "VIDEO_DIGEST_"
LEGACY_ENV_PREFIX = "VIDEO_ANALYSIS_"
TEMP_DIR_ENV = f"{PRIMARY_ENV_PREFIX}TEMP_DIR"
LEGACY_TEMP_DIR_ENV = f"{LEGACY_ENV_PREFIX}TEMP_DIR"


def env_value(environ: Mapping[str, str], suffix: str, default: str | None = None) -> str | None:
    """Resolve one product setting by key presence, preserving an explicitly empty primary value."""
    primary = f"{PRIMARY_ENV_PREFIX}{suffix}"
    legacy = f"{LEGACY_ENV_PREFIX}{suffix}"
    return environ[primary] if primary in environ else environ.get(legacy, default)


def media_source_kind(manifest: Mapping[str, Any]) -> str:
    """Return the declared media kind, migrating legacy fetch manifests in memory."""
    source = manifest.get("source")
    kind = source.get("kind") if isinstance(source, Mapping) else None
    if kind in {"local", "url"}:
        return str(kind)
    params = manifest.get("params")
    fetch = params.get("fetch") if isinstance(params, Mapping) else None
    return "url" if isinstance(fetch, Mapping) and fetch.get("url") else "local"


def same_video(recorded: Mapping[str, Any], identity: Mapping[str, Any]) -> bool:
    """Compare the stable video identity fields shared by extraction and later stages."""
    return all(recorded.get(key) == identity.get(key) for key in ("path", "size", "mtime_ns"))


def app_temp_path(env: Mapping[str, str] | None = None) -> Path:
    environ = os.environ if env is None else env
    selected_name = TEMP_DIR_ENV if TEMP_DIR_ENV in environ else LEGACY_TEMP_DIR_ENV
    override = (env_value(environ, "TEMP_DIR") or "").strip()
    if override:
        path = Path(override)
        if not path.is_absolute():
            raise ValueError(f"{selected_name} must be an absolute path")
        return path
    return Path(__file__).resolve().parent.parent / "_temp"


def app_temp_dir() -> Path:
    """Create the app-local temp root so obsolete work has one place to clean; scripts never delete it."""
    path = app_temp_path()
    path.mkdir(parents=True, exist_ok=True)
    return path


def make_temp_folder(prefix: str) -> Path:
    """Create and return a fresh temporary folder inside the app temp directory."""
    return Path(tempfile.mkdtemp(prefix=prefix, dir=app_temp_dir()))


@dataclass(frozen=True, slots=True)
class Tool:
    path: str
    origin: Literal["env", "path", "bundled", "module"]
    version: str | None = None


def resolve_command(command: str, *, env_var: str | None = None,
                    env: Mapping[str, str] | None = None,
                    which: Callable[[str], str | None] = shutil.which) -> Tool | None:
    environ = os.environ if env is None else env
    override = (env_value(environ, env_var.removeprefix(PRIMARY_ENV_PREFIX)) or "").strip() if env_var else ""
    if override:
        return Tool(override, "env")
    path = which(command)
    return Tool(path, "path") if path else None


def resolve_ffmpeg(*, env: Mapping[str, str] | None = None,
                   which: Callable[[str], str | None] = shutil.which,
                   imageio_lookup: Callable[[], str] | None = None) -> Tool | None:
    command = resolve_command("ffmpeg", env_var=f"{PRIMARY_ENV_PREFIX}FFMPEG", env=env, which=which)
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
    override = (env_value(environ, "YTDLP") or "").strip()
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
