"""Pure host model resolution for Claude Code, Codex, and OpenClaw."""
from __future__ import annotations

import json
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit


@dataclass(frozen=True, slots=True)
class HostModel:
    host: str
    default_model: str | None
    low_cost_model: str | None
    provider: str
    endpoint_identity: str | None
    locality: str
    reasons: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class ExplicitInput:
    host: str | None = None
    provider: str | None = None
    locality: str | None = None


@dataclass(frozen=True, slots=True)
class ConfigReader:
    cwd: Path
    home: Path
    read_text: Callable[[Path], str | None]


@dataclass(frozen=True, slots=True)
class ModelFamily:
    provider_aliases: tuple[str, ...]
    model_patterns: tuple[str, ...]
    low_cost: str | None
    bigger: str | None


MODEL_FAMILIES = (
    ModelFamily(("anthropic", "claude"), (r"claude[-_/].*", r"claude.*"), "haiku", "sonnet"),
    ModelFamily(("openai", "gpt", "codex"), (r"gpt[-_/].*",), "gpt-6-luna", "gpt-5.6-sol"),
    ModelFamily(("glm", "zai", "zhipu"), (r"glm[-_/].*",), "GLM-5.3-Flash", None),
    ModelFamily(("deepseek",), (r"deepseek[-_/].*",), "deepseek-flash", "deepseek-v4-pro"),
    ModelFamily(("qwen", "alibaba"), (r"qwen.*",), "qwen-flash", None),
    ModelFamily(("mistral",), (r"mistral[-_/].*",), "mistral-small-latest", None),
    ModelFamily(("gemini", "google"), (r"gemini[-_/].*",), "gemini-3.5-flash-lite", None),
    ModelFamily(("kimi", "moonshot"), (r"kimi[-_/].*",), None, None),
    ModelFamily(("ollama", "lmstudio", "local"), (r".*",), None, None),
)


def _read(reader: ConfigReader, path: Path) -> str | None:
    try:
        return reader.read_text(path)
    except (OSError, UnicodeError, ValueError):
        return None


def _host(environ: dict[str, str], explicit: ExplicitInput) -> tuple[str, str]:
    if explicit.host:
        return explicit.host.strip().lower(), "selected by explicit host input"
    if environ.get("VIDEO_ANALYSIS_HOST"):
        return environ["VIDEO_ANALYSIS_HOST"].strip().lower(), "selected by VIDEO_ANALYSIS_HOST"
    if environ.get("CLAUDECODE"):
        return "claude-code", "detected from Claude Code marker"
    if environ.get("CODEX_SESSION_ID") or environ.get("CODEX_THREAD_ID"):
        return "codex", "detected from Codex session marker"
    return "unknown", "no explicit host or Claude/Codex marker was present"


def _json_object(source: str | None) -> dict:
    if not source:
        return {}
    try:
        value = json.loads(source)
        return value if isinstance(value, dict) else {}
    except (json.JSONDecodeError, TypeError):
        return {}


def _claude_default(environ: dict[str, str], reader: ConfigReader) -> tuple[str | None, str]:
    if environ.get("ANTHROPIC_MODEL"):
        return environ["ANTHROPIC_MODEL"], "from ANTHROPIC_MODEL"
    for path, label in ((reader.cwd / ".claude/settings.local.json", "local settings"),
                        (reader.cwd / ".claude/settings.json", "project settings"),
                        (reader.home / ".claude/settings.json", "user settings")):
        model = _json_object(_read(reader, path)).get("model")
        if isinstance(model, str) and model:
            return model, f"from Claude {label}"
    return None, "no configured Claude model was found"


def _claude_low(environ: dict[str, str], default: str | None,
                base_url: str | None) -> tuple[str | None, str]:
    for name in ("ANTHROPIC_DEFAULT_HAIKU_MODEL", "ANTHROPIC_SMALL_FAST_MODEL"):
        if environ.get(name):
            return environ[name], f"from {name}"
    if not base_url and default and "claude" in default.lower():
        return "haiku", "native Claude haiku alias; no custom base URL"
    return _family_sibling("anthropic", default, "low_cost")


def _toml_config(environ: dict[str, str], reader: ConfigReader) -> tuple[dict, str]:
    home = Path(environ.get("CODEX_HOME", str(reader.home / ".codex")))
    base = _parse_toml(_read(reader, home / "config.toml"))
    profile = base.get("profile")
    profile_name = profile if isinstance(profile, str) else None
    if profile_name:
        overlay = _parse_toml(_read(reader, home / f"{profile_name}.config.toml"))
        base = {**base, **overlay}
    return base, "Codex configured default; live --oss, -m, and --profile flags are not observable"


def _parse_toml(source: str | None) -> dict:
    try:
        return tomllib.loads(source) if source else {}
    except (tomllib.TOMLDecodeError, TypeError):
        return {}


