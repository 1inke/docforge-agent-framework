"""MCP Server Interface for DocForge Agent Framework (DAF §2.1.B, §5 & Remediation v1.0)."""

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


def create_mcp_server(workspace_root: str | Path | None = None, allowed_source_roots: list[str | Path] | None = None) -> FastMCP:
    """Create and configure a FastMCP server exposing all DAF tools."""
    mcp = FastMCP("docforge-agent-framework")

    def _engine() -> DAFEngine:
        root = workspace_root or os.environ.get("DAF_WORKSPACE_ROOT")
        return DAFEngine(workspace_root=root, allowed_source_roots=allowed_source_roots)

    def _call(tool_name: str, **params: Any) -> dict[str, Any]:
        try:
            return _engine().call_agent_tool(tool_name, params)
        except DAFError as exc:
            return exc.to_dict()

    @mcp.tool()
    def get_capabilities(profile_id: str | None = None) -> dict[str, Any]:
        """Discover DAF protocol version, supported document profiles, tools, error codes, and runtime environment."""
        return _engine().get_capabilities(profile_id=profile_id)

    @mcp.tool()
    def get_task_guidance(
        project_id: str, task_kind: str, section_id: str | None = None, max_guidance_chars: int = 8000,
    ) -> dict[str, Any]:
        """Read current task guidance before source analysis, evidence extraction, planning, drafting, revision, review or release."""
        return _engine().get_task_guidance(project_id, task_kind, section_id, max_guidance_chars)

    @mcp.tool()
    def get_source_policy(project_id: str) -> dict[str, Any]:
        """Read effective Discover/Read/Cite permissions and source authorization requests."""
        return _engine().get_source_policy(project_id)

    @mcp.tool()
    def propose_source_policy_change(project_id: str, proposed_policy: dict[str, Any], reason: str) -> dict[str, Any]:
        """Request source authorization. This never changes permissions or confirms a human decision."""
        return _engine().propose_source_policy_change(project_id, proposed_policy, reason)

    @mcp.tool()
    def get_document_profile(profile_id: str) -> dict[str, Any]:
        """Retrieve the 3-layer DocumentProfileContract (Content, Structure, Layout) for a profile_id."""
        return _engine().get_document_profile(profile_id=profile_id)

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
        profile_overrides: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Initialize an isolated project workspace and atomic SQLite CAS ledger."""
        return _engine().create_project(
            project_id=project_id,
            title=title,
            profile_id=profile_id,
            domain=domain,
            research_direction=research_direction,
            target_audience=target_audience,
            language=language,
            global_constraints=global_constraints,
            profile_overrides=profile_overrides,
        )

    @mcp.tool()
    def update_profile_settings(
        project_id: str,
        profile_overrides: dict[str, Any],
    ) -> dict[str, Any]:
        """Safely update project DocumentProfileContract settings while preserving mandatory approval gates."""
        return _engine().update_profile_settings(
            project_id=project_id,
            profile_overrides=profile_overrides,
        )

    @mcp.tool()
    def ingest_sources(
        project_id: str,
        source_paths: list[str] | None = None,
        template_path: str | None = None,
        is_agent_generated_summary: bool = False,
    ) -> dict[str, Any]:
        """Ingest local source files (.md/.txt/.docx/.pdf) and optional .docx template."""
        return _call("ingest_sources",
            project_id=project_id,
            source_paths=source_paths,
            template_path=template_path,
            is_agent_generated_summary=is_agent_generated_summary,
        )

    @mcp.tool()
    def list_source_blocks(
        project_id: str,
        source_id: str | None = None,
        page_size: int = 50,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """List ingested SourceBlockSlice metadata (source_id, slice_id, locator, char_count, sha256) with stable pagination."""
        return _engine().list_source_blocks(
            project_id=project_id,
            source_id=source_id,
            page_size=page_size,
            cursor=cursor,
        )

    @mcp.tool()
    def get_source_block(
        project_id: str,
        slice_id: str,
        start_char: int = 0,
        max_chars: int = 6000,
        expected_sha256: str | None = None,
    ) -> dict[str, Any]:
        """Read a character window [start_char, end_char) of raw text from an ingested SourceBlockSlice."""
        return _engine().get_source_block(
            project_id=project_id,
            slice_id=slice_id,
            start_char=start_char,
            max_chars=max_chars,
            expected_sha256=expected_sha256,
        )

    @mcp.tool()
    def search_sources(
        project_id: str,
        query: str,
        source_ids: list[str] | None = None,
        page_size: int = 20,
        cursor: str | None = None,
        match_mode: str = "literal",
    ) -> dict[str, Any]:
        """Perform deterministic literal substring search across ingested SourceBlockSlices."""
        return _engine().search_sources(
            project_id=project_id,
            query=query,
            source_ids=source_ids,
            page_size=page_size,
            cursor=cursor,
            match_mode=match_mode,
        )

    @mcp.tool()
    def register_evidence(
        project_id: str,
        evidence_items: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Register evidence items with 3-layer verification (L1 excerpt_match_status, L3 source_reliability)."""
        return _engine().register_evidence(
            project_id=project_id,
            evidence_items=evidence_items,
        )

    @mcp.tool()
    def get_project_context(
        project_id: str,
        task_type: str = "plan_outline",
        token_budget: int | None = None,
        max_context_chars: int | None = None,
    ) -> dict[str, Any]:
        """Assemble task-aware, character-budgeted project context for 'plan_outline' or 'global_review'."""
        return _engine().get_project_context(
            project_id=project_id,
            task_type=task_type,
            token_budget=token_budget,
            max_context_chars=max_context_chars,
        )

    @mcp.tool()
    def submit_outline(
        project_id: str,
        global_thesis: str,
        nodes: list[dict[str, Any]],
        terminology: dict[str, str] | None = None,
        symbol_table: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Validate and commit the structured OutlineGraph and terminology registry."""
        return _engine().submit_outline(
            project_id=project_id,
            global_thesis=global_thesis,
            nodes=nodes,
            terminology=terminology,
            symbol_table=symbol_table,
        )

    @mcp.tool()
    def get_section_context(
        project_id: str,
        section_id: str,
        action: Literal["draft", "revise", "review"] = "draft",
        token_budget: int | None = None,
        max_context_chars: int | None = None,
    ) -> dict[str, Any]:
        """Assemble a character-budgeted SectionContextContract with deterministic context_fingerprint."""
        return _engine().get_section_context(
            project_id=project_id,
            section_id=section_id,
            action=action,
            token_budget=token_budget,
            max_context_chars=max_context_chars,
        )

    @mcp.tool()
    def get_section_content(
        project_id: str,
        section_id: str,
        start_char: int = 0,
        max_chars: int = 8000,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        """Read drafted section markdown in controlled character windows [start_char, end_char)."""
        return _engine().get_section_content(
            project_id=project_id,
            section_id=section_id,
            start_char=start_char,
            max_chars=max_chars,
            expected_revision=expected_revision,
        )

    @mcp.tool()
    def submit_section(
        submission: dict[str, Any] | None = None,
        project_id: str | None = None,
        section_id: str | None = None,
        base_revision: int = 0,
        context_revision: str = "",
        context_fingerprint: str = "",
        idempotency_key: str = "",
        draft_mode: Literal["formal", "draft_preapproval"] = "formal",
        content_markdown: str = "",
        summary: str = "",
        claims: list[str] | None = None,
        source_bindings: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Validate and commit a section draft checking outline gate, base_revision, context_revision fingerprint, and L1/L2 evidence bindings."""
        if submission is not None:
            payload = dict(submission)
        else:
            payload = {
                "project_id": project_id or "",
                "section_id": section_id or "",
                "base_revision": base_revision,
                "context_revision": context_revision,
                "context_fingerprint": context_fingerprint,
                "idempotency_key": idempotency_key,
                "draft_mode": draft_mode,
                "content_markdown": content_markdown,
                "summary": summary,
                "claims": claims or [],
                "source_bindings": source_bindings or [],
            }
        return _engine().submit_section(submission=payload)

    @mcp.tool()
    def review_document(
        project_id: str,
        target_section_ids: list[str] | None = None,
        agent_semantic_issues: list[dict[str, Any]] | None = None,
        claim_support_assessments: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Execute deterministic QA checks (L1/L2a) and generate pending SemanticReviewTasks."""
        return _engine().review_document(
            project_id=project_id,
            target_section_ids=target_section_ids,
            agent_semantic_issues=agent_semantic_issues,
            claim_support_assessments=claim_support_assessments,
        )

    @mcp.tool()
    def list_semantic_review_tasks(
        project_id: str,
        status: str | None = None,
    ) -> dict[str, Any]:
        """List semantic review tasks (pending/claimed/submitted/accepted/stale/rejected/expired) for a project."""
        return _engine().list_semantic_review_tasks(
            project_id=project_id,
            status=status,
        )

    @mcp.tool()
    def claim_semantic_review_task(
        project_id: str,
        task_id: str,
        reviewer_id: str = "reviewer_agent",
    ) -> dict[str, Any]:
        """Claim a pending semantic review task before submitting a structured semantic review report."""
        return _engine().claim_semantic_review_task(
            project_id=project_id,
            task_id=task_id,
            reviewer_id=reviewer_id,
        )

    @mcp.tool()
    def submit_semantic_review(
        project_id: str,
        task_id: str,
        section_revision: int,
        context_fingerprint: str,
        findings: list[dict[str, Any]] | None = None,
        review_summary: str = "",
        recommendation: Literal["approve", "revise", "block"] | None = None,
        reviewed_by: str = "reviewer_agent",
        claim_evaluations: list[dict[str, Any]] | None = None,
        checklist_results: list[dict[str, Any]] | None = None,
        checklist_hash: str | None = None,
        require_structured_checklist: bool = False,
        auto_accept: bool = False,
    ) -> dict[str, Any]:
        """Submit a structured semantic review report, L2b claim evaluations, and checklist_results bound to section_revision and context_fingerprint."""
        return _call("submit_semantic_review",
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
            require_structured_checklist=require_structured_checklist,
            auto_accept=auto_accept,
        )

    @mcp.tool()
    def accept_semantic_review(
        project_id: str,
        task_id: str,
        policy_version: str = "v1",
        require_structured_checklist: bool = False,
    ) -> dict[str, Any]:
        """Accept a submitted semantic review report after verifying revision, content hash, context fingerprint, checklist_results, and open issues."""
        return _engine().accept_semantic_review(
            project_id=project_id,
            task_id=task_id,
            policy_version=policy_version,
            require_structured_checklist=require_structured_checklist,
        )

    @mcp.tool()
    def get_revision_tasks(project_id: str) -> dict[str, Any]:
        """Return open ReviewIssues, pending semantic review tasks, and unverified claim bindings grouped by section."""
        return _engine().get_revision_tasks(project_id=project_id)

    @mcp.tool()
    def submit_feedback(
        project_id: str,
        feedback_text: str,
        target_section_ids: list[str] | None = None,
        add_user_constraints: list[str] | None = None,
        add_forbidden_scopes: list[str] | None = None,
        acknowledge_template_conflicts: list[str] | None = None,
        invalidate_target_approvals: bool = True,
    ) -> dict[str, Any]:
        """Record user feedback, constraints, and forbidden modification scopes into DecisionLog."""
        return _engine().submit_feedback(
            project_id=project_id,
            feedback_text=feedback_text,
            target_section_ids=target_section_ids,
            add_user_constraints=add_user_constraints,
            add_forbidden_scopes=add_forbidden_scopes,
            acknowledge_template_conflicts=acknowledge_template_conflicts,
            invalidate_target_approvals=invalidate_target_approvals,
        )

    @mcp.tool()
    def request_approval(
        project_id: str,
        artifact_type: Literal["outline", "section", "final_draft"],
        target_id: str = "global",
        requested_by: str = "agent",
    ) -> dict[str, Any]:
        """Create a pending human ApprovalRequest bound to the current artifact revision and content hash (never returns confirmation_token)."""
        return _engine().request_approval(
            project_id=project_id,
            artifact_type=artifact_type,
            target_id=target_id,
            requested_by=requested_by,
        )

    @mcp.tool()
    def get_approval_status(
        project_id: str,
        approval_request_id: str | None = None,
        artifact_type: Literal["outline", "section", "final_draft"] | None = None,
        target_id: str = "global",
    ) -> dict[str, Any]:
        """Query the current status of a human ApprovalRequest without exposing internal confirmation secrets."""
        return _engine().get_approval_status(
            project_id=project_id,
            approval_request_id=approval_request_id,
            artifact_type=artifact_type,
            target_id=target_id,
        )

    @mcp.tool()
    def get_project_status(project_id: str) -> dict[str, Any]:
        """Inspect full project state, revisions, approvals, RenderManifest, and recommended next actions."""
        return _engine().get_project_status(project_id=project_id)

    @mcp.tool()
    def render_document(
        project_id: str,
        purpose: Literal["preview", "release"] = "release",
        template_mode: str = "auto",
        section_mapping: dict[str, str] | None = None,
        output_filename: str | None = None,
        degradation_mode: Literal["strict", "preview_plaintext"] | None = None,
    ) -> dict[str, Any]:
        """Compile DocumentIR into a native Word (.docx) file with OMML equations and RenderManifest."""
        return _engine().render_document(
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
        """Export final DOCX (verifying RenderManifest freshness), optional PDF, 3-layer evidence traceability matrix, and audit trail bundle."""
        return _engine().export_document(
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
