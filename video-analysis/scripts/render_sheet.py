"""Validate a host-authored study sheet and render it as an editable DOCX."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import math
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from sheet_schema import SheetValidationError

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
        style_id = paragraph.part.document.styles["Hyperlink"].style_id
    except KeyError:
        color = OxmlElement("w:color")
        color.set(qn("w:val"), "0563C1")
        underline = OxmlElement("w:u")
        underline.set(qn("w:val"), "single")
        properties.extend((color, underline))
    else:
        style = OxmlElement("w:rStyle")
        style.set(qn("w:val"), style_id)
        properties.append(style)
    run.append(properties)
    text_node = OxmlElement("w:t")
    text_node.text = text
    run.append(text_node)
    hyperlink.append(run)
    paragraph._p.append(hyperlink)


def _grouped_count(value: Any, language: str) -> str:
    grouped = f"{int(value):,}"
    return grouped.replace(",", "\u202f") if language.casefold().startswith("fr") else grouped


def _fact_value(key: str, value: Any, language: str) -> str:
    if key == "duration_s":
        hours, remainder = divmod(int(value), 3600)
        minutes, seconds = divmod(remainder, 60)
        return f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes}:{seconds:02d}"
    if key == "file_size_mb":
        return f"{value:.1f} MB"
    if key in {"view_count", "like_count", "comment_count", "automatic_caption_language_count"}:
        return _grouped_count(value, language)
    return ", ".join(map(str, value)) if isinstance(value, list) else str(value)


def _format_usd(value: Any) -> str:
    return "n/a" if value == "n/a" else f"{value:.2f} $"


def _document_from_template(template: Path | None):
    from docx import Document
    if template is None:
        return Document()
    try:
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
        document = Document(target)
        if not document.sections:
            from copy import deepcopy
            fallback = Document()
            document._element.body.append(deepcopy(fallback._element.body.sectPr))
        return document
    except ImportError:
        raise
    except Exception as error:
        raise _RenderError(2, f"template is not a readable Word document: {error}") from error


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


def _cell_borderless(table) -> None:
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    borders = OxmlElement("w:tblBorders")
    for edge in ("top", "left", "bottom", "right", "insideH"):
        node = OxmlElement(f"w:{edge}"); node.set(qn("w:val"), "nil"); borders.append(node)
    node = OxmlElement("w:insideV"); node.set(qn("w:val"), "single"); node.set(qn("w:sz"), "4"); node.set(qn("w:color"), "B7B7B7"); borders.append(node)
    table._tbl.tblPr.append(borders)


def _render_chapter(document, sheet: Mapping[str, Any], chapter: Mapping[str, Any], analysis_dir: Path) -> None:
    from docx.shared import Inches
    heading = _paragraph(document, style="Heading 1")
    _add_linked_time(heading, sheet, chapter["start"]); heading.add_run(" — " + chapter["title"])
    section = document.sections[-1] if document.sections else None
    sect_pr = document._element.body.sectPr
    has_page_size = sect_pr is not None and sect_pr.find("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}pgSz") is not None
    available = (section.page_width - section.left_margin - section.right_margin) if section and has_page_size else Inches(6)
    for block in chapter["blocks"]:
        frame = block["frame"]
        if frame is None:
            _paragraph(document, block["text"])
            continue
        table = document.add_table(rows=1, cols=2); table.autofit = False; _cell_borderless(table)
        text_width, image_width = int(available * .62), int(available * .38)
        table.columns[0].width, table.columns[1].width = text_width, image_width
        table.cell(0, 0).width, table.cell(0, 1).width = text_width, image_width
        table.cell(0, 0).paragraphs[0].add_run(block["text"])
        picture = table.cell(0, 1).paragraphs[0].add_run().add_picture(str(analysis_dir / Path(*frame["file"].split("/"))), width=Inches(1))
        max_width = max(1, image_width - Inches(.18))
        picture.width = max_width
        caption = table.cell(0, 1).add_paragraph(style=_style(document, "Caption"))
        caption.add_run(sheet["labels"]["figure"] + " "); _add_linked_time(caption, sheet, frame["time"])
    for callout in chapter["callouts"]:
        table = document.add_table(rows=1, cols=1); table.autofit = False
        cell = table.cell(0, 0); shade = {"correction": "FDE2E1", "complement": "E3F0FB", "question": "FFF4D6"}[callout["kind"]]
        from docx.oxml import OxmlElement
        from docx.oxml.ns import qn
        shd = OxmlElement("w:shd"); shd.set(qn("w:fill"), shade); cell._tc.get_or_add_tcPr().append(shd)
        paragraph = cell.paragraphs[0]; icon = {"correction": "✖", "complement": "ℹ", "question": "?"}[callout["kind"]]
        paragraph.add_run(icon)
        paragraph.add_run(" " + sheet["labels"][callout["kind"]] + ": " + callout["text"])
        refs = {entry["id"]: entry for entry in sheet["references"]}
        for source_id in callout["sources"]:
            paragraph.add_run(" (" + sheet["labels"]["source"] + " : ")
            _hyperlink(paragraph, str(source_id), refs[source_id]["url"]); paragraph.add_run(")")


def render_document(sheet: Mapping[str, Any], *, analysis_dir: Path, facts: Mapping[str, Any] | None = None,
                    accounting: Mapping[str, Any] | None = None, template: Path | None = None):
    from sheet_schema import is_safe_web_url
    from docx.shared import Inches
    document = _document_from_template(template)
    _clear_body(document)
    _paragraph(document, sheet["title"], "Title")
    facts, accounting = facts or {}, accounting or {"tokens": {"total": 0, "measured": 0, "estimated": 0}, "usd": {"total": "n/a"}}
    section = document.sections[-1] if document.sections else None
    sect_pr = document._element.body.sectPr
    has_page_size = sect_pr is not None and sect_pr.find("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}pgSz") is not None
    available = (section.page_width - section.left_margin - section.right_margin) if section and has_page_size else Inches(6)
    facts_table = document.add_table(rows=0, cols=2); facts_table.autofit = False
    facts_table.columns[0].width, facts_table.columns[1].width =  Inches(1.55), max(Inches(1), available - Inches(1.55))
    rows = [(sheet["labels"]["source"], sheet["source"]["title"])]
    for key, value in facts.items():
        if value is None or value == "" or value == [] or (key == "title" and value == sheet["source"]["title"]):
            continue
        label = sheet["labels"]["facts"].get(key, key)
        shown_value = value if key in {"channel_url", "description_links"} else _fact_value(
            key, value, sheet.get("language", "en"))
        rows.append((label, shown_value))
    tokens, usd = accounting.get("tokens", {}), accounting.get("usd", {})
    usd_text = _format_usd(usd.get("total", 0))
    language = sheet.get("language", "en")
    total_tokens = _grouped_count(tokens.get("total", 0), language)
    measured_tokens = _grouped_count(tokens.get("measured", 0), language)
    estimated_tokens = _grouped_count(tokens.get("estimated", 0), language)
    price_date = sheet["labels"]["prices_as_of"]
    cost_line = (f"{total_tokens} {sheet['labels']['tokens']} · {usd_text} "
                 f"({measured_tokens} {sheet['labels']['measured']} + {estimated_tokens} "
                 f"{sheet['labels']['estimated']}, {price_date} 2026-11-03)")
    rows.append((sheet["labels"]["cost"], cost_line))
    for label, value in rows:
        cells = facts_table.add_row().cells; cells[0].width, cells[1].width = Inches(1.55), max(Inches(1), available - Inches(1.55))
        cells[0].text = str(label); p = cells[1].paragraphs[0]
        if label == sheet["labels"]["source"] and is_safe_web_url(sheet["source"].get("url")):
            p.add_run(str(value) + " ("); _hyperlink(p, sheet["source"]["url"], sheet["source"]["url"]); p.add_run(")")
        elif label == sheet["labels"]["facts"].get("description_links") and isinstance(value, list):
            for index, link in enumerate(value):
                if index:
                    p.add_run().add_break()
                if is_safe_web_url(link): _hyperlink(p, link, link)
                else: p.add_run(str(link))
        elif isinstance(value, str) and is_safe_web_url(value): _hyperlink(p, value, value)
        else: p.add_run(str(value))
        for cell in cells:
            for run in cell.paragraphs[0].runs: run.font.size = __import__("docx").shared.Pt(8)
    context = sheet["context"]
    for field, label_key in (("recording_date", "recording_date"), ("series", "series"), ("author_email", "author_email"), ("comments_summary", "comments")):
        value = context[field]
        if value is None: continue
        p = _paragraph(document); p.add_run(sheet["labels"][label_key] + ": ")
        if field == "series":
            p.add_run(value["theme"])
            if value["first_video_url"] and is_safe_web_url(value["first_video_url"]): p.add_run(" ("); _hyperlink(p, value["first_video_url"], value["first_video_url"]); p.add_run(")")
        else: p.add_run(value["value"] if isinstance(value, dict) else value)
    _paragraph(document, sheet["labels"]["introduction"], "Heading 1"); _paragraph(document, sheet["introduction"])
    _paragraph(document, sheet["labels"]["summary"], "Heading 1")
    for paragraph in sheet["summary"]: _paragraph(document, paragraph)
    _paragraph(document, sheet["labels"]["chapters_rationale"], "Heading 2")
    rationale = _paragraph(document, sheet["chapter_rationale"]); [setattr(run, "italic", True) for run in rationale.runs]
    for chapter in sheet["chapters"]:
        _render_chapter(document, sheet, chapter, analysis_dir)
    if sheet["glossary"]:
        _paragraph(document, sheet["labels"]["glossary"], "Heading 1")
        for item in sheet["glossary"]:
            _paragraph(document, item["term"], "Heading 2")
            _paragraph(document, item["definition"])
    if sheet["references"]:
        _paragraph(document, sheet["labels"]["references"], "Heading 1")
        for reference in sheet["references"]:
            p = _paragraph(document); p.add_run(f"{reference['id']} : "); _hyperlink(p, reference["url"], reference["url"]); p.add_run(" — " + reference["title"])
    properties = document.core_properties
    properties.title = sheet["title"]
    properties.author = "video-analysis"
    properties.created = FIXED_CORE_TIME
    properties.modified = FIXED_CORE_TIME
    return document


def _read_manifest(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeError) as error:
        raise _RenderError(2, "manifest is malformed") from error
    if (not isinstance(value, dict) or not isinstance(value.get("params"), dict)
            or not isinstance(value.get("stages"), dict)):
        raise _RenderError(2, "manifest is malformed")
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


def _prepared_sheet(video: Path, manifest_path: Path, bundle_path: Path, sheet_path: Path,
                    analysis: Path) -> tuple[dict[str, Any], str, dict[str, Any], dict[str, Any], dict[str, Any], str, str]:
    manifest = _read_manifest(manifest_path)
    prep = manifest["stages"].get("sheet_prepare")
    if not isinstance(prep, dict) or prep.get("status") != "ok":
        raise _RenderError(4, "successful sheet preparation is required")
    prep_params = manifest["params"].get("sheet_prepare")
    if not isinstance(prep_params, dict):
        raise _RenderError(2, "manifest sheet preparation parameters are malformed")
    try:
        bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeError) as error:
        raise _RenderError(2, "bundle.json is malformed") from error
    if not isinstance(bundle, dict) or not isinstance(bundle.get("source"), dict):
        raise _RenderError(2, "bundle.json is malformed")
    bundle_digest = _digest(bundle_path)
    if prep_params.get("bundle_sha256") != bundle_digest:
        raise _RenderError(3, "bundle digest changed since sheet preparation")
    bundle_source = bundle["source"]
    expected_source_url = bundle_source.get("url") if isinstance(bundle_source, dict) else None
    if expected_source_url is not None and not isinstance(expected_source_url, str):
        raise _RenderError(2, "bundle.json is malformed")
    from sheet_schema import load_sheet
    from sheet_facts import build_sheet_facts, canonical_sha256
    from sheet_cost import build_sheet_accounting
    expected = {"host": prep_params.get("host"), "model": prep_params.get("model"), "role": "default"}
    raw = json.loads((analysis / "raw_info.json").read_text(encoding="utf-8")) if (analysis / "raw_info.json").is_file() else {}
    metadata = json.loads((analysis / "metadata.json").read_text(encoding="utf-8")) if (analysis / "metadata.json").is_file() else {}
    facts = build_sheet_facts(video, metadata=metadata, raw_info=raw, manifest=manifest, bundle=bundle)
    facts_hash = canonical_sha256(facts)
    comments = json.loads((analysis / "comments.json").read_text(encoding="utf-8")) if (analysis / "comments.json").is_file() else None
    comments_hash = canonical_sha256(comments) if comments is not None else None
    brief_path = analysis / "sheet.brief.md"
    if (facts_hash != prep_params.get("facts_sha256") or comments_hash != prep_params.get("comments_sha256")
            or not brief_path.is_file() or _digest(brief_path) != prep.get("brief_sha256")):
        raise _RenderError(3, "sheet facts, comments, or brief changed since preparation")
    duration = bundle.get("source", {}).get("duration")
    if not isinstance(duration, (int, float)) or isinstance(duration, bool) or not math.isfinite(duration): duration = facts.get("duration_s", 0)
    sheet = load_sheet(sheet_path, analysis_dir=analysis,
        expected_bundle_sha256=bundle_digest, expected_writer=expected,
        expected_source_url=expected_source_url, expected_duration_s=float(duration))
    brief_text = brief_path.read_text(encoding="utf-8")
    bundle_text = bundle_path.read_text(encoding="utf-8")
    sheet_text = sheet_path.read_text(encoding="utf-8")
    accounting = build_sheet_accounting(manifest=manifest, provider=prep_params.get("provider"),
        model=prep_params.get("model", ""), endpoint_identity=prep_params.get("endpoint_identity"),
        brief_text=brief_text, bundle_text=bundle_text, sheet_text=sheet_text, sheet=sheet)
    return manifest, bundle_digest, sheet, facts, accounting, facts_hash, canonical_sha256(accounting)


def _render_params(sheet_path: Path, template: Path | None, facts_sha: str, accounting_sha: str, brief_sha: str) -> dict[str, Any]:
    return {"sheet_sha256": _digest(sheet_path),
            "template_sha256": _digest(template) if template else "builtin", "schema_version": 2,
            "facts_sha256": facts_sha, "accounting_sha256": accounting_sha, "brief_sha256": brief_sha}


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
        try:
            raw_value = json.loads(sheet_path.read_text(encoding="utf-8"))
            raw_version = raw_value.get("schema_version") if isinstance(raw_value, dict) else None
        except (json.JSONDecodeError, UnicodeError):
            raise _RenderError(2, "sheet.json is malformed")
        if raw_version == 1:
            print("regenerate sheet.json with the v0.6.0 brief", file=sys.stderr)
            return 3
        manifest, _, sheet, facts, accounting, facts_sha, accounting_sha = _prepared_sheet(video, manifest_path, bundle_path, sheet_path, analysis)
        prep = manifest["stages"]["sheet_prepare"]
        params = _render_params(sheet_path, template, facts_sha, accounting_sha, prep.get("brief_sha256", ""))
        output_name = f"{video.stem}.docx"
        output_path = analysis / output_name
        resumed = _resume_decision(output_path, output_name, manifest, params)
        if resumed is not None:
            return resumed
        try:
            document = render_document(sheet, analysis_dir=analysis, facts=facts, accounting=accounting, template=template)
        except Exception as error:
            if error.__class__.__module__.startswith("docx.image"):
                raise _RenderError(3, f"invalid frame image: {error}") from error
            raise
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
    except (json.JSONDecodeError, UnicodeError) as error:
        print(f"sheet render input is malformed: {error}", file=sys.stderr)
        return 2
    except SheetValidationError as error:
        print(f"sheet render failed: {error}", file=sys.stderr)
        return 3
    except (OSError, ValueError, TypeError, KeyError, zipfile.BadZipFile) as error:
        print(f"sheet render failed: {error}", file=sys.stderr)
        return 1


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
