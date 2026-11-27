"""Validate a host-authored study sheet and render it as an editable DOCX."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

FIXED_CORE_TIME = datetime(2000, 1, 1, tzinfo=timezone.utc)


def _style(document, name: str):
    try:
        return document.styles[name]
    except (KeyError, TypeError):
        try:
            return document.styles["Normal"]
        except (KeyError, TypeError):
            return None


def _paragraph(document, text: str = "", style: str = "Normal"):
    paragraph = document.add_paragraph(style=_style(document, style))
    if text:
        paragraph.add_run(text)
    return paragraph


def _hyperlink(paragraph, text: str, target: str) -> None:
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.opc.constants import RELATIONSHIP_TYPE as RT
    relation = paragraph.part.relate_to(target, RT.HYPERLINK, is_external=True)
    hyperlink = OxmlElement("w:hyperlink")
    hyperlink.set(qn("r:id"), relation)
    run = OxmlElement("w:r")
    properties = OxmlElement("w:rPr")
    try:
        paragraph.part.document.styles["Hyperlink"]
    except KeyError:
        color = OxmlElement("w:color")
        color.set(qn("w:val"), "0563C1")
        underline = OxmlElement("w:u")
        underline.set(qn("w:val"), "single")
        properties.extend((color, underline))
    else:
        style = OxmlElement("w:rStyle")
        style.set(qn("w:val"), "Hyperlink")
        properties.append(style)
    run.append(properties)
    text_node = OxmlElement("w:t")
    text_node.text = text
    run.append(text_node)
    hyperlink.append(run)
    paragraph._p.append(hyperlink)


def _document_from_template(template: Path | None):
    from docx import Document
    if template is None:
        return Document()
    if template.suffix.lower() == ".docx":
        return Document(template)
    source = io.BytesIO(template.read_bytes())
    target = io.BytesIO()
    with zipfile.ZipFile(source) as incoming, zipfile.ZipFile(target, "w") as outgoing:
        for entry in incoming.infolist():
            payload = incoming.read(entry.filename)
            if entry.filename == "[Content_Types].xml":
                payload = payload.replace(
                    b"application/vnd.openxmlformats-officedocument.wordprocessingml.template.main+xml",
                    b"application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml")
            outgoing.writestr(entry, payload)
    target.seek(0)
    return Document(target)


def _clear_body(document) -> None:
    body = document._element.body
    for child in list(body):
        if child.tag.endswith("}sectPr"):
            continue
        body.remove(child)


def _add_linked_time(paragraph, sheet: Mapping[str, Any], seconds: float) -> None:
    from sheet_schema import build_seek_link, format_hhmmss
    timestamp = format_hhmmss(seconds)
    target = build_seek_link(sheet["source"].get("url"), seconds)
    if target:
        _hyperlink(paragraph, timestamp, target)
    else:
        paragraph.add_run(timestamp)


def _render_chapter(document, sheet: Mapping[str, Any], chapter: Mapping[str, Any],
                    analysis_dir: Path) -> None:
    heading = _paragraph(document, style="Heading 1")
    _add_linked_time(heading, sheet, chapter["start"])
    heading.add_run(" — " + chapter["title"])
    _paragraph(document, chapter["text"])
    for point in chapter["key_points"]:
        _paragraph(document, point, "List Bullet")
    frame = chapter["frame"]
    if frame is None:
        return
    section = document.sections[-1]
    width = section.page_width - section.left_margin - section.right_margin
    document.add_picture(str(analysis_dir / Path(*frame["file"].split("/"))), width=width)
    caption = _paragraph(document, style="Caption")
    caption.add_run(sheet["labels"]["figure"] + " ")
    _add_linked_time(caption, sheet, frame["time"])


def render_document(sheet: Mapping[str, Any], *, analysis_dir: Path,
                    template: Path | None = None):
    document = _document_from_template(template)
    _clear_body(document)
    _paragraph(document, sheet["title"], "Title")
    source = _paragraph(document, style="Normal")
    source.add_run(sheet["labels"]["source"] + ": " + sheet["source"]["title"])
    if sheet["source"].get("url"):
        source.add_run(" (")
        _hyperlink(source, sheet["source"]["url"], sheet["source"]["url"])
        source.add_run(")")
    for field in ("introduction", "summary"):
        _paragraph(document, sheet["labels"][field], "Heading 1")
        _paragraph(document, sheet[field])
    for chapter in sheet["chapters"]:
        _render_chapter(document, sheet, chapter, analysis_dir)
    if sheet["glossary"]:
        _paragraph(document, sheet["labels"]["glossary"], "Heading 1")
        for item in sheet["glossary"]:
            _paragraph(document, item["term"], "Heading 2")
            _paragraph(document, item["definition"])
    properties = document.core_properties
    properties.title = sheet["title"]
    properties.author = "video-analysis"
    properties.created = FIXED_CORE_TIME
    properties.modified = FIXED_CORE_TIME
    return document


def _read_manifest(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if (not isinstance(value, dict) or not isinstance(value.get("params"), dict)
            or not isinstance(value.get("stages"), dict)):
        raise ValueError("malformed manifest")
    return value


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_manifest(path: Path, manifest: dict[str, Any]) -> None:
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")


class _RenderError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


def _render_inputs(video: Path, template: Path | None) -> tuple[Path, Path, Path, Path, Path, Path | None]:
    video = video.expanduser().resolve()
    if not video.is_file():
        raise _RenderError(2, "video does not exist")
    analysis = video.parent / f"{video.stem}.analysis"
    paths = (analysis / "manifest.json", analysis / "bundle.json", analysis / "sheet.json")
    if not analysis.is_dir() or not all(path.is_file() for path in paths):
        raise _RenderError(2, "analysis folder, manifest.json, bundle.json, or sheet.json is missing")
    if template is not None:
        template = template.expanduser().resolve()
        if template.suffix.lower() not in {".docx", ".dotx"}:
            raise _RenderError(2, "template must use .docx or .dotx")
        if not template.is_file():
            raise _RenderError(2, "template does not exist")
    return video, analysis, *paths, template


def _prepared_sheet(manifest_path: Path, bundle_path: Path, sheet_path: Path,
                    analysis: Path) -> tuple[dict[str, Any], str, dict[str, Any]]:
    manifest = _read_manifest(manifest_path)
    prep = manifest["stages"].get("sheet_prepare")
    if not isinstance(prep, dict) or prep.get("status") != "ok":
        raise _RenderError(4, "successful sheet preparation is required")
    prep_params = manifest["params"].get("sheet_prepare")
    if not isinstance(prep_params, dict):
        raise _RenderError(3, "sheet preparation parameters are missing")
    bundle_digest = _digest(bundle_path)
    if prep_params.get("bundle_sha256") != bundle_digest:
        raise _RenderError(3, "bundle digest changed since sheet preparation")
    from sheet_schema import load_sheet
    expected = {"host": prep_params.get("host"), "model": prep_params.get("model"), "role": "default"}
    return manifest, bundle_digest, load_sheet(sheet_path, analysis_dir=analysis,
        expected_bundle_sha256=bundle_digest, expected_writer=expected)


def _render_params(sheet_path: Path, template: Path | None) -> dict[str, Any]:
    return {"sheet_sha256": _digest(sheet_path),
            "template_sha256": _digest(template) if template else "builtin", "schema_version": 1}


def _resume_decision(output_path: Path, output_name: str, manifest: Mapping[str, Any],
                     render_params: Mapping[str, Any]) -> int | None:
    if not output_path.exists():
        return None
    stages, params = manifest["stages"], manifest["params"]
    stage, old_params = stages.get("sheet_render", {}), params.get("sheet_render")
    if (isinstance(stage, dict) and stage.get("status") == "ok"
            and stage.get("output") == output_name and old_params == render_params):
        print("sheet render resumed")
        return 0
    raise _RenderError(3, "render conflict: rename or move the existing DOCX before rerendering")


def run_render(video: Path, template: Path | None = None) -> int:
    try:
        video, analysis, manifest_path, bundle_path, sheet_path, template = _render_inputs(video, template)
        manifest, _, sheet = _prepared_sheet(manifest_path, bundle_path, sheet_path, analysis)
        params = _render_params(sheet_path, template)
        output_name = f"{video.stem}.docx"
        output_path = analysis / output_name
        resumed = _resume_decision(output_path, output_name, manifest, params)
        if resumed is not None:
            return resumed
        document = render_document(sheet, analysis_dir=analysis, template=template)
        document.save(output_path)
        manifest["params"]["sheet_render"] = params
        manifest["stages"]["sheet_render"] = {"status": "ok", "output": output_name}
        _write_manifest(manifest_path, manifest)
        print(f"sheet rendered: {output_path}")
        return 0
    except _RenderError as error:
        print(str(error), file=sys.stderr)
        return error.code
    except ImportError as error:
        print(f"rendering dependency unavailable ({error}); run setup_tools.py", file=sys.stderr)
        return 1
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError, TypeError, KeyError,
            zipfile.BadZipFile) as error:
        print(f"sheet render failed: {error}", file=sys.stderr)
        return 3 if isinstance(error, ValueError) else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=("Validate host-authored sheet.json and render an editable Word study sheet.\n"
                     "A successful preparation stage is required; existing DOCX files are preserved."),
        epilog="Example: python render_sheet.py video.mp4 --template lesson.docx\n"
               "Exit codes: 0 rendered or resumed; 1 processing failure; 2 invalid input;\n"
               "3 invalid sheet or render conflict; 4 successful preparation required.")
    parser.add_argument("video", type=Path, help="source video with prepared sheet.json")
    parser.add_argument("--template", type=Path, help="optional .docx or .dotx Word template")
    args = parser.parse_args(argv)
    return run_render(args.video, args.template)


if __name__ == "__main__":
    sys.exit(main())
