"""Workflow State Machine, Context Fingerprint Enforcement, Trusted Human Approval Gates & L2a/L2b Verification."""

from __future__ import annotations

import difflib
import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone
from typing import Any, Literal

from daf.context_manager.manager import (
    ContextManager,
    compute_canonical_json_hash,
    evaluate_claim_binding_support,
    resolve_section_word_budget,
)
from daf.core.contracts import (
    ApprovalRecord,
    DecisionRecord,
    OutlineNode,
    ProjectWorkspaceState,
    ReviewIssue,
    SectionRevisionRecord,
    SectionState,
    SectionSubmissionContract,
    SourceBinding,
)
from daf.core.errors import (
    ApprovalConflictError,
    BibliographyMissingError,
    ClaimReviewPendingError,
    ContextStaleError,
    ContractValidationError,
    CrossrefUnresolvedError,
    EvidenceChangedError,
    RevisionConflictError,
    SemanticReviewRequiredError,
    SemanticReviewStaleError,
    UntrustedApprovalChannelError,
)
from daf.document_profiles.profiles import check_profile_template_conflicts
from daf.guidance.policy import SourcePolicyEvaluator
from daf.project_store.store import ProjectStore, validate_safe_identifier


def compute_text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def count_effective_words(markdown_text: str) -> int:
    """Count Chinese characters plus English/numeric word tokens in Markdown text."""
    cjk_chars = re.findall(r"[\u4e00-\u9fff]", markdown_text)
    latin_words = re.findall(r"[A-Za-z0-9_]+", markdown_text)
    return len(cjk_chars) + len(latin_words)


def effective_section_ids(state: ProjectWorkspaceState) -> list[str]:
    """Return canonical section IDs: outline nodes if planned, otherwise profile required sections."""
    if state.outline_graph.nodes:
        return [n.section_id for n in state.outline_graph.nodes]
    return [s.section_id for s in state.profile.structure_profile.required_sections]


def _has_valid_bibliography(bib: dict[str, Any] | None) -> bool:
    if not isinstance(bib, dict):
        return False
    title = str(bib.get("title") or "").strip()
    author = bib.get("author")
    issued = bib.get("issued")
    has_author = isinstance(author, list) and len(author) > 0 and any(
        isinstance(a, dict) and bool(a.get("family") or a.get("literal") or a.get("given"))
        for a in author
    )
    has_issued = isinstance(issued, dict) and bool(
        issued.get("date-parts") or issued.get("raw") or issued.get("literal")
    )
    return bool(title and has_author and has_issued)


def compute_final_draft_aggregate_hash(state: ProjectWorkspaceState) -> str:
    """Compute a deterministic SHA-256 hash over all drafted sections, structured blocks, outline, and profile."""
    sections_payload: dict[str, Any] = {}
    for sid in sorted(state.section_versions.sections.keys()):
        sec = state.section_versions.sections[sid]
        if sec.revision > 0:
            sections_payload[sid] = {
                "revision": sec.revision,
                "title": sec.title,
                "content_hash": sec.content_hash,
            }
    agg_payload = {
        "outline_hash": state.outline_graph.outline_hash,
        "profile_hash": compute_canonical_json_hash(state.profile.model_dump(mode="json")),
        "sections": sections_payload,
    }
    return compute_canonical_json_hash(agg_payload)


