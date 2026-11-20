"""Unified DocumentIR -> DOCX Compiler (Builtin / Tagged / Mapped), RenderManifest Snapshot Binder & Isolated PDF Exporter (DAF-P0-04 & DAF-P1-06)."""

from __future__ import annotations

import copy
import hashlib
import io
import os
import re
import shutil
import subprocess
import sys
import tempfile
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
    DocumentIR,
    IRBlock,
    IRSection,
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
from daf.document_engine.ir import build_document_ir
from daf.project_store.store import validate_safe_docx_filename
from daf.workflow_engine.state_machine import compute_final_draft_aggregate_hash

RENDERER_VERSION = "daf-docx-ir-v2"
LATEX_MATH_PATTERN = re.compile(r"(\$\$[\s\S]+?\$\$|(?<!\\)\$[^\$\n]+?(?<!\\)\$)")
PREVIEW_BANNER_TEXT = "[PREVIEW DRAFT - UNAPPROVED / 内部预览草稿 - 未经人工最终批准]"


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _canonicalize_docx_zip(path: Path) -> None:
    """Repack a .docx zip archive in memory with sorted entry names and a fixed DOS timestamp so identical XML content yields an identical SHA-256."""
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


def compute_render_input_fingerprint(
    state: ProjectWorkspaceState,
    purpose: Literal["preview", "release"] = "release",
    template_mode: str = "auto",
    section_mapping: dict[str, str] | None = None,
    degradation_mode: Literal["strict", "preview_plaintext"] = "strict",
) -> dict[str, Any]:
    """Compute the deterministic RenderManifest input fingerprint and component hashes (DAF-P0-04 §4.2)."""
    tpl_report = state.source_registry.template_inspection
    effective_mode = template_mode
    if effective_mode == "auto":
        effective_mode = tpl_report.template_mode if tpl_report is not None else "builtin"

    live_template_sha = ""
    if effective_mode in {"tagged", "mapped"} and tpl_report is not None:
        tpl_path = Path(tpl_report.template_path)
        if tpl_path.exists() and tpl_path.is_file():
            live_template_sha = _sha256_file(tpl_path)
        else:
            live_template_sha = "missing_template"

    ir = build_document_ir(state, purpose=purpose)
    section_hashes = {s.section_id: f"sha256:{s.content_hash}" for s in ir.sections}
    ordered_ir_payload = [s.model_dump(mode="json") for s in ir.sections]

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
            "ordered_ir_sections": ordered_ir_payload,
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
        "ir": ir,
    }


