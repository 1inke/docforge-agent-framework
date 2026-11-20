"""Unified Single-Pass Pandoc DOCX Compiler (Builtin / Mapped), RenderManifest Binder & Isolated PDF Exporter."""

from __future__ import annotations

import copy
import hashlib
import io
import os
import re
import shutil
import subprocess
import sys
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

import docx
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Inches, Pt

from daf.context_manager.manager import JINJA_TAG_PATTERN, compute_canonical_json_hash
from daf.core.contracts import (
    LayoutProfile,
    ProjectWorkspaceState,
    RenderManifest,
)
from daf.core.errors import (
    ArtifactPathInvalidError,
    PDFExportError,
    PandocUnavailableError,
    TemplateRenderError,
)
from daf.document_engine.pandoc_renderer import (
    render_document_single_pass_pandoc,
    render_mapped_sections_single_pass_pandoc,
)
from daf.project_store.store import validate_safe_docx_filename
from daf.workflow_engine.state_machine import (
    compute_final_draft_aggregate_hash,
    effective_section_ids,
)

RENDERER_VERSION = "daf-docx-pandoc-v2"
LATEX_MATH_PATTERN = re.compile(r"(\$\$[\s\S]+?\$\$|(?<!\\)\$[^\$\n]+?(?<!\\)\$)")
PREVIEW_BANNER_TEXT = "[PREVIEW DRAFT - UNAPPROVED / 内部预览草稿 - 未经人工最终批准]"


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _canonicalize_docx_zip(path: Path) -> None:
    """Repack a .docx zip archive in memory with sorted entry names and a fixed DOS timestamp."""
    fixed_dt = (2026, 1, 1, 0, 0, 0)
    raw = path.read_bytes()
    entries: list[tuple[str, bytes]] = []
    with zipfile.ZipFile(io.BytesIO(raw), "r") as zin:
        for info in sorted(zin.infolist(), key=lambda item: item.filename):
            entries.append((info.filename, zin.read(info.filename)))
    out_buf = io.BytesIO()
    with zipfile.ZipFile(out_buf, "w", compression=zipfile.ZIP_DEFLATED) as zout:
        for fname, data in entries:
            zi = zipfile.ZipInfo(filename=fname, date_time=fixed_dt)
            zi.compress_type = zipfile.ZIP_DEFLATED
            zi.external_attr = 0o600 << 16
            zout.writestr(zi, data)
    path.write_bytes(out_buf.getvalue())


def enforce_run_font_uniformity(
    element_root: Any,
    latin_font: str = "Times New Roman",
    cjk_font: str = "宋体",
) -> int:
    """Strip w:hint and *Theme attributes from all w:rFonts and enforce explicit Latin/CJK font families."""
    w_r = qn("w:r")
    w_rPr = qn("w:rPr")
    w_rFonts = qn("w:rFonts")
    w_ascii = qn("w:ascii")
    w_hAnsi = qn("w:hAnsi")
    w_cs = qn("w:cs")
    w_eastAsia = qn("w:eastAsia")
    attrs_to_strip = [
        qn("w:hint"),
        qn("w:asciiTheme"),
        qn("w:hAnsiTheme"),
        qn("w:eastAsiaTheme"),
        qn("w:cstheme"),
    ]

    sanitized_count = 0
    for rfonts in element_root.iter(w_rFonts):
        for attr in attrs_to_strip:
            if attr in rfonts.attrib:
                del rfonts.attrib[attr]
        rfonts.set(w_ascii, latin_font)
        rfonts.set(w_hAnsi, latin_font)
        rfonts.set(w_cs, latin_font)
        rfonts.set(w_eastAsia, cjk_font)
        sanitized_count += 1

    for run in element_root.iter(w_r):
        rpr = run.find(w_rPr)
        if rpr is None:
            rpr = OxmlElement("w:rPr")
            run.insert(0, rpr)
        rfonts = rpr.find(w_rFonts)
        if rfonts is None:
            rfonts = OxmlElement("w:rFonts")
            rpr.insert(0, rfonts)
        for attr in attrs_to_strip:
            if attr in rfonts.attrib:
                del rfonts.attrib[attr]
        rfonts.set(w_ascii, latin_font)
        rfonts.set(w_hAnsi, latin_font)
        rfonts.set(w_cs, latin_font)
        rfonts.set(w_eastAsia, cjk_font)
        sanitized_count += 1

    return sanitized_count


