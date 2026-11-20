"""Unified Document Intermediate Representation (DocumentIR) Parser & Normalizer (DAF-P1-06 §6.2)."""

from __future__ import annotations

import re
from typing import Literal

from daf.core.contracts import (
    DocumentIR,
    IRBlock,
    IRSection,
    ProjectWorkspaceState,
    SectionState,
)

INLINE_MATH_PATTERN = re.compile(r"(?<!\\)\$[^\$\n]+?(?<!\\)\$")
DISPLAY_MATH_BLOCK_PATTERN = re.compile(r"^\$\$\s*([\s\S]+?)\s*\$\$$")
HEADING_LINE_PATTERN = re.compile(r"^(#{1,6})\s+(.+)$")
UNORDERED_LIST_LINE_PATTERN = re.compile(r"^\s*[-*+]\s+(.+)$")
ORDERED_LIST_LINE_PATTERN = re.compile(r"^\s*\d+[.)]\s+(.+)$")


def _split_markdown_chunks(markdown_text: str) -> list[str]:
    """Split markdown into logical blocks while keeping multi-line $$...$$ display math and tables intact."""
    lines = markdown_text.replace("\r\n", "\n").splitlines()
    chunks: list[str] = []
    buf: list[str] = []
    in_display_math = False

    def flush() -> None:
        nonlocal buf
        joined = "\n".join(buf).strip()
        buf = []
        if joined:
            chunks.append(joined)

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("$$"):
            if in_display_math:
                buf.append(line)
                in_display_math = False
                flush()
                continue
            elif stripped.endswith("$$") and len(stripped) > 4:
                flush()
                chunks.append(stripped)
                continue
            else:
                flush()
                in_display_math = True
                buf.append(line)
                continue

        if in_display_math:
            buf.append(line)
            continue

        if not stripped:
            flush()
        else:
            buf.append(line)

    if buf:
        flush()
    return chunks


