"""Shared test helpers and DOCX output assertions for DAF regression tests."""

from __future__ import annotations

from pathlib import Path
import re

import docx


def extract_docx_text(docx_path: Path) -> str:
    doc = docx.Document(str(docx_path))
    parts = [p.text for p in doc.paragraphs]
    for tbl in doc.tables:
        for row in tbl.rows:
            for cell in row.cells:
                parts.append(cell.text)
    return "\n".join(parts)


def assert_clean_rendered_docx(
    docx_path: Path,
    *,
    expected_bib_count: int | None = None,
    allow_preview_placeholder: bool = False,
) -> str:
    """Assert rendered DOCX has no unresolved markup, leaked IDs, or fabricated citations."""
    assert docx_path.exists(), f"Rendered DOCX missing: {docx_path}"
    full_text = extract_docx_text(docx_path)
    forbidden_tokens = [
        "?]",
        "{#",
        "Source Document",
        "@ev_",
        "[@hypothesis]",
        "__DAF_SEC__",
        "__DAF_BIB__",
    ]
    for tok in forbidden_tokens:
        assert tok not in full_text, f"Forbidden token {tok!r} leaked into {docx_path}:\n{full_text}"

    if not allow_preview_placeholder:
        assert "【未登记文献信息" not in full_text, (
            f"Unconfirmed preview bibliography placeholder leaked into release DOCX:\n{full_text}"
        )

    if expected_bib_count is not None:
        doc = docx.Document(str(docx_path))
        paras = [p.text.strip() for p in doc.paragraphs if p.text.strip()]
        in_refs = False
        bib_entries: list[str] = []
        for p_txt in paras:
            if p_txt == "参考文献":
                in_refs = True
                continue
            if in_refs and re.match(r"^\[\d+\]\s*", p_txt):
                bib_entries.append(p_txt)
        if not in_refs:
            # Fallback if references heading was inside a table or custom target
            bib_entries = [line.strip() for line in full_text.splitlines() if re.match(r"^\[\d+\]\s+.+", line.strip())]
        assert len(bib_entries) == expected_bib_count, (
            f"Expected {expected_bib_count} bibliography entries, got {len(bib_entries)}: {bib_entries}\n"
            f"Full text:\n{full_text}"
        )

    return full_text
