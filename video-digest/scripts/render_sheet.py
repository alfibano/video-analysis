"""Validate a host-authored study sheet and render it as an editable DOCX."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import math
import os
import sys
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from sheet_schema import SheetValidationError

FIXED_CORE_TIME = datetime(2000, 1, 1, tzinfo=timezone.utc)
# Bump whenever the DOCX produced from the same inputs changes, so existing unmodified sheets re-render.
RENDER_LAYOUT_VERSION = "0.6.3"


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


_CALLOUT_COLORS = {
    "correction": ("FDE2E1", "D93025"),
    "complement": ("E3F0FB", "1A73E8"),
    "question": ("FFF4D6", "F29900"),
}


def _callout_icon(kind: str) -> bytes:
    from PIL import Image, ImageDraw, ImageFont
    scale = 4
    image = Image.new("RGBA", (96 * scale, 96 * scale), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    color = "#" + _CALLOUT_COLORS[kind][1]
    draw.ellipse((2 * scale, 2 * scale, 94 * scale, 94 * scale), fill=color)
    if kind == "correction":
        draw.line((29 * scale, 29 * scale, 67 * scale, 67 * scale), fill="white", width=10 * scale)
        draw.line((67 * scale, 29 * scale, 29 * scale, 67 * scale), fill="white", width=10 * scale)
    elif kind == "complement":
        draw.rounded_rectangle((43 * scale, 36 * scale, 53 * scale, 68 * scale), radius=4 * scale, fill="white")
        draw.ellipse((43 * scale, 22 * scale, 53 * scale, 32 * scale), fill="white")
    else:
        font = ImageFont.load_default(size=68 * scale)
        bounds = draw.textbbox((0, 0), "?", font=font)
        x = (96 * scale - (bounds[2] - bounds[0])) / 2 - bounds[0]
        y = (96 * scale - (bounds[3] - bounds[1])) / 2 - bounds[1] - 2 * scale
        draw.text((x, y), "?", fill="white", font=font)
    image = image.resize((96, 96), Image.Resampling.LANCZOS)
    output = io.BytesIO()
    image.save(output, format="PNG", optimize=True)
    return output.getvalue()


def _spacer(document) -> None:
    paragraph = document.add_paragraph()
    paragraph.paragraph_format.space_before = 0
    paragraph.paragraph_format.space_after = 4
    paragraph.paragraph_format.line_spacing = 1


def _callout_paragraph(document, sheet: Mapping[str, Any], callout: Mapping[str, Any],
                       width: int, icons: dict[str, tuple[bytes, Any]]):
    from copy import deepcopy
    from docx.oxml import OxmlElement
    from docx.oxml.ns import nsmap, qn
    from docx.shared import Inches
    from docx.text.paragraph import Paragraph

    nsmap.setdefault("wps", "http://schemas.microsoft.com/office/word/2010/wordprocessingShape")
    nsmap.setdefault("mc", "http://schemas.openxmlformats.org/markup-compatibility/2006")
    kind = callout["kind"]
    icon_para = OxmlElement("w:p")
    icon_wrapper = Paragraph(icon_para, document._body)
    icon_bytes, icon_picture = icons[kind]
    if icon_picture is None:
        icon_wrapper.add_run().add_picture(io.BytesIO(icon_bytes), height=Inches(.22))
        icon_picture = deepcopy(icon_para[0][0])
        icons[kind] = (icon_bytes, icon_picture)
    else:
        icon_run = OxmlElement("w:r")
        icon_run.append(deepcopy(icon_picture))
        icon_para.append(icon_run)
    icon_para[0].find(".//" + qn("wp:docPr")).set("id", str(_next_shape_id(document)))

    paragraph = OxmlElement("w:p")
    first = OxmlElement("w:p")
    first.append(icon_para[0])
    from docx.text.paragraph import Paragraph as DocxParagraph
    first_wrapper = DocxParagraph(first, document._body)
    label = first_wrapper.add_run(" " + sheet["labels"][kind] + ":")
    label.bold = True
    text_p = OxmlElement("w:p")
    text_wrapper = DocxParagraph(text_p, document._body)
    text_wrapper.add_run(callout["text"])
    refs = {entry["id"]: entry for entry in sheet["references"]}
    for source_id in callout["sources"]:
        text_wrapper.add_run(" (" + sheet["labels"]["source"] + " : ")
        _hyperlink(text_wrapper, str(source_id), refs[source_id]["url"])
        text_wrapper.add_run(")")

    drawing = OxmlElement("wp:inline")
    drawing.set("distT", "0"); drawing.set("distB", "0")
    drawing.set("distL", "0"); drawing.set("distR", "0")
    extent = OxmlElement("wp:extent"); extent.set("cx", str(width)); extent.set("cy", str(int(Inches(.38))))
    drawing.append(extent)
    doc_pr = OxmlElement("wp:docPr")
    doc_pr.set("id", str(_next_shape_id(document, first)))
    doc_pr.set("name", "Callout")
    drawing.append(doc_pr)
    frame_pr = OxmlElement("wp:cNvGraphicFramePr")
    frame_locks = OxmlElement("a:graphicFrameLocks"); frame_locks.set("noChangeAspect", "1")
    frame_pr.append(frame_locks); drawing.append(frame_pr)
    graphic = OxmlElement("a:graphic")
    graphic_data = OxmlElement("a:graphicData")
    graphic_data.set("uri", "http://schemas.microsoft.com/office/word/2010/wordprocessingShape")
    shape = OxmlElement("wps:wsp")
    nonvisual = OxmlElement("wps:cNvSpPr"); nonvisual.set("txBox", "1"); shape.append(nonvisual)
    shape_props = OxmlElement("wps:spPr")
    transform = OxmlElement("a:xfrm")
    off = OxmlElement("a:off"); off.set("x", "0"); off.set("y", "0")
    ext = OxmlElement("a:ext"); ext.set("cx", str(width)); ext.set("cy", str(int(Inches(.38))))
    transform.extend((off, ext)); shape_props.append(transform)
    geometry = OxmlElement("a:prstGeom"); geometry.set("prst", "roundRect")
    adjustments = OxmlElement("a:avLst"); adjustment = OxmlElement("a:gd")
    adjustment.set("name", "adj"); adjustment.set("fmla", "val 12000")
    adjustments.append(adjustment); geometry.append(adjustments); shape_props.append(geometry)
    fill_color, line_color = _CALLOUT_COLORS[kind]
    fill = OxmlElement("a:solidFill"); fill_rgb = OxmlElement("a:srgbClr"); fill_rgb.set("val", fill_color)
    fill.append(fill_rgb); shape_props.append(fill)
    line = OxmlElement("a:ln"); line.set("w", "12700")
    line_fill = OxmlElement("a:solidFill"); line_rgb = OxmlElement("a:srgbClr"); line_rgb.set("val", line_color)
    line_fill.append(line_rgb); line.append(line_fill); shape_props.append(line)
    shape.append(shape_props)
    textbox = OxmlElement("wps:txbx")
    content = OxmlElement("w:txbxContent"); content.extend((first, text_p)); textbox.append(content)
    shape.append(textbox)
    body_props = OxmlElement("wps:bodyPr")
    body_props.set("wrap", "square"); body_props.set("lIns", "91440"); body_props.set("rIns", "91440")
    body_props.set("tIns", "45720"); body_props.set("bIns", "45720")
    body_props.append(OxmlElement("a:spAutoFit")); shape.append(body_props)
    graphic_data.append(shape); graphic.append(graphic_data); drawing.append(graphic)
    # Word only accepts a DrawingML inline wrapped in w:drawing; LibreOffice tolerates its absence.
    drawing_wrapper = OxmlElement("w:drawing"); drawing_wrapper.append(drawing)
    choice = OxmlElement("mc:Choice"); choice.set("Requires", "wps"); choice.append(drawing_wrapper)
    alternate = OxmlElement("mc:AlternateContent"); alternate.append(choice)
    run = OxmlElement("w:r"); run.append(alternate); paragraph.append(run)
    return paragraph



def _next_shape_id(document, *detached) -> int:
    """Next drawing id unique across the document and not-yet-attached elements (Word rejects duplicates)."""
    from docx.oxml.ns import qn
    ids = [int(value) for element in (document._element, *detached)
           for node in element.iter(qn("wp:docPr")) if (value := node.get("id", "")).isdigit()]
    return max(ids, default=0) + 1

def _render_chapter(document, sheet: Mapping[str, Any], chapter: Mapping[str, Any], analysis_dir: Path,
                    available: int, icons: dict[str, bytes]) -> None:
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
        # Width only: python-docx then derives the height from the image, preserving its aspect ratio.
        table.cell(0, 1).paragraphs[0].add_run().add_picture(
            str(analysis_dir / Path(*frame["file"].split("/"))), width=max(1, image_width - Inches(.18)))
        caption = table.cell(0, 1).add_paragraph(style=_style(document, "Caption"))
        caption.add_run(sheet["labels"]["figure"] + " "); _add_linked_time(caption, sheet, frame["time"])
        _spacer(document)
    for callout in chapter["callouts"]:
        _spacer(document)
        paragraph = _callout_paragraph(document, sheet, callout, available, icons)
        body = document._element.body
        section_properties = body.sectPr
        body.insert(body.index(section_properties), paragraph) if section_properties is not None else body.append(paragraph)
        _spacer(document)


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
    icons: dict[str, tuple[bytes, Any]] = {}
    for chapter in sheet["chapters"]:
        for callout in chapter["callouts"]:
            if callout["kind"] not in icons:
                icons[callout["kind"]] = (_callout_icon(callout["kind"]), None)
        _render_chapter(document, sheet, chapter, analysis_dir, available, icons)
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
    properties.author = "video-digest"
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
            "facts_sha256": facts_sha, "accounting_sha256": accounting_sha, "brief_sha256": brief_sha,
            "layout_version": RENDER_LAYOUT_VERSION}


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
    recorded_sha = stage.get("output_sha256") if isinstance(stage, dict) else None
    if (isinstance(stage, dict) and stage.get("status") == "ok"
            and stage.get("output") == output_name and isinstance(recorded_sha, str)
            and _digest(output_path) == recorded_sha):
        return None
    raise _RenderError(3, "existing DOCX was edited or is untracked; rename or move it before rendering again")


def _save_document_atomic(document: Any, output_path: Path) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{output_path.name}.", suffix=".tmp", dir=output_path.parent)
    os.close(descriptor)
    temporary_path = Path(temporary_name)
    try:
        document.save(temporary_path)
        os.replace(temporary_path, output_path)
    finally:
        temporary_path.unlink(missing_ok=True)


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
        rerendering = output_path.exists()
        try:
            document = render_document(sheet, analysis_dir=analysis, facts=facts, accounting=accounting, template=template)
        except Exception as error:
            if error.__class__.__module__.startswith("docx.image"):
                raise _RenderError(3, f"invalid frame image: {error}") from error
            raise
        _save_document_atomic(document, output_path)
        manifest["params"]["sheet_render"] = params
        manifest["stages"]["sheet_render"] = {
            "status": "ok", "output": output_name, "output_sha256": _digest(output_path)}
        _write_manifest(manifest_path, manifest)
        if rerendering:
            print("sheet re-rendered (previous version was unmodified)")
        else:
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
                     "A successful preparation stage is required; edited DOCX files are protected."),
        epilog="Example: python render_sheet.py video.mp4 --template lesson.docx\n"
               "Exit codes: 0 rendered or resumed; 1 processing failure; 2 invalid input;\n"
               "3 invalid sheet or render conflict; 4 successful preparation required.")
    parser.add_argument("video", type=Path, help="source video with prepared sheet.json")
    parser.add_argument("--template", type=Path, help="optional .docx or .dotx Word template")
    args = parser.parse_args(argv)
    return run_render(args.video, args.template)


if __name__ == "__main__":
    sys.exit(main())
