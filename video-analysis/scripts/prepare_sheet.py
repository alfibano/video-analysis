"""Prepare a host-authored study sheet brief after consent and provenance checks."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from host_models import ConfigReader


@dataclass(frozen=True, slots=True)
class SheetPrepareRequest:
    host: str | None = None
    locality: str | None = None
    allow_remote_ai: bool = False
    language: str = "auto"


@dataclass(frozen=True, slots=True)
class SheetPrepareDeps:
    environ: Mapping[str, str]
    config_reader: ConfigReader | None = None


def _config_reader(deps: SheetPrepareDeps):
    if deps.config_reader is not None:
        return deps.config_reader
    return ConfigReader(Path.cwd(), Path.home(),
                        lambda path: path.read_text(encoding="utf-8") if path.is_file() else None)


def _selection(host, model: str, locality: str) -> None:
    reason = dict(host.reasons).get("default_model", "no host model resolved")
    print(f"AI selection: host={host.host} model={model} role=default reason={reason} locality={locality}")


def _load_inputs(video: Path) -> tuple[Path, dict, dict, str, dict, dict, list | None]:
    if not video.is_file():
        raise ValueError("video does not exist")
    analysis = video.parent / f"{video.stem}.analysis"
    bundle_path, manifest_path = analysis / "bundle.json", analysis / "manifest.json"
    if not analysis.is_dir() or not bundle_path.is_file() or not manifest_path.is_file():
        raise ValueError("analysis folder, bundle.json, or manifest.json is missing")
    bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    metadata_path, raw_path, comments_path = analysis / "metadata.json", analysis / "raw_info.json", analysis / "comments.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8")) if metadata_path.is_file() else {}
    raw_info = json.loads(raw_path.read_text(encoding="utf-8")) if raw_path.is_file() else {}
    comments = json.loads(comments_path.read_text(encoding="utf-8")) if comments_path.is_file() else None
    if (not isinstance(bundle, dict) or not isinstance(manifest, dict)
            or manifest.get("schema_version") != 2
            or not isinstance(manifest.get("source"), dict)
            or not isinstance(manifest.get("params", {}), dict)
            or not isinstance(manifest.get("stages", {}), dict)):
        raise ValueError("bundle or manifest is malformed")
    for section in ("params", "stages"):
        child = manifest.get(section, {}).get("sheet_prepare")
        if child is not None and not isinstance(child, dict):
            raise ValueError("bundle or manifest is malformed")
    digest = hashlib.sha256(bundle_path.read_bytes()).hexdigest()
    if not isinstance(metadata, dict) or not isinstance(raw_info, dict) or (comments is not None and not isinstance(comments, list)):
        raise ValueError("metadata, raw info, or comments are malformed")
    return analysis, bundle, manifest, digest, metadata, raw_info, comments


def _resolved_language(requested: str, bundle: dict) -> tuple[str, str]:
    speech = bundle.get("speech")
    speech_language = speech.get("language") if isinstance(speech, dict) else None
    if requested != "auto":
        return requested, requested
    if isinstance(speech_language, str) and speech_language.strip():
        language = speech_language.strip()
        return language, language
    return "auto", "auto (write in the speech language)"


def _brief(analysis: Path, digest: str, params: dict, facts: dict, comments: list | None) -> str:
    from sheet_schema import SHEET_JSON_SCHEMA_V2
    schema = json.dumps(SHEET_JSON_SCHEMA_V2, ensure_ascii=False, indent=2)
    facts_json = json.dumps(facts, ensure_ascii=False, indent=2)
    language_instruction = (params["language"] if params["language"] != "auto"
                            else "auto; write in the speech language")
    return f"""# Study sheet writer brief

Read `{(analysis / 'bundle.json').resolve()}` and selected chapter frames before writing.
Write only valid UTF-8 JSON to `{(analysis / 'sheet.json').resolve()}`. Do not create or edit other files.

Bundle SHA-256: `{digest}`
Copy `bundle.json` `source.url` exactly into `sheet.json` `source.url`; use `null` when it is absent.
Host: `{params['host']}`
Model: `{params['model']}`
Provider: `{params['provider']}`
Locality: `{params['locality']}`
Language: `{language_instruction}`
Writer role: `default`

Use the current host session default model. Faithfully translate into the sheet language and never betray the author's message. Use author chapters or choose better boundaries and explain the choice. Summary paragraph count must be proportional to duration and at least max(1, ceil(duration_s / 600)). Add several frames per chapter when each adds distinct information; put every frame beside its block text.

Corrections are only for lies or flagrant imprecision. Questions are only for essential unavoidable information. Complements add useful precision without replacing the author's message. Fact-check any factual domain, including official statistics, with official institutions, statistical offices, reference encyclopedias, or peer-reviewed sources. Never debunk religion, spirituality, or esotericism. Corrections and complements require references; questions may have none. Number references from 1 in order and use those IDs in callouts. Total callout text must not exceed total block text.

