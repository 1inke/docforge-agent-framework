"""Streamlined MCP Server Interface (16 core tools, singleton engine, unified DAFError handling)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

try:
    from mcp.server.mcpserver import MCPServer as FastMCP
except ImportError:
    from mcp.server.fastmcp import FastMCP  # type: ignore[no-redef]

from daf.core.engine import DAFEngine
from daf.core.errors import DAFError


def create_mcp_server(
    workspace_root: str | Path | None = None,
    allowed_source_roots: list[str | Path] | None = None,
) -> FastMCP:
    """Create and configure a FastMCP server exposing the streamlined DAF Agent toolset."""
    mcp = FastMCP("docforge-agent-framework")
    root = workspace_root or os.environ.get("DAF_WORKSPACE_ROOT")
    engine = DAFEngine(workspace_root=root, allowed_source_roots=allowed_source_roots)

    def _call(tool_name: str, *, attach_next_action_for: str | None = None, **params: Any) -> dict[str, Any]:
        try:
            res = engine.call_agent_tool(tool_name, params)
            if attach_next_action_for and isinstance(res, dict) and "next_action" not in res:
                try:
                    res["next_action"] = engine.get_project_status(attach_next_action_for).get("next_action")
                except Exception:
                    pass
            return res
        except DAFError as exc:
            return exc.to_dict()
        except Exception as exc:
            return {
                "ok": False,
                "error": {
                    "code": type(exc).__name__,
                    "message": str(exc),
                    "retryable": False,
                    "recommended_tool": None,
                    "details": {},
                },
            }

    # --- Discovery (2) ---
    @mcp.tool()
    def get_capabilities(
        profile_id: str | None = None,
        include_schemas: list[str] | None = None,
    ) -> dict[str, Any]:
        """Discover DAF protocol version, available profiles, compact tool list, and optional JSON schemas."""
        return _call("get_capabilities", profile_id=profile_id, include_schemas=include_schemas)

    @mcp.tool()
    def get_task_guidance(
        project_id: str,
        task_kind: str,
        section_id: str | None = None,
        max_guidance_chars: int = 8000,
    ) -> dict[str, Any]:
        """Read deterministic, state-aware guidance and next_action for the current task."""
        return _call(
            "get_task_guidance",
            project_id=project_id,
            task_kind=task_kind,
            section_id=section_id,
            max_guidance_chars=max_guidance_chars,
        )

    # --- Project (2) ---
    @mcp.tool()
    def create_project(
        project_id: str,
        title: str,
        profile_id: str = "default",
        domain: str = "general",
        research_direction: str = "",
        target_audience: str = "academic_reviewers",
        language: str = "zh-CN",
        global_constraints: list[str] | None = None,
    ) -> dict[str, Any]:
        """Initialize a project workspace from a user-defined YAML profile (or neutral default)."""
        return _call(
            "create_project",
            attach_next_action_for=project_id,
            project_id=project_id,
            title=title,
            profile_id=profile_id,
            domain=domain,
            research_direction=research_direction,
            target_audience=target_audience,
            language=language,
            global_constraints=global_constraints,
        )

    @mcp.tool()
    def get_project_status(project_id: str) -> dict[str, Any]:
        """Read-only inspection of project state, sections, approvals, active render jobs, and next_action."""
        return _call("get_project_status", project_id=project_id)

    # --- Sources & Evidence (4) ---
    @mcp.tool()
    def ingest_sources(
        project_id: str,
        source_paths: list[str] | None = None,
        template_path: str | None = None,
        is_agent_generated_summary: bool = False,
    ) -> dict[str, Any]:
        """Ingest local source files (.md/.txt/.docx/.pdf) and optional .docx template within allowed roots."""
        return _call(
            "ingest_sources",
            attach_next_action_for=project_id,
            project_id=project_id,
            source_paths=source_paths,
            template_path=template_path,
            is_agent_generated_summary=is_agent_generated_summary,
        )

    @mcp.tool()
    def find_sources(
        project_id: str,
        query: str | None = None,
        source_id: str | None = None,
        source_ids: list[str] | None = None,
        page_size: int = 20,
        cursor: str | None = None,
        match_mode: str = "literal",
    ) -> dict[str, Any]:
        """List source block metadata (when query is omitted) or search slices literally (when query is set)."""
        return _call(
            "find_sources",
            project_id=project_id,
            query=query,
            source_id=source_id,
            source_ids=source_ids,
            page_size=page_size,
            cursor=cursor,
            match_mode=match_mode,
        )

    @mcp.tool()
    def read_source_block(
        project_id: str,
        slice_id: str,
        start_char: int = 0,
        max_chars: int = 6000,
        expected_sha256: str | None = None,
    ) -> dict[str, Any]:
        """Read a character window [start_char, end_char) of raw text from an ingested source slice."""
        return _call(
            "read_source_block",
            project_id=project_id,
            slice_id=slice_id,
            start_char=start_char,
            max_chars=max_chars,
            expected_sha256=expected_sha256,
        )

    @mcp.tool()
    def register_evidence(
        project_id: str,
        evidence_items: list[dict[str, Any]],
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Register verbatim quote_excerpt evidence items (L1 substring verified; L3 defaults to unassessed)."""
        return _call(
            "register_evidence",
            attach_next_action_for=project_id,
            project_id=project_id,
            evidence_items=evidence_items,
            idempotency_key=idempotency_key,
        )

    @mcp.tool()
    def propose_source_bibliography(
        project_id: str,
        source_id: str,
        csl_json: dict[str, Any],
    ) -> dict[str, Any]:
        """Propose CSL-JSON bibliographic metadata for an ingested source (pending local user confirmation)."""
        return _call(
            "propose_source_bibliography",
            project_id=project_id,
            source_id=source_id,
            csl_json=csl_json,
        )

    # --- Writing (4) ---
    @mcp.tool()
    def submit_outline(
        project_id: str,
        global_thesis: str,
        nodes: list[dict[str, Any]],
        terminology: dict[str, str] | None = None,
        symbol_table: dict[str, str] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Validate and commit the structured OutlineGraph and terminology registry."""
        return _call(
            "submit_outline",
            attach_next_action_for=project_id,
            project_id=project_id,
            global_thesis=global_thesis,
            nodes=nodes,
            terminology=terminology,
            symbol_table=symbol_table,
            idempotency_key=idempotency_key,
        )

    @mcp.tool()
    def get_section_context(
        project_id: str,
        section_id: str,
        action: Literal["draft", "revise", "review"] = "draft",
        token_budget: int | None = None,
        max_context_chars: int | None = None,
        include_content_window: bool = False,
        start_char: int = 0,
        max_chars: int = 8000,
    ) -> dict[str, Any]:
        """Assemble character-budgeted section context and optionally include a windowed read of current markdown."""
        res = _call(
            "get_section_context",
            project_id=project_id,
            section_id=section_id,
            action=action,
            token_budget=token_budget,
            max_context_chars=max_context_chars,
        )
        if include_content_window and isinstance(res, dict) and "error" not in res:
            res["content_window"] = _call(
                "get_section_content",
                project_id=project_id,
                section_id=section_id,
                start_char=start_char,
                max_chars=max_chars,
            )
        return res

    @mcp.tool()
    def submit_section(
        project_id: str,
        section_id: str,
        base_revision: int,
        context_revision: str,
        idempotency_key: str,
        content_markdown: str,
        summary: str,
        context_fingerprint: str = "",
        draft_mode: Literal["formal", "draft_preapproval"] = "formal",
        author_session: str = "",
    ) -> dict[str, Any]:
        """Commit section markdown; inline Pandoc citations [@ev_id]/[@hypothesis] are parsed and L2a-checked automatically."""
        payload = {
            "project_id": project_id,
            "section_id": section_id,
            "base_revision": base_revision,
            "context_revision": context_revision,
            "context_fingerprint": context_fingerprint,
            "draft_mode": draft_mode,
            "idempotency_key": idempotency_key,
            "author_session": author_session,
            "content_markdown": content_markdown,
            "summary": summary,
        }
        return _call("submit_section", attach_next_action_for=project_id, submission=payload)

    @mcp.tool()
    def record_agent_note(
        project_id: str,
        note_text: str,
        target_section_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        """Record a non-binding agent_note for the user to review/adopt in local UI."""
        return _call(
            "record_agent_note",
            project_id=project_id,
            note_text=note_text,
            target_section_ids=target_section_ids,
        )

    # --- Review (2) ---
    @mcp.tool()
    def start_review(
        project_id: str,
        target_section_ids: list[str] | None = None,
        reviewer_id: str = "reviewer_agent",
    ) -> dict[str, Any]:
        """Run deterministic QA, create semantic review tasks, and claim them (must use an independent reviewer session)."""
        return _call(
            "start_review",
            project_id=project_id,
            target_section_ids=target_section_ids,
            reviewer_id=reviewer_id,
        )

    @mcp.tool()
    def submit_review(
        project_id: str,
        task_id: str,
        section_revision: int,
        context_fingerprint: str,
        findings: list[dict[str, Any]],
        review_summary: str,
        recommendation: Literal["approve", "revise", "block"],
        checklist_results: list[dict[str, Any]],
        checklist_hash: str,
        reviewed_by: str = "reviewer_agent",
        claim_evaluations: list[dict[str, Any]] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Submit structured quote-anchored semantic review; valid 'approve' reports are accepted immediately."""
        return _call(
            "submit_review",
            attach_next_action_for=project_id,
            project_id=project_id,
            task_id=task_id,
            section_revision=section_revision,
            context_fingerprint=context_fingerprint,
            findings=findings,
            review_summary=review_summary,
            recommendation=recommendation,
            reviewed_by=reviewed_by,
            claim_evaluations=claim_evaluations,
            checklist_results=checklist_results,
            checklist_hash=checklist_hash,
            idempotency_key=idempotency_key,
        )

    # --- Approval & Release (3 -> total 17 including export, or 16 core) ---
    @mcp.tool()
    def request_approval(
        project_id: str,
        artifact_type: Literal["outline", "section", "final_draft"],
        target_id: str = "global",
        query_only: bool = False,
        approval_request_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Create/deduplicate a pending user approval request, or query its read-only status when query_only=True."""
        if query_only:
            return _call(
                "get_approval_status",
                project_id=project_id,
                approval_request_id=approval_request_id,
                artifact_type=artifact_type,
                target_id=target_id,
            )
        return _call(
            "request_approval",
            attach_next_action_for=project_id,
            project_id=project_id,
            artifact_type=artifact_type,
            target_id=target_id,
            idempotency_key=idempotency_key,
        )

    @mcp.tool()
    def render_document(
        project_id: str,
        purpose: Literal["preview", "release"] = "release",
        template_mode: str = "auto",
        section_mapping: dict[str, str] | None = None,
        output_filename: str | None = None,
        degradation_mode: Literal["strict", "preview_plaintext"] | None = None,
    ) -> dict[str, Any]:
        """Compile full document via single-pass Pandoc (OMML + CSL + crossref) or mapped DOCX template."""
        return _call(
            "render_document",
            attach_next_action_for=project_id,
            project_id=project_id,
            purpose=purpose,
            template_mode=template_mode,
            section_mapping=section_mapping,
            output_filename=output_filename,
            degradation_mode=degradation_mode,
        )

    @mcp.tool()
    def export_document(
        project_id: str,
        purpose: Literal["preview", "release"] = "release",
        export_pdf: bool = False,
        template_mode: str = "auto",
        section_mapping: dict[str, str] | None = None,
        auto_rerender: bool = True,
    ) -> dict[str, Any]:
        """Export final DOCX, optional PDF, evidence traceability matrix, and audit bundle."""
        return _call(
            "export_document",
            attach_next_action_for=project_id,
            project_id=project_id,
            purpose=purpose,
            export_pdf=export_pdf,
            template_mode=template_mode,
            section_mapping=section_mapping,
            auto_rerender=auto_rerender,
        )

    return mcp


def main() -> None:
    server = create_mcp_server()
    server.run()


if __name__ == "__main__":
    main()
