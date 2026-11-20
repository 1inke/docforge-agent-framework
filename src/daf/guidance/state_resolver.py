"""Pure task routing over the existing workflow and review guards."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from daf.core.contracts import ProjectWorkspaceState
from daf.core.errors import DAFError
from daf.guidance.policy import SourcePolicyEvaluator


@dataclass
class TaskResolution:
    readiness: str = "ready"
    task_status: str = "in_progress"
    tools: list[str] = field(default_factory=list)
    blockers: list[dict[str, Any]] = field(default_factory=list)
    next_action: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    def route(
        self,
        tool: str,
        params: dict[str, Any],
        reason: str,
        *,
        readiness: str = "ready",
        human: bool = False,
    ) -> TaskResolution:
        self.readiness = readiness
        self.task_status = {
            "blocked": "blocked",
            "stale": "stale",
            "needs_input": "not_started",
        }.get(readiness, "in_progress")
        self.tools = [tool]
        self.next_action = {"tool": tool, "params": params, "reason": reason}
        if readiness != "ready":
            self.blockers = [
                {"rule_id": reason, "next_tool": tool, "requires_human": human}
            ]
        return self

    def complete(self, reason: str) -> TaskResolution:
        self.task_status = "completed"
        self.tools = []
        self.next_action = {"tool": None, "params": {}, "reason": reason}
        return self


def resolve_task_state(
    state: ProjectWorkspaceState, task_kind: str, section_id: str | None
) -> TaskResolution:
    # Local imports avoid a second workflow implementation or import cycle.
    from daf.review_manager.reviewer import ReviewManager
    from daf.workflow_engine.state_machine import (
        assert_release_ready,
        assert_semantic_and_claim_reviews_ready,
        compute_final_draft_aggregate_hash,
    )

    view = state.model_copy(deep=True)
    ReviewManager.refresh_semantic_task_staleness(view)
    project = state.brief.project_id
    base = {"project_id": project}
    target = {**base, "section_id": section_id}
    result = TaskResolution()
    section = view.section_versions.sections.get(section_id or "")
    drafted = bool(section and section.revision)
    missing = [
        r.section_id
        for r in state.profile.structure_profile.required_sections
        if not (
            view.section_versions.sections.get(r.section_id)
            and view.section_versions.sections[r.section_id].revision
        )
    ]
    issues = [i for i in view.review_ledger.issues if i.status == "open"]

    def approval_action(kind: str) -> TaskResolution:
        previous = state.approvals.get(kind)
        if previous and previous.status == "rejected":
            return result.route(
                "get_revision_tasks",
                base,
                "APPROVAL_REJECTED",
                readiness="blocked",
                human=True,
            )
        # Only an exact live artifact request can be reused.
        expected = (
            state.outline_graph.outline_hash
            if kind == "outline"
            else compute_final_draft_aggregate_hash(state)
        )
        requests = [
            r
            for r in state.approval_requests.values()
            if r.artifact_type == kind
            and r.status == "pending"
            and r.artifact_hash.removeprefix("sha256:")
            == expected.removeprefix("sha256:")
        ]
        if requests:
            return result.route(
                "get_approval_status",
                {**base, "approval_request_id": requests[-1].approval_request_id},
                "APPROVAL_PENDING",
                readiness="blocked",
                human=True,
            )
        return result.route(
            "request_approval",
            {**base, "artifact_type": kind},
            "APPROVAL_REQUIRED",
            human=True,
        )

    if task_kind in {"source_analysis", "evidence_extraction"}:
        allowed = SourcePolicyEvaluator.allowed_ids(state, "discover")
        if not allowed:
            return result.route(
                "get_source_policy",
                base,
                "NO_AUTHORIZED_SOURCES",
                readiness="needs_input",
            )
        result.route("list_source_blocks", base, "READ_AUTHORIZED_SOURCES")
        result.tools.extend(["search_sources", "get_source_block"])
        if task_kind == "evidence_extraction":
            result.tools.append("register_evidence")
        result.metadata["existing_verified_evidence_ids"] = sorted(
            ev.evidence_id
            for ev in state.evidence_ledger.items.values()
            if ev.source_id in allowed and ev.excerpt_match_status == "matched"
        )
        result.metadata["completion_scope"] = (
            "No persisted analysis/extraction completion flag; existing evidence does not prove source coverage."
        )
        return result

    if task_kind == "plan_outline":
        if not state.outline_graph.nodes:
            result.route(
                "get_project_context",
                {**base, "task_type": "plan_outline"},
                "PLAN_OUTLINE",
            )
            result.tools.append("submit_outline")
            return result
        try:
            for node in state.outline_graph.nodes:
                SourcePolicyEvaluator.assert_evidence(state, node.assigned_evidence_ids)
        except DAFError:
            return result.route(
                "get_source_policy",
                base,
                "SOURCE_SCOPE_DENIED",
                readiness="blocked",
                human=True,
            )
        approval = state.approvals.get("outline")
        if (
            approval
            and approval.status == "approved"
            and approval.content_hash == state.outline_graph.outline_hash
        ):
            return result.complete("OUTLINE_APPROVED")
        if approval and approval.status == "rejected":
            result.route(
                "get_project_context",
                {**base, "task_type": "plan_outline"},
                "OUTLINE_REJECTED",
                readiness="needs_input",
            )
            result.tools.append("submit_outline")
            return result
        return approval_action("outline")

    if task_kind in {"draft_section", "revise_section"}:
        if any(
            scope in {"global", section_id, f"section:{section_id}"}
            for scope in state.decision_log.forbidden_modification_scopes
        ):
            return result.route(
                "get_revision_tasks",
                base,
                "FORBIDDEN_MODIFICATION_SCOPE",
                readiness="blocked",
                human=True,
            )
        if not any(n.section_id == section_id for n in state.outline_graph.nodes):
            return result.route(
                "get_project_context",
                {**base, "task_type": "plan_outline"},
                "OUTLINE_REQUIRED",
                readiness="needs_input",
            )
        if task_kind == "revise_section" and not drafted:
            return result.route(
                "get_task_guidance",
                {**target, "task_kind": "draft_section"},
                "SECTION_DRAFT_REQUIRED",
                readiness="needs_input",
            )
        if task_kind == "draft_section" and drafted and not section.needs_revalidation:
            return result.complete("SECTION_ALREADY_DRAFTED")
        if section and section.needs_revalidation:
            return result.route(
                "get_section_context",
                {**target, "action": "revise"},
                "CONTEXT_STALE",
                readiness="stale",
            )
        if task_kind == "revise_section" and not any(
            i.target_section_id in {None, "global", section_id} for i in issues
        ):
            accepted = any(
                t.section_id == section_id and t.status == "accepted"
                for t in view.review_ledger.semantic_review_tasks
            )
            if accepted:
                return result.complete("NO_PENDING_REVISION")
        result.route(
            "get_section_context",
            {
                **target,
                "action": "revise" if task_kind == "revise_section" else "draft",
            },
            "READ_CURRENT_SECTION_CONTEXT",
        )
        result.tools.append("submit_section")
        return result

    if task_kind == "review_section":
        if not drafted:
            return result.route(
                "get_task_guidance",
                {**target, "task_kind": "draft_section"},
                "SECTION_DRAFT_REQUIRED",
                readiness="needs_input",
            )
        result.metadata["required_claim_binding_ids"] = [
            b.binding_id
            for b in section.source_bindings
            if b.claim_type != "research_hypothesis"
        ]
        tasks = [
            t
            for t in view.review_ledger.semantic_review_tasks
            if t.section_id == section_id or section_id in t.target_section_ids
        ]
        current = [
            t
            for t in tasks
            if t.target_section_revision == section.revision
            and t.status in {"pending", "claimed", "submitted", "accepted"}
        ]
        task = current[-1] if current else (tasks[-1] if tasks else None)
        if task:
            result.metadata["review_tasks"] = [
                {
                    "task_id": task.task_id,
                    "status": task.status,
                    "checklist_hash": task.checklist_hash,
                    "checklist_items": [
                        c.model_dump(mode="json") for c in task.checklist_items
                    ],
                }
            ]
        if section.needs_revalidation:
            return result.route(
                "get_section_context",
                {**target, "action": "revise"},
                "CONTEXT_STALE",
                readiness="stale",
            )
        if not task or task.status in {"stale", "expired"}:
            return result.route(
                "review_document",
                {**base, "target_section_ids": [section_id]},
                "SEMANTIC_REVIEW_STALE" if task else "SEMANTIC_REVIEW_REQUIRED",
                readiness="stale" if task else "needs_input",
            )
        if task.status == "accepted":
            if task.requires_structured_checklist and (
                not task.report
                or task.report.recommendation != "approve"
                or task.report.checklist_verification_mode != "structured_verified"
            ):
                return result.route(
                    "review_document",
                    {**base, "target_section_ids": [section_id]},
                    "REVIEW_CONTRACT_STALE",
                    readiness="stale",
                )
            return result.complete("SECTION_REVIEW_ACCEPTED")
        if task.status == "rejected":
            return result.route(
                "get_revision_tasks",
                base,
                "SEMANTIC_REVIEW_REJECTED",
                readiness="blocked",
            )
        params = {**base, "task_id": task.task_id}
        if task.status == "submitted":
            report = task.report
            if (
                report
                and task.requires_structured_checklist
                and report.recommendation is None
            ):
                return result.route(
                    "submit_semantic_review",
                    params,
                    "REVIEW_DECISION_REQUIRED",
                    readiness="needs_input",
                )
            if (
                not report
                or report.recommendation in {"revise", "block"}
                or any(f.severity in {"blocker", "major"} for f in report.findings)
                or any(
                    c.verdict in {"fail", "needs_revision"}
                    for c in report.checklist_results
                )
            ):
                return result.route(
                    "get_revision_tasks",
                    base,
                    "REVIEW_REQUIRES_REVISION",
                    readiness="blocked",
                )
            required = set(result.metadata["required_claim_binding_ids"])
            evaluated = {str(e.get("binding_id")) for e in report.claim_evaluations}
            if required - evaluated:
                return result.route(
                    "submit_semantic_review",
                    params,
                    "CLAIM_REVIEW_PENDING",
                    readiness="needs_input",
                )
            return result.route(
                "accept_semantic_review", params, "ACCEPT_SUBMITTED_REVIEW"
            )
        result.route(
            "claim_semantic_review_task"
            if task.status == "pending"
            else "submit_semantic_review",
            params,
            "CLAIM_REVIEW_TASK" if task.status == "pending" else "SUBMIT_REVIEW_REPORT",
        )
        result.tools.append("get_section_content")
        return result

    if not state.outline_graph.nodes:
        return result.route(
            "get_project_context",
            {**base, "task_type": "plan_outline"},
            "OUTLINE_REQUIRED",
            readiness="needs_input",
        )
    if missing:
        return result.route(
            "get_task_guidance",
            {**base, "task_kind": "draft_section", "section_id": missing[0]},
            "REQUIRED_SECTIONS_UNDRAFTED",
            readiness="needs_input",
        )
    if any(
        s.needs_revalidation
        for s in view.section_versions.sections.values()
        if s.revision
    ):
        return result.route(
            "get_revision_tasks", base, "CONTEXT_STALE", readiness="stale"
        )
    if task_kind == "release_preflight":
        result.metadata["preview_action"] = {
            "tool": "render_document",
            "params": {**base, "purpose": "preview"},
        }
        if not state.outline_graph.nodes:
            return result.route(
                "get_project_context",
                {**base, "task_type": "plan_outline"},
                "OUTLINE_REQUIRED",
                readiness="needs_input",
            )
        outline = state.approvals.get("outline")
        if (
            not outline
            or outline.status != "approved"
            or outline.content_hash != state.outline_graph.outline_hash
        ):
            return approval_action("outline")
    if any(i.severity in {"blocker", "major"} for i in issues):
        return result.route(
            "get_revision_tasks", base, "OPEN_REVIEW_ISSUES", readiness="blocked"
        )
    try:
        assert_semantic_and_claim_reviews_ready(view)
    except DAFError as exc:
        pending = [
            t
            for t in view.review_ledger.semantic_review_tasks
            if t.status in {"pending", "claimed", "submitted", "stale", "rejected"}
        ]
        if pending:
            return result.route(
                "get_task_guidance",
                {
                    **base,
                    "task_kind": "review_section",
                    "section_id": pending[0].section_id,
                },
                exc.code,
                readiness="stale" if pending[0].status == "stale" else "needs_input",
            )
        return result.route(
            "get_source_policy"
            if exc.code == "SOURCE_SCOPE_DENIED"
            else "review_document",
            base,
            exc.code,
            readiness="blocked" if exc.code == "SOURCE_SCOPE_DENIED" else "needs_input",
        )
    if task_kind == "global_review":
        return result.complete("GLOBAL_REVIEW_COMPLETE")
    try:
        assert_release_ready(view)
    except DAFError as exc:
        if exc.code in {"APPROVAL_REQUIRED", "APPROVAL_STALE"}:
            return approval_action("final_draft")
        return result.route(
            exc.recommended_tool or "get_project_status",
            base,
            exc.code,
            readiness="blocked",
        )
    manifest = state.last_render_manifest
    if (
        manifest
        and manifest.purpose == "release"
        and manifest.artifact_status == "committed"
        and manifest.document_content_hash.removeprefix("sha256:")
        == compute_final_draft_aggregate_hash(state)
    ):
        if state.stage == "exported":
            return result.complete("RELEASE_EXPORTED")
        return result.route(
            "export_document",
            {**base, "purpose": "release"},
            "EXPORT_COMMITTED_RELEASE",
        )
    return result.route(
        "render_document", {**base, "purpose": "release"}, "RENDER_APPROVED_RELEASE"
    )