Use context only with evidence: recording date and author email require non-empty evidence; include email only when published by the author in a description or slide. Include a series only with evidence; host research may use web search, otherwise leave it null. Summarize comments without names or identifiers, and read comments.json only when it exists (fetched top-level count: {len(comments) if comments is not None else 0}). Every visible label must be written in the sheet language. Distinguish on-screen evidence, speech, and inference. Use corrected speech only when its provenance is present in the bundle. Do not call a provider and do not delegate to a low-cost model.

Script-owned facts (include these verbatim; do not invent or edit them):
```json
{facts_json}
```

The JSON must match this exact schema:

```json
{schema}
```
"""


def _write_manifest(path: Path, manifest: dict) -> None:
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")


def run_prepare(video: Path, request: SheetPrepareRequest | None = None,
                deps: SheetPrepareDeps | None = None) -> int:
    request = request or SheetPrepareRequest()
    deps = deps or SheetPrepareDeps(os.environ)
    try:
        video = video.expanduser().resolve()
        analysis, bundle, manifest, digest, metadata, raw_info, comments = _load_inputs(video)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
        print(f"sheet preparation input error: {error}", file=sys.stderr)
        return 2
    try:
        from host_models import ExplicitInput, resolve_host_model
        from tools import media_source_kind
        from correct_transcript import consent_required
        host = resolve_host_model(deps.environ, _config_reader(deps),
                                  ExplicitInput(request.host, request.locality))
        model = host.default_model or "host default"
        locality = host.locality
        language, _ = _resolved_language(request.language, bundle)
        from sheet_facts import build_sheet_facts, canonical_sha256
        facts = build_sheet_facts(video, metadata=metadata, raw_info=raw_info,
                                  manifest=manifest, bundle=bundle)
        facts_hash = canonical_sha256(facts)
        comments_hash = canonical_sha256(comments) if comments is not None else None
        params = {"brief_version": 2, "sheet_schema_version": 2, "facts_sha256": facts_hash,
                  "comments_sha256": comments_hash, "bundle_sha256": digest, "host": host.host, "model": model,
                  "provider": host.provider, "locality": locality, "language": language}
        _selection(host, model, locality)
        stage = manifest.get("stages", {}).get("sheet_prepare", {})
        previous = manifest.get("params", {}).get("sheet_prepare")
        brief_path = analysis / "sheet.brief.md"
        if brief_path.exists() and not (stage.get("status") == "ok" and previous == params):
            print("sheet preparation conflict: existing brief is not tracked by matching successful preparation",
                  file=sys.stderr)
            return 3
        if stage.get("status") == "ok" and previous == params and brief_path.is_file():
            print("sheet preparation resumed")
            return 0
        source_kind = media_source_kind(manifest)
        required = consent_required(source_kind, locality)
        if required and not request.allow_remote_ai:
            manifest.setdefault("stages", {})["sheet_prepare"] = {
                "status": "denied", "consent": {"required": True, "granted": False}}
            _write_manifest(analysis / "manifest.json", manifest)
            print("remote AI consent is required for local media", file=sys.stderr)
            return 4
        consent = ({"required": True, "granted": True, "method": "flag"} if required else
                   {"required": False, "granted": True, "method": "not-required"})
        brief_text = _brief(analysis, digest, params, facts, comments)
        brief_path.write_text(brief_text, encoding="utf-8")
        manifest.setdefault("params", {})["sheet_prepare"] = params
        manifest.setdefault("stages", {})["sheet_prepare"] = {"status": "ok", "consent": consent,
            "brief_sha256": hashlib.sha256(brief_text.encode("utf-8")).hexdigest()}
        _write_manifest(analysis / "manifest.json", manifest)
        print("sheet preparation complete")
        return 0
    except (OSError, UnicodeError, ValueError, TypeError, KeyError) as error:
        print(f"sheet preparation failed: {error}", file=sys.stderr)
        return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=("Prepare an offline writer brief for an editable study sheet.\n"
                     "The host session writes sheet.json after this consent and provenance gate."),
        epilog="Example: python prepare_sheet.py video.mp4 --host codex --language en\n"
               "Exit codes: 0 prepared; 1 processing failure; 2 invalid input;\n"
               "3 preparation conflict; 4 remote-AI consent required.")
    parser.add_argument("video", type=Path, help="source video with a completed analysis bundle")
    parser.add_argument("--host", choices=("claude-code", "codex", "openclaw"),
                        help="host whose session default model will write the sheet")
    parser.add_argument("--ai-locality", choices=("local", "remote"),
                        help="declare the host model locality")
    parser.add_argument("--allow-remote-ai", action="store_true",
                        help="record consent for remote AI to read local-media bundle data")
    parser.add_argument("--language", default="auto", metavar="L",
                        help="writer language (default: infer from bundle speech)")
    args = parser.parse_args(argv)
    request = SheetPrepareRequest(args.host, args.ai_locality, args.allow_remote_ai, args.language)
    return run_prepare(args.video, request)


if __name__ == "__main__":
    sys.exit(main())