def _ordered_drafted_sections(state: ProjectWorkspaceState) -> list[Any]:
    ordered_ids = effective_section_ids(state) or list(state.section_versions.sections.keys())
    seen: set[str] = set()
    sections: list[Any] = []
    for sid in ordered_ids:
        sec = state.section_versions.sections.get(sid)
        if sec is not None and sec.revision > 0:
            sections.append(sec)
            seen.add(sid)
    for sid, sec in state.section_versions.sections.items():
        if sid not in seen and sec.revision > 0:
            sections.append(sec)
    return sections


def _plaintext_lines_from_markdown(md_text: str) -> list[str]:
    lines: list[str] = []
    for raw in md_text.splitlines():
        s = raw.strip()
        if not s or s.startswith(":::"):
            continue
        s = re.sub(r"^#{1,6}\s+", "", s)
        s = s.replace("[@hypothesis]", "")
        lines.append(s)
    return lines


def compute_render_input_fingerprint(
    state: ProjectWorkspaceState,
    purpose: Literal["preview", "release"] = "release",
    template_mode: str = "auto",
    section_mapping: dict[str, str] | None = None,
    degradation_mode: Literal["strict", "preview_plaintext"] = "strict",
) -> dict[str, Any]:
    """Compute the deterministic RenderManifest input fingerprint and component hashes."""
    tpl_report = state.source_registry.template_inspection
    effective_mode = template_mode
    if effective_mode == "auto":
        effective_mode = tpl_report.template_mode if tpl_report is not None else "builtin"

    live_template_sha = ""
    if effective_mode == "mapped" and tpl_report is not None:
        tpl_path = Path(tpl_report.template_path)
        if tpl_path.exists() and tpl_path.is_file():
            live_template_sha = _sha256_file(tpl_path)
        else:
            live_template_sha = "missing_template"

    sections = _ordered_drafted_sections(state)
    section_hashes = {s.section_id: f"sha256:{s.content_hash}" for s in sections}
    ordered_payload = [
        {
            "section_id": s.section_id,
            "title": s.title,
            "revision": s.revision,
            "content_hash": s.content_hash,
        }
        for s in sections
    ]
    expected_eq_count = sum(
        len(LATEX_MATH_PATTERN.findall(s.content_markdown)) for s in sections
    )

    profile_hash = f"sha256:{compute_canonical_json_hash(state.profile.model_dump(mode='json'))}"
    outline_hash = f"sha256:{state.outline_graph.outline_hash}"
    mapping_hash = f"sha256:{compute_canonical_json_hash(section_mapping or {})}"
    doc_content_hash = f"sha256:{compute_final_draft_aggregate_hash(state)}"

    fp_hex = compute_canonical_json_hash(
        {
            "renderer_version": RENDERER_VERSION,
            "purpose": purpose,
            "degradation_mode": degradation_mode,
            "template_mode": effective_mode,
            "template_sha256": live_template_sha,
            "section_mapping_hash": mapping_hash,
            "outline_hash": outline_hash,
            "profile_hash": profile_hash,
            "document_title": state.brief.title,
            "ordered_sections": ordered_payload,
        }
    )
    return {
        "effective_template_mode": effective_mode,
        "input_fingerprint": f"sha256:{fp_hex}",
        "document_content_hash": doc_content_hash,
        "section_hashes": section_hashes,
        "outline_hash": outline_hash,
        "profile_hash": profile_hash,
        "template_sha256": f"sha256:{live_template_sha}" if live_template_sha else "",
        "raw_template_sha256": live_template_sha,
        "section_mapping_hash": mapping_hash,
        "ordered_section_ids": [s.section_id for s in sections],
        "expected_equation_count": expected_eq_count,
    }