def _codex_values(config: dict) -> tuple[str | None, str, str | None]:
    model = config.get("model")
    model = model if isinstance(model, str) else None
    selected = config.get("oss_provider") or config.get("model_provider") or "openai"
    providers = config.get("model_providers", {})
    entry = providers.get(selected, {}) if isinstance(providers, dict) else {}
    endpoint = entry.get("base_url") if isinstance(entry, dict) else None
    return model, str(selected), endpoint if isinstance(endpoint, str) else None


def _openclaw_path(environ: dict[str, str], reader: ConfigReader) -> Path:
    if environ.get("OPENCLAW_CONFIG_PATH"):
        return Path(environ["OPENCLAW_CONFIG_PATH"])
    if environ.get("OPENCLAW_STATE_DIR"):
        return Path(environ["OPENCLAW_STATE_DIR"]) / "openclaw.json"
    return reader.home / ".openclaw/openclaw.json"


def _openclaw_values(source: str | None) -> tuple[str | None, str, str | None, str]:
    try:
        config = json.loads(_normalize_json5(source or ""))
    except (json.JSONDecodeError, ValueError):
        return None, "unknown", None, "OpenClaw config was malformed"
    model = _nested(config, "agents", "defaults", "model", "primary")
    if not isinstance(model, str) or not model:
        return None, "unknown", None, "OpenClaw config has no primary model"
    provider = model.split("/", 1)[0] if "/" in model else "unknown"
    entry = _nested(config, "models", "providers", provider) or _nested(config, "providers", provider) or {}
    endpoint = entry.get("baseUrl") or entry.get("base_url") if isinstance(entry, dict) else None
    return model, provider, endpoint if isinstance(endpoint, str) else None, "from agents.defaults.model.primary"


def _nested(value: dict, *keys: str):
    for key in keys:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def _strip_comments(source: str) -> str:
    out, quote, escaped, index = [], None, False, 0
    while index < len(source):
        char = source[index]
        if quote:
            out.append(char)
            was_escaped = escaped
            escaped = _quoted_step(char, quote, escaped)
            if char == quote and not was_escaped:
                quote = None
        elif char in "'\"":
            quote = char
            out.append(char)
        elif source.startswith("//", index):
            index = _skip_line(source, index)
        elif source.startswith("/*", index):
            index = _skip_block(source, index)
        else:
            out.append(char)
        index += 1
    return "".join(out)


def _quoted_step(char: str, quote: str, escaped: bool) -> bool:
    if escaped:
        return False
    if char == "\\":
        return True
    return False if char == quote else escaped


def _skip_line(source: str, index: int) -> int:
    end = source.find("\n", index)
    return len(source) if end < 0 else end - 1


def _skip_block(source: str, index: int) -> int:
    end = source.find("*/", index + 2)
    return len(source) if end < 0 else end + 1


def _normalize_json5(source: str) -> str:
    clean = _strip_comments(source)
    clean = _single_quotes(clean)
    return _remove_trailing_commas(_quote_keys(clean))


def _quote_keys(source: str) -> str:
    out, index, quote, escaped = [], 0, None, False
    while index < len(source):
        char = source[index]
        if quote:
            quote, escaped = _copy_quoted(out, char, quote, escaped)
        elif char in "\"'":
            quote, escaped = char, False
            out.append(char)
        elif char in "{," and (end := _bare_key_end(source, index + 1)) is not None:
            out.extend((char, source[index + 1:end].strip(), "\""))
            out.insert(len(out) - 2, "\"")
            index = end - 1
        else:
            out.append(char)
        index += 1
    return "".join(out)


def _bare_key_end(source: str, start: int) -> int | None:
    while start < len(source) and source[start].isspace():
        start += 1
    match = re.match(r"[A-Za-z_$][\w$-]*", source[start:])
    if not match:
        return None
    end = start + len(match.group())
    return end if source[end:].lstrip().startswith(":") else None


def _copy_quoted(out: list[str], char: str, quote: str, escaped: bool) -> tuple[str | None, bool]:
    out.append(char)
    if escaped:
        return quote, False
    if char == "\\":
        return quote, True
    return (None, False) if char == quote else (quote, False)


def _remove_trailing_commas(source: str) -> str:
    out, index, quote, escaped = [], 0, None, False
    while index < len(source):
        char = source[index]
        if quote:
            quote, escaped = _copy_quoted(out, char, quote, escaped)
        elif char in "\"'":
            quote, escaped = char, False
            out.append(char)
        elif char == "," and source[index + 1:].lstrip().startswith(("}", "]")):
            pass
        else:
            out.append(char)
        index += 1
    return "".join(out)


