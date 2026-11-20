"""Unified DAFEngine orchestrating all Agent interfaces over atomic SQLite CAS transactions, recoverable RenderJobs, and R2 contracts."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import uuid
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

from daf.capability_registry.registry import CapabilityRegistry
from daf.context_manager.manager import ContextManager, resolve_section_word_budget
from daf.core.contracts import (
    DocumentProfileContract,
    ProjectBrief,
    ProjectSourcePolicy,
    ProjectWorkspaceState,
    RenderJob,
    RenderManifest,
    SectionSubmissionContract,
)
from daf.core.errors import (
    ArtifactPathInvalidError,
    ContractValidationError,
    PolicyConflictError,
    RenderStaleError,
    SourcePolicyChangeRequiresHumanError,
    UntrustedApprovalChannelError,
)
from daf.core.source_import import resolve_source_roots
from daf.document_engine.docx_builder import (
    DocumentEngine,
    compute_render_input_fingerprint,
)
from daf.document_profiles.profiles import (
    check_profile_template_conflicts,
    get_document_profile,
)
from daf.guidance.builder import TaskGuidanceBuilder
from daf.guidance.policy import SourcePolicyEvaluator
from daf.project_store.store import (
    ProjectStore,
    validate_safe_docx_filename,
    validate_safe_identifier,
)
from daf.review_manager.reviewer import ReviewManager
from daf.workflow_engine.state_machine import (
    WorkflowEngine,
    assert_release_ready,
)


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _pid_is_alive(pid: int | None) -> bool:
    """Return True if a local OS process with the given PID appears to be alive."""
    if pid is None or pid <= 0:
        return False
    if pid == os.getpid():
        return True
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


class DAFEngine:
    """Primary programmatic and protocol facade for DocForge Agent Framework."""

    def __init__(
        self,
        workspace_root: str | Path | None = None,
        allowed_source_roots: list[str | Path] | None = None,
    ) -> None:
        default_root = os.environ.get("DAF_WORKSPACE_ROOT") or str(Path.cwd() / ".daf_workspace")
        self.workspace_root = Path(workspace_root or default_root).resolve()
        self.store = ProjectStore(self.workspace_root)
        self.allowed_source_roots = resolve_source_roots(allowed_source_roots)

    # 1. get_capabilities
    def get_capabilities(self, profile_id: str | None = None) -> dict[str, Any]:
        return CapabilityRegistry.get_capabilities(profile_id=profile_id)

    # 2. get_document_profile
    def get_document_profile(self, profile_id: str) -> dict[str, Any]:
        prof = get_document_profile(profile_id, workspace_root=self.workspace_root)
        return prof.model_dump(mode="json")

    # 3. create_project (create-only; profiles are user-owned YAML files under <workspace>/profiles/)
    def create_project(
        self,
        project_id: str,
        title: str,
        profile_id: str = "default",
        domain: str = "general",
        research_direction: str = "",
        target_audience: str = "academic_reviewers",
        language: str = "zh-CN",
        global_constraints: list[str] | None = None,
    ) -> dict[str, Any]:
        validate_safe_identifier(project_id, "project_id")
        profile = get_document_profile(profile_id, workspace_root=self.workspace_root)

        brief = ProjectBrief(
            project_id=project_id,
            title=title.strip(),
            profile_id=profile.profile_id,
            domain=domain.strip() or "general",
            research_direction=research_direction.strip(),
            target_audience=target_audience.strip() or "academic_reviewers",
            language=language.strip() or "zh-CN",
            global_constraints=list(global_constraints or []),
        )
        state = ProjectWorkspaceState(
            brief=brief,
            profile=profile,
            stage="initialized",
            source_policy=ProjectSourcePolicy(),
        )
        state = self.store.create_initial_state(
            state=state,
            audit_summary=f"Created project '{project_id}' with profile '{profile.profile_id}' in domain '{domain}'.",
            audit_payload={"title": title, "profile_id": profile.profile_id, "domain": domain},
        )
        return {
            "project_id": project_id,
            "stage": state.stage,
            "state_version": state.state_version,
            "workspace_dir": str(self.store.get_project_dir(project_id)),
            "profile_id": profile.profile_id,
            "require_outline_approval": "outline" in profile.approval_policy,
            "require_final_approval": "final_draft" in profile.approval_policy,
            "required_sections": [
                {"section_id": s.section_id, "title": s.title}
                for s in profile.structure_profile.required_sections
            ],
        }

    def get_task_guidance(
        self, project_id: str, task_kind: str, section_id: str | None = None,
        max_guidance_chars: int = 8000,
    ) -> dict[str, Any]:
        state = self.store.load_state(project_id)
        return TaskGuidanceBuilder.build(state, task_kind, section_id, max_guidance_chars).model_dump(mode="json")

    def get_source_policy(self, project_id: str) -> dict[str, Any]:
        state = self.store.load_state(project_id)
        return {"project_id": project_id, **SourcePolicyEvaluator.public_policy(state)}

    def set_source_policy_by_user(
        self,
        project_id: str,
        policy: dict[str, Any],
        reason: str = "Updated by local user",
        updated_by: str = "local_user",
    ) -> dict[str, Any]:
        """Directly update project source policy from local UI/CLI (not exposed to Agents)."""
        try:
            validated = ProjectSourcePolicy.model_validate(policy)
        except ValueError as exc:
            raise PolicyConflictError("Invalid source policy contract.") from exc
        if validated.mode == "project_only" and validated.external_lookup_enabled:
            raise PolicyConflictError("project_only does not allow external_lookup_enabled.")

        def _mutator(state: ProjectWorkspaceState, _conn: sqlite3.Connection) -> tuple[dict[str, Any], str, dict[str, Any]]:
            now = datetime.now(UTC).isoformat()
            before = SourcePolicyEvaluator.allowed_ids(state, "cite")
            validated.revision = SourcePolicyEvaluator.revision(state) + 1
            validated.updated_by = updated_by
            validated.approved_at = now
            validated.reason = reason.strip() or "Updated by local user"
            state.source_policy = validated
            revoked = before - SourcePolicyEvaluator.allowed_ids(state, "cite")
            affected: set[str] = set()
            denied_evidence = {
                eid for eid, ev in state.evidence_ledger.items.items() if ev.source_id in revoked
            }
            for eid in sorted(denied_evidence):
                affected.update(
                    ContextManager.invalidate_evidence_dependents(
                        state, eid, "source_authorization_revoked"
                    )
                )
            invalidated_outline = any(
                denied_evidence.intersection(n.assigned_evidence_ids)
                for n in state.outline_graph.nodes
            )
            if invalidated_outline:
                for key in ("outline", "final_draft", "final_draft:global"):
                    app = state.approvals.get(key)
                    if app and app.status == "approved":
                        app.status = "invalidated_by_change"
                        app.invalidation_reason = "Source authorization revoked."
                for pending in state.approval_requests.values():
                    if pending.status == "pending" and pending.artifact_type in {"outline", "final_draft"}:
                        pending.status = "stale"
            result = {
                "project_id": project_id,
                "policy_revision": SourcePolicyEvaluator.revision(state),
                "revoked_source_ids": sorted(revoked),
                "invalidated_sections": sorted(affected),
                "outline_invalidated": invalidated_outline,
            }
            return result, "Updated project source policy.", {**result, "actor": updated_by, "reason": validated.reason}

        return self.store.transact_project(project_id, "set_source_policy_by_user", _mutator)

    def apply_profile_by_user(
        self,
        project_id: str,
        profile_id: str,
    ) -> dict[str, Any]:
        """Apply a user-authored YAML profile from <workspace>/profiles/ to a project (CLI/UI only).

        Invalidates both outline and final_draft approvals if the profile changes.
        """
        new_profile = get_document_profile(profile_id, workspace_root=self.workspace_root)

        def _mutator(
            state: ProjectWorkspaceState, _conn: sqlite3.Connection
        ) -> tuple[dict[str, Any], str, dict[str, Any]]:
            state.profile = new_profile
            state.brief.profile_id = new_profile.profile_id
            invalidated: list[str] = []
            for gk in ("outline", "final_draft"):
                app = state.approvals.get(gk)
                if app and app.status == "approved":
                    app.status = "invalidated_by_change"
                    app.invalidation_reason = f"Profile '{profile_id}' applied by local user."
                    invalidated.append(gk)
            for req in state.approval_requests.values():
                if req.status == "pending" and req.artifact_type in {"outline", "final_draft"}:
                    req.status = "stale"
            res = {
                "project_id": project_id,
                "profile_id": new_profile.profile_id,
                "invalidated_approvals": invalidated,
                "profile": new_profile.model_dump(mode="json"),
            }
            return res, f"Applied profile '{profile_id}' by local user.", {"profile_id": profile_id}

        return self.store.transact_project(project_id, "apply_profile_by_user", _mutator)

    def set_evidence_reliability_by_user(
        self,
        project_id: str,
        evidence_id: str,
        reliability: Literal["trusted", "conditional", "unassessed", "rejected"],
        updated_by: str = "local_user",
    ) -> dict[str, Any]:
        """Local user-only L3 reliability grading (only users may mark evidence 'trusted')."""

        def _mutator(
            state: ProjectWorkspaceState, _conn: sqlite3.Connection
        ) -> tuple[dict[str, Any], str, dict[str, Any]]:
            ev = state.evidence_ledger.items.get(evidence_id)
            if ev is None:
                raise ContractValidationError(f"Unknown evidence_id '{evidence_id}'.")
            ev.source_reliability = reliability
            ev.evidence_revision += 1
            ev.updated_at = datetime.now(UTC).isoformat()
            ev.updated_reason = f"L3 reliability set to '{reliability}' by {updated_by}"
            res = {
                "project_id": project_id,
                "evidence_id": evidence_id,
                "source_reliability": ev.source_reliability,
                "evidence_revision": ev.evidence_revision,
            }
            return res, f"Set evidence '{evidence_id}' reliability to '{reliability}'.", res

        return self.store.transact_project(project_id, "set_evidence_reliability_by_user", _mutator)

    # 4. ingest_sources
    def ingest_sources(
        self,
        project_id: str,
        source_paths: list[str] | None = None,
        template_path: str | None = None,
        is_agent_generated_summary: bool = False,
    ) -> dict[str, Any]:
        parsed_sources = [
            ContextManager.ingest_source_file(
                sp,
                allowed_roots=self.allowed_source_roots,
                is_agent_generated_summary=is_agent_generated_summary,
            )
            for sp in (source_paths or [])
        ]
        tpl_report = (
            ContextManager.inspect_docx_template(
                template_path, allowed_roots=self.allowed_source_roots
            )
            if template_path
            else None
        )

        def _mutator(
            state: ProjectWorkspaceState, _conn: sqlite3.Connection
        ) -> tuple[dict[str, Any], str, dict[str, Any]]:
            ingested_docs: list[dict[str, Any]] = []
            invalidated_evidence: list[str] = []

            for doc_rec, slices in parsed_sources:
                inv_ids = ContextManager.apply_source_ingestion_to_state(state, doc_rec, slices)
                invalidated_evidence.extend(inv_ids)
                ingested_docs.append(
                    {
                        "source_id": doc_rec.source_id,
                        "file_name": doc_rec.file_name,
                        "file_type": doc_rec.file_type,
                        "file_sha256": doc_rec.file_sha256,
                        "slice_ids": doc_rec.slice_ids,
                        "is_agent_generated_summary": doc_rec.is_agent_generated_summary,
                    }
                )

            tpl_report_dict: dict[str, Any] | None = None
            conflicts: list[dict[str, str]] = []
            if tpl_report is not None:
                state.source_registry.template_inspection = tpl_report
                tpl_report_dict = tpl_report.model_dump(mode="json")
                conflicts = check_profile_template_conflicts(state.profile, tpl_report)

            if state.stage == "initialized":
                state.stage = "sources_ingested"

            res = {
                "project_id": project_id,
                "stage": state.stage,
                "ingested_sources": ingested_docs,
                "total_slices": len(state.source_registry.slices),
                "invalidated_evidence_ids": invalidated_evidence,
                "template_inspection": tpl_report_dict,
                "profile_template_conflicts": conflicts,
            }
            summary = f"Ingested {len(ingested_docs)} source files; template={tpl_report is not None}; invalidated_evidence={len(invalidated_evidence)}."
            return res, summary, res

        return self.store.transact_project(project_id, "ingest_sources", _mutator)

    # 4b. register_evidence (R2-P0-03 versioned evidence + cascade invalidation)
    def register_evidence(
        self,
        project_id: str,
        evidence_items: list[dict[str, Any]],
        *,
        allow_source_replacement: bool = False,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        def _mutator(
            state: ProjectWorkspaceState, _conn: sqlite3.Connection
        ) -> tuple[dict[str, Any], str, dict[str, Any]]:
            reg_out = ContextManager.register_or_update_evidence_items(
                state,
                evidence_items,
                allow_source_replacement=allow_source_replacement,
            )
            verified_ids = [item["evidence_id"] for item in reg_out["verified_items"]]
            unverified_ids = [item["evidence_id"] for item in reg_out["unverified_items"]]
            all_items = reg_out["verified_items"] + reg_out["unverified_items"]

            res = {
                "project_id": project_id,
                "verified_evidence_ids": verified_ids,
                "unverified_evidence_ids": unverified_ids,
                "updated_evidence_ids": reg_out["updated_evidence_ids"],
                "unchanged_evidence_ids": reg_out["unchanged_evidence_ids"],
                "invalidated_sections": reg_out["invalidated_sections"],
                "invalidated_approvals": reg_out.get("invalidated_approvals", []),
                "evidence_items": all_items,
            }
            summary = (
                f"Registered {len(all_items)} evidence items "
                f"({len(verified_ids)} L1 matched, {len(unverified_ids)} unmatched, "
                f"updated={reg_out['updated_evidence_ids']}, invalidated_sections={reg_out['invalidated_sections']})."
            )
            return res, summary, {
                "verified_ids": verified_ids,
                "unverified_ids": unverified_ids,
                "updated_evidence_ids": reg_out["updated_evidence_ids"],
                "invalidated_sections": reg_out["invalidated_sections"],
            }

        return self.store.transact_project(
            project_id,
            "register_evidence",
            _mutator,
            idempotency_key=idempotency_key,
            request_payload={"project_id": project_id, "evidence_items": evidence_items},
        )

    # 4c. list_source_blocks (R3-04 §1.2A)
    def list_source_blocks(
        self,
        project_id: str,
        source_id: str | None = None,
        page_size: int = 50,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        state = self.store.load_state(project_id)
        return ContextManager.list_source_blocks(
            state=state,
            source_id=source_id,
            page_size=page_size,
            cursor=cursor,
        )

    # 4d. get_source_block (R3-04 §1.2B)
    def get_source_block(
        self,
        project_id: str,
        slice_id: str,
        start_char: int = 0,
        max_chars: int = 6000,
        expected_sha256: str | None = None,
    ) -> dict[str, Any]:
        state = self.store.load_state(project_id)
        return ContextManager.get_source_block(
            state=state,
            slice_id=slice_id,
            start_char=start_char,
            max_chars=max_chars,
            expected_sha256=expected_sha256,
        )

    # 4e. search_sources (R3-04 §1.2C)
    def search_sources(
        self,
        project_id: str,
        query: str,
        source_ids: list[str] | None = None,
        page_size: int = 20,
        cursor: str | None = None,
        match_mode: str = "literal",
    ) -> dict[str, Any]:
        state = self.store.load_state(project_id)
        return ContextManager.search_sources(
            state=state,
            query=query,
            source_ids=source_ids,
            page_size=page_size,
            cursor=cursor,
            match_mode=match_mode,
        )

    # 5. get_project_context (R3-02 & R3-05)
    def get_project_context(
        self,
        project_id: str,
        task_type: str = "plan_outline",
        token_budget: int | None = None,
        max_context_chars: int | None = None,
    ) -> dict[str, Any]:
        state = self.store.load_state(project_id)
        return ContextManager.build_project_context(
            state=SourcePolicyEvaluator.context_view(state),
            task_type=task_type,
            token_budget=token_budget,
            max_context_chars=max_context_chars,
        )

    # 6. submit_outline
    def submit_outline(
        self,
        project_id: str,
        global_thesis: str,
        nodes: list[dict[str, Any]],
        terminology: dict[str, str] | None = None,
        symbol_table: dict[str, str] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        def _mutator(
            state: ProjectWorkspaceState, _conn: sqlite3.Connection
        ) -> tuple[dict[str, Any], str, dict[str, Any]]:
            res = WorkflowEngine.submit_outline(
                state=state,
                global_thesis=global_thesis,
                nodes=nodes,
                terminology=terminology,
                symbol_table=symbol_table,
            )
            summary = f"Submitted outline revision {res['outline_revision']} ({res['section_count']} sections)."
            return res, summary, res

        return self.store.transact_project(
            project_id,
            "submit_outline",
            _mutator,
            idempotency_key=idempotency_key,
            request_payload={
                "project_id": project_id,
                "global_thesis": global_thesis,
                "nodes": nodes,
                "terminology": terminology,
                "symbol_table": symbol_table,
            },
            hydrate_slices=False,
        )

    # 7. get_section_context (R3-03 & R3-05)
    def get_section_context(
        self,
        project_id: str,
        section_id: str,
        action: Literal["draft", "revise", "review"] = "draft",
        token_budget: int | None = None,
        max_context_chars: int | None = None,
    ) -> dict[str, Any]:
        state = self.store.load_state(project_id)
        ctx = ContextManager.build_section_context(
            state=SourcePolicyEvaluator.context_view(state),
            section_id=section_id,
            action=action,
            token_budget=token_budget,
            max_context_chars=max_context_chars,
        )
        return ctx.model_dump(mode="json")

    # 7b. get_section_content (R3-02 §3.2 controlled section content reader)
    def get_section_content(
        self,
        project_id: str,
        section_id: str,
        start_char: int = 0,
        max_chars: int = 8000,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        state = self.store.load_state(project_id)
        return ContextManager.get_section_content(
            state=state,
            section_id=section_id,
            start_char=start_char,
            max_chars=max_chars,
            expected_revision=expected_revision,
        )

    # 8. submit_section
    def submit_section(
        self,
        submission: dict[str, Any] | SectionSubmissionContract,
        *,
        simulate_mid_transaction_crash: bool = False,
    ) -> dict[str, Any]:
        sub_obj = (
            submission
            if isinstance(submission, SectionSubmissionContract)
            else SectionSubmissionContract.model_validate(submission)
        )
        project_id = validate_safe_identifier(sub_obj.project_id, "project_id")
        semantic_payload = sub_obj.model_dump(mode="json")

        def _mutator(
            state: ProjectWorkspaceState, _conn: sqlite3.Connection
        ) -> tuple[dict[str, Any], str, dict[str, Any]]:
            res = WorkflowEngine.submit_section(state=state, submission=sub_obj)
            summary = f"Submitted section '{sub_obj.section_id}' revision {res['new_revision']}."
            return res, summary, res

        return self.store.transact_project(
            project_id=project_id,
            tool_name="submit_section",
            mutator=_mutator,
            idempotency_key=sub_obj.idempotency_key,
            request_payload=semantic_payload,
            simulate_mid_transaction_crash=simulate_mid_transaction_crash,
        )

    # 9. review_document (R2-P0-02 deterministic checks + creates pending SemanticReviewTasks)
    def review_document(
        self,
        project_id: str,
        target_section_ids: list[str] | None = None,
        agent_semantic_issues: list[dict[str, Any]] | None = None,
        claim_support_assessments: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        def _mutator(
            state: ProjectWorkspaceState, _conn: sqlite3.Connection
        ) -> tuple[dict[str, Any], str, dict[str, Any]]:
            res = ReviewManager.run_review(
                state=state,
                target_section_ids=target_section_ids,
                agent_semantic_issues=agent_semantic_issues,
                claim_support_assessments=claim_support_assessments,
            )
            summary = (
                f"Ran deterministic QA review: {res['open_issue_counts']['total_open']} open issues, "
                f"{len(res['semantic_review_tasks'])} semantic review tasks."
            )
            return res, summary, res["open_issue_counts"]

        return self.store.transact_project(project_id, "review_document", _mutator)

    # 9b. list_semantic_review_tasks (R2-P0-02)
    def list_semantic_review_tasks(
        self,
        project_id: str,
        status: str | None = None,
    ) -> dict[str, Any]:
        state = self.store.load_state(project_id)
        return ReviewManager.list_semantic_review_tasks(state, status=status)

    # 9c. claim_semantic_review_task (R2-P0-02)
    def claim_semantic_review_task(
        self,
        project_id: str,
        task_id: str,
        agent_session_id: str = "external-agent",
        reviewer_id: str | None = None,
    ) -> dict[str, Any]:
        effective_agent = reviewer_id or agent_session_id

        def _mutator(
            state: ProjectWorkspaceState, _conn: sqlite3.Connection
        ) -> tuple[dict[str, Any], str, dict[str, Any]]:
            res = ReviewManager.claim_semantic_review_task(
                state=state,
                task_id=task_id,
                agent_session_id=effective_agent,
            )
            return res, f"Claimed semantic review task '{task_id}'.", res

        return self.store.transact_project(project_id, "claim_semantic_review_task", _mutator)

    # 9d. submit_semantic_review (R2-P0-02, R2-P1-06 & R3-01)
    def submit_semantic_review(
        self,
        project_id: str,
        task_id: str,
        section_revision: int,
        context_fingerprint: str,
        findings: list[dict[str, Any]] | None = None,
        review_summary: str = "",
        reviewer_agent: str = "external-agent",
        reviewed_by: str | None = None,
        recommendation: str | None = None,
        claim_evaluations: list[dict[str, Any]] | None = None,
        checklist_results: list[dict[str, Any]] | None = None,
        checklist_hash: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Submit a structured semantic review report; valid approving reports are accepted immediately."""
        effective_reviewer = reviewed_by or reviewer_agent

        def _mutator(
            state: ProjectWorkspaceState, _conn: sqlite3.Connection
        ) -> tuple[dict[str, Any], str, dict[str, Any]]:
            res = ReviewManager.submit_semantic_review(
                state=state,
                task_id=task_id,
                section_revision=section_revision,
                context_fingerprint=context_fingerprint,
                findings=findings,
                review_summary=review_summary,
                reviewer_agent=effective_reviewer,
                claim_evaluations=claim_evaluations,
                checklist_results=checklist_results,
                checklist_hash=checklist_hash,
                require_structured_checklist=True,
                recommendation=recommendation,
            )
            summary = f"Submitted semantic review for task '{task_id}' (rev={section_revision}, status={res['status']})."
            return res, summary, res

        try:
            return self.store.transact_project(
                project_id,
                "submit_semantic_review",
                _mutator,
                idempotency_key=idempotency_key,
                request_payload={
                    "project_id": project_id,
                    "task_id": task_id,
                    "section_revision": section_revision,
                    "context_fingerprint": context_fingerprint,
                    "recommendation": recommendation,
                    "checklist_hash": checklist_hash,
                },
                hydrate_slices=False,
            )
        except ContractValidationError as exc:
            if exc.code == "CLAIM_SUPPORT_CONFLICT":
                self.store.record_audit_event(
                    project_id, "semantic_review_conflict", "Rejected semantic override of deterministic claim checks.",
                    {"task_id": task_id, "reason_code": exc.code},
                )
            raise

    # 10. get_revision_tasks
    def get_revision_tasks(self, project_id: str) -> dict[str, Any]:
        state = self.store.load_state(project_id)
        return ReviewManager.get_revision_tasks(state)

    # 11. record_agent_note (Agent-callable: source='agent_note' only; cannot mutate user constraints)
    def record_agent_note(
        self,
        project_id: str,
        note_text: str,
        target_section_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        def _mutator(
            state: ProjectWorkspaceState, _conn: sqlite3.Connection
        ) -> tuple[dict[str, Any], str, dict[str, Any]]:
            res = WorkflowEngine.record_agent_note(
                state=state,
                note_text=note_text,
                target_section_ids=target_section_ids,
            )
            return res, f"Recorded agent note '{res['decision_id']}'.", res

        return self.store.transact_project(project_id, "record_agent_note", _mutator)

    def add_user_decision_by_user(
        self,
        project_id: str,
        feedback_text: str,
        target_section_ids: list[str] | None = None,
        add_user_constraints: list[str] | None = None,
        add_forbidden_scopes: list[str] | None = None,
        acknowledge_template_conflicts: list[str] | None = None,
        invalidate_target_approvals: bool = True,
    ) -> dict[str, Any]:
        """Local user-only decision & constraint recorder (CLI/UI only; never exposed to Agents)."""

        def _mutator(
            state: ProjectWorkspaceState, _conn: sqlite3.Connection
        ) -> tuple[dict[str, Any], str, dict[str, Any]]:
            res = WorkflowEngine.add_user_decision(
                state=state,
                feedback_text=feedback_text,
                target_section_ids=target_section_ids,
                add_user_constraints=add_user_constraints,
                add_forbidden_scopes=add_forbidden_scopes,
                acknowledge_template_conflicts=acknowledge_template_conflicts,
                invalidate_target_approvals=invalidate_target_approvals,
            )
            return res, f"Recorded user decision '{res['decision_id']}'.", res

        return self.store.transact_project(project_id, "add_user_decision_by_user", _mutator)

    def adopt_agent_note_by_user(
        self,
        project_id: str,
        decision_id: str,
    ) -> dict[str, Any]:
        """Promote an agent_note to an active user constraint (CLI/UI only)."""

        def _mutator(
            state: ProjectWorkspaceState, _conn: sqlite3.Connection
        ) -> tuple[dict[str, Any], str, dict[str, Any]]:
            res = WorkflowEngine.adopt_agent_note(state=state, decision_id=decision_id)
            return res, f"Adopted agent note '{decision_id}' as user constraint.", res

        return self.store.transact_project(project_id, "adopt_agent_note_by_user", _mutator)

    def list_approvals(self, project_id: str) -> dict[str, Any]:
        """Read-only list of approval requests and recorded approvals for local UI/CLI."""
        state = self.store.load_state(project_id)
        return {
            "project_id": project_id,
            "requests": [r.model_dump(mode="json") for r in state.approval_requests.values()],
            "approvals": {k: v.model_dump(mode="json") for k, v in state.approvals.items()},
        }

    def show_approval_detail(self, project_id: str, approval_request_id: str) -> dict[str, Any]:
        """Build a rich read-only inspection bundle (outline tree, section diffs, open issues, reviews) for local UI/CLI."""
        import difflib

        state = self.store.load_state(project_id)
        req = state.approval_requests.get(approval_request_id)
        if req is None:
            raise ContractValidationError(f"Unknown approval_request_id '{approval_request_id}'.")

        detail: dict[str, Any] = {
            "project_id": project_id,
            "title": state.brief.title,
            "profile_id": state.profile.profile_id,
            "request": req.model_dump(mode="json"),
            "open_issues": [
                iss.model_dump(mode="json")
                for iss in state.review_ledger.issues
                if iss.status == "open"
            ],
            "agent_notes": [
                e.model_dump(mode="json")
                for e in state.decision_log.entries
                if e.source == "agent_note" and not e.adopted_as_constraint
            ],
        }
        if req.artifact_type == "outline":
            rules_by_id = {
                r.section_id: r
                for r in (
                    state.profile.structure_profile.required_sections
                    + state.profile.structure_profile.optional_sections
                )
            }
            nodes_view = []
            for n in state.outline_graph.nodes:
                min_w, max_w, origin = resolve_section_word_budget(rules_by_id.get(n.section_id), n)
                ev_quotes = [
                    {
                        "evidence_id": eid,
                        "quote_excerpt": state.evidence_ledger.items[eid].quote_excerpt,
                        "source_reliability": state.evidence_ledger.items[eid].source_reliability,
                    }
                    for eid in n.assigned_evidence_ids
                    if eid in state.evidence_ledger.items
                ]
                nodes_view.append(
                    {
                        "section_id": n.section_id,
                        "title": n.title,
                        "depends_on": n.depends_on,
                        "word_budget": {"min_words": min_w, "max_words": max_w, "origin": origin},
                        "assigned_evidence": ev_quotes,
                    }
                )
            detail["outline"] = {
                "global_thesis": state.outline_graph.global_thesis,
                "outline_revision": state.outline_graph.outline_revision,
                "nodes": nodes_view,
            }
        else:
            section_diffs: list[dict[str, Any]] = []
            for sid, sec in sorted(state.section_versions.sections.items()):
                prev_md = ""
                if len(sec.history) >= 2:
                    prev_md = sec.history[-2].content_markdown
                diff_text = "".join(
                    difflib.unified_diff(
                        prev_md.splitlines(keepends=True),
                        sec.content_markdown.splitlines(keepends=True),
                        fromfile=f"{sid}@prev",
                        tofile=f"{sid}@r{sec.revision}",
                        n=2,
                    )
                )
                rep = state.review_ledger.semantic_reports.get(sid)
                section_diffs.append(
                    {
                        "section_id": sid,
                        "title": sec.title,
                        "revision": sec.revision,
                        "content_hash": sec.content_hash,
                        "diff": diff_text,
                        "review_summary": rep.review_summary if rep else "",
                        "review_status": rep.status if rep else "none",
                    }
                )
            detail["sections"] = section_diffs
            detail["last_rendered_docx_path"] = state.last_rendered_docx_path
        return detail

    # 12a. request_approval (idempotent & deduplicated by artifact_hash)
    def request_approval(
        self,
        project_id: str,
        artifact_type: Literal["outline", "section", "final_draft"],
        target_id: str = "global",
        ttl_seconds: int = 3600,
        requested_by: str = "agent",
        channel: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        _ = requested_by
        if channel is not None:
            raise UntrustedApprovalChannelError(
                f"Agent tool 'request_approval' forbids specifying approval channel '{channel}'. "
                "Human confirmation must occur in local UI or CLI.",
                details={"attempted_channel": channel},
            )

        def _mutator(
            state: ProjectWorkspaceState, _conn: sqlite3.Connection
        ) -> tuple[dict[str, Any], str, dict[str, Any]]:
            res = WorkflowEngine.request_approval(
                state=state,
                artifact_type=artifact_type,
                target_id=target_id,
                ttl_seconds=ttl_seconds,
            )
            summary = (
                f"Created/reused pending ApprovalRequest '{res['approval_request_id']}' for {artifact_type} "
                f"(target={res['target_id']}, rev={res['artifact_revision']})."
            )
            return res, summary, res

        return self.store.transact_project(
            project_id,
            "request_approval",
            _mutator,
            idempotency_key=idempotency_key,
            request_payload={
                "project_id": project_id,
                "artifact_type": artifact_type,
                "target_id": target_id,
                "ttl_seconds": ttl_seconds,
            },
            hydrate_slices=False,
        )

    # 12b. get_approval_status (strictly read-only — never mutates state_version or writes audit)
    def get_approval_status(
        self,
        project_id: str,
        approval_request_id: str | None = None,
        artifact_type: Literal["outline", "section", "final_draft"] | None = None,
        target_id: str = "global",
    ) -> dict[str, Any]:
        state = self.store.load_state(project_id, hydrate_slices=False, hydrate_sections=False)
        return WorkflowEngine.get_approval_status(
            state=state,
            approval_request_id=approval_request_id,
            artifact_type=artifact_type,
            target_id=target_id,
        )

    def decide_approval_by_human(
        self,
        project_id: str,
        approval_request_id: str,
        decision: Literal["approved", "rejected"],
        human_actor_id: str = "local_user",
        decided_by: str | None = None,
        confirmation_channel: Literal["local_ui", "local_cli"] = "local_cli",
        comment: str = "",
    ) -> dict[str, Any]:
        """Local user confirmation entrypoint (used by local UI and CLI; never exposed to Agent tools)."""
        effective_actor = decided_by or human_actor_id

        def _mutator(
            state: ProjectWorkspaceState, _conn: sqlite3.Connection
        ) -> tuple[dict[str, Any], str, dict[str, Any]]:
            res = WorkflowEngine.record_human_approval_decision(
                state=state,
                approval_request_id=approval_request_id,
                decision=decision,
                human_actor_id=effective_actor,
                confirmation_channel=confirmation_channel,
                comment=comment,
            )
            summary = (
                f"User '{effective_actor}' {decision} request '{approval_request_id}' "
                f"via '{confirmation_channel}'."
            )
            return res, summary, res

        return self.store.transact_project(project_id, "decide_approval_by_human", _mutator)

    # 13. get_project_status (strictly read-only; never mutates state or reaps active render jobs)
    def get_project_status(self, project_id: str) -> dict[str, Any]:
        from daf.guidance.state_resolver import resolve_task_state

        state = self.store.load_state(project_id, hydrate_slices=False)
        ReviewManager.refresh_semantic_task_staleness(state)
        rules_by_id = {
            r.section_id: r
            for r in (
                state.profile.structure_profile.required_sections
                + state.profile.structure_profile.optional_sections
            )
        }
        node_map = {n.section_id: n for n in state.outline_graph.nodes}
        # P1-2: Outline nodes are the canonical section list once planned; fall back to profile rules before outline
        ordered_ids = (
            [n.section_id for n in state.outline_graph.nodes]
            if state.outline_graph.nodes
            else list(rules_by_id.keys())
        )
        sections_status: list[dict[str, Any]] = []
        for sid in ordered_ids:
            req = rules_by_id.get(sid)
            sec = state.section_versions.sections.get(sid)
            sec_app = state.approvals.get(f"section:{sid}")
            node = node_map.get(sid)
            _, _, w_meta = resolve_section_word_budget(req, node, state.outline_graph)
            sections_status.append(
                {
                    "section_id": sid,
                    "title": sec.title if sec else (node.title if node else (req.title if req else sid)),
                    "required": req.required if req else True,
                    "revision": sec.revision if sec else 0,
                    "content_hash": sec.content_hash if sec else "",
                    "is_preapproval_draft": sec.is_preapproval_draft if sec else False,
                    "needs_revalidation": sec.needs_revalidation if sec else False,
                    "stale_reason": sec.stale_reason if sec else "",
                    "approval_status": sec_app.status if sec_app else "pending",
                    "word_budget": w_meta,
                }
            )

        open_issues = [i for i in state.review_ledger.issues if i.status == "open"]
        active_render_jobs = [
            j.model_dump(mode="json")
            for j in state.render_jobs.values()
            if j.phase in {"prepared", "built", "promoted"}
        ]

        # P1-2: Single source of truth for next_action via state_resolver
        if not state.source_registry.documents:
            resolution = resolve_task_state(state, "source_analysis", None)
        elif state.outline_graph.outline_revision == 0 or (
            state.approvals.get("outline") is None or state.approvals["outline"].status != "approved"
        ):
            resolution = resolve_task_state(state, "plan_outline", None)
        else:
            undrafted = next(
                (
                    sid
                    for sid in ordered_ids
                    if sid not in state.section_versions.sections
                    or state.section_versions.sections[sid].revision == 0
                ),
                None,
            )
            if undrafted is not None:
                resolution = resolve_task_state(state, "draft_section", undrafted)
            else:
                resolution = resolve_task_state(state, "release_preflight", None)

        next_action = resolution.next_action
        next_actions = [f"{next_action.get('tool') or 'done'}: {next_action.get('reason', '')}".strip()]

        return {
            "project_id": state.brief.project_id,
            "title": state.brief.title,
            "profile_id": state.profile.profile_id,
            "domain": state.brief.domain,
            "stage": state.stage,
            "state_version": state.state_version,
            "updated_at": state.updated_at,
            "source_document_count": len(state.source_registry.documents),
            "source_slice_count": len(state.source_registry.slices),
            "verified_evidence_count": sum(
                1 for e in state.evidence_ledger.items.values() if e.excerpt_match_status == "matched"
            ),
            "unverified_evidence_count": sum(
                1 for e in state.evidence_ledger.items.values() if e.excerpt_match_status != "matched"
            ),
            "source_policy": SourcePolicyEvaluator.public_policy(state),
            "outline_revision": state.outline_graph.outline_revision,
            "outline_hash": state.outline_graph.outline_hash,
            "sections": sections_status,
            "approval_requests": {k: v.model_dump(mode="json") for k, v in state.approval_requests.items()},
            "approvals": {k: v.model_dump(mode="json") for k, v in state.approvals.items()},
            "open_issue_count": len(open_issues),
            "active_render_jobs": active_render_jobs,
            "active_user_constraints": list(state.decision_log.active_user_constraints),
            "forbidden_modification_scopes": list(state.decision_log.forbidden_modification_scopes),
            "last_render_manifest": (
                state.last_render_manifest.model_dump(mode="json")
                if state.last_render_manifest
                else None
            ),
            "last_rendered_docx_path": state.last_rendered_docx_path,
            "last_exported_pdf_path": state.last_exported_pdf_path,
            "next_action": next_action,
            "recommended_next_actions": next_actions,
        }

    # 14a. recover_render_jobs (P1-4: lease- & PID-aware crash recovery only)
    def recover_render_jobs(self, project_id: str) -> dict[str, Any]:
        """Recover only incomplete RenderJobs whose lease has expired AND whose owner PID is no longer alive."""
        now = datetime.now(timezone.utc)
        state_peek = self.store.load_state(project_id, hydrate_slices=False, hydrate_sections=False)

        def _is_reapable(job: RenderJob) -> bool:
            if job.phase not in {"prepared", "built", "promoted"}:
                return False
            if job.lease_until:
                try:
                    if datetime.fromisoformat(job.lease_until) > now:
                        return False
                except ValueError:
                    pass
            if _pid_is_alive(job.owner_pid):
                return False
            return True

        reapable_ids = [jid for jid, j in state_peek.render_jobs.items() if _is_reapable(j)]
        if not reapable_ids:
            return {"project_id": project_id, "recovered_jobs": []}

        def _mutator(
            state: ProjectWorkspaceState, _conn: sqlite3.Connection
        ) -> tuple[dict[str, Any], str, dict[str, Any]]:
            committed_paths: set[str] = set()
            if state.last_render_manifest and state.last_render_manifest.artifact_status == "committed":
                committed_paths.add(str(Path(state.last_render_manifest.docx_path).resolve()))
            for hist_m in state.render_history:
                if hist_m.artifact_status in {"committed", "superseded"}:
                    committed_paths.add(str(Path(hist_m.docx_path).resolve()))

            recovered: list[str] = []
            now_iso = datetime.now(timezone.utc).isoformat()
            for jid, job in state.render_jobs.items():
                if _is_reapable(job):
                    if job.temp_path:
                        tp = Path(job.temp_path)
                        if tp.exists():
                            try:
                                tp.unlink()
                            except Exception:
                                pass
                    if job.target_path:
                        tgp = Path(job.target_path).resolve()
                        if tgp.exists() and str(tgp) not in committed_paths:
                            try:
                                tgp.unlink()
                            except Exception:
                                pass
                    job.phase = "aborted_recovered"
                    job.updated_at = now_iso
                    job.error_message = "Recovered expired render job whose owner process exited."
                    recovered.append(jid)

            res = {"project_id": project_id, "recovered_jobs": recovered}
            return res, f"Recovered {len(recovered)} expired render jobs.", res

        return self.store.transact_project(
            project_id, "recover_render_jobs", _mutator, hydrate_slices=False, hydrate_sections=False
        )

    # 14. render_document (R2-P1-05 two-phase Prepare -> Build (out-of-tx) -> Revalidate & Finalize)
    def render_document(
        self,
        project_id: str,
        purpose: Literal["preview", "release"] = "release",
        template_mode: str = "auto",
        section_mapping: dict[str, str] | None = None,
        output_filename: str | None = None,
        degradation_mode: Literal["strict", "preview_plaintext"] | None = None,
        pandoc_bin_override: str | None = "AUTO",
        *,
        simulate_crash_before_finalize: bool = False,
    ) -> dict[str, Any]:
        if output_filename is not None:
            validate_safe_docx_filename(output_filename)

        self.recover_render_jobs(project_id)
        artifacts_dir = self.store.ensure_project_dirs(project_id) / "artifacts"

        eff_degradation: Literal["strict", "preview_plaintext"] = (
            degradation_mode
            if degradation_mode is not None
            else ("strict" if purpose == "release" else "strict")
        )

        # Phase 1: Prepare (short SQLite transaction)
        prepared_holder: dict[str, Any] = {}

        def _prepare_mutator(
            state: ProjectWorkspaceState, _conn: sqlite3.Connection
        ) -> tuple[dict[str, Any], str, dict[str, Any]]:
            if purpose == "release":
                assert_release_ready(state)

            fp_info = compute_render_input_fingerprint(
                state=state,
                purpose=purpose,
                template_mode=template_mode,
                section_mapping=section_mapping,
                degradation_mode=eff_degradation,
            )
            job_id = f"rjob_{uuid.uuid4().hex[:8]}"
            render_seq = len(state.render_history) + len(state.render_jobs) + 1
            render_id = f"render_{render_seq:04d}_{job_id[-4:]}"
            fp_short = fp_info["input_fingerprint"].removeprefix("sha256:")[:8]

            if output_filename is not None:
                fname = validate_safe_docx_filename(output_filename)
            else:
                suffix = "_PREVIEW_DRAFT.docx" if purpose == "preview" else ".docx"
                fname = f"{project_id}_{render_id}_{fp_short}{suffix}"

            target_path = (artifacts_dir.resolve() / fname).resolve()
            try:
                target_path.relative_to(artifacts_dir.resolve())
            except ValueError as exc:
                raise ArtifactPathInvalidError(
                    f"Output path '{target_path}' escapes project artifacts directory."
                ) from exc

            now_utc = datetime.now(timezone.utc)
            job = RenderJob(
                job_id=job_id,
                render_id=render_id,
                project_id=project_id,
                purpose=purpose,
                phase="prepared",
                input_fingerprint=fp_info["input_fingerprint"],
                target_path=str(target_path),
                owner_pid=os.getpid(),
                heartbeat_at=now_utc.isoformat(),
                lease_until=(now_utc + timedelta(minutes=10)).isoformat(),
            )
            state.render_jobs[job_id] = job
            prepared_holder["state_snapshot"] = state.model_copy(deep=True)
            prepared_holder["job_id"] = job_id
            prepared_holder["render_id"] = render_id
            prepared_holder["input_fingerprint"] = fp_info["input_fingerprint"]
            prepared_holder["target_path"] = target_path
            res = {"job_id": job_id, "render_id": render_id, "phase": "prepared"}
            return res, f"Prepared RenderJob '{job_id}' ({render_id}).", res

        self.store.transact_project(project_id, "render_prepare", _prepare_mutator)

        state_snapshot: ProjectWorkspaceState = prepared_holder["state_snapshot"]
        job_id: str = prepared_holder["job_id"]
        render_id: str = prepared_holder["render_id"]

        # Phase 2 & 3: Build & Promote OUTSIDE SQLite write transaction (R2-P1-05 §5.3 & T-R2-ART-04)
        try:
            build_res = DocumentEngine.render_document(
                state=state_snapshot,
                output_dir=artifacts_dir,
                purpose=purpose,
                template_mode=template_mode,
                section_mapping=section_mapping,
                output_filename=output_filename,
                degradation_mode=eff_degradation,
                pandoc_bin_override=pandoc_bin_override,
                job_id=job_id,
                render_id_override=render_id,
                update_state_in_place=False,
                simulate_crash_before_finalize=simulate_crash_before_finalize,
            )
        except Exception as exc:
            # If simulated crash after promotion, leave job in 'promoted' state so recover_render_jobs can test recovery (T-R2-ART-03)
            if not simulate_crash_before_finalize:
                error_message = str(exc)
                try:
                    def _fail_mutator(
                        st: ProjectWorkspaceState, _c: sqlite3.Connection
                    ) -> tuple[dict[str, Any], str, dict[str, Any]]:
                        if job_id in st.render_jobs:
                            st.render_jobs[job_id].phase = "failed"
                            st.render_jobs[job_id].error_message = error_message
                        return {"job_id": job_id}, f"RenderJob '{job_id}' failed.", {"error": error_message}

                    self.store.transact_project(project_id, "render_failed", _fail_mutator)
                except Exception:
                    pass
            raise

        # Phase 4 & 5: Revalidate & Finalize in a short SQLite transaction
        stale_error_holder: list[RenderStaleError] = []

        def _finalize_mutator(
            state: ProjectWorkspaceState, _conn: sqlite3.Connection
        ) -> tuple[dict[str, Any], str, dict[str, Any]]:
            if purpose == "release":
                assert_release_ready(state)

            live_fp = compute_render_input_fingerprint(
                state=state,
                purpose=purpose,
                template_mode=template_mode,
                section_mapping=section_mapping,
                degradation_mode=eff_degradation,
            )
            promoted_file = Path(build_res["docx_path"])
            if live_fp["input_fingerprint"] != build_res["input_fingerprint"]:
                if job_id in state.render_jobs:
                    state.render_jobs[job_id].phase = "stale"
                    state.render_jobs[job_id].updated_at = datetime.now(timezone.utc).isoformat()
                if promoted_file.exists():
                    try:
                        promoted_file.unlink()
                    except Exception:
                        pass
                stale_error_holder.append(
                    RenderStaleError(
                        "Project state changed while DOCX build was in progress; rendered artifact discarded.",
                        details={
                            "job_id": job_id,
                            "build_input_fingerprint": build_res["input_fingerprint"],
                            "live_input_fingerprint": live_fp["input_fingerprint"],
                        },
                    )
                )
                return {"job_id": job_id, "phase": "stale"}, f"RenderJob '{job_id}' marked stale.", {"job_id": job_id}

            if not promoted_file.exists():
                raise RenderStaleError(
                    f"Promoted DOCX file missing at '{promoted_file}' before finalize."
                )
            actual_sha = _sha256_file(promoted_file)
            if actual_sha != build_res["docx_sha256"]:
                raise RenderStaleError(
                    "Promoted DOCX SHA-256 mismatch during finalize."
                )

            manifest = RenderManifest.model_validate(build_res["render_manifest"])
            manifest.artifact_status = "committed"

            if state.last_render_manifest is not None:
                state.last_render_manifest.artifact_status = "superseded"
                state.render_history.append(state.last_render_manifest)

            state.last_render_manifest = manifest
            state.last_rendered_docx_path = str(promoted_file)
            if job_id in state.render_jobs:
                state.render_jobs[job_id].phase = "committed"
                state.render_jobs[job_id].output_sha256 = f"sha256:{actual_sha}"
                state.render_jobs[job_id].updated_at = datetime.now(timezone.utc).isoformat()

            if purpose == "release":
                state.stage = "rendered"

            summary = (
                f"Finalized DOCX render (purpose={purpose}, mode={build_res['template_mode']}, "
                f"render_id={render_id}, job_id={job_id}) to '{build_res['docx_path']}'."
            )
            return build_res, summary, build_res

        fin_res = self.store.transact_project(project_id, "render_document", _finalize_mutator)
        if stale_error_holder:
            raise stale_error_holder[0]
        return fin_res

    # 15. export_document (DAF-P0-02, DAF-P0-04, DAF-P1-05 & R2-P1-05)
    def export_document(
        self,
        project_id: str,
        purpose: Literal["preview", "release"] = "release",
        export_pdf: bool = False,
        template_mode: str = "auto",
        section_mapping: dict[str, str] | None = None,
        auto_rerender: bool = True,
    ) -> dict[str, Any]:
        self.recover_render_jobs(project_id)
        artifacts_dir = self.store.ensure_project_dirs(project_id) / "artifacts"

        state_check = self.store.load_state(project_id)
        if purpose == "release":
            assert_release_ready(state_check)

        live_fp = compute_render_input_fingerprint(
            state=state_check,
            purpose=purpose,
            template_mode=template_mode,
            section_mapping=section_mapping,
            degradation_mode="strict",
        )
        manifest = state_check.last_render_manifest
        stale_reasons: list[str] = []

        if manifest is None:
            stale_reasons.append("No existing committed RenderManifest found.")
        else:
            if manifest.artifact_status != "committed":
                stale_reasons.append(f"Manifest artifact_status is '{manifest.artifact_status}' (not 'committed').")
            if manifest.purpose != purpose:
                stale_reasons.append(
                    f"Manifest purpose '{manifest.purpose}' does not match requested export purpose '{purpose}'."
                )
            if manifest.input_fingerprint != live_fp["input_fingerprint"]:
                stale_reasons.append(
                    f"Manifest input_fingerprint ({manifest.input_fingerprint[:18]}) does not match current project snapshot ({live_fp['input_fingerprint'][:18]})."
                )
            docx_file = Path(manifest.docx_path)
            if not docx_file.exists():
                stale_reasons.append(f"Rendered DOCX file does not exist at '{docx_file}'.")
            else:
                actual_sha = f"sha256:{_sha256_file(docx_file)}"
                if actual_sha != manifest.docx_sha256:
                    stale_reasons.append(
                        f"On-disk DOCX SHA-256 ({actual_sha[:18]}) does not match RenderManifest docx_sha256 ({manifest.docx_sha256[:18]})."
                    )

        rerendered = False
        if stale_reasons:
            if not auto_rerender:
                raise RenderStaleError(
                    f"Cannot export document: existing rendered DOCX is stale or mismatched ({'; '.join(stale_reasons)})",
                    details={
                        "project_id": project_id,
                        "stale_reasons": stale_reasons,
                        "live_input_fingerprint": live_fp["input_fingerprint"],
                    },
                )
            self.render_document(
                project_id=project_id,
                purpose=purpose,
                template_mode=template_mode,
                section_mapping=section_mapping,
                degradation_mode="strict",
            )
            rerendered = True

        # Reload committed manifest after optional out-of-tx rerender
        state_post_render = self.store.load_state(project_id)
        manifest = state_post_render.last_render_manifest
        assert manifest is not None and manifest.artifact_status == "committed"
        docx_path = Path(manifest.docx_path)
        verified_docx_sha = _sha256_file(docx_path)
        if f"sha256:{verified_docx_sha}" != manifest.docx_sha256:
            raise RenderStaleError(
                "On-disk DOCX SHA-256 mismatch before export.",
                details={"docx_path": str(docx_path)},
            )

        # Run PDF export outside SQLite write transaction if requested (T-R2-ART-06)
        pdf_info: dict[str, Any] | None = None
        if export_pdf:
            pdf_target = artifacts_dir / f"{docx_path.stem}.pdf"
            pdf_info = DocumentEngine.export_pdf_via_word_com(docx_path, pdf_target)
            if pdf_info.get("source_docx_sha256") != verified_docx_sha:
                raise RenderStaleError(
                    "PDF export source_docx_sha256 does not match committed RenderManifest DOCX SHA-256."
                )

        def _export_mutator(
            state: ProjectWorkspaceState, _conn: sqlite3.Connection
        ) -> tuple[dict[str, Any], str, dict[str, Any]]:
            SourcePolicyEvaluator.assert_document(state)
            if purpose == "release":
                assert_release_ready(state)

            cur_manifest = state.last_render_manifest
            if cur_manifest is None or cur_manifest.render_id != manifest.render_id:
                raise RenderStaleError("RenderManifest changed concurrently during export.")

            if pdf_info is not None:
                state.last_exported_pdf_path = pdf_info["pdf_path"]
                state.last_exported_pdf_source_docx_sha256 = pdf_info["source_docx_sha256"]

            evidence_matrix: list[dict[str, Any]] = []
            for sec_id, sec in state.section_versions.sections.items():
                for b in sec.source_bindings:
                    ev = state.evidence_ledger.items.get(b.evidence_id) if b.evidence_id else None
                    src_doc = (
                        state.source_registry.documents.get(ev.source_id)
                        if ev and ev.source_id in state.source_registry.documents
                        else None
                    )
                    evidence_matrix.append(
                        {
                            "binding_id": b.binding_id,
                            "section_id": sec_id,
                            "section_revision": sec.revision,
                            "claim_text": b.claim_text,
                            "claim_type": b.claim_type,
                            "rule_check_status": b.rule_check_status,
                            "semantic_support_status": b.semantic_support_status,
                            "support_status": b.support_status,
                            "support_rationale": b.support_rationale,
                            "reviewer": b.reviewer,
                            "reviewed_against_revision": b.reviewed_against_revision,
                            "reviewed_against_section_hash": b.reviewed_against_section_hash,
                            "reviewed_against_evidence_fingerprint": b.reviewed_against_evidence_fingerprint,
                            "reviewed_evidence_revision": b.reviewed_evidence_revision,
                            "evidence_id": b.evidence_id,
                            "evidence_revision": ev.evidence_revision if ev else 0,
                            "evidence_fingerprint": ev.evidence_fingerprint if ev else "",
                            "evidence_quote_hash": b.evidence_quote_hash,
                            "quote_excerpt": ev.quote_excerpt if ev else "",
                            "excerpt_match_status": ev.excerpt_match_status if ev else "not_applicable",
                            "verification_status": ev.verification_status if ev else "not_applicable",
                            "source_reliability": ev.source_reliability if ev else "not_applicable",
                            "source_id": ev.source_id if ev else "",
                            "source_file_name": src_doc.file_name if src_doc else "",
                            "source_file_sha256": ev.source_file_sha256 if ev else "",
                            "slice_id": ev.slice_id if ev else "",
                            "locator": ev.locator if ev else "",
                        }
                    )

            bundle_suffix = "preview_bundle.json" if purpose == "preview" else "export_bundle.json"
            bundle_path = artifacts_dir / f"{project_id}_{bundle_suffix}"
            bundle_payload = {
                "exported_at": datetime.now(timezone.utc).isoformat(),
                "project_id": project_id,
                "purpose": purpose,
                "is_official_release": purpose == "release",
                "title": state.brief.title,
                "profile_id": state.profile.profile_id,
                "render_manifest": cur_manifest.model_dump(mode="json"),
                "rerendered_during_export": rerendered,
                "rerender_reasons": stale_reasons,
                "docx_path": str(docx_path),
                "source_docx_sha256": verified_docx_sha,
                "pdf_export": pdf_info,
                "approvals": {k: v.model_dump(mode="json") for k, v in state.approvals.items()},
                "evidence_traceability_matrix": evidence_matrix,
                "evidence_ledger": {
                    k: v.model_dump(mode="json") for k, v in state.evidence_ledger.items.items()
                    if SourcePolicyEvaluator.permits(state, v.source_id, "read")
                },
            }
            bundle_path.write_text(
                json.dumps(bundle_payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

            state.stage = "exported"
            res = {
                "project_id": project_id,
                "purpose": purpose,
                "is_official_release": purpose == "release",
                "stage": state.stage,
                "docx_path": str(docx_path),
                "source_docx_sha256": verified_docx_sha,
                "render_id": cur_manifest.render_id,
                "input_fingerprint": cur_manifest.input_fingerprint,
                "rerendered_during_export": rerendered,
                "rerender_reasons": stale_reasons,
                "pdf_export": pdf_info,
                "export_bundle_json_path": str(bundle_path),
                "evidence_traceability_path": str(bundle_path),
                "evidence_trace_count": len(evidence_matrix),
            }
            summary = (
                f"Exported {purpose} bundle for '{project_id}' "
                f"(render_id={cur_manifest.render_id}, rerendered={rerendered}, PDF={export_pdf})."
            )
            return res, summary, res

        return self.store.transact_project(project_id, "export_document", _export_mutator)

    # Unified Agent Tool Dispatcher (enforces non-privileged Agent domain boundary, R2-P0-01)
    def call_agent_tool(self, tool_name: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Dispatch a tool call from an external Agent (MCP / CLI `call` / HTTP `/api/v1/tools/{tool_name}`).
        Strictly blocks any attempt by an Agent to call trusted human confirmation methods or mint tokens.
        """
        payload = dict(params or {})
        if tool_name in {"set_source_policy_by_user", "update_source_policy", "set_source_policy"}:
            raise SourcePolicyChangeRequiresHumanError("Agent cannot modify source policy directly; ask the user via local UI or CLI.")
        if tool_name in {
            "decide_approval_by_human",
            "confirm_approval",
        }:
            raise UntrustedApprovalChannelError(
                f"Tool '{tool_name}' is restricted to local user confirmation. "
                "External Agents may only call 'request_approval' and 'get_approval_status'.",
                details={"attempted_tool": tool_name},
            )

        allowed_agent_tools: dict[str, Any] = {
            "get_capabilities": self.get_capabilities,
            "get_document_profile": self.get_document_profile,
            "get_task_guidance": self.get_task_guidance,
            "get_source_policy": self.get_source_policy,
            "create_project": self.create_project,
            "ingest_sources": self.ingest_sources,
            "list_source_blocks": self.list_source_blocks,
            "get_source_block": self.get_source_block,
            "search_sources": self.search_sources,
            "register_evidence": self.register_evidence,
            "get_project_context": self.get_project_context,
            "submit_outline": self.submit_outline,
            "get_section_context": self.get_section_context,
            "get_section_content": self.get_section_content,
            "submit_section": self.submit_section,
            "review_document": self.review_document,
            "list_semantic_review_tasks": self.list_semantic_review_tasks,
            "claim_semantic_review_task": self.claim_semantic_review_task,
            "submit_semantic_review": self.submit_semantic_review,
            "get_revision_tasks": self.get_revision_tasks,
            "record_agent_note": self.record_agent_note,
            "request_approval": self.request_approval,
            "get_approval_status": self.get_approval_status,
            "get_project_status": self.get_project_status,
            "render_document": self.render_document,
            "export_document": self.export_document,
        }
        if tool_name not in allowed_agent_tools:
            raise ContractValidationError(
                f"Unknown or non-exposed Agent tool '{tool_name}'.",
                details={"tool_name": tool_name, "available_tools": sorted(allowed_agent_tools.keys())},
            )
        fn = allowed_agent_tools[tool_name]
        if tool_name == "submit_section" and "submission" not in payload:
            return fn(submission=payload)
        return fn(**payload)