class DocumentEngine:
    """Compiles drafted Markdown into native Word (.docx) across Builtin and Mapped modes via single-pass Pandoc."""

    @staticmethod
    def configure_document_styles(doc: docx.Document, layout: LayoutProfile) -> None:
        normal_style = doc.styles["Normal"]
        normal_style.font.name = layout.latin_font
        normal_style.font.size = Pt(layout.body_font_size_pt)
        normal_style.paragraph_format.line_spacing = layout.line_spacing
        if normal_style._element.rPr is None:
            normal_style._element.get_or_add_rPr()
        rfonts = normal_style._element.rPr.get_or_add_rFonts()
        rfonts.set(qn("w:ascii"), layout.latin_font)
        rfonts.set(qn("w:hAnsi"), layout.latin_font)
        rfonts.set(qn("w:cs"), layout.latin_font)
        rfonts.set(qn("w:eastAsia"), layout.cjk_font)

        for section in doc.sections:
            section.top_margin = Inches(layout.margin_top_in)
            section.bottom_margin = Inches(layout.margin_bottom_in)
            section.left_margin = Inches(layout.margin_left_in)
            section.right_margin = Inches(layout.margin_right_in)

        heading_sizes = {1: 16.0, 2: 14.0, 3: 12.0}
        for level, sz in heading_sizes.items():
            style_name = f"Heading {level}"
            if style_name in doc.styles:
                h_style = doc.styles[style_name]
                h_style.font.name = layout.latin_font
                h_style.font.size = Pt(sz)
                h_style.font.bold = True
                if h_style._element.rPr is None:
                    h_style._element.get_or_add_rPr()
                h_rfonts = h_style._element.rPr.get_or_add_rFonts()
                h_rfonts.set(qn("w:ascii"), layout.latin_font)
                h_rfonts.set(qn("w:hAnsi"), layout.latin_font)
                h_rfonts.set(qn("w:cs"), layout.latin_font)
                h_rfonts.set(qn("w:eastAsia"), layout.heading_cjk_font)

    @staticmethod
    def _insert_elements_into_body(body_el: Any, elements: list[Any]) -> None:
        sect_pr = body_el.find(qn("w:sectPr"))
        for el in elements:
            if sect_pr is not None:
                sect_pr.addprevious(el)
            else:
                body_el.append(el)

    @staticmethod
    def _insert_elements_into_table_cell(tc_el: Any, elements: list[Any], layout: LayoutProfile) -> None:
        """Replace the contents of a <w:tc> element with the compiled elements while preserving <w:tcPr>."""
        w_p = qn("w:p")
        w_tbl = qn("w:tbl")
        for child in list(tc_el):
            if child.tag in {w_p, w_tbl}:
                tc_el.remove(child)

        for el in elements:
            tc_el.append(el)

        children_tags = [c.tag for c in tc_el]
        if not children_tags or children_tags[-1] != w_p:
            trailing_p = OxmlElement("w:p")
            enforce_run_font_uniformity(trailing_p, layout.latin_font, layout.cjk_font)
            tc_el.append(trailing_p)

    @classmethod
    def _apply_preview_watermark_and_metadata(
        cls,
        doc: docx.Document,
        purpose: Literal["preview", "release"],
        degradation_mode: Literal["strict", "preview_plaintext"],
        layout: LayoutProfile,
    ) -> None:
        fixed_epoch = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        doc.core_properties.created = fixed_epoch
        doc.core_properties.modified = fixed_epoch
        if purpose == "preview":
            doc.core_properties.category = "PREVIEW_DRAFT_UNAPPROVED"
            doc.core_properties.comments = (
                f"{PREVIEW_BANNER_TEXT} (degradation_mode={degradation_mode})"
            )
            for section in doc.sections:
                header = section.header
                header_p = header.paragraphs[0] if header.paragraphs else header.add_paragraph()
                header_p.text = PREVIEW_BANNER_TEXT
                enforce_run_font_uniformity(header_p._p, layout.latin_font, layout.heading_cjk_font)
        else:
            doc.core_properties.category = "OFFICIAL_RELEASE"
            doc.core_properties.comments = f"Compiled by DAF {RENDERER_VERSION}"

    @classmethod
    def verify_rendered_docx_qa(
        cls,
        artifact_path: Path,
        expected_equation_count: int,
        effective_mode: str,
        degradation_mode: Literal["strict", "preview_plaintext"],
        template_path: Path | None = None,
        template_before_sha: str | None = None,
        initial_tail_paragraph_texts: list[str] | None = None,
        allowed_tail_suffix_count: int = 0,
    ) -> dict[str, Any]:
        """Run unified OpenXML & structural QA checks on the compiled .docx before atomic promotion."""
        if not artifact_path.exists() or artifact_path.stat().st_size == 0:
            raise TemplateRenderError("Rendered DOCX file is missing or empty.")

        if template_path is not None and template_before_sha is not None:
            after_sha = _sha256_file(template_path)
            if after_sha != template_before_sha:
                raise TemplateRenderError(
                    "CRITICAL: Original template SHA-256 changed during rendering! Template immutability violated."
                )

        doc = docx.Document(str(artifact_path))
        body = doc._body._body

        all_texts: list[str] = [p.text for p in doc.paragraphs]
        for tbl in doc.tables:
            for row in tbl.rows:
                for cell in row.cells:
                    all_texts.append(cell.text)
        combined_text = "\n".join(all_texts)

        if JINJA_TAG_PATTERN.search(combined_text):
            raise TemplateRenderError(
                "Unrendered Jinja2 '{{ ... }}' placeholders remain in rendered DOCX output.",
                code="TEMPLATE_MAPPING_INVALID",
            )

        m_oMath = qn("m:oMath")
        m_oMathPara = qn("m:oMathPara")
        omml_nodes = list(body.iter(m_oMath)) + list(body.iter(m_oMathPara))
        if degradation_mode == "strict" and expected_equation_count > 0:
            if len(omml_nodes) == 0:
                raise TemplateRenderError(
                    f"Expected at least {expected_equation_count} native OMML equations in strict mode, "
                    "but found 0 <m:oMath> nodes in compiled DOCX.",
                    code="RENDER_DEPENDENCY_MISSING",
                )
            if "$$" in combined_text:
                raise TemplateRenderError(
                    "Raw uncompiled '$$...$$' LaTeX delimiters leaked into strict DOCX output.",
                    code="RENDER_DEPENDENCY_MISSING",
                )

        w_rFonts = qn("w:rFonts")
        w_hint = qn("w:hint")
        for rfonts in body.iter(w_rFonts):
            if w_hint in rfonts.attrib:
                raise TemplateRenderError("OpenXML QA failed: w:hint attribute was not stripped from w:rFonts.")

        if effective_mode == "mapped" and initial_tail_paragraph_texts is not None:
            current_body_paras = [p.text.strip() for p in doc.paragraphs if p.text.strip()]
            if len(current_body_paras) > len(initial_tail_paragraph_texts) + allowed_tail_suffix_count:
                raise TemplateRenderError(
                    "Mapped template QA failed: content was appended to the document tail outside mapped targets.",
                    code="TEMPLATE_MAPPING_INVALID",
                )

        return {
            "qa_passed": True,
            "omml_node_count": len(omml_nodes),
            "table_count": len(doc.tables),
            "paragraph_count": len(doc.paragraphs),
        }

    @classmethod
    def render_document(
        cls,
        state: ProjectWorkspaceState,
        output_dir: Path,
        purpose: Literal["preview", "release"] = "release",
        template_mode: str = "auto",
        section_mapping: dict[str, str] | None = None,
        output_filename: str | None = None,
        degradation_mode: Literal["strict", "preview_plaintext"] | None = None,
        pandoc_bin_override: str | None = "AUTO",
        *,
        job_id: str = "",
        render_id_override: str | None = None,
        update_state_in_place: bool = True,
        simulate_crash_before_finalize: bool = False,
    ) -> dict[str, Any]:
        """Compile drafted Markdown into a native .docx via single-pass Pandoc and atomic .tmp promotion."""
        if output_dir.exists() and output_dir.is_symlink():
            raise ArtifactPathInvalidError(
                f"Security violation: output_dir '{output_dir}' cannot be a symbolic link."
            )
        output_dir.mkdir(parents=True, exist_ok=True)
        resolved_out_dir = output_dir.resolve()
        tmp_work_dir = resolved_out_dir / ".tmp"
        if tmp_work_dir.exists() and tmp_work_dir.is_symlink():
            raise ArtifactPathInvalidError(
                f"Security violation: temporary render directory '{tmp_work_dir}' cannot be a symbolic link."
            )
        tmp_work_dir.mkdir(parents=True, exist_ok=True)

        if output_filename is not None:
            validate_safe_docx_filename(output_filename)

        eff_degradation: Literal["strict", "preview_plaintext"] = (
            degradation_mode
            if degradation_mode is not None
            else "strict"
        )
        if purpose == "release" and eff_degradation != "strict":
            raise TemplateRenderError(
                "Official 'release' rendering forbids 'preview_plaintext' degradation mode.",
                code="RENDER_DEPENDENCY_MISSING",
            )

        fp_info = compute_render_input_fingerprint(
            state=state,
            purpose=purpose,
            template_mode=template_mode,
            section_mapping=section_mapping,
            degradation_mode=eff_degradation,
        )
        effective_mode: str = fp_info["effective_template_mode"]
        if effective_mode == "tagged":
            raise TemplateRenderError(
                "template_mode='tagged' is no longer supported; use 'builtin' or 'mapped'.",
                code="TEMPLATE_MODE_UNSUPPORTED",
            )
        if effective_mode not in {"builtin", "mapped"}:
            raise TemplateRenderError(
                f"Unsupported template_mode '{effective_mode}'. Use 'builtin' or 'mapped'.",
                code="TEMPLATE_MODE_UNSUPPORTED",
            )

        sections = _ordered_drafted_sections(state)
        if not sections:
            raise TemplateRenderError(
                "Cannot render document: no drafted sections with revision > 0 exist.",
                code="TEMPLATE_MAPPING_INVALID",
            )

        resolved_pandoc_bin = (
            None
            if pandoc_bin_override == ""
            else (shutil.which("pandoc") if pandoc_bin_override == "AUTO" else pandoc_bin_override)
        )
        if resolved_pandoc_bin is None:
            if purpose == "release":
                raise PandocUnavailableError(
                    "Pandoc executable is required for official 'release' rendering."
                )
            eff_degradation = "preview_plaintext"

        layout = state.profile.layout_profile
        tpl_report = state.source_registry.template_inspection
        template_path: Path | None = None
        template_before_sha: str | None = None
        if effective_mode == "mapped":
            if tpl_report is None:
                raise TemplateRenderError(
                    "template_mode='mapped' requires an inspected .docx template.",
                    code="TEMPLATE_MAPPING_INVALID",
                )
            template_path = Path(tpl_report.template_path)
            if not template_path.exists():
                raise TemplateRenderError(
                    f"Inspected template file no longer exists: {template_path}",
                    code="TEMPLATE_MAPPING_INVALID",
                )
            template_before_sha = _sha256_file(template_path)

        render_seq = len(state.render_history) + (1 if state.last_render_manifest else 0) + 1
        render_id = render_id_override or f"render_{render_seq:04d}_{uuid.uuid4().hex[:6]}"
        fp_short = fp_info["input_fingerprint"].removeprefix("sha256:")[:8]

        if output_filename is not None:
            fname = validate_safe_docx_filename(output_filename)
        else:
            suffix = "_PREVIEW_DRAFT.docx" if purpose == "preview" else ".docx"
            fname = f"{state.brief.project_id}_{render_id}_{fp_short}{suffix}"

        final_output_path = (resolved_out_dir / fname).resolve()
        try:
            final_output_path.relative_to(resolved_out_dir)
        except ValueError as exc:
            raise ArtifactPathInvalidError(
                f"Security violation: output path '{final_output_path}' escapes output directory '{resolved_out_dir}'.",
                details={"output_filename": output_filename},
            ) from exc

        if template_path is not None and final_output_path == template_path.resolve():
            raise TemplateRenderError("Refusing to overwrite original template file.")

        tmp_output_path = tmp_work_dir / f"tmp_{uuid.uuid4().hex[:10]}_{fname}"

        total_tables = 0
        total_runs_sanitized = 0
        initial_tail_paras: list[str] | None = None
        allowed_tail_suffix_count = 0

        try:
            if effective_mode == "builtin":
                if eff_degradation == "strict" and resolved_pandoc_bin is not None:
                    render_document_single_pass_pandoc(
                        state=state,
                        output_docx_path=tmp_output_path,
                        purpose=purpose,
                        project_dir=output_dir.parent,
                        pandoc_bin=resolved_pandoc_bin,
                    )
                    doc = docx.Document(str(tmp_output_path))
                else:
                    doc = docx.Document()
                    cls.configure_document_styles(doc, layout)
                    if purpose == "preview":
                        doc.add_paragraph(PREVIEW_BANNER_TEXT)
                    doc.add_heading(state.brief.title, level=0)
                    for sec in sections:
                        doc.add_heading(sec.title, level=1)
                        for line in _plaintext_lines_from_markdown(sec.content_markdown):
                            doc.add_paragraph(line)

                cls._apply_preview_watermark_and_metadata(doc, purpose, eff_degradation, layout)
                total_runs_sanitized += enforce_run_font_uniformity(
                    doc._body._body, layout.latin_font, layout.cjk_font
                )
                total_tables = len(doc.tables)
                doc.save(str(tmp_output_path))

            elif effective_mode == "mapped":
                assert template_path is not None
                if not section_mapping:
                    raise TemplateRenderError(
                        "template_mode='mapped' requires explicit section_mapping (e.g. {'sec_id': 'table:0:r1:c1'}); "
                        "silent guessing of template locations is forbidden.",
                        code="TEMPLATE_MAPPING_INVALID",
                    )

                used_targets: set[str] = set()
                for _k, target_coord in section_mapping.items():
                    if target_coord in used_targets:
                        raise TemplateRenderError(
                            f"Duplicate mapping target '{target_coord}' in section_mapping.",
                            code="TEMPLATE_MAPPING_INVALID",
                        )
                    used_targets.add(target_coord)

                for sec in sections:
                    if not section_mapping.get(sec.section_id):
                        raise TemplateRenderError(
                            f"Missing explicit mapping in section_mapping for drafted section '{sec.section_id}'.",
                            code="TEMPLATE_MAPPING_INVALID",
                        )

                shutil.copy2(template_path, tmp_output_path)
                mapped_doc = docx.Document(str(tmp_output_path))
                cls.configure_document_styles(mapped_doc, layout)
                cls._apply_preview_watermark_and_metadata(mapped_doc, purpose, eff_degradation, layout)

                all_table_targets = all(
                    section_mapping.get(s.section_id, "").startswith("table:") for s in sections
                )
                if all_table_targets:
                    initial_tail_paras = [p.text.strip() for p in mapped_doc.paragraphs if p.text.strip()]

                section_elements: dict[str, list[Any]]
                bib_elements: list[Any]
                if eff_degradation == "strict" and resolved_pandoc_bin is not None:
                    mapped_res = render_mapped_sections_single_pass_pandoc(
                        state=state,
                        project_dir=output_dir.parent,
                        pandoc_bin=resolved_pandoc_bin,
                    )
                    section_elements = mapped_res["section_elements"]
                    bib_elements = mapped_res["bib_elements"]
                else:
                    section_elements = {}
                    for sec in sections:
                        scratch = docx.Document()
                        for line in _plaintext_lines_from_markdown(sec.content_markdown):
                            p = scratch.add_paragraph(line)
                            section_elements.setdefault(sec.section_id, []).append(copy.deepcopy(p._p))
                    bib_elements = []

                def _inject_elements_at(target_loc: str, els: list[Any]) -> None:
                    if target_loc.startswith("table:"):
                        m = re.match(r"^table:(\d+):r(\d+):c(\d+)$", target_loc)
                        if not m:
                            raise TemplateRenderError(
                                f"Invalid table cell mapping coordinate '{target_loc}'.",
                                code="TEMPLATE_MAPPING_INVALID",
                            )
                        t_i, r_i, c_i = int(m.group(1)), int(m.group(2)), int(m.group(3))
                        try:
                            cell = mapped_doc.tables[t_i].rows[r_i].cells[c_i]
                        except IndexError as exc:
                            raise TemplateRenderError(
                                f"Table coordinate '{target_loc}' out of range for template.",
                                code="TEMPLATE_MAPPING_INVALID",
                            ) from exc
                        cls._insert_elements_into_table_cell(cell._tc, els, layout)
                    elif target_loc.startswith("heading:"):
                        heading_needle = target_loc[len("heading:") :].strip()
                        matched_para = next(
                            (p for p in mapped_doc.paragraphs if heading_needle and heading_needle in p.text),
                            None,
                        )
                        if matched_para is None:
                            raise TemplateRenderError(
                                f"Mapped heading anchor '{heading_needle}' not found in template.",
                                code="TEMPLATE_MAPPING_INVALID",
                            )
                        anchor = matched_para._p
                        for el in els:
                            anchor.addnext(el)
                            anchor = el
                    else:
                        raise TemplateRenderError(
                            f"Unsupported mapping target '{target_loc}'. Use 'table:<t>:r<r>:c<c>' or 'heading:<text>'.",
                            code="TEMPLATE_MAPPING_INVALID",
                        )

                for sec in sections:
                    _inject_elements_at(
                        section_mapping[sec.section_id],
                        section_elements.get(sec.section_id, []),
                    )

                if bib_elements:
                    bib_target = section_mapping.get("__bibliography__") or section_mapping.get("references")
                    if bib_target:
                        _inject_elements_at(bib_target, bib_elements)
                    else:
                        scratch_bib = docx.Document()
                        bib_h = scratch_bib.add_heading("参考文献", level=1)
                        cls._insert_elements_into_body(mapped_doc._body._body, [copy.deepcopy(bib_h._p), *bib_elements])
                        w_t = qn("w:t")
                        allowed_tail_suffix_count = 1 + sum(
                            1
                            for el in bib_elements
                            if "".join(t.text or "" for t in el.iter(w_t)).strip()
                        )

                total_runs_sanitized += enforce_run_font_uniformity(
                    mapped_doc._body._body, layout.latin_font, layout.cjk_font
                )
                total_tables = len(mapped_doc.tables)
                mapped_doc.save(str(tmp_output_path))

            _canonicalize_docx_zip(tmp_output_path)
            qa_report = cls.verify_rendered_docx_qa(
                artifact_path=tmp_output_path,
                expected_equation_count=fp_info["expected_equation_count"],
                effective_mode=effective_mode,
                degradation_mode=eff_degradation,
                template_path=template_path,
                template_before_sha=template_before_sha,
                initial_tail_paragraph_texts=initial_tail_paras,
                allowed_tail_suffix_count=allowed_tail_suffix_count,
            )

            os.replace(str(tmp_output_path), str(final_output_path))
        finally:
            if tmp_output_path.exists():
                try:
                    tmp_output_path.unlink()
                except Exception:
                    pass

        output_sha = _sha256_file(final_output_path)
        if simulate_crash_before_finalize:
            raise RuntimeError(
                f"Simulated crash after file promotion to '{final_output_path}' before DB finalize."
            )

        now_iso = datetime.now(timezone.utc).isoformat()
        manifest = RenderManifest(
            render_id=render_id,
            job_id=job_id,
            purpose=purpose,
            degradation_mode=eff_degradation,
            input_fingerprint=fp_info["input_fingerprint"],
            document_content_hash=fp_info["document_content_hash"],
            section_hashes=fp_info["section_hashes"],
            outline_hash=fp_info["outline_hash"],
            profile_hash=fp_info["profile_hash"],
            template_sha256=fp_info["template_sha256"],
            section_mapping_hash=fp_info["section_mapping_hash"],
            renderer_version=RENDERER_VERSION,
            artifact_path=str(final_output_path),
            artifact_sha256=f"sha256:{output_sha}",
            artifact_status="committed",
            omml_equations_count=qa_report["omml_node_count"],
            tables_rendered=total_tables,
            created_at=now_iso,
        )
        if update_state_in_place:
            if state.last_render_manifest is not None:
                state.last_render_manifest.artifact_status = "superseded"
                state.render_history.append(state.last_render_manifest)
            state.last_render_manifest = manifest
            if purpose == "release":
                state.stage = "rendered"

        return {
            "project_id": state.brief.project_id,
            "render_id": render_id,
            "job_id": job_id,
            "purpose": purpose,
            "degradation_mode": eff_degradation,
            "template_mode": effective_mode,
            "artifact_path": str(final_output_path),
            "artifact_sha256": output_sha,
            "input_fingerprint": fp_info["input_fingerprint"],
            "sections_rendered": fp_info["ordered_section_ids"],
            "omml_equations_count": qa_report["omml_node_count"],
            "tables_rendered": total_tables,
            "runs_font_sanitized": total_runs_sanitized,
            "template_immutability_verified": template_before_sha is not None,
            "render_manifest": manifest.model_dump(mode="json"),
        }

    @staticmethod
    def export_pdf_via_word_com(source_docx_path: str | Path, pdf_path: str | Path, timeout_sec: int = 60) -> dict[str, Any]:
        """Export a .docx file to .pdf on Windows using an isolated child process running Word COM."""
        src = Path(source_docx_path).resolve()
        dst = Path(pdf_path).resolve()
        if not src.exists():
            raise PDFExportError(f"Cannot export PDF: source DOCX does not exist at '{src}'.")
        if sys.platform != "win32":
            raise PDFExportError(
                f"Word COM PDF export is only supported on Windows (current platform: {sys.platform})."
            )

        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp_pdf = dst.parent / f".tmp_{uuid.uuid4().hex[:8]}_{dst.name}"
        child_script = (
            "import sys, pythoncom, win32com.client\n"
            "src, dst = sys.argv[1], sys.argv[2]\n"
            "pythoncom.CoInitialize()\n"
            "word = None\n"
            "doc = None\n"
            "try:\n"
            "    word = win32com.client.DispatchEx('Word.Application')\n"
            "    word.Visible = False\n"
            "    word.DisplayAlerts = 0\n"
            "    doc = word.Documents.Open(src, ReadOnly=True)\n"
            "    doc.ExportAsFixedFormat(dst, 17)\n"
            "    doc.Close(False)\n"
            "    doc = None\n"
            "    word.Quit()\n"
            "    word = None\n"
            "except Exception as e:\n"
            "    sys.stderr.write(f'WORD_COM_ERROR: {e}\\n')\n"
            "    sys.exit(2)\n"
            "finally:\n"
            "    pythoncom.CoUninitialize()\n"
        )

        proc = subprocess.run(
            [sys.executable, "-X", "faulthandler=0", "-c", child_script, str(src), str(tmp_pdf)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_sec,
            check=False,
        )
        if not tmp_pdf.exists() or tmp_pdf.stat().st_size == 0:
            err_detail = proc.stderr.strip() or proc.stdout.strip() or f"exit_code={proc.returncode}"
            raise PDFExportError(f"Word COM PDF export failed for '{src}': {err_detail}")

        os.replace(str(tmp_pdf), str(dst))
        return {
            "pdf_path": str(dst),
            "pdf_sha256": _sha256_file(dst),
            "source_docx_sha256": _sha256_file(src),
            "pdf_size_bytes": dst.stat().st_size,
        }