def assert_semantic_and_claim_reviews_ready(state: ProjectWorkspaceState) -> None:
    """Enforce (accepted semantic reviews for current section revisions),
 (secondary evidence revision & quote_hash consistency),
    and (L2a passed + L2b supported_reviewed for all factual claims).
    """
    SourcePolicyEvaluator.assert_document(state)
    # 1. Check that no drafted section is marked needs_revalidation
    stale_sections = [
        sid
        for sid, sec in state.section_versions.sections.items()
        if sec.revision > 0 and sec.needs_revalidation
    ]
    if stale_sections:
        has_stale_sem_task = any(
            t.status in {"stale", "accepted"} and t.section_id in stale_sections
            for t in state.review_ledger.semantic_review_tasks
        )
        if has_stale_sem_task:
            raise SemanticReviewStaleError(
                f"Sections and their semantic reviews are stale due to upstream or evidence changes: {stale_sections}.",
                details={"stale_sections": stale_sections},
            )
        raise ApprovalConflictError(
            f"Sections require revalidation due to upstream or evidence changes: {stale_sections}.",
            code="APPROVAL_STALE",
            details={"stale_sections": stale_sections},
        )

    # 2. First inspect all SourceBindings for L1/L2a/L3 and evidence version consistency
    changed_evidence_bindings: list[dict[str, Any]] = []
    unsupported_bindings: list[dict[str, Any]] = []
    pending_l2b_bindings: list[dict[str, Any]] = []

    for sid, sec in state.section_versions.sections.items():
        if sec.revision == 0:
            continue
        for b in sec.source_bindings:
            if b.claim_type == "research_hypothesis" and not b.evidence_id:
                det_status, det_reason = evaluate_claim_binding_support(b, None)
                if b.rule_check_status == "rule_failed" or det_status == "unsupported":
                    unsupported_bindings.append(
                        {
                            "section_id": sid,
                            "binding_id": b.binding_id,
                            "support_status": b.support_status,
                            "rationale": det_reason,
                        }
                    )
                continue

            ev = state.evidence_ledger.items.get(b.evidence_id)
            if ev is None:
                unsupported_bindings.append(
                    {
                        "section_id": sid,
                        "binding_id": b.binding_id,
                        "support_status": "unsupported",
                        "rationale": f"Evidence '{b.evidence_id}' not found.",
                    }
                )
                continue

            # Secondary consistency check against live EvidenceItem & SourceDocumentRecord
            source_doc = state.source_registry.documents.get(ev.source_id)
            if (
                ev.excerpt_match_status in {"source_stale", "stale"}
                or b.semantic_support_status == "stale"
                or b.support_status == "stale"
                or (source_doc is not None and ev.source_file_sha256 != source_doc.file_sha256)
                or (b.evidence_quote_hash and b.evidence_quote_hash != ev.quote_hash)
                or (b.reviewed_evidence_revision is not None and b.reviewed_evidence_revision != ev.evidence_revision)
                or (
                    b.reviewed_against_evidence_fingerprint
                    and b.reviewed_against_evidence_fingerprint != ev.evidence_fingerprint
                )
            ):
                changed_evidence_bindings.append(
                    {
                        "section_id": sid,
                        "binding_id": b.binding_id,
                        "evidence_id": b.evidence_id,
                        "binding_quote_hash": b.evidence_quote_hash,
                        "live_quote_hash": ev.quote_hash,
                        "reviewed_evidence_revision": b.reviewed_evidence_revision,
                        "live_evidence_revision": ev.evidence_revision,
                    }
                )
                continue

            if ev.excerpt_match_status != "matched" or ev.source_reliability == "rejected":
                unsupported_bindings.append(
                    {
                        "section_id": sid,
                        "binding_id": b.binding_id,
                        "support_status": "unsupported",
                        "rationale": f"Evidence '{ev.evidence_id}' L1={ev.excerpt_match_status}, L3={ev.source_reliability}.",
                    }
                )
                continue

            # Run live L2a rule check to ensure no ungrounded metrics_data or quantitative/overclaim conflict
            det_status, det_reason = evaluate_claim_binding_support(b, ev)
            if b.rule_check_status == "rule_failed" or det_status in {"unsupported", "partially_supported"}:
                unsupported_bindings.append(
                    {
                        "section_id": sid,
                        "binding_id": b.binding_id,
                        "support_status": det_status,
                        "rationale": det_reason,
                    }
                )
                continue

            if (
                b.semantic_support_status != "supported_reviewed"
                or b.reviewed_against_revision != sec.revision
                or b.reviewed_against_section_hash != sec.content_hash
            ):
                pending_l2b_bindings.append(
                    {
                        "section_id": sid,
                        "binding_id": b.binding_id,
                        "rule_check_status": b.rule_check_status,
                        "semantic_support_status": b.semantic_support_status,
                    }
                )

    if changed_evidence_bindings:
        raise EvidenceChangedError(
            f"Release/approval blocked: {len(changed_evidence_bindings)} bound evidence items changed after section drafting or review.",
            details={"changed_evidence_bindings": changed_evidence_bindings},
        )

    if unsupported_bindings:
        raise ApprovalConflictError(
            f"Release/approval blocked: {len(unsupported_bindings)} claim-evidence bindings failed L1/L2a/L2b support checks.",
            code="CLAIM_UNSUPPORTED",
            recommended_tool="review_document",
            details={"unsupported_bindings": unsupported_bindings},
        )

    # 3. Verify open blocker/major review issues before checking semantic review tasks
    open_issues = [iss for iss in state.review_ledger.issues if iss.status == "open"]
    blockers = [iss.issue_id for iss in open_issues if iss.severity == "blocker"]
    majors = [iss.issue_id for iss in open_issues if iss.severity == "major"]
    if blockers or (state.profile.block_release_on_major_issues and majors):
        raise ApprovalConflictError(
            f"Release/approval blocked: open review issues remain (blockers={blockers}, majors={majors}).",
            code="APPROVAL_REQUIRED",
            recommended_tool="get_revision_tasks",
            details={"blockers": blockers, "majors": majors},
        )

    # 4. Verify each drafted section has an ACCEPTED SemanticReviewTask matching current revision, content_hash, and context_fingerprint
    unreviewed_sections: list[str] = []
    stale_reviewed_sections: list[str] = []

    for sid, sec in state.section_versions.sections.items():
        if sec.revision == 0:
            continue
        _, live_fp, _ = ContextManager.compute_section_dependency_snapshot(state, sid)
        matching_tasks = [
            t
            for t in state.review_ledger.semantic_review_tasks
            if t.section_id == sid
        ]
        if not matching_tasks:
            unreviewed_sections.append(sid)
            continue

        accepted_current = [
            t
            for t in matching_tasks
            if t.status == "accepted"
            and (t.report is None or t.report.recommendation == "approve")
            and (not t.requires_structured_checklist or (
                t.report is not None and t.report.checklist_verification_mode == "structured_verified"
                and t.report.checklist_hash == t.checklist_hash
                and {r.check_id for r in t.report.checklist_results} == {c.check_id for c in t.checklist_items}
                and all(r.verdict in {"pass", "not_applicable"} for r in t.report.checklist_results)
            ))
            and t.target_section_revision == sec.revision
            and (not t.target_section_content_hash or t.target_section_content_hash == sec.content_hash)
            and (not t.target_context_fingerprint or t.target_context_fingerprint == live_fp)
        ]
        if accepted_current:
            continue

        # Distinguish never-submitted/pending vs stale
        if any(t.status == "stale" or t.target_section_revision != sec.revision or t.target_context_fingerprint != live_fp for t in matching_tasks if t.status in {"accepted", "stale"}):
            stale_reviewed_sections.append(sid)
        else:
            unreviewed_sections.append(sid)

    if stale_reviewed_sections:
        raise SemanticReviewStaleError(
            f"Release/approval blocked: semantic review is stale for sections {stale_reviewed_sections}. Re-run review_document and submit_semantic_review.",
            details={"stale_reviewed_sections": stale_reviewed_sections},
        )

    if unreviewed_sections:
        raise SemanticReviewRequiredError(
            f"Release/approval blocked: sections lack an accepted semantic review for their current revision: {unreviewed_sections}. "
            "Deterministic QA alone cannot substitute for accepted semantic review.",
            details={"unreviewed_sections": unreviewed_sections},
        )

    if pending_l2b_bindings:
        raise ClaimReviewPendingError(
            f"Release/approval blocked: {len(pending_l2b_bindings)} factual claim bindings passed L2a rules but lack accepted L2b semantic support review.",
            details={"pending_claim_bindings": pending_l2b_bindings},
        )


