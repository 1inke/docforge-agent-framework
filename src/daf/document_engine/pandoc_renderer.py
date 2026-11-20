"""Single-pass Pandoc DOCX renderer with CSL bibliography, Lua cross-references, and OMML equations (P1-3)."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import tempfile
from typing import Any

import docx
from docx.shared import Inches, Pt

from daf.core.contracts import LayoutProfile, ProjectWorkspaceState
from daf.core.errors import PandocUnavailableError, TemplateRenderError
from daf.core.pandoc_ast import find_pandoc_binary

ASSETS_DIR = Path(__file__).resolve().parent / "assets"
DEFAULT_CSL_PATH = ASSETS_DIR / "gb-t-7714-2015-numeric.csl"
DEFAULT_CROSSREF_LUA = ASSETS_DIR / "crossref.lua"


def build_reference_docx(layout: LayoutProfile, out_path: Path) -> Path:
    """Generate a Pandoc --reference-doc styled according to LayoutProfile."""
    doc = docx.Document()
    for sec in doc.sections:
        sec.top_margin = Inches(1.0)
        sec.bottom_margin = Inches(1.0)
        sec.left_margin = Inches(1.25)
        sec.right_margin = Inches(1.25)
    normal = doc.styles["Normal"]
    normal.font.name = layout.latin_font
    normal.font.size = Pt(layout.body_font_size_pt)
    doc.save(str(out_path))
    return out_path


def collect_csl_bibliography(state: ProjectWorkspaceState) -> list[dict[str, Any]]:
    """Map every cited EvidenceItem ID to a CSL-JSON entry derived from its SourceDocumentRecord."""
    entries: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for ev_id, ev in state.evidence_ledger.items.items():
        if ev_id in seen_ids:
            continue
        seen_ids.add(ev_id)
        doc_rec = state.source_registry.documents.get(ev.source_id)
        if doc_rec and doc_rec.bibliography:
            entry = dict(doc_rec.bibliography)
            entry["id"] = ev_id
            entries.append(entry)
        else:
            title = doc_rec.file_name if doc_rec else ev.source_id
            entries.append(
                {
                    "id": ev_id,
                    "type": "article-journal",
                    "title": title,
                    "author": [{"literal": "Source Document"}],
                    "issued": {"date-parts": [[2026]]},
                }
            )
    return entries


def render_document_single_pass_pandoc(
    state: ProjectWorkspaceState,
    output_docx_path: Path,
    *,
    purpose: str = "release",
    project_dir: Path | None = None,
    csl_path: Path | None = None,
    pandoc_bin: str | None = None,
) -> dict[str, Any]:
    """Assemble full Markdown from outline-ordered sections and compile once via Pandoc to DOCX."""
    binary = pandoc_bin or find_pandoc_binary()
    if not binary:
        raise PandocUnavailableError("Pandoc executable is required for single-pass DOCX rendering.")

    ordered_ids = (
        [n.section_id for n in state.outline_graph.nodes]
        if state.outline_graph.nodes
        else list(state.section_versions.sections.keys())
    )

    md_parts: list[str] = [f"# {state.brief.title}\n"]
    if purpose == "preview":
        md_parts.append("> **[PREVIEW DRAFT - UNAPPROVED / 内部预览草稿 - 未经人工最终批准]**\n")

    section_count = 0
    for sid in ordered_ids:
        sec = state.section_versions.sections.get(sid)
        if sec is None or sec.revision == 0:
            continue
        section_count += 1
        md_parts.append(f"\n## {sec.title}\n\n{sec.content_markdown.strip()}\n")

    full_markdown = "\n".join(md_parts)
    csl_entries = collect_csl_bibliography(state)
    has_bib = bool(csl_entries)

    with tempfile.TemporaryDirectory(prefix="daf_pandoc_") as tmpdir:
        tmp = Path(tmpdir)
        ref_docx = build_reference_docx(state.profile.layout_profile, tmp / "reference.docx")
        cmd = [
            binary,
            "-f",
            "markdown",
            "-t",
            "docx",
            f"--reference-doc={ref_docx}",
            "-o",
            str(output_docx_path),
        ]
        if project_dir is not None:
            cmd.append(f"--resource-path={project_dir}")

        crossref_bin = shutil.which("pandoc-crossref")
        if crossref_bin:
            cmd.extend(["--filter", crossref_bin])
        elif DEFAULT_CROSSREF_LUA.is_file():
            cmd.extend(["--lua-filter", str(DEFAULT_CROSSREF_LUA)])

        if has_bib:
            refs_json = tmp / "refs.json"
            refs_json.write_text(json.dumps(csl_entries, ensure_ascii=False, indent=2), encoding="utf-8")
            eff_csl = csl_path or DEFAULT_CSL_PATH
            cmd.extend(["--citeproc", f"--bibliography={refs_json}"])
            if eff_csl.is_file():
                cmd.append(f"--csl={eff_csl}")

        proc = subprocess.run(
            cmd,
            input=full_markdown.encode("utf-8"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=60,
        )
        if proc.returncode != 0:
            err = proc.stderr.decode("utf-8", errors="replace").strip()
            raise TemplateRenderError(f"Single-pass Pandoc DOCX rendering failed: {err}")

    return {
        "pandoc_invocations": 1,
        "rendered_sections": section_count,
        "bibliography_entries": len(csl_entries),
        "used_citeproc": has_bib,
        "output_path": str(output_docx_path),
    }
