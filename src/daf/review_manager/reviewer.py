"""Hybrid Review Manager: Deterministic Rule QA (L1/L2a) + Stateful Agent Semantic Review Lifecycle (L2b)."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from daf.context_manager.manager import (
    JINJA_TAG_PATTERN,
    ContextManager,
    evaluate_claim_binding_support,
    normalize_for_verification,
    record_semantic_support_review,
    resolve_section_word_budget,
    run_claim_rule_checks,
)
from daf.core.contracts import (
    ChecklistItemResult,
    ChecklistItemSpec,
    ProjectWorkspaceState,
    ReviewIssue,
    SemanticReviewFinding,
    SemanticReviewReport,
    SemanticReviewTask,
)
from daf.core.errors import (
    ApprovalConflictError,
    ContractValidationError,
    ReviewChecklistIncompleteError,
    SemanticReviewRequiredError,
    SemanticReviewStaleError,
)
from daf.core.pandoc_ast import analyze_markdown_with_pandoc
from daf.guidance.policy import SourcePolicyEvaluator
from daf.workflow_engine.state_machine import count_effective_words, effective_section_ids

UNBALANCED_LATEX_INLINE = re.compile(r"(?<!\\)\$")

DEFAULT_FALLBACK_CHECKLIST: list[str] = [
    "论证链条是否严密自洽？",
    "术语与前文是否完全一致？",
    "所有事实主张是否均由所绑定证据实质支撑？",
]


class ReviewManager:
    """Executes deterministic QA rules (L1/L2a) and manages the pending -> claimed -> submitted -> accepted semantic review lifecycle (/03)."""

    @staticmethod
    def _effective_checklist_for_section(state: ProjectWorkspaceState, sec_id: str) -> list[str]:
        rule = next(
            (
                r
                for r in (
                    state.profile.structure_profile.required_sections
                    + state.profile.structure_profile.optional_sections
                )
                if r.section_id == sec_id
            ),
            None,
        )
        return (
            list(rule.review_checklist)
            if rule and rule.review_checklist
            else list(DEFAULT_FALLBACK_CHECKLIST)
        )

    @classmethod
    def refresh_semantic_task_staleness(cls, state: ProjectWorkspaceState) -> None:
        """Mark any semantic review task or report as 'stale' if its target section, evidence, constraints, or checklist questions changed."""
        for task in state.review_ledger.semantic_review_tasks:
            if task.status in {"stale", "expired", "rejected"}:
                continue
            sec_id = task.section_id
            if not sec_id:
                continue
            sec_state = state.section_versions.sections.get(sec_id)
            if sec_state is None or sec_state.revision == 0:
                task.status = "stale"
                task.stale_reason = f"Section '{sec_id}' has no active draft."
                state.review_ledger.reviewed_section_revisions.pop(sec_id, None)
                continue

            _, current_fp, _ = ContextManager.compute_section_dependency_snapshot(state, sec_id)
            expected_questions = cls._effective_checklist_for_section(state, sec_id)
            task_questions = [c.question for c in task.checklist_items]
            checklist_changed = bool(task_questions and task_questions != expected_questions)
            if (
                task.target_section_revision != sec_state.revision
                or (task.target_section_content_hash and task.target_section_content_hash != sec_state.content_hash)
                or (task.target_context_fingerprint and task.target_context_fingerprint != current_fp)
                or sec_state.needs_revalidation
                or checklist_changed
            ):
                task.status = "stale"
                task.stale_reason = (
                    "Section review_checklist questions changed in profile."
                    if checklist_changed
                    else (
                        sec_state.stale_reason
                        or f"Section '{sec_id}' revision/hash or dependency context_fingerprint changed."
                    )
                )
                if task.report is not None:
                    task.report.status = "stale"
                rep = state.review_ledger.semantic_reports.get(sec_id)
                if rep is not None:
                    rep.status = "stale"
                state.review_ledger.reviewed_section_revisions.pop(sec_id, None)

    @classmethod
    def run_review(
        cls,
        state: ProjectWorkspaceState,
        target_section_ids: list[str] | None = None,
        agent_semantic_issues: list[dict[str, Any] | ReviewIssue] | None = None,
        claim_support_assessments: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Run deterministic QA checks (L1 + L2a) and create/preserve `pending` SemanticReviewTasks.

        Invariants:
        - NEVER marks newly created SemanticReviewTasks as `completed` or `accepted` on creation.
        - NEVER promotes factual SourceBindings to `supported_reviewed` without an accepted semantic review.
        """
        now_iso = datetime.now(timezone.utc).isoformat()
        state.review_ledger.last_reviewed_at = now_iso

        sections_to_check = (
            target_section_ids
            if target_section_ids
            else effective_section_ids(state)
        )
        for section_id in sections_to_check:
            SourcePolicyEvaluator.assert_section(state, section_id)

        # Note: Direct caller-supplied `claim_support_assessments` in `run_review` may only flag negative issues
        # (`unsupported` / `partially_supported`), NEVER bypass L2b `submit_semantic_review` to force `supported_reviewed`!
        assessment_by_binding: dict[str, dict[str, Any]] = {}
        for item in claim_support_assessments or []:
            bid = str(item.get("binding_id") or "")
            if bid:
                assessment_by_binding[bid] = item

        retained_issues: list[ReviewIssue] = [
            iss
            for iss in state.review_ledger.issues
            if iss.status == "resolved"
            or iss.category in {"user_feedback", "UNCITED_FACTUAL_CLAIM"}
            or (iss.target_section_id not in sections_to_check and iss.target_section_id is not None)
        ]

        new_issues: list[ReviewIssue] = []
        issue_seq = len(retained_issues) + 1

        def add_issue(
            severity: str,
            category: str,
            sec_id: str | None,
            description: str,
            suggested_fix: str,
            evidence_ids: list[str] | None = None,
        ) -> None:
            nonlocal issue_seq
            new_issues.append(
                ReviewIssue(
                    issue_id=f"iss_qa_{issue_seq:03d}",
                    severity=severity,  # type: ignore[arg-type]
                    category=category,  # type: ignore[arg-type]
                    target_section_id=sec_id,
                    description=description,
                    suggested_fix=suggested_fix,
                    evidence_ids=list(evidence_ids or []),
                    status="open",
                )
            )
            issue_seq += 1

        required_map = {
            s.section_id: s for s in state.profile.structure_profile.required_sections
        }
        optional_map = {
            s.section_id: s for s in state.profile.structure_profile.optional_sections
        }
        outline_map = {n.section_id: n for n in state.outline_graph.nodes}

        for sec_id in sections_to_check:
            rule = required_map.get(sec_id) or optional_map.get(sec_id)
            node = outline_map.get(sec_id)
            sec_state = state.section_versions.sections.get(sec_id)

            # 1. Missing required section check
            if sec_id in required_map and (sec_state is None or sec_state.revision == 0):
                add_issue(
                    severity="blocker",
                    category="structure_completeness",
                    sec_id=sec_id,
                    description=f"Required section '{sec_id}' has not been drafted yet.",
                    suggested_fix=f"Call get_section_context and submit_section for '{sec_id}'.",
                )
                continue

            if sec_state is None or sec_state.revision == 0:
                continue

            text = sec_state.content_markdown
            words = count_effective_words(text)

            # 2. Word budget check (explicit-override-only inheritance)
            min_w, max_w, _ = resolve_section_word_budget(rule, node, state.outline_graph)
            if words < min_w:
                add_issue(
                    severity="major",
                    category="word_budget",
                    sec_id=sec_id,
                    description=(
                        f"Section '{sec_id}' word count ({words}) is below minimum threshold ({min_w})."
                    ),
                    suggested_fix=f"Expand technical depth, evidence analysis, or methodology details to reach at least {min_w} words.",
                )
            elif words > max_w:
                add_issue(
                    severity="minor",
                    category="word_budget",
                    sec_id=sec_id,
                    description=(
                        f"Section '{sec_id}' word count ({words}) exceeds maximum threshold ({max_w})."
                    ),
                    suggested_fix=f"Condense redundant prose to stay within {max_w} words.",
                )

            # 3. Forbidden phrases check
            for phrase in state.profile.content_profile.forbidden_phrases:
                if phrase and phrase in text:
                    add_issue(
                        severity="blocker",
                        category="forbidden_phrase",
                        sec_id=sec_id,
                        description=f"Section '{sec_id}' contains forbidden phrase '{phrase}'.",
                        suggested_fix=f"Remove '{phrase}' and replace with objective, evidence-backed statements.",
                    )

            # 4. 3-Layer Evidence & L2a Deterministic Rule Checks
            if state.profile.content_profile.evidence_density == "high" and not sec_state.source_bindings:
                add_issue(
                    severity="major",
                    category="evidence_binding",
                    sec_id=sec_id,
                    description=(
                        f"Section '{sec_id}' has zero bound evidence items despite profile requiring high evidence density."
                    ),
                    suggested_fix="Bind matched EvidenceItem IDs in source_bindings when calling submit_section.",
                )

            for binding in sec_state.source_bindings:
                if binding.binding_id in assessment_by_binding:
                    assess = assessment_by_binding[binding.binding_id]
                    req_status = str(assess.get("semantic_support_status") or "")
                    if req_status in {"unsupported", "partially_supported"}:
                        binding.semantic_support_status = req_status  # type: ignore[assignment]
                    if "rationale" in assess:
                        binding.rationale = str(assess["rationale"])

                if binding.claim_type == "research_hypothesis" and not binding.evidence_id:
                    sup_status, sup_reason = evaluate_claim_binding_support(binding, None)
                    binding.rationale = sup_reason
                    if binding.rule_check_status == "rule_failed":
                        add_issue(
                            severity="blocker",
                            category="claim_support",
                            sec_id=sec_id,
                            description=(
                                f"Section '{sec_id}' hypothesis binding '{binding.binding_id}' failed L2a check: {sup_reason}"
                            ),
                            suggested_fix="Phrase unverified hypotheses as research hypotheses rather than completed experimental results, or bind verified experimental evidence.",
                        )
                    continue

                ev = state.evidence_ledger.items.get(binding.evidence_id)
                if ev is None or ev.excerpt_match_status != "matched":
                    binding.rule_check_status = "rule_failed"
                    binding.semantic_support_status = (
                        "stale" if (ev and ev.excerpt_match_status in {"source_stale", "stale"}) else "unsupported"
                    )
                    binding.reviewed_against_revision = None
                    binding.reviewed_against_section_hash = ""
                    binding.reviewed_against_evidence_fingerprint = ""
                    add_issue(
                        severity="blocker",
                        category="evidence_binding",
                        sec_id=sec_id,
                        description=(
                            f"Section '{sec_id}' binds to unmatched or stale evidence '{binding.evidence_id}' "
                            f"(L1 status={ev.excerpt_match_status if ev else 'missing'})."
                        ),
                        suggested_fix="Re-ingest/re-verify evidence or replace with a matched evidence_id from EvidenceLedger.",
                        evidence_ids=[binding.evidence_id] if binding.evidence_id else [],
                    )
                    continue

                det_status, det_reason = evaluate_claim_binding_support(binding, ev)
                binding.rationale = det_reason

                if binding.rule_check_status == "rule_failed" or det_status in {"unsupported", "partially_supported", "stale"}:
                    add_issue(
                        severity="blocker",
                        category="claim_support",
                        sec_id=sec_id,
                        description=(
                            f"Section '{sec_id}' claim binding '{binding.binding_id}' (evidence '{binding.evidence_id}') "
                            f"has status='{det_status}' (L2a={binding.rule_check_status}): {det_reason}"
                        ),
                        suggested_fix="Align claim_text strictly with the quantitative metrics and scope of the bound evidence quote_excerpt.",
                        evidence_ids=[binding.evidence_id],
                    )

            # 5. Downstream revalidation check
            if sec_state.needs_revalidation:
                add_issue(
                    severity="major",
                    category="cross_section_consistency",
                    sec_id=sec_id,
                    description=(
                        f"Section '{sec_id}' is marked needs_revalidation: {sec_state.stale_reason}"
                    ),
                    suggested_fix=f"Inspect upstream changes via get_section_context('{sec_id}') and submit an aligned revision.",
                )

            # 6. LaTeX & template placeholder leak checks
            if JINJA_TAG_PATTERN.search(text):
                add_issue(
                    severity="blocker",
                    category="format_equation",
                    sec_id=sec_id,
                    description=f"Section '{sec_id}' contains raw '{{{{ ... }}}}' template placeholder syntax.",
                    suggested_fix="Remove raw Jinja placeholders from section prose.",
                )

            text_without_display = re.sub(r"\$\$[\s\S]*?\$\$", "", text)
            inline_dollars = UNBALANCED_LATEX_INLINE.findall(text_without_display)
            if len(inline_dollars) % 2 != 0:
                add_issue(
                    severity="major",
                    category="format_equation",
                    sec_id=sec_id,
                    description=f"Section '{sec_id}' has unbalanced inline '$' math delimiters.",
                    suggested_fix="Ensure every inline LaTeX formula is enclosed in balanced '$...$' delimiters.",
                )

            # Record deterministic rule check revision (distinct from accepted semantic review!)
            state.review_ledger.deterministic_checked_revisions[sec_id] = sec_state.revision

        # Merge external Agent semantic review issues if supplied directly
        for raw_sem in agent_semantic_issues or []:
            sem_issue = (
                raw_sem
                if isinstance(raw_sem, ReviewIssue)
                else ReviewIssue.model_validate(raw_sem)
            )
            new_issues.append(sem_issue)

        state.review_ledger.issues = retained_issues + new_issues

        # Refresh existing semantic review tasks and create pending tasks for unreviewed/updated sections
        cls.refresh_semantic_task_staleness(state)
        existing_tasks_by_id = {t.task_id: t for t in state.review_ledger.semantic_review_tasks}
        updated_tasks: list[SemanticReviewTask] = [
            t
            for t in state.review_ledger.semantic_review_tasks
            if t.section_id not in sections_to_check
        ]

        for sec_id in sections_to_check:
            sec_state = state.section_versions.sections.get(sec_id)
            if sec_state is None or sec_state.revision == 0:
                continue
            _, current_fp, _ = ContextManager.compute_section_dependency_snapshot(state, sec_id)
            task_id = f"sem_rev_{sec_id}_r{sec_state.revision}"
            existing_task = existing_tasks_by_id.get(task_id)

            if (
                existing_task is not None
                and existing_task.status == "accepted"
                and (not existing_task.requires_structured_checklist or (
                    existing_task.report is not None
                    and existing_task.report.recommendation == "approve"
                    and existing_task.report.checklist_verification_mode == "structured_verified"
                ))
                and existing_task.target_section_revision == sec_state.revision
                and existing_task.target_section_content_hash == sec_state.content_hash
                and existing_task.target_context_fingerprint == current_fp
                and not sec_state.needs_revalidation
            ):
                updated_tasks.append(existing_task)
                state.review_ledger.reviewed_section_revisions[sec_id] = sec_state.revision
                continue

            # Mark any older tasks for this section as stale
            for old_t in state.review_ledger.semantic_review_tasks:
                old_sec = old_t.section_id
                if old_sec == sec_id and old_t.task_id != task_id and old_t.status not in {"stale", "expired", "rejected"}:
                    old_t.status = "stale"
                    old_t.stale_reason = f"Superseded by task '{task_id}'."

            state.review_ledger.reviewed_section_revisions.pop(sec_id, None)
            checklist = cls._effective_checklist_for_section(state, sec_id)
            checklist_items = [
                ChecklistItemSpec(
                    check_id=f"{task_id}:q{idx:02d}",
                    question=q,
                    required=True,
                )
                for idx, q in enumerate(checklist, start=1)
            ]
            updated_tasks.append(
                SemanticReviewTask(
                    task_id=task_id,
                    requires_structured_checklist=True,
                    section_id=sec_id,
                    target_section_revision=sec_state.revision,
                    target_section_content_hash=sec_state.content_hash,
                    target_context_fingerprint=current_fp,
                    review_dimension="section_argumentation_and_consistency",
                    checklist_items=checklist_items,
                    evaluation_rubric=(
                        "Evaluate whether the section satisfies all required elements, answers every checklist item, "
                        "uses verified evidence accurately without overclaiming (L2b semantic support), and aligns with upstream sections."
                    ),
                    status="pending",
                    completed_at=None,
                )
            )

        state.review_ledger.semantic_review_tasks = updated_tasks
        state.stage = "reviewing"

        open_issues = [i for i in state.review_ledger.issues if i.status == "open"]
        blockers = [i for i in open_issues if i.severity == "blocker"]
        majors = [i for i in open_issues if i.severity == "major"]
        minors = [i for i in open_issues if i.severity == "minor"]

        all_required_semantically_accepted = all(
            state.review_ledger.reviewed_section_revisions.get(req_sec_id)
            == state.section_versions.sections[req_sec_id].revision
            for req_sec_id in required_map
            if req_sec_id in state.section_versions.sections
            and state.section_versions.sections[req_sec_id].revision > 0
        ) and all(
            req_sec_id in state.section_versions.sections
            and state.section_versions.sections[req_sec_id].revision > 0
            for req_sec_id in required_map
        )

        return {
            "project_id": state.brief.project_id,
            "reviewed_at": now_iso,
            "ready_for_final_approval": (
                len(blockers) == 0
                and len(majors) == 0
                and all_required_semantically_accepted
            ),
            "deterministic_checked_revisions": dict(state.review_ledger.deterministic_checked_revisions),
            "reviewed_section_revisions": dict(state.review_ledger.reviewed_section_revisions),
            "open_issue_counts": {
                "blocker": len(blockers),
                "major": len(majors),
                "minor": len(minors),
                "total_open": len(open_issues),
            },
            "open_issues": [i.model_dump(mode="json") for i in open_issues],
            "semantic_review_tasks": [t.model_dump(mode="json") for t in updated_tasks],
            "pending_semantic_review_tasks": [
                t.model_dump(mode="json") for t in updated_tasks if t.status != "accepted"
            ],
        }

    @classmethod
    def list_semantic_review_tasks(
        cls,
        state: ProjectWorkspaceState,
        status: str | None = None,
    ) -> dict[str, Any]:
        """List semantic review tasks after refreshing staleness against current state."""
        cls.refresh_semantic_task_staleness(state)
        tasks = state.review_ledger.semantic_review_tasks
        if status:
            tasks = [t for t in tasks if t.status == status]

        enriched: list[dict[str, Any]] = []
        for t in tasks:
            d = t.model_dump(mode="json")
            sec_id = t.section_id
            sec = state.section_versions.sections.get(sec_id)
            if sec:
                d["pending_claim_bindings"] = [
                    {
                        "binding_id": b.binding_id,
                        "claim_text": b.claim_text,
                        "claim_type": b.claim_type,
                        "evidence_id": b.evidence_id,
                        "rule_check_status": b.rule_check_status,
                        "semantic_support_status": b.semantic_support_status,
                    }
                    for b in sec.source_bindings
                ]
            enriched.append(d)

        return {
            "project_id": state.brief.project_id,
            "tasks": enriched,
        }

    @classmethod
    def claim_semantic_review_task(
        cls,
        state: ProjectWorkspaceState,
        task_id: str,
        agent_session_id: str | None = None,
    ) -> dict[str, Any]:
        """Claim a pending semantic review task."""
        eff_reviewer = (agent_session_id or "").strip()
        if len(eff_reviewer) < 3:
            raise ContractValidationError(
                "reviewer_id / agent_session_id is required (>= 3 non-whitespace characters).",
                code="REVIEWER_ID_REQUIRED",
                details={"task_id": task_id},
            )
        cls.refresh_semantic_task_staleness(state)
        task = next((t for t in state.review_ledger.semantic_review_tasks if t.task_id == task_id), None)
        if task is None:
            raise ContractValidationError(
                f"SemanticReviewTask '{task_id}' not found.",
                code="SEMANTIC_REVIEW_REQUIRED",
                details={"task_id": task_id},
            )
        if task.status == "stale":
            raise SemanticReviewStaleError(
                f"SemanticReviewTask '{task_id}' is stale: {task.stale_reason}",
                details={"task_id": task_id, "stale_reason": task.stale_reason},
            )
        if task.status not in {"pending", "claimed"}:
            raise ContractValidationError(
                f"SemanticReviewTask '{task_id}' cannot be claimed from status '{task.status}'.",
                details={"task_id": task_id, "status": task.status},
            )
        # Prevent missing or identical author_session from claiming the section review
        sec_id = task.section_id
        sec_state = state.section_versions.sections.get(sec_id)
        eff_author = (sec_state.author_session or "").strip() if sec_state is not None else ""
        if sec_state is not None and (len(eff_author) < 3 or eff_author == eff_reviewer):
            raise ContractValidationError(
                f"Reviewer '{eff_reviewer}' cannot review section '{sec_id}' (missing/invalid or matching author_session '{eff_author}').",
                code="REVIEWER_IS_AUTHOR",
                details={
                    "task_id": task_id,
                    "section_id": sec_id,
                    "author_session": sec_state.author_session,
                    "reviewer_id": eff_reviewer,
                },
            )
        now_iso = datetime.now(timezone.utc).isoformat()
        task.status = "claimed"
        task.claimed_by = eff_reviewer
        task.claimed_at = now_iso
        return task.model_dump(mode="json")

    @classmethod
    def submit_semantic_review(
        cls,
        state: ProjectWorkspaceState,
        task_id: str,
        section_revision: int,
        context_fingerprint: str,
        findings: list[dict[str, Any] | SemanticReviewFinding] | None,
        review_summary: str,
        reviewer_agent: str | None = None,
        claim_evaluations: list[dict[str, Any]] | None = None,
        checklist_results: list[dict[str, Any] | ChecklistItemResult] | None = None,
        checklist_hash: str | None = None,
        require_structured_checklist: bool = True,
        recommendation: str | None = None,
    ) -> dict[str, Any]:
        """Submit a structured SemanticReviewReport for a specific task_id, revision, context_fingerprint, and checklist."""
        eff_reviewer = (reviewer_agent or "").strip()
        if len(eff_reviewer) < 3:
            raise ContractValidationError(
                "reviewer_id / reviewed_by is required (>= 3 non-whitespace characters).",
                code="REVIEWER_ID_REQUIRED",
                details={"task_id": task_id},
            )
        cls.refresh_semantic_task_staleness(state)
        task = next((t for t in state.review_ledger.semantic_review_tasks if t.task_id == task_id), None)
        if task is None:
            raise SemanticReviewRequiredError(
                f"SemanticReviewTask '{task_id}' does not exist. Run review_document first.",
                details={"task_id": task_id},
            )
        if task.status == "stale":
            raise SemanticReviewStaleError(
                f"SemanticReviewTask '{task_id}' is stale ({task.stale_reason}). Run review_document again.",
                details={"task_id": task_id, "stale_reason": task.stale_reason},
            )
        if task.status == "expired":
            raise SemanticReviewStaleError(
                f"SemanticReviewTask '{task_id}' has expired.",
                details={"task_id": task_id},
            )

        sec_id = task.section_id
        SourcePolicyEvaluator.assert_section(state, sec_id)
        require_structured_checklist = require_structured_checklist or task.requires_structured_checklist
        missing = [name for name, value in (("recommendation", recommendation), ("findings", findings)) if value is None]
        if missing:
            raise ContractValidationError("必须显式提交 recommendation 和 findings（允许空列表）。", details={"missing_fields": missing})
        if recommendation not in {"approve", "revise", "block"}:
            raise ContractValidationError("recommendation 必须为 approve、revise 或 block。")
        sec_state = state.section_versions.sections.get(sec_id)
        if sec_state is None or sec_state.revision == 0:
            task.status = "stale"
            raise SemanticReviewStaleError(
                f"Target section '{sec_id}' for task '{task_id}' has no drafted content.",
                details={"task_id": task_id, "section_id": sec_id},
            )
        eff_author = (sec_state.author_session or "").strip()
        if len(eff_author) < 3 or eff_author == eff_reviewer:
            raise ContractValidationError(
                f"Reviewer '{eff_reviewer}' cannot submit review for section '{sec_id}' (missing/invalid or matching author_session '{eff_author}').",
                code="REVIEWER_IS_AUTHOR",
                details={
                    "task_id": task_id,
                    "section_id": sec_id,
                    "author_session": sec_state.author_session,
                    "reviewer_id": eff_reviewer,
                },
            )

        binding_ids = {binding.binding_id for binding in sec_state.source_bindings}
        if claim_evaluations is not None and (
            not isinstance(claim_evaluations, list) or any(not isinstance(item, dict) for item in claim_evaluations)
        ):
            raise ContractValidationError("claim_evaluations 必须是对象列表。", details={"task_id": task_id})
        evaluation_ids = [str(item.get("binding_id") or "") for item in claim_evaluations or []]
        if len(evaluation_ids) != len(set(evaluation_ids)) or any(bid not in binding_ids for bid in evaluation_ids):
            raise ContractValidationError("Claim 评价包含重复或未知 binding_id。", details={"task_id": task_id})

        _, live_fp, _ = ContextManager.compute_section_dependency_snapshot(state, sec_id)
        if (
            section_revision != sec_state.revision
            or task.target_section_revision != sec_state.revision
            or context_fingerprint != live_fp
        ):
            task.status = "stale"
            task.stale_reason = (
                f"Submitted review (rev={section_revision}, fp={context_fingerprint[:16]}) does not match "
                f"live section '{sec_id}' (rev={sec_state.revision}, fp={live_fp[:16]})."
            )
            raise SemanticReviewStaleError(
                task.stale_reason,
                details={
                    "task_id": task_id,
                    "section_id": sec_id,
                    "submitted_revision": section_revision,
                    "current_revision": sec_state.revision,
                    "submitted_context_fingerprint": context_fingerprint,
                    "current_context_fingerprint": live_fp,
                },
            )

        summary_clean = (review_summary or "").strip()
        if not summary_clean:
            task.status = "rejected"
            task.rejection_reason = "review_summary cannot be empty."
            raise ContractValidationError(
                "Semantic review rejected: review_summary cannot be empty.",
                details={"task_id": task_id},
            )

        try:
            validated_findings = [
                f if isinstance(f, SemanticReviewFinding) else SemanticReviewFinding.model_validate(f)
                for f in (findings or [])
            ]
        except Exception as exc:
            task.status = "rejected"
            task.rejection_reason = f"Invalid findings schema: {exc}"
            raise ContractValidationError(
                f"Semantic review rejected due to invalid findings format: {exc}",
                details={"task_id": task_id},
            ) from exc

        # Validate structured checklist_results if provided or required
        expected_check_map = {item.check_id: item.question for item in task.checklist_items}
        parsed_checklist_results: list[ChecklistItemResult] = []
        checklist_mode: str = "structured_verified"
        eff_checklist_hash: str = ""

        if task.requires_structured_checklist and checklist_hash is None:
            raise ReviewChecklistIncompleteError(
                "新审核任务必须提交 checklist_hash 与完整逐项结果。",
                details={"task_id": task_id, "expected_checklist_hash": task.checklist_hash},
            )

        if checklist_hash is not None and checklist_hash != task.checklist_hash:
            task.status = "rejected"
            task.rejection_reason = (
                f"Submitted checklist_hash '{checklist_hash}' does not match task checklist_hash '{task.checklist_hash}'."
            )
            raise ReviewChecklistIncompleteError(
                task.rejection_reason,
                details={
                    "task_id": task_id,
                    "submitted_checklist_hash": checklist_hash,
                    "expected_checklist_hash": task.checklist_hash,
                },
            )

        if checklist_results is not None or checklist_hash is not None or require_structured_checklist:
            if not checklist_results:
                task.status = "rejected"
                task.rejection_reason = f"checklist_results is empty for task '{task_id}'."
                raise ReviewChecklistIncompleteError(
                    task.rejection_reason,
                    details={
                        "task_id": task_id,
                        "expected_check_ids": list(expected_check_map.keys()),
                    },
                )
            try:
                parsed_checklist_results = [
                    r if isinstance(r, ChecklistItemResult) else ChecklistItemResult.model_validate(r)
                    for r in checklist_results
                ]
            except Exception as exc:
                task.status = "rejected"
                task.rejection_reason = f"Invalid checklist_results schema: {exc}"
                raise ReviewChecklistIncompleteError(
                    f"Invalid checklist_results format: {exc}",
                    details={"task_id": task_id},
                ) from exc

            seen_check_ids: set[str] = set()
            norm_section_body = normalize_for_verification(sec_state.content_markdown)
            full_sentence_norms: set[str] | None = None
            for r_item in parsed_checklist_results:
                if r_item.check_id in seen_check_ids:
                    task.status = "rejected"
                    task.rejection_reason = f"Duplicate check_id '{r_item.check_id}' in checklist_results."
                    raise ReviewChecklistIncompleteError(
                        task.rejection_reason,
                        details={"task_id": task_id, "duplicate_check_id": r_item.check_id},
                    )
                seen_check_ids.add(r_item.check_id)
                if r_item.check_id not in expected_check_map:
                    task.status = "rejected"
                    task.rejection_reason = f"Unknown check_id '{r_item.check_id}' for task '{task_id}'."
                    raise ReviewChecklistIncompleteError(
                        task.rejection_reason,
                        details={
                            "task_id": task_id,
                            "unknown_check_id": r_item.check_id,
                            "expected_check_ids": list(expected_check_map.keys()),
                        },
                    )
                rationale_clean = (r_item.rationale or "").strip()
                if r_item.verdict == "not_applicable":
                    if len(rationale_clean) < 20:
                        task.status = "rejected"
                        task.rejection_reason = (
                            f"Checklist item '{r_item.check_id}' marked 'not_applicable' requires a rationale of at least 20 characters."
                        )
                        raise ReviewChecklistIncompleteError(
                            task.rejection_reason,
                            details={"task_id": task_id, "check_id": r_item.check_id},
                        )
                else:
                    if len(rationale_clean) < 15:
                        task.status = "rejected"
                        task.rejection_reason = (
                            f"Checklist item '{r_item.check_id}' verdict '{r_item.verdict}' requires a substantive rationale of at least 15 characters."
                        )
                        raise ReviewChecklistIncompleteError(
                            task.rejection_reason,
                            details={"task_id": task_id, "check_id": r_item.check_id, "rationale_length": len(rationale_clean)},
                        )
                    if not r_item.quotes:
                        task.status = "rejected"
                        task.rejection_reason = (
                            f"Checklist item '{r_item.check_id}' must include at least one quote from section '{sec_id}'."
                        )
                        raise ReviewChecklistIncompleteError(
                            task.rejection_reason,
                            details={"task_id": task_id, "check_id": r_item.check_id},
                        )
                    for q in r_item.quotes:
                        norm_q = normalize_for_verification(q)
                        if not norm_q or norm_q not in norm_section_body:
                            task.status = "rejected"
                            task.rejection_reason = (
                                f"Checklist item '{r_item.check_id}' quote is not a substring of section '{sec_id}'."
                            )
                            raise ReviewChecklistIncompleteError(
                                task.rejection_reason,
                                details={"task_id": task_id, "check_id": r_item.check_id, "quote": q[:120]},
                            )
                        if len(norm_q) < 10:
                            if full_sentence_norms is None:
                                parsed_sents, _stats, _ast = analyze_markdown_with_pandoc(sec_state.content_markdown)
                                full_sentence_norms = {
                                    normalize_for_verification(s.text)
                                    for s in parsed_sents
                                    if s.text and normalize_for_verification(s.text)
                                }
                            norm_q_stripped = normalize_for_verification(re.sub(r"\[@[^\]]+\]", "", q))
                            if norm_q not in full_sentence_norms and norm_q_stripped not in full_sentence_norms:
                                task.status = "rejected"
                                task.rejection_reason = (
                                    f"Checklist item '{r_item.check_id}' quote must be at least 10 normalized characters "
                                    "or equal a complete parsed sentence from the section."
                                )
                                raise ReviewChecklistIncompleteError(
                                    task.rejection_reason,
                                    details={"task_id": task_id, "check_id": r_item.check_id, "quote_length": len(norm_q)},
                                )
                if not r_item.question:
                    r_item.question = expected_check_map[r_item.check_id]
                elif r_item.question != expected_check_map[r_item.check_id]:
                    raise ReviewChecklistIncompleteError("审核题目与任务清单不一致。", details={"check_id": r_item.check_id})
                SourcePolicyEvaluator.assert_evidence(state, r_item.evidence_ids)
                if not r_item.section_locations:
                    r_item.section_locations = [f"{sec_id}:section"]

            active_quoted_items = [r for r in parsed_checklist_results if r.verdict != "not_applicable"]
            if len(active_quoted_items) > 3:
                distinct_quotes = {
                    normalize_for_verification(q)
                    for r in active_quoted_items
                    for q in r.quotes
                    if normalize_for_verification(q)
                }
                if len(distinct_quotes) <= 1:
                    task.status = "rejected"
                    task.rejection_reason = (
                        f"Checklist has {len(active_quoted_items)} non-N/A items (>3); all items cannot cite the same single quote."
                    )
                    raise ReviewChecklistIncompleteError(
                        task.rejection_reason,
                        details={"task_id": task_id, "checklist_count": len(active_quoted_items)},
                    )

            missing_check_ids = [cid for cid in expected_check_map if cid not in seen_check_ids]
            if missing_check_ids:
                task.status = "rejected"
                task.rejection_reason = (
                    f"Incomplete checklist_results for task '{task_id}'; missing check_ids: {missing_check_ids}."
                )
                raise ReviewChecklistIncompleteError(
                    task.rejection_reason,
                    details={
                        "task_id": task_id,
                        "missing_check_ids": missing_check_ids,
                        "expected_check_ids": list(expected_check_map.keys()),
                    },
                )
            checklist_mode = "structured_verified"
            eff_checklist_hash = task.checklist_hash

        # Convert semantic findings & failing checklist items into ReviewLedger issues immediately on submission
        for finding in validated_findings:
            SourcePolicyEvaluator.assert_evidence(state, finding.evidence_ids)
        created_issue_ids: list[str] = []
        seq_base = len(state.review_ledger.issues) + 1
        for idx, finding in enumerate(validated_findings):
            iss_id = f"iss_sem_{sec_id}_r{sec_state.revision}_{seq_base + idx:02d}"
            created_issue_ids.append(iss_id)
            state.review_ledger.issues.append(
                ReviewIssue(
                    issue_id=iss_id,
                    severity=finding.severity,
                    category=finding.category,
                    target_section_id=sec_id,
                    description=finding.description,
                    suggested_fix=finding.suggested_fix or "Address semantic review finding in next section revision.",
                    evidence_ids=list(finding.evidence_ids),
                    status="open",
                )
            )

        for r_item in parsed_checklist_results:
            if r_item.verdict in {"fail", "needs_revision"}:
                iss_id = f"iss_chk_{r_item.check_id.replace(':', '_')}"
                created_issue_ids.append(iss_id)
                state.review_ledger.issues.append(
                    ReviewIssue(
                        issue_id=iss_id,
                        severity="blocker" if r_item.verdict == "fail" else "major",
                        category="semantic_argumentation",
                        target_section_id=sec_id,
                        description=(
                            f"Checklist item '{r_item.check_id}' ({r_item.question}) verdict='{r_item.verdict}': "
                            f"{r_item.rationale}"
                        ),
                        suggested_fix=(
                            f"Revise section '{sec_id}' at {r_item.section_locations} to satisfy checklist requirement '{r_item.check_id}'."
                        ),
                        evidence_ids=list(r_item.evidence_ids),
                        status="open",
                    )
                )

        if recommendation in {"revise", "block"} and not any(f.severity in {"blocker", "major"} for f in validated_findings):
            issue_id = f"iss_recommendation_{task_id}"
            if not any(i.issue_id == issue_id for i in state.review_ledger.issues):
                state.review_ledger.issues.append(ReviewIssue(
                    issue_id=issue_id, severity="blocker" if recommendation == "block" else "major",
                    category="semantic_argumentation", target_section_id=sec_id,
                    description=f"Reviewer recommendation={recommendation}: {summary_clean}",
                    suggested_fix="根据审稿结论修订章节，并重新审核。", status="open",
                ))
                created_issue_ids.append(issue_id)

        now_iso = datetime.now(timezone.utc).isoformat()
        _, post_submit_fp, _ = ContextManager.compute_section_dependency_snapshot(state, sec_id)
        task.target_context_fingerprint = post_submit_fp
        report = SemanticReviewReport(
            task_id=task_id,
            project_id=state.brief.project_id,
            section_id=sec_id,
            section_revision=sec_state.revision,
            section_content_hash=sec_state.content_hash,
            context_fingerprint=post_submit_fp,
            reviewer_agent=eff_reviewer,
            review_summary=summary_clean,
            recommendation=recommendation,  # type: ignore[arg-type]
            findings=validated_findings,
            claim_evaluations=list(claim_evaluations or []),
            checklist_hash=eff_checklist_hash,
            checklist_results=parsed_checklist_results,
            checklist_verification_mode=checklist_mode,  # type: ignore[arg-type]
            submitted_at=now_iso,
            status="submitted",
        )
        task.report = report
        task.status = "submitted"
        task.submitted_at = now_iso
        task.rejection_reason = ""

        # Submit-is-accept for valid approving reports
        has_failing_check = any(r.verdict in {"fail", "needs_revision"} for r in parsed_checklist_results)
        has_major_finding = any(f.severity in {"critical", "major"} for f in validated_findings)
        if report.recommendation == "approve" and not has_failing_check and not has_major_finding:
            accepted_out = cls.accept_semantic_review(
                state=state,
                task_id=task_id,
                policy_version="v1",
                require_structured_checklist=True,
            )
            accepted_out["created_issue_ids"] = created_issue_ids
            return accepted_out

        out = task.model_dump(mode="json")
        out["created_issue_ids"] = created_issue_ids
        return out

    @classmethod
    def accept_semantic_review(
        cls,
        state: ProjectWorkspaceState,
        task_id: str,
        policy_version: str = "v1",
        require_structured_checklist: bool = False,
    ) -> dict[str, Any]:
        """Validate a submitted SemanticReviewReport against live section/evidence hashes, L2a rules, and checklist verdicts, then mark accepted."""
        cls.refresh_semantic_task_staleness(state)
        task = next((t for t in state.review_ledger.semantic_review_tasks if t.task_id == task_id), None)
        if task is None:
            raise SemanticReviewRequiredError(
                f"SemanticReviewTask '{task_id}' not found.",
                details={"task_id": task_id},
            )
        if task.status == "stale":
            raise SemanticReviewStaleError(
                f"Cannot accept stale SemanticReviewTask '{task_id}': {task.stale_reason}",
                details={"task_id": task_id, "stale_reason": task.stale_reason},
            )
        if task.status != "submitted" or task.report is None:
            raise ContractValidationError(
                f"SemanticReviewTask '{task_id}' must be in 'submitted' state before acceptance (current='{task.status}').",
                code="SEMANTIC_REVIEW_REQUIRED",
                details={"task_id": task_id, "status": task.status},
            )

        report = task.report
        require_structured_checklist = require_structured_checklist or task.requires_structured_checklist
        SourcePolicyEvaluator.assert_section(state, report.section_id)
        if report.recommendation != "approve":
            raise ApprovalConflictError(
                "审稿要求修订或阻断，不能接受为通过。", code="SEMANTIC_REVIEW_REQUIRED",
                details={"recommendation": report.recommendation, "task_id": task_id},
            )
        sec_id = report.section_id
        sec_state = state.section_versions.sections.get(sec_id)
        if sec_state is None or sec_state.revision == 0:
            task.status = "stale"
            raise SemanticReviewStaleError(
                f"Section '{sec_id}' has no drafted content.",
                details={"task_id": task_id, "section_id": sec_id},
            )

        _, live_fp, _ = ContextManager.compute_section_dependency_snapshot(state, sec_id)
        if (
            sec_state.revision != report.section_revision
            or sec_state.content_hash != report.section_content_hash
            or live_fp != report.context_fingerprint
        ):
            task.status = "stale"
            report.status = "stale"
            task.stale_reason = "Section content or dependency fingerprint changed after semantic review was submitted."
            raise SemanticReviewStaleError(
                task.stale_reason,
                details={
                    "task_id": task_id,
                    "section_id": sec_id,
                    "report_revision": report.section_revision,
                    "current_revision": sec_state.revision,
                },
            )

        # Validate checklist integrity and verdicts
        if require_structured_checklist and report.checklist_verification_mode != "structured_verified":
            raise ReviewChecklistIncompleteError(
                f"Cannot accept task '{task_id}': structured checklist_results are required but report mode is '{report.checklist_verification_mode}'.",
                details={"task_id": task_id, "checklist_verification_mode": report.checklist_verification_mode},
            )

        if report.checklist_verification_mode == "structured_verified":
            expected_ids = {item.check_id for item in task.checklist_items}
            submitted_ids = {r.check_id for r in report.checklist_results}
            if report.checklist_hash != task.checklist_hash or submitted_ids != expected_ids:
                task.status = "rejected"
                report.status = "rejected"
                raise ReviewChecklistIncompleteError(
                    f"Checklist hash or item set mismatch for task '{task_id}'.",
                    details={
                        "task_id": task_id,
                        "report_checklist_hash": report.checklist_hash,
                        "task_checklist_hash": task.checklist_hash,
                    },
                )
            failed_checks = [
                r.check_id
                for r in report.checklist_results
                if r.verdict in {"fail", "needs_revision"}
            ]
            if failed_checks:
                raise ApprovalConflictError(
                    f"Cannot accept semantic review task '{task_id}' for section '{sec_id}': checklist items failed or require revision: {failed_checks}.",
                    code="SEMANTIC_REVIEW_REQUIRED",
                    details={
                        "task_id": task_id,
                        "section_id": sec_id,
                        "failed_check_ids": failed_checks,
                    },
                )

        # Apply L2b claim_evaluations and verify no rule_failed binding is overridden
        binding_map = {b.binding_id: b for b in sec_state.source_bindings}
        evaluation_ids = [str(item.get("binding_id") or "") for item in report.claim_evaluations]
        if len(evaluation_ids) != len(set(evaluation_ids)) or any(bid not in binding_map for bid in evaluation_ids):
            raise ContractValidationError("Claim 评价包含重复或未知 binding_id。", details={"task_id": task_id})
        if task.requires_structured_checklist:
            required_ids = {bid for bid, binding in binding_map.items() if binding.claim_type != "research_hypothesis"}
            missing_ids = sorted(required_ids - set(evaluation_ids))
            if missing_ids:
                raise ContractValidationError("审核未覆盖全部事实性 Claim。", code="CLAIM_REVIEW_PENDING", details={"missing_binding_ids": missing_ids})
        for eval_item in report.claim_evaluations:
            bid = str(eval_item.get("binding_id") or "")
            sem_status = str(eval_item.get("semantic_support_status") or "")
            rationale = str(eval_item.get("rationale") or "")
            if not bid or bid not in binding_map:
                task.status = "rejected"
                report.status = "rejected"
                task.rejection_reason = f"Unknown binding_id '{bid}' in claim_evaluations."
                raise ContractValidationError(
                    task.rejection_reason,
                    details={"task_id": task_id, "binding_id": bid},
                )
            if sem_status == "supported":
                sem_status = "supported_reviewed"
            if sem_status not in {
                "supported_reviewed",
                "partially_supported",
                "unsupported",
                "hypothesis_proposal",
            }:
                task.status = "rejected"
                report.status = "rejected"
                task.rejection_reason = f"Invalid semantic_support_status '{sem_status}' for binding '{bid}'."
                raise ContractValidationError(
                    task.rejection_reason,
                    details={"task_id": task_id, "binding_id": bid, "semantic_support_status": sem_status},
                )

            target_binding = binding_map[bid]
            ev = state.evidence_ledger.items.get(target_binding.evidence_id) if target_binding.evidence_id else None
            try:
                record_semantic_support_review(
                    target_binding,
                    ev,
                    section_revision=sec_state.revision,
                    section_hash=sec_state.content_hash,
                    reviewer=report.reviewer_agent,
                    semantic_status=sem_status,  # type: ignore[arg-type]
                    rationale=rationale,
                )
            except ContractValidationError as exc:
                # Rule conflict: create conflict ReviewIssue and reject report override (§6.4)
                state.review_ledger.issues.append(
                    ReviewIssue(
                        issue_id=f"iss_conflict_{task_id}_{bid}",
                        severity="blocker",
                        category="claim_support",
                        target_section_id=sec_id,
                        description=f"L2a/L2b conflict on binding '{bid}': {exc.message}",
                        suggested_fix="Fix the quantitative or overclaim conflict in section prose or evidence binding.",
                        evidence_ids=[target_binding.evidence_id] if target_binding.evidence_id else [],
                        status="open",
                    )
                )
                task.status = "rejected"
                report.status = "rejected"
                task.rejection_reason = exc.message
                raise

            if sem_status in {"partially_supported", "unsupported"}:
                state.review_ledger.issues.append(
                    ReviewIssue(
                        issue_id=f"iss_l2b_{task_id}_{bid}",
                        severity="blocker",
                        category="claim_support",
                        target_section_id=sec_id,
                        description=(
                            f"Section '{sec_id}' claim binding '{bid}' evaluated as '{sem_status}' in L2b semantic review: "
                            f"{target_binding.rationale}"
                        ),
                        suggested_fix="Revise claim scope or bind stronger supporting evidence, then re-run semantic review.",
                        evidence_ids=[target_binding.evidence_id] if target_binding.evidence_id else [],
                        status="open",
                    )
                )

        # Block acceptance if the report contains blocker or major semantic findings
        blocking_findings = [
            f for f in report.findings if f.severity in {"blocker", "major"}
        ]
        if blocking_findings:
            raise ApprovalConflictError(
                f"Cannot accept semantic review task '{task_id}' for section '{sec_id}' while blocker/major semantic findings exist.",
                code="SEMANTIC_REVIEW_REQUIRED",
                details={
                    "task_id": task_id,
                    "section_id": sec_id,
                    "blocking_findings_count": len(blocking_findings),
                },
            )

        now_iso = datetime.now(timezone.utc).isoformat()
        report.status = "accepted"
        report.accepted_at = now_iso
        report.policy_version = policy_version
        task.status = "accepted"
        task.completed_at = now_iso
        state.review_ledger.semantic_reports[sec_id] = report
        state.review_ledger.reviewed_section_revisions[sec_id] = sec_state.revision

        return {
            "task_id": task_id,
            "section_id": sec_id,
            "section_revision": sec_state.revision,
            "status": task.status,
            "completed_at": task.completed_at,
            "findings_added": len(report.findings),
            "claims_evaluated": len(report.claim_evaluations),
            "checklist_verification_mode": report.checklist_verification_mode,
            "checklist_items_evaluated": len(report.checklist_results),
        }

    @classmethod
    def get_revision_tasks(cls, state: ProjectWorkspaceState) -> dict[str, Any]:
        """Group open ReviewIssues, pending/stale semantic reviews, pending claim reviews, and human rejection feedback into actionable revision batches."""
        cls.refresh_semantic_task_staleness(state)
        open_issues = [i for i in state.review_ledger.issues if i.status == "open"]
        by_section: dict[str, list[dict[str, Any]]] = {}
        for iss in open_issues:
            key = iss.target_section_id or "global"
            by_section.setdefault(key, []).append(iss.model_dump(mode="json"))

        stale_sections = [
            {
                "section_id": s.section_id,
                "current_revision": s.revision,
                "stale_reason": s.stale_reason,
            }
            for s in state.section_versions.sections.values()
            if s.revision > 0 and s.needs_revalidation
        ]

        pending_semantic_tasks = [
            t.model_dump(mode="json")
            for t in state.review_ledger.semantic_review_tasks
            if t.status in {"pending", "claimed", "stale", "rejected"}
        ]

        pending_claim_reviews: list[dict[str, Any]] = []
        for sec in state.section_versions.sections.values():
            if sec.revision == 0:
                continue
            for b in sec.source_bindings:
                if b.claim_type != "research_hypothesis" and b.semantic_support_status != "supported_reviewed":
                    pending_claim_reviews.append(
                        {
                            "section_id": sec.section_id,
                            "binding_id": b.binding_id,
                            "evidence_id": b.evidence_id,
                            "rule_check_status": b.rule_check_status,
                            "semantic_support_status": b.semantic_support_status,
                        }
                    )

        return {
            "project_id": state.brief.project_id,
            "total_open_issues": len(open_issues),
            "revision_tasks_by_section": by_section,
            "sections_needing_revalidation": stale_sections,
            "pending_semantic_review_tasks": pending_semantic_tasks,
            "pending_claim_reviews": pending_claim_reviews,
        }