def _single_quotes(source: str) -> str:
    out, in_single, escaped = [], False, False
    for char in source:
        if in_single:
            if char == "'" and escaped:
                out.pop()
                out.append("'")
                escaped = False
            elif char == '"' and not escaped:
                out.append('\\"')
            elif char == "'" and not escaped:
                out.append('"')
                in_single = False
            else:
                out.append(char)
            escaped = char == "\\" and not escaped
        elif char == "'":
            out.append('"')
            in_single, escaped = True, False
        else:
            out.append(char)
    return "".join(out)


def _endpoint_identity(endpoint: str | None, provider: str) -> str:
    if not endpoint:
        return f"provider:{provider}" if provider != "unknown" else "unknown"
    try:
        parsed = urlsplit(endpoint)
        if not parsed.scheme or not parsed.hostname:
            return f"provider:{provider}"
        host = parsed.hostname.lower()
        port = f":{parsed.port}" if parsed.port else ""
        path = parsed.path.rstrip("/")
        return f"{parsed.scheme.lower()}://{host}{port}{path}"
    except ValueError:
        return f"provider:{provider}"


def _family(provider: str, model: str | None) -> ModelFamily | None:
    normalized_provider, normalized_model = provider.lower(), (model or "").lower()
    normalized_model = normalized_model.split("/", 1)[-1]
    return next((family for family in MODEL_FAMILIES
                 if any(alias in normalized_provider for alias in family.provider_aliases)
                 and any(re.fullmatch(pattern, normalized_model) for pattern in family.model_patterns)), None)


def _family_sibling(provider: str, model: str | None, sibling: str) -> tuple[str | None, str]:
    family = _family(provider, model)
    value = getattr(family, sibling) if family else None
    if not family or not value:
        return None, "no known same-family sibling"
    prefix = model.split("/", 1)[0] + "/" if model and "/" in model else ""
    if value.lower() in (model or "").lower():
        return model, "configured model already has the low-cost role"
    return prefix + value, f"selected from {provider} model family"


def _locality(environ: dict[str, str], explicit: ExplicitInput, provider: str) -> tuple[str, str]:
    declared = explicit.locality or environ.get("VIDEO_ANALYSIS_AI_LOCALITY") or environ.get("VIDEO_ANALYSIS_HOST_AI")
    if declared:
        value = "local" if declared.lower() == "local" else "remote"
        return value, "explicit locality declaration" if declared.lower() in {"local", "remote"} else "invalid locality defaults to remote"
    if provider.lower() in {"ollama", "lmstudio", "local"}:
        return "local", "provider ID identifies local inference"
    return "remote", "no local provider evidence or explicit declaration"


def _host_model(environ: dict[str, str], reader: ConfigReader, explicit: ExplicitInput,
                host: str) -> tuple[str | None, str, str | None, str | None, str, str]:
    if host == "claude-code":
        model, reason = _claude_default(environ, reader)
        endpoint = environ.get("ANTHROPIC_BASE_URL")
        low, why = _claude_low(environ, model, endpoint)
        return model, "anthropic", endpoint, low, why, reason
    return _configured_host_model(environ, reader, explicit, host)


def _configured_host_model(environ: dict[str, str], reader: ConfigReader, explicit: ExplicitInput,
                           host: str) -> tuple[str | None, str, str | None, str | None, str, str]:
    if host == "codex":
        config, reason = _toml_config(environ, reader)
        model, provider, endpoint = _codex_values(config)
    elif host == "openclaw":
        model, provider, endpoint, reason = _openclaw_values(_read(reader, _openclaw_path(environ, reader)))
        provider = explicit.provider or provider
    else:
        return None, explicit.provider or "unknown", None, None, "host has no model configuration", "no host model resolved"
    low, why = _family_sibling(provider, model, "low_cost")
    return model, provider, endpoint, low, why, reason


def resolve_host_model(environ: dict[str, str], config_reader: ConfigReader,
                       explicit: ExplicitInput) -> HostModel:
    """Resolve safe model facts without filesystem or provider access."""
    host, host_reason = _host(environ, explicit)
    default, provider, endpoint, low, low_reason, default_reason = _host_model(environ, config_reader, explicit, host)
    if explicit.provider and host not in {"codex", "claude-code"}:
        provider = explicit.provider
    endpoint_id = "anthropic-native" if host == "claude-code" and not endpoint else _endpoint_identity(endpoint, provider)
    locality, locality_reason = _locality(environ, explicit, provider)
    reasons = (("host", host_reason), ("default_model", default_reason), ("low_cost_model", low_reason),
               ("provider", "provider resolved from host config" if provider != "unknown" else "no provider resolved"),
               ("endpoint_identity", "sanitized endpoint identity"), ("locality", locality_reason))
    return HostModel(host, default, low, provider, endpoint_id, locality, reasons)