def assert_release_ready(state: ProjectWorkspaceState) -> dict[str, Any]:
    """Verify all mandatory conditions before allowing purpose='release' rendering or export."""
    SourcePolicyEvaluator.assert_document(state)
    # 1. Outline approval gate (cannot be bypassed by mutating approval_policy)
    outline_app = state.approvals.get("outline")
    if outline_app is None or outline_app.status != "approved":
        raise ApprovalConflictError(
            f"Release blocked: outline is not approved (status='{outline_app.status if outline_app else 'missing'}').",
            code="APPROVAL_REQUIRED",
            details={
                "artifact_type": "outline",
                "project_id": state.brief.project_id,
                "status": outline_app.status if outline_app else "missing",
            },
        )
    if (
        outline_app.approved_revision != state.outline_graph.outline_revision
        or outline_app.content_hash != state.outline_graph.outline_hash
    ):
        raise ApprovalConflictError(
            "Release blocked: outline approval does not match current outline revision/hash.",
            code="APPROVAL_STALE",
            details={
                "approved_revision": outline_app.approved_revision,
                "current_outline_revision": state.outline_graph.outline_revision,
            },
        )

    # 2. Effective sections present, non-empty, and not preapproval drafts
    required_ids = effective_section_ids(state)
    missing_or_empty: list[str] = []
    preapproval_sections: list[str] = []
    for sid in required_ids:
        sec = state.section_versions.sections.get(sid)
        if sec is None or sec.revision == 0 or not sec.content_markdown.strip():
            missing_or_empty.append(sid)
        elif sec.is_preapproval_draft:
            preapproval_sections.append(sid)
    if missing_or_empty:
        raise ApprovalConflictError(
            f"Release blocked: required sections are undrafted or empty: {missing_or_empty}.",
            code="APPROVAL_REQUIRED",
            details={"missing_sections": missing_or_empty},
        )
    if preapproval_sections:
        raise ApprovalConflictError(
            f"Release blocked: sections were submitted in 'draft_preapproval' mode and must be re-submitted after outline approval: {preapproval_sections}.",
            code="APPROVAL_REQUIRED",
            details={"preapproval_sections": preapproval_sections},
        )

    # 3, 4, 5. Semantic reviews, L2a/L2b claim bindings, evidence consistency & open issues
    assert_semantic_and_claim_reviews_ready(state)

    # 6. Template-profile major conflicts acknowledged
    if state.source_registry.template_inspection is not None:
        conflicts = check_profile_template_conflicts(
            state.profile, state.source_registry.template_inspection
        )
        unack_major = [
            c["type"]
            for c in conflicts
            if c.get("severity") in {"major", "blocker"}
            and c["type"] not in state.decision_log.acknowledged_template_conflicts
        ]
        if unack_major:
            raise ApprovalConflictError(
                f"Release blocked: major template-profile conflicts require user acknowledgment: {unack_major}.",
                code="APPROVAL_REQUIRED",
                details={"unacknowledged_conflicts": unack_major},
            )

    # 6b. R1: All cited source documents must have user-confirmed CSL-JSON bibliography
    missing_bib_sources: list[str] = []
    seen_cited_sources: set[str] = set()
    for sid in effective_section_ids(state):
        sec = state.section_versions.sections.get(sid)
        if sec is None or sec.revision == 0:
            continue
        for b in sec.source_bindings:
            if not b.evidence_id:
                continue
            ev = state.evidence_ledger.items.get(b.evidence_id)
            if ev is None or ev.source_id in seen_cited_sources:
                continue
            seen_cited_sources.add(ev.source_id)
            doc_rec = state.source_registry.documents.get(ev.source_id)
            if doc_rec is None or not _has_valid_bibliography(doc_rec.bibliography):
                missing_bib_sources.append(ev.source_id)
    if missing_bib_sources:
        raise BibliographyMissingError(
            f"Release blocked: cited source(s) lack user-confirmed bibliography (title, author, issued): {sorted(missing_bib_sources)}.",
            details={"missing_source_ids": sorted(missing_bib_sources)},
        )

    # 6c. R2: All cross-references across effective sections must resolve to defined labels
    all_defined_labels: set[str] = set()
    all_xref_targets: list[str] = []
    for sid in effective_section_ids(state):
        sec = state.section_versions.sections.get(sid)
        if sec is None or sec.revision == 0:
            continue
        all_defined_labels.update(sec.defined_labels)
        all_xref_targets.extend(sec.crossref_targets)
    unresolved_xrefs = sorted({x for x in all_xref_targets if x not in all_defined_labels})
    if unresolved_xrefs:
        raise CrossrefUnresolvedError(
            f"Release blocked: unresolved cross-reference target(s): {unresolved_xrefs}.",
            details={"unresolved_crossrefs": unresolved_xrefs, "defined_labels": sorted(all_defined_labels)},
        )

    # 7. final_draft approval matches live aggregate content hash (cannot be bypassed via approval_policy)
    live_agg_hash = compute_final_draft_aggregate_hash(state)
    final_app = state.approvals.get("final_draft")
    if final_app is None or final_app.status != "approved":
        raise ApprovalConflictError(
            f"Release blocked: final_draft is not approved (status='{final_app.status if final_app else 'missing'}').",
            code="APPROVAL_REQUIRED",
            details={
                "artifact_type": "final_draft",
                "live_aggregate_hash": live_agg_hash,
                "status": final_app.status if final_app else "missing",
            },
        )
    if final_app.content_hash != live_agg_hash:
        raise ApprovalConflictError(
            "Release blocked: final_draft approval hash does not match current document snapshot.",
            code="APPROVAL_STALE",
            details={
                "approved_hash": final_app.content_hash,
                "live_aggregate_hash": live_agg_hash,
            },
        )

    return {
        "release_ready": True,
        "live_aggregate_hash": live_agg_hash,
    }


