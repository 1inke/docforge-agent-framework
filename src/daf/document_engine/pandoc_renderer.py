"""Single-pass Pandoc DOCX renderer with source-level CSL bibliography, two-pass Lua crossrefs, and OMML."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile
from typing import Any

import docx
from docx.shared import Inches, Pt
import yaml

from daf.core.contracts import LayoutProfile, ProjectWorkspaceState
from daf.core.errors import PandocUnavailableError, TemplateRenderError
from daf.core.pandoc_ast import find_pandoc_binary
from daf.workflow_engine.state_machine import _has_valid_bibliography, effective_section_ids

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


def collect_csl_bibliography_and_map(
    state: ProjectWorkspaceState,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, str]]]:
    """Build source-keyed CSL entries and evidence_id -> {source, locator} mapping.

    Only sources actually cited by drafted effective sections are included in the bibliography.
    Missing bibliography entries in preview mode emit a visible placeholder title and NEVER
    fabricate authors or years.
    """
    evidence_map: dict[str, dict[str, str]] = {}
    cited_source_ids: list[str] = []
    seen_sources: set[str] = set()

    for sid in effective_section_ids(state):
        sec = state.section_versions.sections.get(sid)
        if sec is None or sec.revision == 0:
            continue
        for b in sec.source_bindings:
            if not b.evidence_id:
                continue
            ev = state.evidence_ledger.items.get(b.evidence_id)
            if ev is None:
                continue
            evidence_map[ev.evidence_id] = {
                "source": ev.source_id,
                "locator": ev.locator or "",
            }
            if ev.source_id not in seen_sources:
                seen_sources.add(ev.source_id)
                cited_source_ids.append(ev.source_id)

    # Also include any registered evidence mapping so crossref.lua can resolve citations in preview
    for ev_id, ev in state.evidence_ledger.items.items():
        if ev_id not in evidence_map:
            evidence_map[ev_id] = {
                "source": ev.source_id,
                "locator": ev.locator or "",
            }

    entries: list[dict[str, Any]] = []
    for src_id in cited_source_ids:
        doc_rec = state.source_registry.documents.get(src_id)
        if doc_rec and _has_valid_bibliography(doc_rec.bibliography):
            entry = dict(doc_rec.bibliography)
            entry["id"] = src_id
            entries.append(entry)
        else:
            fname = doc_rec.file_name if doc_rec else src_id
            entries.append(
                {
                    "id": src_id,
                    "type": "article-journal",
                    "title": f"【未登记文献信息：{fname}】",
                }
            )
    return entries, evidence_map


def render_document_single_pass_pandoc(
    state: ProjectWorkspaceState,
    output_docx_path: Path,
    *,
    purpose: str = "release",
    project_dir: Path | None = None,
    csl_path: Path | None = None,
    pandoc_bin: str | None = None,
) -> dict[str, Any]:
    """Assemble full Markdown from effective sections and compile once via Pandoc to DOCX."""
    binary = pandoc_bin or find_pandoc_binary()
    if not binary:
        raise PandocUnavailableError("Pandoc executable is required for single-pass DOCX rendering.")

    ordered_ids = effective_section_ids(state) or list(state.section_versions.sections.keys())

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

    csl_entries, evidence_map = collect_csl_bibliography_and_map(state)
    has_bib = bool(csl_entries)
    if has_bib:
        md_parts.append("\n## 参考文献\n")

    full_markdown = "\n".join(md_parts)

    with tempfile.TemporaryDirectory(prefix="daf_pandoc_") as tmpdir:
        tmp = Path(tmpdir)
        ref_docx = build_reference_docx(state.profile.layout_profile, tmp / "reference.docx")
        meta_yaml = tmp / "metadata.yaml"
        meta_yaml.write_text(
            yaml.safe_dump({"daf-evidence-map": evidence_map}, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )

        cmd = [
            binary,
            "-f",
            "markdown",
            "-t",
            "docx",
            f"--reference-doc={ref_docx}",
            f"--metadata-file={meta_yaml}",
            "-o",
            str(output_docx_path),
        ]
        if project_dir is not None:
            cmd.append(f"--resource-path={project_dir}")

        if DEFAULT_CROSSREF_LUA.is_file():
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