class DocumentEngine:
    """Compiles DocumentIR into native Word (.docx) across Builtin / Tagged / Mapped modes with RenderManifest verification."""

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
            section.top_margin = Inches(1.0)
            section.bottom_margin = Inches(1.0)
            section.left_margin = Inches(1.0)
            section.right_margin = Inches(1.0)

        for level in (1, 2, 3):
            style_name = f"Heading {level}"
            if style_name in doc.styles:
                h_style = doc.styles[style_name]
                h_style.font.name = layout.latin_font
                if h_style._element.rPr is None:
                    h_style._element.get_or_add_rPr()
                h_rfonts = h_style._element.rPr.get_or_add_rFonts()
                h_rfonts.set(qn("w:ascii"), layout.latin_font)
                h_rfonts.set(qn("w:hAnsi"), layout.latin_font)
                h_rfonts.set(qn("w:cs"), layout.latin_font)
                h_rfonts.set(qn("w:eastAsia"), layout.heading_cjk_font)

    @classmethod
    def _compile_markdown_fragment_to_elements(
        cls,
        markdown_text: str,
        layout: LayoutProfile,
        degradation_mode: Literal["strict", "preview_plaintext"] = "strict",
        pandoc_bin_override: str | None = "AUTO",
    ) -> tuple[list[Any], int, int]:
        """Compile a Markdown / LaTeX fragment into detached, font-sanitized OpenXML (<w:p>, <w:tbl>) elements.

        Never mutates any caller document directly, preventing tail-append displacement bugs in mapped/tagged modes (T-DOC-02).
        """
        if not markdown_text.strip():
            return [], 0, 0

        has_math = bool(LATEX_MATH_PATTERN.search(markdown_text))
        pandoc_bin = shutil.which("pandoc") if pandoc_bin_override == "AUTO" else pandoc_bin_override

        if pandoc_bin is None and has_math and (
            degradation_mode == "strict" or layout.equation_mode == "pandoc_omml"
        ):
            if degradation_mode == "strict":
                raise PandocUnavailableError(
                    "Document contains LaTeX math formulas ($...$ or $$...$$) and degradation_mode='strict', "
                    "but 'pandoc' executable is unavailable. Refusing silent plaintext degradation.",
                    details={"equation_mode": layout.equation_mode, "degradation_mode": degradation_mode},
                )

        elements: list[Any] = []
        omml_count = 0
        runs_sanitized = 0

        if pandoc_bin is not None:
            with tempfile.TemporaryDirectory() as tmp_dir:
                tmp_path = Path(tmp_dir)
                md_file = tmp_path / "fragment.md"
                docx_file = tmp_path / "fragment.docx"
                md_file.write_text(markdown_text, encoding="utf-8")

                proc = subprocess.run(
                    [
                        pandoc_bin,
                        str(md_file),
                        "-f",
                        "markdown+tex_math_dollars",
                        "-t",
                        "docx",
                        "-o",
                        str(docx_file),
                    ],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    check=False,
                )
                if proc.returncode != 0 or not docx_file.exists():
                    raise TemplateRenderError(
                        f"Pandoc compilation failed (exit {proc.returncode}): {proc.stderr.strip()}",
                        code="RENDER_DEPENDENCY_MISSING",
                    )

                frag_doc = docx.Document(str(docx_file))
                m_oMath = qn("m:oMath")
                m_oMathPara = qn("m:oMathPara")
                w_p = qn("w:p")
                w_tbl = qn("w:tbl")
                r_embed = qn("r:embed")
                r_id = qn("r:id")

                for child in list(frag_doc._body._body):
                    if child.tag not in {w_p, w_tbl}:
                        continue
                    cloned = copy.deepcopy(child)
                    # Strip any dangling external relationship attributes on cloned fragment nodes
                    for el in cloned.iter():
                        for rattr in (r_embed, r_id):
                            if rattr in el.attrib:
                                del el.attrib[rattr]

                    is_heading = False
                    pPr = cloned.find(qn("w:pPr"))
                    if pPr is not None:
                        pStyle = pPr.find(qn("w:pStyle"))
                        if pStyle is not None:
                            val = pStyle.get(qn("w:val"), "")
                            if "Heading" in val or "heading" in val:
                                is_heading = True
                    cjk_target = layout.heading_cjk_font if is_heading else layout.cjk_font
                    runs_sanitized += enforce_run_font_uniformity(
                        cloned,
                        latin_font=layout.latin_font,
                        cjk_font=cjk_target,
                    )
                    omml_count += len(list(cloned.iter(m_oMath))) + len(list(cloned.iter(m_oMathPara)))
                    elements.append(cloned)
        else:
            # Scratch document fallback (only reached when has_math is False or degradation_mode == 'preview_plaintext')
            scratch_doc = docx.Document()
            for block in markdown_text.split("\n\n"):
                clean_block = block.strip()
                if not clean_block:
                    continue
                if clean_block.startswith("### "):
                    p = scratch_doc.add_heading(clean_block[4:].strip(), level=3)
                    cjk_f = layout.heading_cjk_font
                elif clean_block.startswith("## "):
                    p = scratch_doc.add_heading(clean_block[3:].strip(), level=2)
                    cjk_f = layout.heading_cjk_font
                elif clean_block.startswith("# "):
                    p = scratch_doc.add_heading(clean_block[2:].strip(), level=1)
                    cjk_f = layout.heading_cjk_font
                else:
                    p = scratch_doc.add_paragraph(clean_block)
                    cjk_f = layout.cjk_font
                runs_sanitized += enforce_run_font_uniformity(
                    p._p, latin_font=layout.latin_font, cjk_font=cjk_f
                )
                elements.append(copy.deepcopy(p._p))

        return elements, omml_count, runs_sanitized

    @classmethod
    def render_ir_block_to_elements(
        cls,
        block: IRBlock,
        layout: LayoutProfile,
        degradation_mode: Literal["strict", "preview_plaintext"] = "strict",
        pandoc_bin_override: str | None = "AUTO",
    ) -> tuple[list[Any], int, int, int]:
        """Compile a single IRBlock into detached OpenXML elements (`<w:p>`, `<w:tbl>`).

        Returns `(elements, omml_count, tables_count, runs_sanitized)`.
        """
        elements: list[Any] = []
        omml_count = 0
        tables_count = 0
        runs_sanitized = 0

        if block.block_type == "heading":
            prefix = "#" * min(3, max(1, block.level))
            els, omml, san = cls._compile_markdown_fragment_to_elements(
                f"{prefix} {block.text}",
                layout=layout,
                degradation_mode=degradation_mode,
                pandoc_bin_override=pandoc_bin_override,
            )
            elements.extend(els)
            omml_count += omml
            runs_sanitized += san

        elif block.block_type == "paragraph":
            els, omml, san = cls._compile_markdown_fragment_to_elements(
                block.text,
                layout=layout,
                degradation_mode=degradation_mode,
                pandoc_bin_override=pandoc_bin_override,
            )
            elements.extend(els)
            omml_count += omml
            runs_sanitized += san

        elif block.block_type == "list":
            md_lines = [
                (f"{i}. {item}" if block.list_ordered else f"- {item}")
                for i, item in enumerate(block.list_items, start=1)
            ]
            els, omml, san = cls._compile_markdown_fragment_to_elements(
                "\n".join(md_lines),
                layout=layout,
                degradation_mode=degradation_mode,
                pandoc_bin_override=pandoc_bin_override,
            )
            elements.extend(els)
            omml_count += omml
            runs_sanitized += san

        elif block.block_type == "equation":
            tex = block.latex.strip().removeprefix("$$").removesuffix("$$").strip()
            els, omml, san = cls._compile_markdown_fragment_to_elements(
                f"$$\n{tex}\n$$",
                layout=layout,
                degradation_mode=degradation_mode,
                pandoc_bin_override=pandoc_bin_override,
            )
            elements.extend(els)
            omml_count += omml
            runs_sanitized += san
            if block.caption:
                scratch = docx.Document()
                cap_p = scratch.add_paragraph(f"公式：{block.caption}")
                runs_sanitized += enforce_run_font_uniformity(
                    cap_p._p, layout.latin_font, layout.cjk_font
                )
                elements.append(copy.deepcopy(cap_p._p))

        elif block.block_type == "table":
            scratch = docx.Document()
            if block.caption and layout.table_caption_style == "above_table":
                cap_p = scratch.add_paragraph(f"表：{block.caption}")
                runs_sanitized += enforce_run_font_uniformity(
                    cap_p._p, layout.latin_font, layout.heading_cjk_font
                )
                elements.append(copy.deepcopy(cap_p._p))

            cols = len(block.table_headers) or (len(block.table_rows[0]) if block.table_rows else 1)
            rows_count = (1 if block.table_headers else 0) + len(block.table_rows)
            if rows_count > 0 and cols > 0:
                tbl = scratch.add_table(rows=rows_count, cols=cols)
                row_offset = 0
                if block.table_headers:
                    for c_i, h_text in enumerate(block.table_headers[:cols]):
                        tbl.cell(0, c_i).text = str(h_text)
                    row_offset = 1
                for r_i, row_data in enumerate(block.table_rows):
                    for c_i, cell_val in enumerate(row_data[:cols]):
                        tbl.cell(r_i + row_offset, c_i).text = str(cell_val)
                runs_sanitized += enforce_run_font_uniformity(
                    tbl._tbl, layout.latin_font, layout.cjk_font
                )
                elements.append(copy.deepcopy(tbl._tbl))
                tables_count += 1

            if block.caption and layout.table_caption_style == "below_table":
                cap_p = scratch.add_paragraph(f"表：{block.caption}")
                runs_sanitized += enforce_run_font_uniformity(
                    cap_p._p, layout.latin_font, layout.heading_cjk_font
                )
                elements.append(copy.deepcopy(cap_p._p))

        elif block.block_type in {"figure_ref", "caption"}:
            scratch = docx.Document()
            p = scratch.add_paragraph(block.text or f"图：{block.caption}")
            runs_sanitized += enforce_run_font_uniformity(
                p._p, layout.latin_font, layout.cjk_font
            )
            elements.append(copy.deepcopy(p._p))

        elif block.block_type == "page_break":
            scratch = docx.Document()
            p = scratch.add_page_break()
            elements.append(copy.deepcopy(p._p))

        return elements, omml_count, tables_count, runs_sanitized

    @classmethod
    def render_ir_section_to_elements(
        cls,
        ir_section: IRSection,
        layout: LayoutProfile,
        include_section_heading: bool = True,
        degradation_mode: Literal["strict", "preview_plaintext"] = "strict",
        pandoc_bin_override: str | None = "AUTO",
    ) -> tuple[list[Any], int, int, int]:
        """Compile an entire IRSection into an ordered sequence of detached OpenXML elements."""
        sec_elements: list[Any] = []
        total_omml = 0
        total_tables = 0
        total_sanitized = 0

        if include_section_heading:
            scratch = docx.Document()
            h_p = scratch.add_heading(ir_section.title, level=1)
            total_sanitized += enforce_run_font_uniformity(
                h_p._p, layout.latin_font, layout.heading_cjk_font
            )
            sec_elements.append(copy.deepcopy(h_p._p))

        for blk in ir_section.blocks:
            els, omml, tbls, san = cls.render_ir_block_to_elements(
                blk,
                layout=layout,
                degradation_mode=degradation_mode,
                pandoc_bin_override=pandoc_bin_override,
            )
            sec_elements.extend(els)
            total_omml += omml
            total_tables += tbls
            total_sanitized += san

        return sec_elements, total_omml, total_tables, total_sanitized

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

        # OpenXML requires every <w:tc> to end with a <w:p>
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
        docx_path: Path,
        ir: DocumentIR,
        effective_mode: str,
        degradation_mode: Literal["strict", "preview_plaintext"],
        template_path: Path | None = None,
        template_before_sha: str | None = None,
        initial_tail_paragraph_texts: list[str] | None = None,
    ) -> dict[str, Any]:
        """Run unified OpenXML & structural QA checks on the compiled .docx before atomic promotion (DAF-P1-06 §6.5)."""
        if not docx_path.exists() or docx_path.stat().st_size == 0:
            raise TemplateRenderError("Rendered DOCX file is missing or empty.")

        # 1. Verify original template immutability (T-DOC-05)
        if template_path is not None and template_before_sha is not None:
            after_sha = _sha256_file(template_path)
            if after_sha != template_before_sha:
                raise TemplateRenderError(
                    "CRITICAL: Original template SHA-256 changed during rendering! Template immutability violated."
                )

        # 2. Re-open with python-docx
        doc = docx.Document(str(docx_path))
        body = doc._body._body

        # 3. Check for unrendered Jinja2 placeholders
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

        # 4. Check OMML equations in strict mode (T-DOC-01)
        m_oMath = qn("m:oMath")
        m_oMathPara = qn("m:oMathPara")
        omml_nodes = list(body.iter(m_oMath)) + list(body.iter(m_oMathPara))
        if degradation_mode == "strict" and ir.expected_equation_count > 0:
            if len(omml_nodes) == 0:
                raise TemplateRenderError(
                    f"Expected at least {ir.expected_equation_count} native OMML equations in strict mode, "
                    "but found 0 <m:oMath> nodes in compiled DOCX.",
                    code="RENDER_DEPENDENCY_MISSING",
                )
            if "$$" in combined_text:
                raise TemplateRenderError(
                    "Raw uncompiled '$$...$$' LaTeX delimiters leaked into strict DOCX output.",
                    code="RENDER_DEPENDENCY_MISSING",
                )

        # 5. Check that w:hint is completely stripped across all w:rFonts (T-DOC-05)
        w_rFonts = qn("w:rFonts")
        w_hint = qn("w:hint")
        for rfonts in body.iter(w_rFonts):
            if w_hint in rfonts.attrib:
                raise TemplateRenderError("OpenXML QA failed: w:hint attribute was not stripped from w:rFonts.")

        # 6. In mapped table mode, verify no section content was accidentally appended to the document tail (T-DOC-02)
        if effective_mode == "mapped" and initial_tail_paragraph_texts is not None:
            current_body_paras = [p.text.strip() for p in doc.paragraphs if p.text.strip()]
            if len(current_body_paras) > len(initial_tail_paragraph_texts):
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
        """Compile DocumentIR into a native .docx via atomic .tmp promotion and record RenderManifest (DAF-P0-04, DAF-P1-06 & R2-P1-05)."""
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
            else ("strict" if purpose == "release" else "strict")
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
        ir: DocumentIR = fp_info["ir"]
        layout = state.profile.layout_profile

        if not ir.sections:
            raise TemplateRenderError(
                "Cannot render document: no drafted sections with revision > 0 exist.",
                code="TEMPLATE_MAPPING_INVALID",
            )

        tpl_report = state.source_registry.template_inspection
        template_path: Path | None = None
        template_before_sha: str | None = None
        if effective_mode in {"tagged", "mapped"}:
            if tpl_report is None:
                raise TemplateRenderError(
                    f"template_mode='{effective_mode}' requires an inspected .docx template.",
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

        total_omml = 0
        total_tables = 0
        total_runs_sanitized = 0
        initial_tail_paras: list[str] | None = None

        try:
            if effective_mode == "builtin":
                if eff_degradation == "strict" and pandoc_bin_override != "":
                    from daf.document_engine.pandoc_renderer import render_document_single_pass_pandoc

                    p_override = None if pandoc_bin_override == "AUTO" else pandoc_bin_override
                    render_document_single_pass_pandoc(
                        state=state,
                        output_docx_path=tmp_output_path,
                        purpose=purpose,
                        project_dir=output_dir.parent,
                        pandoc_bin=p_override,
                    )
                    doc = docx.Document(str(tmp_output_path))
                    cls._apply_preview_watermark_and_metadata(doc, purpose, eff_degradation, layout)
                    total_runs_sanitized += enforce_run_font_uniformity(
                        doc._body._body, layout.latin_font, layout.cjk_font
                    )
                    total_omml = sum(1 for _ in doc._body._body.iter(qn("m:oMath")))
                    total_tables = len(doc.tables)
                    doc.save(str(tmp_output_path))
                else:
                    doc = docx.Document()
                    cls.configure_document_styles(doc, layout)
                    cls._apply_preview_watermark_and_metadata(doc, purpose, eff_degradation, layout)

                    if purpose == "preview":
                        banner_p = doc.add_paragraph(PREVIEW_BANNER_TEXT)
                        total_runs_sanitized += enforce_run_font_uniformity(
                            banner_p._p, layout.latin_font, layout.heading_cjk_font
                        )

                    title_h = doc.add_heading(state.brief.title, level=0)
                    total_runs_sanitized += enforce_run_font_uniformity(
                        title_h._p, layout.latin_font, layout.heading_cjk_font
                    )

                    for ir_sec in ir.sections:
                        sec_els, omml, tbls, san = cls.render_ir_section_to_elements(
                            ir_sec,
                            layout=layout,
                            include_section_heading=True,
                            degradation_mode=eff_degradation,
                            pandoc_bin_override=pandoc_bin_override,
                        )
                        cls._insert_elements_into_body(doc._body._body, sec_els)
                        total_omml += omml
                        total_tables += tbls
                        total_runs_sanitized += san

                    total_runs_sanitized += enforce_run_font_uniformity(
                        doc._body._body, layout.latin_font, layout.cjk_font
                    )
                    doc.save(str(tmp_output_path))

            elif effective_mode == "tagged":
                assert template_path is not None
                shutil.copy2(template_path, tmp_output_path)
                tagged_doc = docx.Document(str(tmp_output_path))
                cls.configure_document_styles(tagged_doc, layout)
                cls._apply_preview_watermark_and_metadata(tagged_doc, purpose, eff_degradation, layout)

                # Build section IR lookup by placeholder tag name
                sec_by_tag: dict[str, IRSection] = {}
                for ir_sec in ir.sections:
                    sec_by_tag[ir_sec.section_id] = ir_sec
                    sec_by_tag[f"section_{ir_sec.section_id}"] = ir_sec
                if section_mapping:
                    for sec_id, tag_name in section_mapping.items():
                        matched_sec = next((s for s in ir.sections if s.section_id == sec_id), None)
                        if matched_sec is not None:
                            sec_by_tag[tag_name] = matched_sec

                scalar_vars = {
                    "title": state.brief.title,
                    "project_id": state.brief.project_id,
                    "domain": state.brief.domain,
                    "global_thesis": state.outline_graph.global_thesis,
                }

                # Helper to process paragraphs in body or inside table cells
                def _process_tagged_paragraphs(paragraphs: list[Any], in_table_cell: bool = False) -> None:
                    nonlocal total_omml, total_tables, total_runs_sanitized
                    for para in list(paragraphs):
                        raw_txt = para.text
                        tags = JINJA_TAG_PATTERN.findall(raw_txt)
                        if not tags:
                            continue

                        # Check if any tag is a section IR placeholder
                        sec_tags = [t for t in tags if t in sec_by_tag]
                        if sec_tags:
                            if len(tags) > 1 or re.sub(JINJA_TAG_PATTERN, "", raw_txt).strip():
                                raise TemplateRenderError(
                                    f"Section placeholder '{{{{ {sec_tags[0]} }}}}' cannot be embedded inline with other text in the same paragraph. "
                                    "Place section tags in their own dedicated paragraph or table cell.",
                                    code="TEMPLATE_MAPPING_INVALID",
                                )
                            target_ir_sec = sec_by_tag[sec_tags[0]]
                            sec_els, omml, tbls, san = cls.render_ir_section_to_elements(
                                target_ir_sec,
                                layout=layout,
                                include_section_heading=not in_table_cell,
                                degradation_mode=eff_degradation,
                                pandoc_bin_override=pandoc_bin_override,
                            )
                            parent_el = para._p.getparent()
                            for el in sec_els:
                                para._p.addprevious(el)
                            parent_el.remove(para._p)
                            # Ensure table cell never becomes completely empty of <w:p>
                            if in_table_cell and not any(c.tag == qn("w:p") for c in parent_el):
                                parent_el.append(OxmlElement("w:p"))
                            total_omml += omml
                            total_tables += tbls
                            total_runs_sanitized += san
                        else:
                            # Scalar replacement
                            new_txt = raw_txt
                            for t in tags:
                                if t not in scalar_vars:
                                    raise TemplateRenderError(
                                        f"Unmapped Jinja2 placeholder '{{{{ {t} }}}}' in tagged template.",
                                        code="TEMPLATE_MAPPING_INVALID",
                                    )
                                new_txt = re.sub(
                                    r"\{\{\s*" + re.escape(t) + r"\s*\}\}",
                                    str(scalar_vars[t]),
                                    new_txt,
                                )
                            para.text = new_txt
                            total_runs_sanitized += enforce_run_font_uniformity(
                                para._p, layout.latin_font, layout.cjk_font
                            )

                _process_tagged_paragraphs(tagged_doc.paragraphs, in_table_cell=False)
                for tbl in tagged_doc.tables:
                    for row in tbl.rows:
                        for cell in row.cells:
                            _process_tagged_paragraphs(cell.paragraphs, in_table_cell=True)

                total_runs_sanitized += enforce_run_font_uniformity(
                    tagged_doc._body._body, layout.latin_font, layout.cjk_font
                )
                tagged_doc.save(str(tmp_output_path))

            elif effective_mode == "mapped":
                assert template_path is not None
                if not section_mapping:
                    raise TemplateRenderError(
                        "template_mode='mapped' requires explicit section_mapping (e.g. {'sec_id': 'table:0:r1:c1'}); "
                        "silent guessing of template locations is forbidden.",
                        code="TEMPLATE_MAPPING_INVALID",
                    )

                # Validate no duplicate mapping targets
                used_targets: set[str] = set()
                for sec_id, target_coord in section_mapping.items():
                    if target_coord in used_targets:
                        raise TemplateRenderError(
                            f"Duplicate mapping target '{target_coord}' in section_mapping.",
                            code="TEMPLATE_MAPPING_INVALID",
                        )
                    used_targets.add(target_coord)

                shutil.copy2(template_path, tmp_output_path)
                mapped_doc = docx.Document(str(tmp_output_path))
                cls.configure_document_styles(mapped_doc, layout)
                cls._apply_preview_watermark_and_metadata(mapped_doc, purpose, eff_degradation, layout)

                all_table_Targets = all(
                    section_mapping.get(s.section_id, "").startswith("table:") for s in ir.sections
                )
                if all_table_Targets:
                    initial_tail_paras = [p.text.strip() for p in mapped_doc.paragraphs if p.text.strip()]

                for ir_sec in ir.sections:
                    target_loc = section_mapping.get(ir_sec.section_id)
                    if not target_loc:
                        raise TemplateRenderError(
                            f"Missing explicit mapping in section_mapping for drafted section '{ir_sec.section_id}'.",
                            code="TEMPLATE_MAPPING_INVALID",
                        )

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

                        sec_els, omml, tbls, san = cls.render_ir_section_to_elements(
                            ir_sec,
                            layout=layout,
                            include_section_heading=False,
                            degradation_mode=eff_degradation,
                            pandoc_bin_override=pandoc_bin_override,
                        )
                        cls._insert_elements_into_table_cell(cell._tc, sec_els, layout)
                        total_omml += omml
                        total_tables += tbls
                        total_runs_sanitized += san

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
                        sec_els, omml, tbls, san = cls.render_ir_section_to_elements(
                            ir_sec,
                            layout=layout,
                            include_section_heading=False,
                            degradation_mode=eff_degradation,
                            pandoc_bin_override=pandoc_bin_override,
                        )
                        anchor = matched_para._p
                        for el in sec_els:
                            anchor.addnext(el)
                            anchor = el
                        total_omml += omml
                        total_tables += tbls
                        total_runs_sanitized += san
                    else:
                        raise TemplateRenderError(
                            f"Unsupported mapping target '{target_loc}'. Use 'table:<t>:r<r>:c<c>' or 'heading:<text>'.",
                            code="TEMPLATE_MAPPING_INVALID",
                        )

                total_runs_sanitized += enforce_run_font_uniformity(
                    mapped_doc._body._body, layout.latin_font, layout.cjk_font
                )
                mapped_doc.save(str(tmp_output_path))
            else:
                raise TemplateRenderError(f"Unsupported template_mode '{effective_mode}'.")

            # Run unified OpenXML & structural QA before promoting from .tmp/
            _canonicalize_docx_zip(tmp_output_path)
            qa_report = cls.verify_rendered_docx_qa(
                docx_path=tmp_output_path,
                ir=ir,
                effective_mode=effective_mode,
                degradation_mode=eff_degradation,
                template_path=template_path,
                template_before_sha=template_before_sha,
                initial_tail_paragraph_texts=initial_tail_paras,
            )

            # Atomically move verified .tmp file to final output path
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
            docx_path=str(final_output_path),
            artifact_path=str(final_output_path),
            docx_sha256=f"sha256:{output_sha}",
            output_sha256=f"sha256:{output_sha}",
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
            state.last_rendered_docx_path = str(final_output_path)
            if purpose == "release":
                state.stage = "rendered"

        return {
            "project_id": state.brief.project_id,
            "render_id": render_id,
            "job_id": job_id,
            "purpose": purpose,
            "degradation_mode": eff_degradation,
            "template_mode": effective_mode,
            "docx_path": str(final_output_path),
            "docx_sha256": output_sha,
            "input_fingerprint": fp_info["input_fingerprint"],
            "sections_rendered": [s.section_id for s in ir.sections],
            "omml_equations_count": qa_report["omml_node_count"],
            "tables_rendered": total_tables,
            "runs_font_sanitized": total_runs_sanitized,
            "template_immutability_verified": template_before_sha is not None,
            "render_manifest": manifest.model_dump(mode="json"),
        }

    @staticmethod
    def export_pdf_via_word_com(docx_path: str | Path, pdf_path: str | Path, timeout_sec: int = 60) -> dict[str, Any]:
        """Export a .docx file to .pdf on Windows using an isolated child process running Word COM."""
        src = Path(docx_path).resolve()
        dst = Path(pdf_path).resolve()
        if not src.exists():
            raise PDFExportError(f"Cannot export PDF: source DOCX does not exist at '{src}'.")
        if sys.platform != "win32":
            raise PDFExportError(
                f"Word COM PDF export is only supported on Windows (current platform: {sys.platform})."
            )

        dst.parent.mkdir(parents=True, exist_ok=True)
        # Remove any pre-existing target PDF first so an old PDF can NEVER be mistaken for a new export on failure (T-REN-05)
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