class WorkflowEngine:
    """Enforces workflow transitions, context fingerprint verification, optimistic locking, and trusted human approvals."""

    @classmethod
    def submit_outline(
        cls,
        state: ProjectWorkspaceState,
        global_thesis: str,
        nodes: list[dict[str, Any] | OutlineNode],
        terminology: dict[str, str] | None = None,
        symbol_table: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        if not global_thesis or not global_thesis.strip():
            raise ContractValidationError("global_thesis must be a non-empty string.")

        parsed_nodes: list[OutlineNode] = []
        seen_ids: set[str] = set()
        rule_map = {
            r.section_id: r
            for r in (
                state.profile.structure_profile.required_sections
                + state.profile.structure_profile.optional_sections
            )
        }
        budget_origins: dict[str, str] = {}
        resolved_budgets: dict[str, dict[str, Any]] = {}

        for raw_node in nodes:
            node = raw_node if isinstance(raw_node, OutlineNode) else OutlineNode.model_validate(raw_node)
            validate_safe_identifier(node.section_id, "section_id")
            if node.section_id in seen_ids:
                raise ContractValidationError(f"Duplicate section_id '{node.section_id}' in outline.")
            seen_ids.add(node.section_id)

            rule = rule_map.get(node.section_id)
            if node.target_min_words is not None and node.target_max_words is not None:
                budget_origins[node.section_id] = "outline"
            elif node.target_min_words is None and node.target_max_words is None:
                budget_origins[node.section_id] = "profile"
            else:
                budget_origins[node.section_id] = "mixed"

            # Validate resolved word budget (raises WordBudgetInvalidError on invalid bounds)
            _, _, w_meta = resolve_section_word_budget(rule, node, None)
            resolved_budgets[node.section_id] = w_meta

            for ev_id in node.assigned_evidence_ids:
                SourcePolicyEvaluator.assert_evidence(state, [ev_id])
                ev = state.evidence_ledger.items.get(ev_id)
                if ev is None:
                    raise ContractValidationError(
                        f"Outline section '{node.section_id}' references unknown evidence_id '{ev_id}'.",
                        code="EVIDENCE_UNVERIFIED",
                    )
                if ev.excerpt_match_status != "matched":
                    raise ContractValidationError(
                        f"Outline section '{node.section_id}' references unverified evidence_id '{ev_id}' "
                        f"(excerpt_match_status={ev.excerpt_match_status}). Only matched evidence may be bound.",
                        code="EVIDENCE_UNVERIFIED",
                    )
            parsed_nodes.append(node)

        required_ids = [s.section_id for s in state.profile.structure_profile.required_sections]
        missing_required = [sid for sid in required_ids if sid not in seen_ids]
        if missing_required:
            raise ContractValidationError(
                f"Submitted outline is missing required profile sections: {missing_required}."
            )

        new_rev = state.outline_graph.outline_revision + 1
        canonical_payload = json.dumps(
            {
                "global_thesis": global_thesis.strip(),
                "nodes": [n.model_dump(mode="json") for n in parsed_nodes],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        outline_hash = compute_text_hash(canonical_payload)

        state.outline_graph.outline_revision = new_rev
        state.outline_graph.outline_hash = outline_hash
        state.outline_graph.global_thesis = global_thesis.strip()
        state.outline_graph.nodes = parsed_nodes
        state.outline_graph.word_budget_origins = budget_origins

        if terminology:
            state.terminology.terms.update(terminology)
        if symbol_table:
            state.terminology.symbol_table.update(symbol_table)

        for node in parsed_nodes:
            if node.section_id not in state.section_versions.sections:
                state.section_versions.sections[node.section_id] = SectionState(
                    section_id=node.section_id,
                    title=node.title,
                    revision=0,
                )
            else:
                state.section_versions.sections[node.section_id].title = node.title

        invalidated_approvals: list[str] = []
        for gate_key in ("outline", "final_draft"):
            existing_app = state.approvals.get(gate_key)
            if existing_app and existing_app.status == "approved":
                existing_app.status = "invalidated_by_edit"
                existing_app.invalidation_reason = f"Outline updated to revision {new_rev}."
                invalidated_approvals.append(gate_key)

        for req in state.approval_requests.values():
            if req.status == "pending" and req.artifact_type in {"outline", "final_draft"}:
                req.status = "stale"

        state.stage = "outline_pending_approval"
        return {
            "project_id": state.brief.project_id,
            "stage": state.stage,
            "outline_revision": new_rev,
            "outline_hash": outline_hash,
            "section_count": len(parsed_nodes),
            "resolved_word_budgets": resolved_budgets,
            "invalidated_approvals": invalidated_approvals,
        }

    @classmethod
    def submit_section(
        cls,
        state: ProjectWorkspaceState,
        submission: SectionSubmissionContract,
    ) -> dict[str, Any]:
        section_id = validate_safe_identifier(submission.section_id, "section_id")
        if submission.project_id != state.brief.project_id:
            raise ContractValidationError(
                f"Submission project_id '{submission.project_id}' does not match workspace project_id '{state.brief.project_id}'."
            )

        # 1. Check Outline Approval Gate when draft_mode == 'formal'
        outline_app = state.approvals.get("outline")
        outline_approved = (
            outline_app is not None
            and outline_app.status == "approved"
            and outline_app.content_hash == state.outline_graph.outline_hash
        )
        if submission.draft_mode == "formal" and not outline_approved:
            raise ApprovalConflictError(
                f"Cannot submit formal draft for section '{section_id}' before the current outline is approved by the user. "
                "Obtain outline approval first, or set draft_mode='draft_preapproval' for a preliminary draft.",
                code="APPROVAL_REQUIRED",
                details={
                    "project_id": state.brief.project_id,
                    "section_id": section_id,
                    "outline_revision": state.outline_graph.outline_revision,
                },
            )
        if outline_approved and state.outline_graph.nodes:
            outline_ids = [n.section_id for n in state.outline_graph.nodes]
            if section_id not in outline_ids:
                raise ContractValidationError(
                    f"Section '{section_id}' is not declared in the approved outline.",
                    code="SECTION_NOT_IN_OUTLINE",
                    details={
                        "project_id": state.brief.project_id,
                        "section_id": section_id,
                        "outline_section_ids": outline_ids,
                    },
                )

        # 2. Check forbidden modification scopes from DecisionLog before mutating state
        for scope in state.decision_log.forbidden_modification_scopes:
            if scope in {"global", f"section:{section_id}", section_id}:
                raise ContractValidationError(
                    f"Section '{section_id}' is locked by user forbidden_modification_scope '{scope}'."
                )

        existing_sec = state.section_versions.sections.get(section_id)
        current_rev = existing_sec.revision if existing_sec is not None else 0

        # 3. Optimistic concurrency lock check (base_revision)
        if submission.base_revision != current_rev:
            raise RevisionConflictError(
                f"Optimistic concurrency conflict on section '{section_id}': "
                f"submitted base_revision={submission.base_revision}, but current revision={current_rev}. "
                "Call get_section_context to fetch the latest revision before retrying.",
                details={
                    "project_id": state.brief.project_id,
                    "section_id": section_id,
                    "submitted_base_revision": submission.base_revision,
                    "current_revision": current_rev,
                },
            )

        # 4. Deterministic Context Fingerprint & context_revision check
        _, live_ctx_fp, live_ctx_rev = ContextManager.compute_section_dependency_snapshot(
            state, section_id
        )
        if submission.context_revision != live_ctx_rev or (
            submission.context_fingerprint and submission.context_fingerprint != live_ctx_fp
        ):
            raise ContextStaleError(
                f"Section context for '{section_id}' changed after it was retrieved.",
                details={
                    "error_code": "CONTEXT_STALE",
                    "project_id": state.brief.project_id,
                    "section_id": section_id,
                    "submitted_context_revision": submission.context_revision,
                    "current_context_revision": live_ctx_rev,
                    "current_context_fingerprint": live_ctx_fp,
                    "suggested_action": "Call get_section_context and regenerate or merge your draft.",
                },
            )

        # 5. Check locked text snippets in forbidden_modification_scopes
        if existing_sec and existing_sec.content_markdown:
            for scope in state.decision_log.forbidden_modification_scopes:
                if scope.startswith("locked_text:"):
                    locked_snippet = scope[len("locked_text:") :].strip()
                    if (
                        locked_snippet
                        and locked_snippet in existing_sec.content_markdown
                        and locked_snippet not in submission.content_markdown
                    ):
                        raise ContractValidationError(
                            f"Submission for section '{section_id}' modifies or removes user-locked text: '{locked_snippet}'."
                        )

        new_rev = current_rev + 1

        # 6. Parse content_markdown via Pandoc JSON AST and derive bindings + L2a checks
        from daf.core.contracts import ReviewIssue
        from daf.core.pandoc_ast import (
            analyze_markdown_with_pandoc,
            classify_uncited_sentence,
            hypothesis_has_false_experiment_marker,
        )

        parsed_sentences, ast_stats, _ast = analyze_markdown_with_pandoc(submission.content_markdown)
        bound_evidence_ids: list[str] = []
        enriched_bindings: list[SourceBinding] = []
        derived_claims: list[str] = []
        l2_warnings: list[dict[str, str]] = []
        uncited_issues: list[dict[str, Any]] = []
        extra_units = list(state.profile.content_profile.extra_physical_units)

        # Clear previous open UNCITED_FACTUAL_CLAIM issues for this section before re-evaluating
        for old_iss in state.review_ledger.issues:
            if (
                old_iss.target_section_id == section_id
                and old_iss.category == "UNCITED_FACTUAL_CLAIM"
                and old_iss.status == "open"
            ):
                old_iss.status = "resolved"
                old_iss.resolved_in_revision = new_rev

        for sent in parsed_sentences:
            if not sent.citation_ids and not sent.is_hypothesis:
                # R-A (narrowed R3): check uncited factual/comparative sentences
                sev = classify_uncited_sentence(sent.text, extra_units=extra_units)
                if sev is not None:
                    issue_id = f"uncited_{section_id}_r{new_rev}_s{sent.index:03d}"
                    issue_desc = (
                        f"Uncited factual/quantitative sentence in section '{section_id}': "
                        f"'{sent.text[:140]}'. Add inline citation [@ev_id] or mark as [@hypothesis]."
                    )
                    uncited_issues.append(
                        {
                            "issue_id": issue_id,
                            "category": "UNCITED_FACTUAL_CLAIM",
                            "severity": sev,
                            "sentence_index": sent.index,
                            "sentence_text": sent.text,
                            "description": issue_desc,
                        }
                    )
                    state.review_ledger.issues.append(
                        ReviewIssue(
                            issue_id=issue_id,
                            target_section_id=section_id,
                            severity=sev,
                            category="UNCITED_FACTUAL_CLAIM",
                            description=issue_desc,
                            suggested_fix="Add a verified inline citation [@ev_id] or mark the statement with [@hypothesis].",
                            status="open",
                        )
                    )
                continue

            derived_claims.append(sent.text)

            # R-D: hypothesis-only sentence
            if sent.is_hypothesis and not sent.citation_ids:
                bid = f"{section_id}_s{sent.index:03d}_{sent.sentence_hash[:8]}"
                b_copy = SourceBinding(
                    binding_id=bid,
                    section_id=section_id,
                    section_revision=new_rev,
                    claim_text=sent.text,
                    evidence_id="",
                    claim_type="research_hypothesis",
                )
                false_markers = hypothesis_has_false_experiment_marker(sent.text)
                if false_markers:
                    b_copy.rule_check_status = "rule_failed"
                    b_copy.semantic_support_status = "unsupported"
                    b_copy.rationale = (
                        f"Hypothesis sentence contains false experimental phrasing {false_markers}."
                    )
                    l2_warnings.append(
                        {
                            "binding_id": bid,
                            "rule_check_status": b_copy.rule_check_status,
                            "support_status": b_copy.support_status,
                            "rationale": b_copy.rationale,
                        }
                    )
                else:
                    sup_status, sup_reason = evaluate_claim_binding_support(b_copy, None)
                    b_copy.rationale = sup_reason
                enriched_bindings.append(b_copy)
                continue

            # Cited sentence: generate one SourceBinding per cited evidence_id
            for ev_idx, ev_id in enumerate(sent.citation_ids):
                ev = state.evidence_ledger.items.get(ev_id)
                # R-C: nonexistent or L1-unmatched evidence rejects submission
                if ev is None:
                    raise ContractValidationError(
                        f"Section '{section_id}' cites nonexistent evidence_id '{ev_id}'.",
                        code="EVIDENCE_UNVERIFIED",
                    )
                if ev.excerpt_match_status != "matched":
                    raise ContractValidationError(
                        f"Section '{section_id}' cites unverified/stale evidence_id '{ev_id}' "
                        f"(excerpt_match_status={ev.excerpt_match_status}). Unmatched evidence cannot be committed.",
                        code="EVIDENCE_UNVERIFIED",
                    )
                SourcePolicyEvaluator.assert_source(state, ev.source_id, "cite")

                suffix = "" if len(sent.citation_ids) == 1 else f"_{ev_idx:02d}"
                bid = f"{section_id}_s{sent.index:03d}_{sent.sentence_hash[:8]}{suffix}"
                b_copy = SourceBinding(
                    binding_id=bid,
                    section_id=section_id,
                    section_revision=new_rev,
                    claim_text=sent.text,
                    evidence_id=ev_id,
                    claim_type="research_hypothesis" if sent.is_hypothesis else "experimental_result",
                    evidence_quote_hash=ev.quote_hash,
                    semantic_support_status="pending",
                )
                sup_status, sup_reason = evaluate_claim_binding_support(b_copy, ev)
                b_copy.rationale = sup_reason
                if b_copy.rule_check_status == "rule_failed" or b_copy.support_status in {
                    "unsupported",
                    "partially_supported",
                }:
                    l2_warnings.append(
                        {
                            "binding_id": b_copy.binding_id,
                            "evidence_id": b_copy.evidence_id,
                            "rule_check_status": b_copy.rule_check_status,
                            "support_status": b_copy.support_status,
                            "rationale": sup_reason,
                        }
                    )
                if ev_id not in bound_evidence_ids:
                    bound_evidence_ids.append(ev_id)
                enriched_bindings.append(b_copy)

        old_markdown = existing_sec.content_markdown if existing_sec else ""
        diff_lines = list(
            difflib.unified_diff(
                old_markdown.splitlines(keepends=True),
                submission.content_markdown.splitlines(keepends=True),
                fromfile=f"{section_id}@r{current_rev}",
                tofile=f"{section_id}@r{new_rev}",
                n=2,
            )
        )
        diff_text = "".join(diff_lines)
        content_hash = compute_text_hash(submission.content_markdown)
        now_iso = datetime.now(timezone.utc).isoformat()

        outline_node = next((n for n in state.outline_graph.nodes if n.section_id == section_id), None)
        profile_rule = next(
            (
                r
                for r in (
                    state.profile.structure_profile.required_sections
                    + state.profile.structure_profile.optional_sections
                )
                if r.section_id == section_id
            ),
            None,
        )
        sec_title = (
            outline_node.title
            if outline_node
            else (profile_rule.title if profile_rule else (existing_sec.title if existing_sec else section_id))
        )

        rev_record = SectionRevisionRecord(
            revision=new_rev,
            base_revision=submission.base_revision,
            context_revision=submission.context_revision,
            context_fingerprint=live_ctx_fp,
            draft_mode=submission.draft_mode,
            idempotency_key=submission.idempotency_key,
            author_session=submission.author_session,
            content_hash=content_hash,
            content_markdown=submission.content_markdown,
            summary=submission.summary,
            claims=derived_claims,
            source_bindings=enriched_bindings,
            unresolved_questions=list(submission.unresolved_questions),
            diff_from_previous=diff_text,
            created_at=now_iso,
        )

        if existing_sec is None:
            existing_sec = SectionState(section_id=section_id, title=sec_title)
            state.section_versions.sections[section_id] = existing_sec

        existing_sec.title = sec_title
        existing_sec.revision = new_rev
        existing_sec.author_session = submission.author_session
        existing_sec.content_hash = content_hash
        existing_sec.context_revision_used = submission.context_revision
        existing_sec.context_fingerprint_used = live_ctx_fp
        existing_sec.is_preapproval_draft = submission.draft_mode == "draft_preapproval"
        existing_sec.content_markdown = submission.content_markdown
        existing_sec.summary = submission.summary
        existing_sec.claims = derived_claims
        existing_sec.source_bindings = enriched_bindings
        existing_sec.uncited_claim_issues = uncited_issues
        existing_sec.defined_labels = list(ast_stats.defined_labels)
        existing_sec.crossref_targets = list(ast_stats.crossref_targets)
        existing_sec.unresolved_questions = list(submission.unresolved_questions)
        existing_sec.needs_revalidation = False
        existing_sec.stale_reason = ""
        existing_sec.history.append(rev_record)

        # Mark resolved issues in ReviewLedger
        resolved_ids: list[str] = []
        for iss in state.review_ledger.issues:
            if iss.issue_id in submission.resolved_issue_ids and iss.status == "open":
                iss.status = "resolved"
                iss.resolved_in_revision = new_rev
                resolved_ids.append(iss.issue_id)

        # Invalidate approvals & semantic reviews for this section and final_draft
        invalidated_approvals: list[str] = []
        for gate_key in (f"section:{section_id}", "final_draft"):
            app_rec = state.approvals.get(gate_key)
            if app_rec and app_rec.status == "approved":
                app_rec.status = "invalidated_by_edit"
                app_rec.invalidation_reason = f"Section '{section_id}' updated to revision {new_rev}."
                invalidated_approvals.append(gate_key)

        for req in state.approval_requests.values():
            if req.status == "pending" and (
                req.artifact_type == "final_draft"
                or (req.artifact_type == "section" and req.target_id == section_id)
            ):
                req.status = "stale"

        # Mark downstream dependent sections as needs_revalidation=True
        downstream_marked: list[str] = []
        for other_id, other_sec in state.section_versions.sections.items():
            if other_id == section_id or other_sec.revision == 0:
                continue
            other_node = next((n for n in state.outline_graph.nodes if n.section_id == other_id), None)
            other_rule = next(
                (
                    r
                    for r in (
                        state.profile.structure_profile.required_sections
                        + state.profile.structure_profile.optional_sections
                    )
                    if r.section_id == other_id
                ),
                None,
            )
            deps = (
                list(other_node.depends_on)
                if other_node is not None
                else (list(other_rule.depends_on) if other_rule is not None else [])
            )
            if section_id in deps:
                other_sec.needs_revalidation = True
                other_sec.stale_reason = (
                    f"Upstream dependency '{section_id}' changed to revision {new_rev}."
                )
                downstream_marked.append(other_id)
                dep_app = state.approvals.get(f"section:{other_id}")
                if dep_app and dep_app.status == "approved":
                    dep_app.status = "invalidated_by_edit"
                    dep_app.invalidation_reason = (
                        f"Upstream section '{section_id}' was modified to revision {new_rev}."
                    )
                    invalidated_approvals.append(f"section:{other_id}")

        # Invalidate semantic review tasks/reports for this section & downstream sections
        stale_sec_set = {section_id, *downstream_marked}
        for s_id in stale_sec_set:
            state.review_ledger.reviewed_section_revisions.pop(s_id, None)
            rep = state.review_ledger.semantic_reports.get(s_id)
            if rep is not None:
                rep.status = "stale"
        for task in state.review_ledger.semantic_review_tasks:
            if task.section_id in stale_sec_set and task.status in {"pending", "claimed", "submitted", "accepted"}:
                task.status = "stale"
                task.stale_reason = f"Section '{section_id}' updated to revision {new_rev}."

        state.stage = "drafting_sections"
        return {
            "project_id": state.brief.project_id,
            "section_id": section_id,
            "new_revision": new_rev,
            "base_revision": submission.base_revision,
            "context_revision": submission.context_revision,
            "context_fingerprint": live_ctx_fp,
            "draft_mode": submission.draft_mode,
            "content_hash": content_hash,
            "word_count": count_effective_words(submission.content_markdown),
            "bound_evidence_ids": bound_evidence_ids,
            "source_bindings": [b.model_dump(mode="json") for b in enriched_bindings],
            "l2_claim_support_warnings": l2_warnings,
            "uncited_claim_issues": uncited_issues,
            "resolved_issue_ids": resolved_ids,
            "downstream_sections_marked_for_revalidation": downstream_marked,
            "invalidated_approvals": invalidated_approvals,
            "diff_line_count": len(diff_lines),
        }

    @classmethod
    def inspect_artifact_for_approval(
        cls,
        state: ProjectWorkspaceState,
        artifact_type: Literal["outline", "section", "final_draft"],
        target_id: str = "global",
        *,
        require_semantic_review_for_final: bool = True,
    ) -> tuple[str, int, str]:
        """Compute canonical (normalized_target_id, current_revision, current_content_hash) for an approval gate."""
        if artifact_type == "outline":
            for node in state.outline_graph.nodes:
                SourcePolicyEvaluator.assert_evidence(state, node.assigned_evidence_ids)
            if state.outline_graph.outline_revision == 0:
                raise ApprovalConflictError(
                    "Cannot approve outline before submit_outline has been called.",
                    code="APPROVAL_REQUIRED",
                )
            return "outline", state.outline_graph.outline_revision, state.outline_graph.outline_hash

        if artifact_type == "section":
            SourcePolicyEvaluator.assert_section(state, target_id)
            validate_safe_identifier(target_id, "target_id")
            sec = state.section_versions.sections.get(target_id)
            if sec is None or sec.revision == 0:
                raise ApprovalConflictError(
                    f"Cannot approve section '{target_id}' because it has no submitted draft.",
                    code="APPROVAL_REQUIRED",
                )
            return target_id, sec.revision, sec.content_hash

        if artifact_type == "final_draft":
            required_ids = [s.section_id for s in state.profile.structure_profile.required_sections]
            missing = [
                sid
                for sid in required_ids
                if sid not in state.section_versions.sections
                or state.section_versions.sections[sid].revision == 0
            ]
            if missing:
                raise ApprovalConflictError(
                    f"Cannot approve final_draft while required sections are undrafted: {missing}.",
                    code="APPROVAL_REQUIRED",
                )
            if require_semantic_review_for_final:
                assert_semantic_and_claim_reviews_ready(state)
            rev = sum(s.revision for s in state.section_versions.sections.values())
            chash = compute_final_draft_aggregate_hash(state)
            return "final_draft", rev, chash

        raise ValueError(f"Unsupported artifact_type: {artifact_type}")

    @classmethod
    def request_approval(
        cls,
        state: ProjectWorkspaceState,
        artifact_type: Literal["outline", "section", "final_draft"],
        target_id: str = "global",
        ttl_seconds: int = 3600,
    ) -> dict[str, Any]:
        """Agent-callable non-privileged approval request.
        Creates a pending ApprovalRequest and NEVER returns any secret confirmation token.
        """
        norm_target_id, rev, chash = cls.inspect_artifact_for_approval(
            state,
            artifact_type,
            target_id,
            require_semantic_review_for_final=True,
        )
        req = ProjectStore.create_approval_request_in_tx(
            state=state,
            artifact_type=artifact_type,
            target_id=norm_target_id,
            artifact_revision=rev,
            artifact_hash=chash,
            ttl_seconds=ttl_seconds,
        )
        return {
            "approval_request_id": req.approval_request_id,
            "project_id": req.project_id,
            "artifact_type": req.artifact_type,
            "target_id": req.target_id,
            "artifact_revision": req.artifact_revision,
            "artifact_hash": req.artifact_hash,
            "status": req.status,
            "requires_human_action": req.requires_human_action,
            "expires_at": req.expires_at,
        }

    @classmethod
    def get_approval_status(
        cls,
        state: ProjectWorkspaceState,
        approval_request_id: str | None = None,
        artifact_type: Literal["outline", "section", "final_draft"] | None = None,
        target_id: str = "global",
    ) -> dict[str, Any]:
        """Agent-callable non-privileged status query for an ApprovalRequest."""
        req = None
        if approval_request_id is not None:
            req = state.approval_requests.get(approval_request_id)
        elif artifact_type is not None:
            norm_target = "global" if artifact_type in {"outline", "final_draft"} else target_id
            matching = [
                r
                for r in state.approval_requests.values()
                if r.artifact_type == artifact_type and r.target_id == norm_target
            ]
            if matching:
                req = matching[-1]

        if req is None:
            raise ApprovalConflictError(
                f"ApprovalRequest '{approval_request_id or f'{artifact_type}:{target_id}'}' not found.",
                code="APPROVAL_REQUIRED",
                details={
                    "approval_request_id": approval_request_id,
                    "artifact_type": artifact_type,
                    "target_id": target_id,
                },
            )
        if req.status == "pending":
            now = datetime.now(timezone.utc)
            if now > datetime.fromisoformat(req.expires_at):
                req.status = "expired"
            else:
                try:
                    _, live_rev, live_hash = cls.inspect_artifact_for_approval(
                        state,
                        req.artifact_type,
                        req.target_id,
                        require_semantic_review_for_final=False,
                    )
                    norm_live_hash = live_hash if live_hash.startswith("sha256:") else f"sha256:{live_hash}"
                    if req.artifact_revision != live_rev or req.artifact_hash != norm_live_hash:
                        req.status = "stale"
                except Exception:
                    req.status = "stale"
        return req.model_dump(mode="json")

    @classmethod
    def record_human_approval_decision(
        cls,
        state: ProjectWorkspaceState,
        approval_request_id: str,
        decision: Literal["approved", "rejected"],
        human_actor_id: str = "human_user",
        confirmation_channel: Literal["local_ui", "local_cli"] = "local_cli",
        comment: str = "",
    ) -> dict[str, Any]:
        """Trusted confirmation domain entrypoint to approve or reject a pending ApprovalRequest."""
        req = state.approval_requests.get(approval_request_id)
        if req is None:
            raise ApprovalConflictError(
                f"ApprovalRequest '{approval_request_id}' not found.",
                code="APPROVAL_REQUIRED",
                details={"approval_request_id": approval_request_id},
            )

        norm_target_id, rev, chash = cls.inspect_artifact_for_approval(
            state,
            req.artifact_type,
            req.target_id,
            require_semantic_review_for_final=(decision == "approved"),
        )
        updated_req, decision_rec = ProjectStore.decide_approval_request_in_tx(
            state=state,
            approval_request_id=approval_request_id,
            decision=decision,
            human_actor_id=human_actor_id,
            confirmation_channel=confirmation_channel,
            current_revision=rev,
            current_hash=chash,
            comment=comment,
        )

        if req.artifact_type == "outline":
            gate_key = "outline"
            if decision == "approved" and state.stage == "outline_pending_approval":
                state.stage = "outline_approved"
        elif req.artifact_type == "section":
            gate_key = f"section:{norm_target_id}"
            if decision == "approved":
                sec = state.section_versions.sections[norm_target_id]
                sec.needs_revalidation = False
                sec.stale_reason = ""
        else:
            gate_key = "final_draft"
            if decision == "approved":
                state.stage = "final_approved"
            else:
                state.stage = "reviewing"

        approval_id = f"appr_{len(state.approvals) + 1:03d}_{rev}"
        record = ApprovalRecord(
            approval_id=approval_id,
            request_id=approval_request_id,
            artifact_type=req.artifact_type,
            target_id=norm_target_id,
            status=decision,
            approved_revision=rev,
            content_hash=chash,
            artifact_hash=f"sha256:{chash}" if not chash.startswith("sha256:") else chash,
            confirmed_via=confirmation_channel,
            confirmed_by=human_actor_id,
            confirmed_at=decision_rec.confirmed_at,
            audit_id=decision_rec.audit_id,
            comment=comment,
        )
        state.approvals[gate_key] = record
        if decision == "rejected" and comment:
            state.decision_log.entries.append(
                DecisionRecord(
                    decision_id=f"dec_{len(state.decision_log.entries) + 1:03d}",
                    source="approval_gate",
                    summary=f"Human rejected {req.artifact_type} ({norm_target_id}): {comment}",
                    scope=[norm_target_id]
                    if req.artifact_type == "section"
                    else [f"approval:{req.artifact_type}"],
                )
            )
        return {
            "status": decision,
            "approval_request": updated_req.model_dump(mode="json"),
            "approval_record": record.model_dump(mode="json"),
            "decision": decision_rec.model_dump(mode="json"),
        }

    @classmethod
    def record_agent_note(
        cls,
        state: ProjectWorkspaceState,
        note_text: str,
        target_section_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        """Agent-callable note recorder: writes source='agent_note' only; cannot touch user constraints or conflict acknowledgments."""
        if not note_text or not note_text.strip():
            raise ContractValidationError("note_text must not be empty.")
        targets = list(target_section_ids or [])
        for tid in targets:
            validate_safe_identifier(tid, "target_section_id")

        decision_id = f"note_{len(state.decision_log.entries) + 1:03d}"
        decision = DecisionRecord(
            decision_id=decision_id,
            source="agent_note",
            summary=note_text.strip(),
            scope=targets if targets else ["global"],
            forbidden_modification_scopes=[],
            adopted_as_constraint=False,
        )
        state.decision_log.entries.append(decision)
        return {
            "decision_id": decision_id,
            "source": "agent_note",
            "summary": decision.summary,
            "scope": decision.scope,
            "adopted_as_constraint": False,
        }

    @classmethod
    def add_user_decision(
        cls,
        state: ProjectWorkspaceState,
        feedback_text: str,
        target_section_ids: list[str] | None = None,
        add_user_constraints: list[str] | None = None,
        add_forbidden_scopes: list[str] | None = None,
        acknowledge_template_conflicts: list[str] | None = None,
        invalidate_target_approvals: bool = True,
    ) -> dict[str, Any]:
        """Local user-only decision recorder (CLI/UI): writes source='user' and may update constraints/scopes/template conflicts."""
        if not feedback_text or not feedback_text.strip():
            raise ContractValidationError("feedback_text must not be empty.")

        targets = list(target_section_ids or [])
        for tid in targets:
            validate_safe_identifier(tid, "target_section_id")

        decision_id = f"dec_{len(state.decision_log.entries) + 1:03d}"
        decision = DecisionRecord(
            decision_id=decision_id,
            source="user",
            summary=feedback_text.strip(),
            scope=targets if targets else ["global"],
            forbidden_modification_scopes=list(add_forbidden_scopes or []),
            adopted_as_constraint=True,
        )
        state.decision_log.entries.append(decision)

        for constraint in add_user_constraints or []:
            if constraint and constraint not in state.decision_log.active_user_constraints:
                state.decision_log.active_user_constraints.append(constraint)

        for scope in add_forbidden_scopes or []:
            if scope and scope not in state.decision_log.forbidden_modification_scopes:
                state.decision_log.forbidden_modification_scopes.append(scope)

        for ack in acknowledge_template_conflicts or []:
            if ack and ack not in state.decision_log.acknowledged_template_conflicts:
                state.decision_log.acknowledged_template_conflicts.append(ack)

        created_issues: list[str] = []
        invalidated_approvals: list[str] = []
        if targets:
            for tid in targets:
                issue_id = f"iss_user_{len(state.review_ledger.issues) + 1:03d}"
                issue = ReviewIssue(
                    issue_id=issue_id,
                    severity="major",
                    category="user_feedback",
                    target_section_id=tid,
                    description=f"User feedback requiring adjustment: {feedback_text.strip()}",
                    suggested_fix=feedback_text.strip(),
                    status="open",
                )
                state.review_ledger.issues.append(issue)
                created_issues.append(issue_id)

                if invalidate_target_approvals:
                    for gk in (f"section:{tid}", "final_draft"):
                        app = state.approvals.get(gk)
                        if app and app.status == "approved":
                            app.status = "invalidated_by_edit"
                            app.invalidation_reason = f"Invalidated by user decision ({decision_id})."
                            invalidated_approvals.append(gk)

        return {
            "decision_id": decision_id,
            "source": "user",
            "active_user_constraints": list(state.decision_log.active_user_constraints),
            "forbidden_modification_scopes": list(state.decision_log.forbidden_modification_scopes),
            "acknowledged_template_conflicts": list(state.decision_log.acknowledged_template_conflicts),
            "created_issue_ids": created_issues,
            "invalidated_approvals": invalidated_approvals,
        }

    @classmethod
    def adopt_agent_note(
        cls,
        state: ProjectWorkspaceState,
        decision_id: str,
    ) -> dict[str, Any]:
        """Promote an agent_note into active_user_constraints (local UI/CLI only)."""
        entry = next((e for e in state.decision_log.entries if e.decision_id == decision_id), None)
        if entry is None:
            raise ContractValidationError(f"Unknown note/decision '{decision_id}'.")
        entry.adopted_as_constraint = True
        if entry.summary not in state.decision_log.active_user_constraints:
            state.decision_log.active_user_constraints.append(entry.summary)
        return {
            "decision_id": decision_id,
            "adopted_as_constraint": True,
            "active_user_constraints": list(state.decision_log.active_user_constraints),
        }