def parse_section_to_ir(sec: SectionState) -> tuple[IRSection, int, int]:
    """Parse a SectionState (content_markdown + structured_blocks) into a deduplicated IRSection.

    Returns `(ir_section, equation_count, table_count)`.
    """
    blocks: list[IRBlock] = []
    eq_count = 0
    tbl_count = 0
    seq = 0
    seen_equation_latex: set[str] = set()
    seen_table_signatures: set[str] = set()
    ref_ids = [b.evidence_id for b in sec.source_bindings if b.evidence_id]

    chunks = _split_markdown_chunks(sec.content_markdown)
    for chunk in chunks:
        seq += 1
        block_id = f"ir_{sec.section_id}_{seq:03d}"

        # 1. Display equation block $$...$$
        m_disp = DISPLAY_MATH_BLOCK_PATTERN.match(chunk)
        if m_disp:
            latex_body = m_disp.group(1).strip()
            norm_latex = re.sub(r"\s+", " ", latex_body)
            seen_equation_latex.add(norm_latex)
            eq_count += 1
            blocks.append(
                IRBlock(
                    block_id=block_id,
                    section_id=sec.section_id,
                    block_type="equation",
                    style_role="display_equation",
                    latex=latex_body,
                    source_refs=ref_ids,
                )
            )
            continue

        # 2. Markdown Pipe Table
        lines = [ln.strip() for ln in chunk.splitlines() if ln.strip()]
        if len(lines) >= 2 and all("|" in ln for ln in lines) and re.match(r"^\|?[\s:\-|]+\|?$", lines[1]):
            headers = [c.strip() for c in lines[0].strip("|").split("|")]
            rows: list[list[str]] = []
            for r_line in lines[2:]:
                rows.append([c.strip() for c in r_line.strip("|").split("|")])
            sig = "|".join(headers) + f":{len(rows)}"
            seen_table_signatures.add(sig)
            tbl_count += 1
            blocks.append(
                IRBlock(
                    block_id=block_id,
                    section_id=sec.section_id,
                    block_type="table",
                    style_role="data_table",
                    table_headers=headers,
                    table_rows=rows,
                    source_refs=ref_ids,
                )
            )
            continue

        # 3. Heading
        if len(lines) == 1:
            m_head = HEADING_LINE_PATTERN.match(lines[0])
            if m_head:
                lvl = min(3, max(1, len(m_head.group(1))))
                htext = m_head.group(2).strip()
                eq_count += len(INLINE_MATH_PATTERN.findall(htext))
                blocks.append(
                    IRBlock(
                        block_id=block_id,
                        section_id=sec.section_id,
                        block_type="heading",
                        style_role=f"heading_{lvl}",
                        text=htext,
                        level=lvl,
                        source_refs=ref_ids,
                    )
                )
                continue

        # 4. Unordered or Ordered List
        if all(UNORDERED_LIST_LINE_PATTERN.match(ln) for ln in lines):
            items = [UNORDERED_LIST_LINE_PATTERN.match(ln).group(1).strip() for ln in lines]  # type: ignore[union-attr]
            for it in items:
                eq_count += len(INLINE_MATH_PATTERN.findall(it))
            blocks.append(
                IRBlock(
                    block_id=block_id,
                    section_id=sec.section_id,
                    block_type="list",
                    style_role="bullet_list",
                    list_items=items,
                    list_ordered=False,
                    source_refs=ref_ids,
                )
            )
            continue

        if all(ORDERED_LIST_LINE_PATTERN.match(ln) for ln in lines):
            items = [ORDERED_LIST_LINE_PATTERN.match(ln).group(1).strip() for ln in lines]  # type: ignore[union-attr]
            for it in items:
                eq_count += len(INLINE_MATH_PATTERN.findall(it))
            blocks.append(
                IRBlock(
                    block_id=block_id,
                    section_id=sec.section_id,
                    block_type="list",
                    style_role="ordered_list",
                    list_items=items,
                    list_ordered=True,
                    source_refs=ref_ids,
                )
            )
            continue

        # 5. Standard Paragraph (may contain inline math $...$)
        para_text = "\n".join(lines)
        eq_count += len(INLINE_MATH_PATTERN.findall(para_text))
        blocks.append(
            IRBlock(
                block_id=block_id,
                section_id=sec.section_id,
                block_type="paragraph",
                style_role="body",
                text=para_text,
                source_refs=ref_ids,
            )
        )

    # Append structured_blocks without duplicating equations or tables already in the Markdown
    for sblk in sec.structured_blocks:
        seq += 1
        bid = sblk.block_id or f"ir_{sec.section_id}_sb_{seq:03d}"
        if sblk.block_type == "equation" and sblk.latex:
            clean_latex = sblk.latex.strip().removeprefix("$$").removesuffix("$$").strip()
            norm_latex = re.sub(r"\s+", " ", clean_latex)
            if norm_latex in seen_equation_latex:
                continue
            seen_equation_latex.add(norm_latex)
            eq_count += 1
            blocks.append(
                IRBlock(
                    block_id=bid,
                    section_id=sec.section_id,
                    block_type="equation",
                    style_role="display_equation",
                    latex=clean_latex,
                    caption=sblk.caption,
                    source_refs=ref_ids,
                )
            )
        elif sblk.block_type == "table":
            sig = "|".join(sblk.table_headers) + f":{len(sblk.table_rows)}"
            if sig in seen_table_signatures:
                continue
            seen_table_signatures.add(sig)
            tbl_count += 1
            blocks.append(
                IRBlock(
                    block_id=bid,
                    section_id=sec.section_id,
                    block_type="table",
                    style_role="data_table",
                    table_headers=list(sblk.table_headers),
                    table_rows=[list(r) for r in sblk.table_rows],
                    caption=sblk.caption,
                    source_refs=ref_ids,
                )
            )
        elif sblk.block_type == "figure_placeholder":
            blocks.append(
                IRBlock(
                    block_id=bid,
                    section_id=sec.section_id,
                    block_type="figure_ref",
                    style_role="figure_caption",
                    caption=sblk.caption or sblk.block_id,
                    text=f"[图占位符: {sblk.caption or sblk.block_id}]",
                    source_refs=ref_ids,
                )
            )

    ir_sec = IRSection(
        section_id=sec.section_id,
        title=sec.title,
        revision=sec.revision,
        content_hash=sec.content_hash,
        blocks=blocks,
    )
    return ir_sec, eq_count, tbl_count


def build_document_ir(
    state: ProjectWorkspaceState,
    purpose: Literal["preview", "release"] = "release",
) -> DocumentIR:
    """Build a complete, ordered DocumentIR from ProjectWorkspaceState."""
    ordered_sections: list[SectionState] = []
    if state.outline_graph.nodes:
        for node in state.outline_graph.nodes:
            sec = state.section_versions.sections.get(node.section_id)
            if sec and sec.revision > 0 and sec.content_markdown.strip():
                ordered_sections.append(sec)
    else:
        for sec in state.section_versions.sections.values():
            if sec and sec.revision > 0 and sec.content_markdown.strip():
                ordered_sections.append(sec)

    ir_sections: list[IRSection] = []
    total_eq = 0
    total_tbl = 0
    for sec in ordered_sections:
        ir_sec, eq_c, tbl_c = parse_section_to_ir(sec)
        ir_sections.append(ir_sec)
        total_eq += eq_c
        total_tbl += tbl_c

    return DocumentIR(
        project_id=state.brief.project_id,
        title=state.brief.title,
        purpose=purpose,
        sections=ir_sections,
        expected_equation_count=total_eq,
        expected_table_count=total_tbl,
    )
